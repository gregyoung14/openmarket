use chrono::Utc;
use tracing::{info, warn};
use uuid::Uuid;

use crate::config;
use crate::models::{
    Direction, EntrySignal, ExecutionEvent, ExitStrategy, ExitType, LivePrices, MarketContext,
    Position, StatusPosition, StatusPrices, StatusWallet,
};
use crate::wallet::WalletBalances;

/// Manages positions and trade lifecycle.
/// Uses on-chain wallet balance as the sole source of truth — no paper bankroll.
pub struct PositionManager {
    pub positions: Vec<Position>,
    pub closed_positions: Vec<Position>,
    pub strategy: ExitStrategy,
}

impl PositionManager {
    pub fn new(strategy: ExitStrategy) -> Self {
        Self {
            positions: Vec::new(),
            closed_positions: Vec::new(),
            strategy,
        }
    }

    /// Can we open a new trade? Uses on-chain wallet balance.
    pub fn can_trade(&self, wallet_usdc: f64) -> bool {
        if self.positions.len() >= config::MAX_OPEN_POSITIONS {
            return false;
        }
        if config::is_test_mode() {
            // In test mode, we only need enough for 1 share (~$0.50)
            if wallet_usdc <= 0.0 {
                warn!(wallet_usdc, "Wallet USDC.e is zero — cannot trade");
                return false;
            }
            return true;
        }
        if wallet_usdc <= 1.0 {
            warn!(wallet_usdc, "Wallet USDC.e too low to trade");
            return false;
        }
        true
    }

    /// Compute position sizing and create a Position from a signal engine entry signal.
    /// The signal engine has already applied all v9.2 filters (regime gate, edge,
    /// price cap, hour blacklist, adaptive confirmation). We trust it completely.
    pub fn create_position(
        &self,
        signal: &EntrySignal,
        market: &MarketContext,
        entry_ask: f64,
        wallet_usdc: f64,
    ) -> Option<Position> {
        let direction = Direction::from_str_loose(&signal.direction)?;

        // Price = ask + slippage, rounded up to configured decimals
        let entry_price =
            config::ceil_decimals(entry_ask + config::SLIPPAGE, config::ORDER_PRICE_DECIMALS)
                .min(config::MAX_ENTRY_PRICE);

        let (shares, bet_amount) = if config::is_test_mode() {
            // TEST MODE: minimum shares to meet Polymarket's $1 minimum order size
            let min_shares = (1.0_f64 / entry_price).ceil().max(config::TEST_MODE_SHARES);
            let shares = min_shares;
            let bet_amount = (shares * entry_price) / (1.0 - config::FEE_RATE);
            info!(
                "🧪 TEST MODE: {} share(s) @ {:.4}, bet_amount={:.4}, wallet={:.2}",
                shares, entry_price, bet_amount, wallet_usdc
            );
            (shares, bet_amount)
        } else {
            // ── Half-Kelly sizing ──
            // bet_fraction = KELLY_MULTIPLIER * edge * confidence
            // clamped to [KELLY_MIN_FRACTION, KELLY_MAX_FRACTION]
            let edge = signal.edge.unwrap_or(0.0).max(0.0);
            let kelly_raw = config::KELLY_MULTIPLIER * edge * signal.confidence;
            let kelly_fraction =
                kelly_raw.clamp(config::KELLY_MIN_FRACTION, config::KELLY_MAX_FRACTION);

            info!(
                edge = edge,
                confidence = signal.confidence,
                kelly_raw = format!("{:.4}", kelly_raw),
                kelly_fraction = format!("{:.4}", kelly_fraction),
                wallet = format!("{:.2}", wallet_usdc),
                "Kelly sizing: {:.1}% of bankroll",
                kelly_fraction * 100.0
            );

            let bet_amount = wallet_usdc * kelly_fraction;
            let fee_entry = bet_amount * config::FEE_RATE;
            let capital = bet_amount - fee_entry;

            // Calculate shares using floor to stay within budget
            let raw_shares = capital / entry_price;
            let shares = if config::ORDER_SIZE_DECIMALS == 0 {
                raw_shares.floor()
            } else {
                config::truncate_decimals(raw_shares, config::ORDER_SIZE_DECIMALS)
            };
            (shares, bet_amount)
        };

        if shares <= 0.0 || bet_amount <= 0.0 {
            warn!("Invalid position sizing: shares={shares}, bet={bet_amount}");
            return None;
        }

        let token_id = match direction {
            Direction::Up => market.up_token_id.clone(),
            Direction::Down => market.down_token_id.clone(),
        };

        Some(Position {
            id: Uuid::new_v4().to_string(),
            market_slug: market.slug.clone(),
            side: direction,
            token_id,
            entry_price,
            shares,
            bet_amount,
            confidence: signal.confidence,
            consistency: signal.consistency.unwrap_or(0.0),
            entry_time: Utc::now(),
            market_end_ms: market.market_end_ms,
            strategy: self.strategy,
            exit_price: None,
            exit_time: None,
            exit_type: None,
            pnl: None,
        })
    }

    /// Register a position as opened (after order confirmed).
    pub fn register_open(&mut self, position: Position) -> ExecutionEvent {
        let event = ExecutionEvent::TradeOpened {
            position_id: position.id.clone(),
            market: position.market_slug.clone(),
            side: position.side.as_str().to_string(),
            entry_price: position.entry_price,
            shares: position.shares,
            bet_amount: position.bet_amount,
            confidence: position.confidence,
            timestamp: Utc::now().timestamp_millis(),
        };

        info!(
            id = %position.id,
            side = position.side.as_str(),
            entry = position.entry_price,
            shares = position.shares,
            bet = position.bet_amount,
            "Position opened"
        );

        self.positions.push(position);
        event
    }

    /// Check all open positions for exit conditions.
    /// Returns (positions_to_close, events).
    pub fn check_exits(&mut self, prices: &LivePrices) -> Vec<(usize, f64, ExitType)> {
        let now_ms = Utc::now().timestamp_millis();
        let mut exits = Vec::new();

        for (idx, pos) in self.positions.iter().enumerate() {
            // 1. Resolution exit: market has ended
            if now_ms >= pos.market_end_ms {
                let resolve_price = match pos.side {
                    Direction::Up => prices.up_bid.unwrap_or(0.0),
                    Direction::Down => prices.down_bid.unwrap_or(0.0),
                };

                if resolve_price > config::WIN_THRESHOLD {
                    exits.push((idx, 1.0, ExitType::ResolveWin));
                } else {
                    exits.push((idx, 0.0, ExitType::ResolveLoss));
                }
                continue;
            }

            // 2. Momentum take-profit (only if strategy is Momentum)
            if self.strategy == ExitStrategy::Momentum {
                let current_bid = match pos.side {
                    Direction::Up => prices.up_bid.unwrap_or(0.0),
                    Direction::Down => prices.down_bid.unwrap_or(0.0),
                };

                let tp_price = pos.entry_price + config::MOMENTUM_TP;
                if current_bid >= tp_price {
                    let exit_price = (current_bid - config::SLIPPAGE).max(0.0);
                    exits.push((idx, exit_price, ExitType::TakeProfit));
                }
            }
        }

        exits
    }

    /// Close a position at the given exit price.
    pub fn close_position(
        &mut self,
        position_idx: usize,
        exit_price: f64,
        exit_type: ExitType,
    ) -> Option<ExecutionEvent> {
        if position_idx >= self.positions.len() {
            return None;
        }

        let mut pos = self.positions.remove(position_idx);

        let payout = pos.shares * exit_price;
        let fee_exit = if payout > 0.0 {
            payout * config::FEE_RATE
        } else {
            0.0
        };
        let net_payout = payout - fee_exit;
        let pnl = net_payout - pos.bet_amount;

        pos.exit_price = Some(exit_price);
        pos.exit_time = Some(Utc::now());
        pos.exit_type = Some(exit_type);
        pos.pnl = Some(pnl);

        let event = ExecutionEvent::TradeClosed {
            position_id: pos.id.clone(),
            market: pos.market_slug.clone(),
            side: pos.side.as_str().to_string(),
            entry_price: pos.entry_price,
            exit_price,
            shares: pos.shares,
            pnl,
            exit_type: format!("{exit_type:?}"),
            timestamp: Utc::now().timestamp_millis(),
        };

        info!(
            id = %pos.id,
            side = pos.side.as_str(),
            entry = pos.entry_price,
            exit = exit_price,
            pnl = pnl,
            exit_type = ?exit_type,
            "Position closed"
        );

        self.closed_positions.push(pos);
        Some(event)
    }

    /// Generate a full status event with all wallet/account data.
    pub fn status_event(
        &self,
        wallet_address: &str,
        uptime_secs: u64,
        clob_connected: bool,
        market_slug: &str,
        live_prices: &LivePrices,
        balances: &WalletBalances,
    ) -> ExecutionEvent {
        let total_trades = self.closed_positions.len();
        let wins = self
            .closed_positions
            .iter()
            .filter(|p| p.pnl.unwrap_or(0.0) > 0.0)
            .count();
        let win_rate = if total_trades > 0 {
            wins as f64 / total_trades as f64
        } else {
            0.0
        };
        let total_pnl: f64 = self
            .closed_positions
            .iter()
            .map(|p| p.pnl.unwrap_or(0.0))
            .sum();

        // Wallet balance IS the bankroll — no separate paper tracking
        let wallet_usdc = balances.usdc_e;

        let positions: Vec<StatusPosition> = self
            .positions
            .iter()
            .map(|p| {
                let current_bid = match p.side {
                    Direction::Up => live_prices.up_bid.unwrap_or(p.entry_price),
                    Direction::Down => live_prices.down_bid.unwrap_or(p.entry_price),
                };
                let unrealized_pnl = (current_bid * p.shares) - p.bet_amount;
                StatusPosition {
                    id: p.id.clone(),
                    side: p.side.as_str().to_string(),
                    entry_price: p.entry_price,
                    shares: p.shares,
                    bet_amount: p.bet_amount,
                    confidence: p.confidence,
                    entry_time: p.entry_time.to_rfc3339(),
                    unrealized_pnl,
                }
            })
            .collect();

        ExecutionEvent::Status {
            bankroll: wallet_usdc,
            peak_bankroll: wallet_usdc,
            open_positions: self.positions.len(),
            total_trades,
            wins,
            win_rate,
            total_pnl,
            drawdown_pct: 0.0,
            strategy: format!("{:?}", self.strategy),
            clob_connected,
            wallet_address: wallet_address.to_string(),
            uptime_secs,
            market_slug: market_slug.to_string(),
            prices: StatusPrices {
                up_bid: live_prices.up_bid,
                up_ask: live_prices.up_ask,
                down_bid: live_prices.down_bid,
                down_ask: live_prices.down_ask,
            },
            wallet_balances: StatusWallet {
                usdc_e: balances.usdc_e,
                usdc_native: balances.usdc_native,
                matic: balances.matic,
            },
            positions,
            timestamp: Utc::now().timestamp_millis(),
        }
    }

    /// Save trade log to JSON file
    pub fn save_trade_log(&self, path: &str) {
        let all: Vec<&Position> = self.closed_positions.iter().collect();
        match serde_json::to_string_pretty(&all) {
            Ok(json) => {
                if let Err(e) = std::fs::write(path, json) {
                    warn!(error = %e, "Failed to write trade log");
                }
            }
            Err(e) => warn!(error = %e, "Failed to serialize trade log"),
        }
    }
}
// ─── Unit Tests ────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::EntrySignal;

    fn make_market() -> MarketContext {
        MarketContext {
            slug: "btc-updown-15m-1771695900".to_string(),
            up_token_id: "token_up_111".to_string(),
            down_token_id: "token_down_222".to_string(),
            market_end_ms: (1771695900 + 900) * 1000,
        }
    }

    fn make_entry_signal(direction: &str, confidence: f64, entry_ask: Option<f64>) -> EntrySignal {
        make_entry_signal_with_edge(direction, confidence, entry_ask, Some(0.20))
    }

    fn make_entry_signal_with_edge(
        direction: &str,
        confidence: f64,
        entry_ask: Option<f64>,
        edge: Option<f64>,
    ) -> EntrySignal {
        EntrySignal {
            direction: direction.to_string(),
            confidence,
            consistency: Some(1.0),
            raw_prob: Some(confidence),
            combined_prob_up: Some(confidence),
            drift_prob_up: Some(0.99),
            market: Some("btc-updown-15m-1771695900".to_string()),
            secs_in: Some(120),
            secs_left: Some(780),
            entry_ask,
            entry_bid: Some(0.44),
            btc_price: Some(68000.0),
            n_trades: Some(500),
            edge,
            regime: Some("trend".to_string()),
            path_eff: Some(0.85),
            autocorr: Some(0.10),
            ofi_accel: Some(0.0),
            adaptive_confirm: Some(30),
            vol_1s: Some(0.0001),
            timestamp: Some(1771696000000),
            version: Some(btc_common::version::SIGNAL_VERSION.to_string()),
        }
    }

    // ── Position creation from EntrySignal ──────────────────────────

    #[test]
    fn test_create_position_up() {
        let pm = PositionManager::new(ExitStrategy::HoldToResolve);
        let market = make_market();
        let signal = make_entry_signal("UP", 0.75, Some(0.45));

        let pos = pm.create_position(&signal, &market, 0.45, 200.0);
        assert!(pos.is_some(), "Position should be created");

        let pos = pos.unwrap();
        assert_eq!(pos.side, Direction::Up);
        assert_eq!(pos.token_id, "token_up_111");
        assert_eq!(pos.market_slug, "btc-updown-15m-1771695900");
        assert!(pos.entry_price > 0.45, "Price should include slippage");
        assert!(pos.shares > 0.0);
        assert!(pos.bet_amount > 0.0);
        assert!((pos.confidence - 0.75).abs() < 0.001);
    }

    #[test]
    fn test_create_position_down() {
        let pm = PositionManager::new(ExitStrategy::HoldToResolve);
        let market = make_market();
        let signal = make_entry_signal("DOWN", 0.68, Some(0.50));

        let pos = pm.create_position(&signal, &market, 0.50, 200.0);
        assert!(pos.is_some());

        let pos = pos.unwrap();
        assert_eq!(pos.side, Direction::Down);
        assert_eq!(pos.token_id, "token_down_222");
    }

    #[test]
    fn test_create_position_invalid_direction_returns_none() {
        let pm = PositionManager::new(ExitStrategy::HoldToResolve);
        let market = make_market();
        let signal = make_entry_signal("SIDEWAYS", 0.70, Some(0.45));

        let pos = pm.create_position(&signal, &market, 0.45, 200.0);
        assert!(pos.is_none(), "Invalid direction should return None");
    }

    #[test]
    fn test_create_position_insufficient_funds() {
        let pm = PositionManager::new(ExitStrategy::HoldToResolve);
        let market = make_market();
        let signal = make_entry_signal("UP", 0.75, Some(0.45));

        // Very low wallet balance → shares would round to 0
        let pos = pm.create_position(&signal, &market, 0.45, 0.10);
        assert!(pos.is_none(), "Should fail with insufficient funds");
    }

    #[test]
    fn test_kelly_sizing_replaces_flat_2pct() {
        // Verify Kelly is actually being used, NOT the old flat 2%
        let pm = PositionManager::new(ExitStrategy::HoldToResolve);
        let market = make_market();
        let wallet_usdc = 500.0;

        // edge=0.20, confidence=0.75 → kelly_raw = 0.5 * 0.20 * 0.75 = 0.075 → clamped to 5%
        let signal = make_entry_signal_with_edge("UP", 0.75, Some(0.45), Some(0.20));
        let pos = pm
            .create_position(&signal, &market, 0.45, wallet_usdc)
            .unwrap();

        let flat_2pct_bet = wallet_usdc * 0.02; // = $10.00
        let kelly_5pct_bet = wallet_usdc * config::KELLY_MAX_FRACTION; // = $25.00

        // Should be ~$25 (Kelly at 5% cap), NOT $10 (flat 2%)
        assert!(
            (pos.bet_amount - kelly_5pct_bet).abs() < 0.01,
            "Should use Kelly (5% cap), not flat 2%. got={:.2}, expected={:.2}, flat_2pct={:.2}",
            pos.bet_amount,
            kelly_5pct_bet,
            flat_2pct_bet,
        );
    }

    // ── Half-Kelly Sizing Tests ─────────────────────────────────────

    #[test]
    fn test_kelly_high_edge_capped_at_5pct() {
        // edge=0.32, confidence=0.79 → kelly_raw = 0.5 * 0.32 * 0.79 = 0.1264 → capped to 5%
        let pm = PositionManager::new(ExitStrategy::HoldToResolve);
        let market = make_market();
        let signal = make_entry_signal_with_edge("UP", 0.79, Some(0.46), Some(0.32));
        let wallet = 131.44;

        let pos = pm
            .create_position(&signal, &market, 0.46, wallet)
            .unwrap();

        let expected_bet = wallet * config::KELLY_MAX_FRACTION; // $6.572
        assert!(
            (pos.bet_amount - expected_bet).abs() < 0.01,
            "High edge should cap at KELLY_MAX (5%): got={:.4}, expected={:.4}",
            pos.bet_amount,
            expected_bet,
        );
    }

    #[test]
    fn test_kelly_medium_edge_proportional() {
        // edge=0.08, confidence=0.60 → kelly_raw = 0.5 * 0.08 * 0.60 = 0.024
        let pm = PositionManager::new(ExitStrategy::HoldToResolve);
        let market = make_market();
        let signal = make_entry_signal_with_edge("UP", 0.60, Some(0.52), Some(0.08));
        let wallet = 500.0;

        let pos = pm
            .create_position(&signal, &market, 0.52, wallet)
            .unwrap();

        let expected_fraction = 0.5 * 0.08 * 0.60; // = 0.024 (2.4%)
        let expected_bet = wallet * expected_fraction; // = $12.00
        assert!(
            (pos.bet_amount - expected_bet).abs() < 0.01,
            "Medium edge should size proportionally: got={:.4}, expected={:.4} ({:.1}%)",
            pos.bet_amount,
            expected_bet,
            expected_fraction * 100.0,
        );
    }

    #[test]
    fn test_kelly_low_edge_floored_at_1pct() {
        // edge=0.02, confidence=0.55 → kelly_raw = 0.5 * 0.02 * 0.55 = 0.0055 → floored to 1%
        let pm = PositionManager::new(ExitStrategy::HoldToResolve);
        let market = make_market();
        let signal = make_entry_signal_with_edge("DOWN", 0.55, Some(0.53), Some(0.02));
        let wallet = 200.0;

        let pos = pm
            .create_position(&signal, &market, 0.53, wallet)
            .unwrap();

        let expected_bet = wallet * config::KELLY_MIN_FRACTION; // $2.00
        assert!(
            (pos.bet_amount - expected_bet).abs() < 0.01,
            "Low edge should floor at KELLY_MIN (1%): got={:.4}, expected={:.4}",
            pos.bet_amount,
            expected_bet,
        );
    }

    #[test]
    fn test_kelly_zero_edge_floored_at_1pct() {
        // edge=0.0 → kelly_raw = 0 → floored to 1%
        let pm = PositionManager::new(ExitStrategy::HoldToResolve);
        let market = make_market();
        let signal = make_entry_signal_with_edge("UP", 0.70, Some(0.45), Some(0.0));
        let wallet = 300.0;

        let pos = pm
            .create_position(&signal, &market, 0.45, wallet)
            .unwrap();

        let expected_bet = wallet * config::KELLY_MIN_FRACTION; // $3.00
        assert!(
            (pos.bet_amount - expected_bet).abs() < 0.01,
            "Zero edge should floor at KELLY_MIN (1%): got={:.4}, expected={:.4}",
            pos.bet_amount,
            expected_bet,
        );
    }

    #[test]
    fn test_kelly_missing_edge_floored_at_1pct() {
        // edge=None → treated as 0.0 → floored to 1%
        let pm = PositionManager::new(ExitStrategy::HoldToResolve);
        let market = make_market();
        let signal = make_entry_signal_with_edge("UP", 0.80, Some(0.45), None);
        let wallet = 250.0;

        let pos = pm
            .create_position(&signal, &market, 0.45, wallet)
            .unwrap();

        let expected_bet = wallet * config::KELLY_MIN_FRACTION; // $2.50
        assert!(
            (pos.bet_amount - expected_bet).abs() < 0.01,
            "Missing edge should floor at KELLY_MIN (1%): got={:.4}, expected={:.4}",
            pos.bet_amount,
            expected_bet,
        );
    }

    #[test]
    fn test_kelly_shares_computed_from_bet_minus_fees() {
        // Verify shares = floor((bet - fees) / entry_price)
        let pm = PositionManager::new(ExitStrategy::HoldToResolve);
        let market = make_market();
        // edge=0.14, confidence=0.66 → kelly_raw = 0.5 * 0.14 * 0.66 = 0.0462
        let signal = make_entry_signal_with_edge("DOWN", 0.66, Some(0.52), Some(0.14));
        let wallet = 1000.0;

        let pos = pm
            .create_position(&signal, &market, 0.52, wallet)
            .unwrap();

        let kelly_fraction: f64 = (0.5_f64 * 0.14 * 0.66).clamp(
            config::KELLY_MIN_FRACTION,
            config::KELLY_MAX_FRACTION,
        );
        let expected_bet = wallet * kelly_fraction;
        let fee = expected_bet * config::FEE_RATE;
        let capital = expected_bet - fee;
        // entry_price = ask + slippage, rounded up to 2 decimals
        let entry_price = config::ceil_decimals(0.52 + config::SLIPPAGE, config::ORDER_PRICE_DECIMALS);
        let expected_shares = (capital / entry_price).floor();

        assert!(
            (pos.bet_amount - expected_bet).abs() < 0.01,
            "Bet amount: got={:.4}, expected={:.4}",
            pos.bet_amount,
            expected_bet,
        );
        assert!(
            (pos.shares - expected_shares).abs() < 0.01,
            "Shares: got={:.1}, expected={:.1}",
            pos.shares,
            expected_shares,
        );
    }

    #[test]
    fn test_kelly_fraction_boundary_exactly_at_min() {
        // Configure edge/confidence so kelly_raw ≈ 0.01 (right at the boundary)
        // kelly_raw = 0.5 * edge * conf = 0.01 → edge * conf = 0.02
        // e.g. edge=0.0333, confidence=0.6 → 0.5 * 0.0333 * 0.6 = 0.01
        let pm = PositionManager::new(ExitStrategy::HoldToResolve);
        let market = make_market();
        let signal = make_entry_signal_with_edge("UP", 0.60, Some(0.45), Some(0.0333));
        let wallet = 100.0;

        let pos = pm
            .create_position(&signal, &market, 0.45, wallet)
            .unwrap();

        // kelly_raw ≈ 0.5 * 0.0333 * 0.60 ≈ 0.0100
        let kelly_raw: f64 = 0.5 * 0.0333 * 0.60;
        let expected_fraction: f64 = kelly_raw.clamp(
            config::KELLY_MIN_FRACTION,
            config::KELLY_MAX_FRACTION,
        );
        let expected_bet = wallet * expected_fraction;
        assert!(
            (pos.bet_amount - expected_bet).abs() < 0.1,
            "At boundary: got={:.4}, expected={:.4}, fraction={:.4}",
            pos.bet_amount,
            expected_bet,
            expected_fraction,
        );
    }

    #[test]
    fn test_kelly_negative_edge_treated_as_zero() {
        // Negative edge should be clamped to 0, then floored to 1%
        let pm = PositionManager::new(ExitStrategy::HoldToResolve);
        let market = make_market();
        let signal = make_entry_signal_with_edge("UP", 0.70, Some(0.45), Some(-0.10));
        let wallet = 200.0;

        let pos = pm
            .create_position(&signal, &market, 0.45, wallet)
            .unwrap();

        let expected_bet = wallet * config::KELLY_MIN_FRACTION; // 1% floor
        assert!(
            (pos.bet_amount - expected_bet).abs() < 0.01,
            "Negative edge should floor at 1%: got={:.4}, expected={:.4}",
            pos.bet_amount,
            expected_bet,
        );
    }

    #[test]
    fn test_create_position_entry_price_includes_slippage() {
        let pm = PositionManager::new(ExitStrategy::HoldToResolve);
        let market = make_market();
        let signal = make_entry_signal("DOWN", 0.70, Some(0.50));

        let pos = pm.create_position(&signal, &market, 0.50, 200.0).unwrap();

        // entry_price should be ask + slippage (0.005), rounded up
        assert!(
            pos.entry_price >= 0.50 + 0.005,
            "Entry price should be >= ask + slippage: got {}",
            pos.entry_price
        );
    }

    // ── Can trade checks ────────────────────────────────────────────

    #[test]
    fn test_can_trade_with_sufficient_balance() {
        let pm = PositionManager::new(ExitStrategy::HoldToResolve);
        assert!(pm.can_trade(100.0));
    }

    #[test]
    fn test_cannot_trade_with_low_balance() {
        let pm = PositionManager::new(ExitStrategy::HoldToResolve);
        assert!(!pm.can_trade(0.5));
    }

    #[test]
    fn test_cannot_trade_with_position_open() {
        let mut pm = PositionManager::new(ExitStrategy::HoldToResolve);
        let market = make_market();
        let signal = make_entry_signal("UP", 0.75, Some(0.45));

        let pos = pm.create_position(&signal, &market, 0.45, 200.0).unwrap();
        pm.register_open(pos);

        // MAX_OPEN_POSITIONS = 1, so should be blocked now
        assert!(!pm.can_trade(200.0));
    }

    // ── Position close & PnL ────────────────────────────────────────

    #[test]
    fn test_close_position_win() {
        let mut pm = PositionManager::new(ExitStrategy::HoldToResolve);
        let market = make_market();
        let signal = make_entry_signal("UP", 0.75, Some(0.45));

        let pos = pm.create_position(&signal, &market, 0.45, 200.0).unwrap();
        pm.register_open(pos);

        let event = pm.close_position(0, 1.0, ExitType::ResolveWin);
        assert!(event.is_some());

        // Should have positive PnL on a win
        let closed = &pm.closed_positions[0];
        assert!(closed.pnl.unwrap() > 0.0, "Win should have positive PnL");
        assert_eq!(closed.exit_type, Some(ExitType::ResolveWin));
    }

    #[test]
    fn test_close_position_loss() {
        let mut pm = PositionManager::new(ExitStrategy::HoldToResolve);
        let market = make_market();
        let signal = make_entry_signal("DOWN", 0.70, Some(0.50));

        let pos = pm.create_position(&signal, &market, 0.50, 200.0).unwrap();
        pm.register_open(pos);

        let event = pm.close_position(0, 0.0, ExitType::ResolveLoss);
        assert!(event.is_some());

        let closed = &pm.closed_positions[0];
        assert!(closed.pnl.unwrap() < 0.0, "Loss should have negative PnL");
        assert_eq!(closed.exit_type, Some(ExitType::ResolveLoss));
    }

    // ── Exit checks ─────────────────────────────────────────────────

    #[test]
    fn test_exit_check_after_market_end() {
        let mut pm = PositionManager::new(ExitStrategy::HoldToResolve);

        // Create a market that ended in the past
        let past_market = MarketContext {
            slug: "btc-updown-15m-1000000000".to_string(),
            up_token_id: "token_up_111".to_string(),
            down_token_id: "token_down_222".to_string(),
            market_end_ms: 1000000000, // far in the past
        };
        let signal = make_entry_signal("UP", 0.75, Some(0.45));

        let pos = pm
            .create_position(&signal, &past_market, 0.45, 200.0)
            .unwrap();
        pm.register_open(pos);

        let prices = LivePrices {
            up_bid: Some(0.95),
            up_ask: Some(0.96),
            down_bid: Some(0.05),
            down_ask: Some(0.06),
        };

        let exits = pm.check_exits(&prices);
        assert_eq!(exits.len(), 1, "Should have one exit (market ended)");

        // up_bid = 0.95 > WIN_THRESHOLD (0.90)
        assert!(matches!(exits[0].2, ExitType::ResolveWin));
    }

    // ── Deserialization roundtrip: JSON → EntrySignal → Position ────

    #[test]
    fn test_entry_json_to_position_creation() {
        let json = format!(
            r#"{{
            "type": "entry",
            "direction": "DOWN",
            "confidence": 0.72,
            "consistency": 0.85,
            "entry_ask": 0.48,
            "entry_bid": 0.47,
            "market": "btc-updown-15m-1771695900",
            "edge": 0.24,
            "regime": "trend",
            "version": "{}"
        }}"#,
            btc_common::version::SIGNAL_VERSION
        );

        let msg: crate::models::SignalMessage = serde_json::from_str(&json).unwrap();
        let entry = match msg {
            crate::models::SignalMessage::Entry(e) => e,
            other => panic!("Expected Entry, got {other:?}"),
        };

        let pm = PositionManager::new(ExitStrategy::HoldToResolve);
        let market = make_market();
        let pos = pm.create_position(&entry, &market, 0.48, 300.0);
        assert!(pos.is_some());

        let pos = pos.unwrap();
        assert_eq!(pos.side, Direction::Down);
        assert_eq!(pos.token_id, "token_down_222");
        assert!((pos.confidence - 0.72).abs() < 0.001);
        assert!((pos.consistency - 0.85).abs() < 0.001);
    }
}
