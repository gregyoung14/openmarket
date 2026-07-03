mod config;
mod handlers;
mod models;
mod order_executor;
mod position;
mod signal_consumer;
mod state;
mod wallet;

use std::str::FromStr;
use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::Duration;

use axum::{Router, routing::get};
use polymarket_client_sdk::clob::types::OrderStatusType;
use tokio::sync::mpsc;
use tower_http::cors::CorsLayer;
use tracing::{error, info, warn};

use models::*;
use state::AppState;

fn update_market_context(state: &Arc<AppState>, market_info: MarketInfoMsg) {
    if let (Some(slug), Some(token_ids)) = (market_info.slug.clone(), market_info.token_ids.clone())
        && token_ids.len() >= 2
    {
        let market_end_ms = slug
            .rsplit('-')
            .next()
            .and_then(|s| s.parse::<i64>().ok())
            .map(|start| (start + config::MARKET_DURATION_SECS) * 1000)
            .unwrap_or(0);

        let ctx = MarketContext {
            slug: slug.clone(),
            up_token_id: token_ids[0].clone(),
            down_token_id: token_ids[1].clone(),
            market_end_ms,
        };

        info!(
            slug = %slug,
            up_token = %ctx.up_token_id,
            down_token = %ctx.down_token_id,
            market_end_ms = market_end_ms,
            "Market context updated"
        );

        *state.market_context.lock() = Some(ctx);
    }
}

fn apply_signal_price_update(state: &Arc<AppState>, pc: PriceChangeMsg) {
    let resolved_side = pc
        .market_side
        .as_deref()
        .map(|s| s.to_uppercase())
        .filter(|s| s == "UP" || s == "DOWN")
        .or_else(|| {
            let asset_id = pc.asset_id.as_deref().or(pc.token_id.as_deref())?;
            let ctx = state.market_context.lock();
            let market = ctx.as_ref()?;

            if market.up_token_id == asset_id {
                Some("UP".to_string())
            } else if market.down_token_id == asset_id {
                Some("DOWN".to_string())
            } else {
                None
            }
        });

    let Some(side) = resolved_side else {
        return;
    };

    let mut prices = state.live_prices.lock();
    match side.as_str() {
        "UP" => {
            if let Some(bid) = pc.best_bid.filter(|v| v.is_finite() && *v > 0.0) {
                prices.up_bid = Some(bid);
            }
            if let Some(ask) = pc.best_ask.filter(|v| v.is_finite() && *v > 0.0) {
                prices.up_ask = Some(ask);
            }
        }
        "DOWN" => {
            if let Some(bid) = pc.best_bid.filter(|v| v.is_finite() && *v > 0.0) {
                prices.down_bid = Some(bid);
            }
            if let Some(ask) = pc.best_ask.filter(|v| v.is_finite() && *v > 0.0) {
                prices.down_ask = Some(ask);
            }
        }
        _ => {}
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Initialize tracing
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "execution_engine=info".into()),
        )
        .init();

    let test_mode = config::is_test_mode();
    info!("═══════════════════════════════════════════════════════════");
    if test_mode {
        info!("  Polymarket Execution Engine v1.0.0  🧪 TEST MODE");
        info!(
            "  Fixed sizing: {} share per trade",
            config::TEST_MODE_SHARES
        );
    } else {
        info!("  Polymarket Execution Engine v1.0.0");
    }
    info!("  Pure Rust — 100% signal-engine driven");
    info!("  Trade decisions: signal-engine v9.2 entry signals only");
    info!("  No internal signal aggregation — signal engine is source of truth");
    info!("═══════════════════════════════════════════════════════════");

    // Live execution requires an explicit environment variable. The public
    // repository does not read private keys from machine-local fallback paths.
    let private_key = std::env::var(config::PRIVATE_KEY_ENV)
        .expect("Set POLYMARKET_PRIVATE_KEY to enable execution-engine");

    if private_key.is_empty() {
        anyhow::bail!("Private key is empty");
    }

    // Choose strategy from env or default
    let strategy = match std::env::var("EXIT_STRATEGY").as_deref() {
        Ok("momentum" | "MOMENTUM") => ExitStrategy::Momentum,
        _ => ExitStrategy::HoldToResolve,
    };
    info!(strategy = ?strategy, "Exit strategy selected");

    // Initialize order executor
    let executor = order_executor::OrderExecutor::new(&private_key)?;

    // Derive wallet address from private key
    let wallet_address = {
        let signer = alloy::signers::local::PrivateKeySigner::from_str(
            private_key.strip_prefix("0x").unwrap_or(&private_key),
        )?;
        format!("{:#x}", signer.address())
    };
    info!(wallet = %wallet_address, "Wallet address derived");

    // Build shared state
    let state = Arc::new(AppState::new(executor, strategy, wallet_address));

    // ─── Spawn signal consumer (ws://127.0.0.1:8003/ws) ──────────────
    let (signal_tx, signal_rx) = mpsc::unbounded_channel::<SignalMessage>();
    {
        tokio::spawn(signal_consumer::run(signal_tx));
    }

    // ─── Spawn polymarket price feed (ws://127.0.0.1:8002/ws) ────────
    let (price_tx, price_rx) = mpsc::unbounded_channel::<PolymarketMessage>();
    {
        tokio::spawn(signal_consumer::run_price_feed(price_tx));
    }

    // ─── Spawn CLOB health checker ───────────────────────────────────
    {
        let state = Arc::clone(&state);
        tokio::spawn(async move {
            loop {
                let healthy = state.order_executor.health_check().await;
                state.clob_healthy.store(healthy, Ordering::Relaxed);
                tokio::time::sleep(std::time::Duration::from_secs(30)).await;
            }
        });
    }

    // ─── Spawn on-chain balance poller (every 30s) ───────────────────
    {
        let state = Arc::clone(&state);
        tokio::spawn(async move {
            loop {
                match wallet::fetch_balances(&state.wallet_address).await {
                    Ok(balances) => {
                        info!(
                            usdc_e = balances.usdc_e,
                            usdc_native = balances.usdc_native,
                            matic = balances.matic,
                            "On-chain balances updated"
                        );
                        *state.wallet_balances.lock() = balances;
                    }
                    Err(e) => warn!(error = %e, "Failed to fetch on-chain balances"),
                }
                tokio::time::sleep(std::time::Duration::from_secs(30)).await;
            }
        });
    }

    // ─── Spawn signal processing loop ────────────────────────────────
    {
        let state = Arc::clone(&state);
        tokio::spawn(signal_processing_loop(state, signal_rx));
    }

    // ─── Spawn price update loop ─────────────────────────────────────
    {
        let state = Arc::clone(&state);
        tokio::spawn(price_update_loop(state, price_rx));
    }

    // ─── Spawn exit checker (1s tick) ────────────────────────────────
    {
        let state = Arc::clone(&state);
        tokio::spawn(exit_check_loop(state));
    }

    // ─── Spawn periodic status broadcast ─────────────────────────────
    {
        let state = Arc::clone(&state);
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(std::time::Duration::from_secs(10)).await;

                let uptime_secs = state.start_time.elapsed().as_secs();
                let clob_connected = state.clob_healthy.load(Ordering::Relaxed);
                let market_slug = state
                    .market_context
                    .lock()
                    .as_ref()
                    .map(|m| m.slug.clone())
                    .unwrap_or_default();
                let live_prices = state.live_prices.lock().clone();
                let balances = state.wallet_balances.lock().clone();

                let event = state.position_manager.lock().status_event(
                    &state.wallet_address,
                    uptime_secs,
                    clob_connected,
                    &market_slug,
                    &live_prices,
                    &balances,
                );
                state.broadcast(event);
            }
        });
    }

    // ─── Spawn market refresh scheduler (15m aligned ticks) ──────────
    {
        let state = Arc::clone(&state);
        tokio::spawn(market_refresh_loop(state));
    }

    // ─── HTTP + WS Server ────────────────────────────────────────────
    let app = Router::new()
        .route("/health", get(handlers::health))
        .route("/status", get(handlers::status))
        .route("/ws", get(handlers::ws_handler))
        .layer(CorsLayer::permissive())
        .with_state(Arc::clone(&state));

    let addr = format!("{}:{}", config::SERVER_HOST, config::SERVER_PORT);
    info!(addr = %addr, "Starting HTTP server");

    let listener = tokio::net::TcpListener::bind(&addr).await?;
    axum::serve(listener, app).await?;

    Ok(())
}

/// Poll market boundaries every 900s, aligned to wall clock quarter-hours.
/// Never stops; on each tick, drops stale context so fresh market info is required.
async fn market_refresh_loop(state: Arc<AppState>) {
    let interval = config::MARKET_DURATION_SECS.max(1);
    info!(interval_secs = interval, "Market refresh scheduler started");

    loop {
        let now = chrono::Utc::now().timestamp();
        let next_tick = ((now / interval) + 1) * interval;
        let sleep_secs = (next_tick - now).max(1) as u64;

        tokio::time::sleep(Duration::from_secs(sleep_secs)).await;

        let expected_start = (chrono::Utc::now().timestamp() / interval) * interval;

        let mut stale_or_missing = true;
        {
            let context = state.market_context.lock();
            if let Some(ctx) = context.as_ref() {
                let slug_start = ctx
                    .slug
                    .rsplit('-')
                    .next()
                    .and_then(|s| s.parse::<i64>().ok());
                if let Some(start) = slug_start {
                    stale_or_missing = start < expected_start;
                }
            }
        }

        if stale_or_missing {
            {
                let mut context = state.market_context.lock();
                *context = None;
            }
            {
                let mut prices = state.live_prices.lock();
                prices.up_bid = None;
                prices.up_ask = None;
                prices.down_bid = None;
                prices.down_ask = None;
            }

            warn!(
                expected_start,
                "Market refresh tick: cleared stale/missing market context; waiting for fresh market info"
            );
        } else {
            info!(
                expected_start,
                "Market refresh tick: market context already fresh"
            );
        }
    }
}

/// Process incoming signal messages from the signal engine.
/// Trade decisions are made ENTIRELY by the signal engine. When we receive an
/// Entry signal, we execute it. Predictions are logged for monitoring only.
async fn signal_processing_loop(
    state: Arc<AppState>,
    mut rx: mpsc::UnboundedReceiver<SignalMessage>,
) {
    info!("Signal processing loop started — entry signals drive all trades");

    while let Some(msg) = rx.recv().await {
        match msg {
            SignalMessage::Connected { .. } => {
                info!("Signal engine connected");
            }

            SignalMessage::Ready { features } => {
                info!(features = ?features, "Signal engine ready");
            }

            SignalMessage::MarketInfo(market_info) | SignalMessage::NewMarket(market_info) => {
                update_market_context(&state, market_info);
            }

            SignalMessage::Prediction(pred) => {
                // Informational only — log for monitoring, no trade decisions
                let direction = &pred.direction;
                let confidence = pred.confidence;
                let market = pred.market.as_deref().unwrap_or("?");
                let secs_in = pred.secs_in.unwrap_or(0);

                // Broadcast as a watching signal for monitoring clients
                if let Some(market_ctx) = state.market_context.lock().as_ref() {
                    state.broadcast(ExecutionEvent::Signal {
                        direction: direction.clone(),
                        confidence,
                        consistency: 0.0,
                        n_predictions: 0,
                        market: market_ctx.slug.clone(),
                        secs_in,
                        action: "WATCHING".to_string(),
                        timestamp: chrono::Utc::now().timestamp_millis(),
                    });
                }

                tracing::debug!(
                    direction = %direction,
                    confidence = confidence,
                    market = %market,
                    secs_in = secs_in,
                    "Prediction received (informational only)"
                );
            }

            SignalMessage::Entry(entry) => {
                // ═══════════════════════════════════════════════════════════
                // THIS IS THE ONLY PATH THAT TRIGGERS TRADES.
                // The signal engine has already applied all v9.2 filters:
                //   - Regime gating (skip chop)
                //   - Adaptive confirmation (15–50s sustained)
                //   - Price cap (≤ 0.55)
                //   - EV edge filter (≥ 0.08)
                //   - Hour blacklist
                // We trust the signal engine completely and just execute.
                // ═══════════════════════════════════════════════════════════
                info!(
                    direction = %entry.direction,
                    confidence = entry.confidence,
                    consistency = ?entry.consistency,
                    edge = ?entry.edge,
                    regime = ?entry.regime,
                    entry_ask = ?entry.entry_ask,
                    market = ?entry.market,
                    secs_in = ?entry.secs_in,
                    version = ?entry.version,
                    "🚀 ENTRY signal received from signal engine — executing trade"
                );

                // Check if we can trade
                let can_trade = {
                    let pm = state.position_manager.lock();
                    let wallet_usdc = state.wallet_balances.lock().usdc_e;
                    pm.can_trade(wallet_usdc)
                };

                let market = state.market_context.lock().clone();

                match (can_trade, market) {
                    (true, Some(market_ctx)) => {
                        // Use live WS ask first (current market truth), fall back to signal entry_ask
                        let direction_upper = entry.direction.to_uppercase();
                        let entry_ask = {
                            let prices = state.live_prices.lock();
                            let live_ask = match direction_upper.as_str() {
                                "UP" => prices.up_ask,
                                "DOWN" => prices.down_ask,
                                _ => None,
                            };
                            live_ask.or(entry.entry_ask)
                        };

                        let Some(ask) = entry_ask.filter(|v| v.is_finite() && *v > 0.0) else {
                            warn!(
                                direction = %entry.direction,
                                "No valid ask price for entry — skipping"
                            );
                            continue;
                        };

                        // Broadcast entering signal
                        state.broadcast(ExecutionEvent::Signal {
                            direction: entry.direction.clone(),
                            confidence: entry.confidence,
                            consistency: entry.consistency.unwrap_or(0.0),
                            n_predictions: 0,
                            market: market_ctx.slug.clone(),
                            secs_in: entry.secs_in.unwrap_or(0) as i64,
                            action: "ENTERING".to_string(),
                            timestamp: chrono::Utc::now().timestamp_millis(),
                        });

                        execute_signal_entry(Arc::clone(&state), &entry, &market_ctx, ask).await;
                    }
                    (false, Some(market_ctx)) => {
                        warn!(
                            direction = %entry.direction,
                            market = %market_ctx.slug,
                            "Entry signal received but cannot trade (position open or insufficient funds)"
                        );
                        state.broadcast(ExecutionEvent::Signal {
                            direction: entry.direction.clone(),
                            confidence: entry.confidence,
                            consistency: entry.consistency.unwrap_or(0.0),
                            n_predictions: 0,
                            market: market_ctx.slug.clone(),
                            secs_in: entry.secs_in.unwrap_or(0) as i64,
                            action: "BLOCKED".to_string(),
                            timestamp: chrono::Utc::now().timestamp_millis(),
                        });
                    }
                    (_, None) => {
                        warn!(
                            direction = %entry.direction,
                            "Entry signal received but no market context yet — skipping"
                        );
                    }
                }
            }

            SignalMessage::PriceChange(pc) => apply_signal_price_update(&state, pc),

            SignalMessage::Exit(_) | SignalMessage::Unknown => {
                // Exit signals and unknown types — informational only
            }
        }
    }

    error!("Signal processing loop ended — channel closed");
}

/// Process live price updates from the polymarket-websocket service.
async fn price_update_loop(
    state: Arc<AppState>,
    mut rx: mpsc::UnboundedReceiver<PolymarketMessage>,
) {
    info!("Price update loop started");

    fn parse_book_price(levels: &Option<Vec<Vec<serde_json::Value>>>, is_bid: bool) -> Option<f64> {
        let rows = levels.as_ref()?;
        let mut best: Option<f64> = None;

        for row in rows {
            let Some(px_raw) = row.first() else {
                continue;
            };
            let px = match px_raw {
                serde_json::Value::Number(n) => n.as_f64(),
                serde_json::Value::String(s) => s.parse::<f64>().ok(),
                _ => None,
            };

            let Some(px) = px.filter(|p| p.is_finite() && *p > 0.0) else {
                continue;
            };

            best = match best {
                None => Some(px),
                Some(curr) if is_bid && px > curr => Some(px),
                Some(curr) if !is_bid && px < curr => Some(px),
                Some(curr) => Some(curr),
            };
        }

        best
    }

    fn apply_price_update(
        state: &Arc<AppState>,
        side: Option<&str>,
        asset_id: Option<&str>,
        best_bid: Option<f64>,
        best_ask: Option<f64>,
    ) {
        let resolved_side = side
            .map(|s| s.to_uppercase())
            .filter(|s| s == "UP" || s == "DOWN")
            .or_else(|| {
                let asset_id = asset_id?;
                let ctx = state.market_context.lock();
                let market = ctx.as_ref()?;

                if market.up_token_id == asset_id {
                    Some("UP".to_string())
                } else if market.down_token_id == asset_id {
                    Some("DOWN".to_string())
                } else {
                    None
                }
            });

        let Some(side_key) = resolved_side else {
            return;
        };

        let mut prices = state.live_prices.lock();
        match side_key.as_str() {
            "UP" => {
                if let Some(b) = best_bid.filter(|v| v.is_finite() && *v > 0.0) {
                    prices.up_bid = Some(b);
                }
                if let Some(a) = best_ask.filter(|v| v.is_finite() && *v > 0.0) {
                    prices.up_ask = Some(a);
                }
            }
            "DOWN" => {
                if let Some(b) = best_bid.filter(|v| v.is_finite() && *v > 0.0) {
                    prices.down_bid = Some(b);
                }
                if let Some(a) = best_ask.filter(|v| v.is_finite() && *v > 0.0) {
                    prices.down_ask = Some(a);
                }
            }
            _ => {}
        }
    }

    while let Some(msg) = rx.recv().await {
        match msg {
            PolymarketMessage::PriceChange {
                best_bid,
                best_ask,
                price,
                asset_id,
                side,
                market_side,
                ..
            } => {
                let order_side = side.as_ref().map(|s| s.to_uppercase());
                let mut resolved_bid = best_bid;
                let mut resolved_ask = best_ask;

                if let Some(px) = price.filter(|v| v.is_finite() && *v > 0.0) {
                    match order_side.as_deref() {
                        Some("BUY") | Some("BID") if resolved_bid.is_none() => {
                            resolved_bid = Some(px);
                        }
                        Some("SELL") | Some("ASK") if resolved_ask.is_none() => {
                            resolved_ask = Some(px);
                        }
                        _ => {}
                    }
                }

                // Prefer resolved market_side (UP/DOWN) from polymarket-websocket
                // over the raw order side (BUY/SELL) which can't tell us UP vs DOWN
                let effective_side = market_side.as_deref().or(side.as_deref());
                apply_price_update(
                    &state,
                    effective_side,
                    asset_id.as_deref(),
                    resolved_bid,
                    resolved_ask,
                );
            }
            PolymarketMessage::Book {
                asset_id,
                bids,
                asks,
                side,
                market_side,
            } => {
                let best_bid = parse_book_price(&bids, true);
                let best_ask = parse_book_price(&asks, false);
                // Prefer market_side (UP/DOWN) over raw side for consistency
                let effective_side = market_side.as_deref().or(side.as_deref());
                apply_price_update(
                    &state,
                    effective_side,
                    asset_id.as_deref(),
                    best_bid,
                    best_ask,
                );
            }
            PolymarketMessage::MarketInfo {
                slug, token_ids, ..
            }
            | PolymarketMessage::NewMarket {
                slug, token_ids, ..
            } => {
                // Keep market context fresh from polymarket WS token mapping.
                if let (Some(slug), Some(ids)) = (slug, token_ids)
                    && ids.len() >= 2
                {
                    let mut ctx = state.market_context.lock();
                    let market_end_ms = slug
                        .rsplit('-')
                        .next()
                        .and_then(|s| s.parse::<i64>().ok())
                        .map(|start| (start + config::MARKET_DURATION_SECS) * 1000)
                        .unwrap_or(0);

                    let should_update = match ctx.as_ref() {
                        Some(existing) => {
                            existing.slug != slug
                                || existing.up_token_id != ids[0]
                                || existing.down_token_id != ids[1]
                        }
                        None => true,
                    };

                    if should_update {
                        *ctx = Some(MarketContext {
                            slug: slug.clone(),
                            up_token_id: ids[0].clone(),
                            down_token_id: ids[1].clone(),
                            market_end_ms,
                        });
                        info!(slug = %slug, up_token = %ids[0], down_token = %ids[1], "Market context updated from polymarket WS");
                    }
                }
            }
            PolymarketMessage::MarketResolved {
                winning_asset_id,
                winning_outcome,
            } => {
                info!(
                    winning_asset_id = ?winning_asset_id,
                    winning_outcome = ?winning_outcome,
                    "Market resolved event received"
                );
            }
            _ => {}
        }
    }
}

/// Execute an entry trade based on a signal engine entry signal.
/// The signal engine has already validated everything (v9.2 filters).
/// We just size the position and place the order.
async fn execute_signal_entry(
    state: Arc<AppState>,
    entry: &EntrySignal,
    market: &MarketContext,
    _entry_ask: f64,
) {
    let mut last_error: Option<String> = None;
    let direction_upper = entry.direction.to_uppercase();

    // ── FIX 3: Price sanity check ──
    // Cross-check the signal engine's entry_ask against our own live price feed.
    // If they diverge by > $0.10, the token-side mapping may be out of sync
    // between signal engine and execution engine (the root cause of side inversion).
    if let Some(signal_ask) = entry.entry_ask {
        let our_ask = {
            let prices = state.live_prices.lock();
            match direction_upper.as_str() {
                "UP" => prices.up_ask,
                "DOWN" => prices.down_ask,
                _ => None,
            }
        };
        if let Some(our_ask) = our_ask {
            let diff = (our_ask - signal_ask).abs();
            if diff > 0.10 {
                warn!(
                    signal_ask = signal_ask,
                    our_ask = our_ask,
                    diff = diff,
                    direction = %entry.direction,
                    "⛔ Price mismatch between signal engine and live feed — \
                     possible token-side mapping desync. Skipping entry."
                );
                state.broadcast(ExecutionEvent::Error {
                    message: format!(
                        "Price mismatch: signal_ask={signal_ask:.4}, our_ask={our_ask:.4}, diff={diff:.4}. Skipping."
                    ),
                    timestamp: chrono::Utc::now().timestamp_millis(),
                });
                return;
            }
        }
    }

    for attempt in 1..=config::ENTRY_RETRY_ATTEMPTS {
        // Always use freshest websocket ask first; fall back to signal ask only if WS missing.
        let latest_ask = {
            let prices = state.live_prices.lock();
            let live_ask = match direction_upper.as_str() {
                "UP" => prices.up_ask,
                "DOWN" => prices.down_ask,
                _ => None,
            };
            live_ask.or(entry.entry_ask)
        };

        let Some(latest_ask) = latest_ask.filter(|v| v.is_finite() && *v > 0.0) else {
            let msg = format!(
                "Missing live {} ask from websocket; skipping entry attempt",
                entry.direction
            );
            warn!(attempt, direction = %entry.direction, "{msg}");
            last_error = Some(msg);
            break;
        };

        let position = {
            let pm = state.position_manager.lock();
            let wallet_usdc = state.wallet_balances.lock().usdc_e;
            pm.create_position(entry, market, latest_ask, wallet_usdc)
        };

        let Some(position) = position else {
            warn!(attempt, "Failed to compute position sizing for entry");
            last_error = Some("position sizing failed".to_string());
            break;
        };

        info!(
            attempt,
            side = position.side.as_str(),
            token = %position.token_id,
            ask = latest_ask,
            price = position.entry_price,
            shares = position.shares,
            bet = position.bet_amount,
            "Attempting fast entry order (FOK)"
        );

        match state.order_executor.enter_position(&position).await {
            Ok(resp) => {
                let instant_fill = resp.success && matches!(resp.status, OrderStatusType::Matched);

                if instant_fill {
                    info!(order_id = %resp.order_id, "Entry filled instantly");

                    // Register position only after confirmed immediate fill.
                    let event = state.position_manager.lock().register_open(position);
                    state.broadcast(event);

                    state
                        .position_manager
                        .lock()
                        .save_trade_log("live_trades.json");
                    return;
                }

                warn!(
                    attempt,
                    order_id = %resp.order_id,
                    success = resp.success,
                    status = ?resp.status,
                    error_msg = ?resp.error_msg,
                    "Entry not filled instantly; canceling and retrying"
                );

                if let Err(cancel_err) = state.order_executor.cancel_order(&resp.order_id).await {
                    warn!(order_id = %resp.order_id, error = %cancel_err, "Best-effort cancel failed");
                }

                last_error = Some(format!(
                    "not instantly filled (status={:?}, success={})",
                    resp.status, resp.success
                ));
            }
            Err(e) => {
                let err_chain = format!("{e:#}");
                warn!(attempt, error = %err_chain, "Entry attempt failed");
                last_error = Some(err_chain);
            }
        }

        if attempt < config::ENTRY_RETRY_ATTEMPTS {
            tokio::time::sleep(std::time::Duration::from_millis(
                config::ENTRY_RETRY_DELAY_MS,
            ))
            .await;
        }
    }

    let reason = last_error.unwrap_or_else(|| "unknown entry failure".to_string());
    error!(error = %reason, "Failed to place entry order after retries");
    state.broadcast(ExecutionEvent::Error {
        message: format!("Entry order failed after retries: {reason}"),
        timestamp: chrono::Utc::now().timestamp_millis(),
    });
}

/// Check exits every second.
async fn exit_check_loop(state: Arc<AppState>) {
    info!("Exit check loop started");

    loop {
        tokio::time::sleep(std::time::Duration::from_secs(1)).await;

        let exits = {
            let prices = state.live_prices.lock().clone();
            let mut pm = state.position_manager.lock();
            pm.check_exits(&prices)
        };

        if exits.is_empty() {
            continue;
        }

        // Process exits in reverse order (to maintain indices)
        let mut sorted_exits = exits;
        sorted_exits.sort_by(|a, b| b.0.cmp(&a.0));

        for (idx, exit_price, exit_type) in sorted_exits {
            // Get position info for the sell order
            let position_info = {
                let pm = state.position_manager.lock();
                pm.positions.get(idx).cloned()
            };

            let Some(pos) = position_info else {
                continue;
            };

            // For resolution exits, we don't need to place a sell order
            // (the market resolves automatically). For take-profit, we do.
            let needs_sell = matches!(exit_type, models::ExitType::TakeProfit);

            if needs_sell {
                match state.order_executor.exit_position(&pos, exit_price).await {
                    Ok(order_id) => {
                        info!(order_id = %order_id, exit_type = ?exit_type, "Exit order placed");
                    }
                    Err(e) => {
                        error!(error = %e, "Failed to place exit order, forcing close");
                        // Still close the position in our tracking even if order fails
                    }
                }
            }

            // Close position in our tracking
            if let Some(event) = state
                .position_manager
                .lock()
                .close_position(idx, exit_price, exit_type)
            {
                state.broadcast(event);
            }

            // Save trade log
            state
                .position_manager
                .lock()
                .save_trade_log("live_trades.json");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::time::{Duration, timeout};

    fn test_state() -> Arc<AppState> {
        let executor = order_executor::OrderExecutor::new(
            // Placeholder: use a test-only private key here (e.g., Anvil default).
            "0x0000000000000000000000000000000000000000000000000000000000000001",
        )
        .expect("dummy private key should parse");

        Arc::new(AppState::new(
            executor,
            ExitStrategy::HoldToResolve,
            "0xtest_wallet".to_string(),
        ))
    }

    #[test]
    fn update_market_context_sets_tokens_and_end_time() {
        let state = test_state();
        update_market_context(
            &state,
            MarketInfoMsg {
                slug: Some("btc-updown-15m-1771695900".to_string()),
                question: None,
                token_ids: Some(vec![
                    "up_token_123".to_string(),
                    "down_token_456".to_string(),
                ]),
                end_date: None,
            },
        );

        let ctx = state
            .market_context
            .lock()
            .clone()
            .expect("market context should exist");
        assert_eq!(ctx.slug, "btc-updown-15m-1771695900");
        assert_eq!(ctx.up_token_id, "up_token_123");
        assert_eq!(ctx.down_token_id, "down_token_456");
        assert_eq!(
            ctx.market_end_ms,
            (1771695900_i64 + config::MARKET_DURATION_SECS) * 1000
        );
    }

    #[test]
    fn apply_signal_price_update_resolves_side_from_asset_id() {
        let state = test_state();
        update_market_context(
            &state,
            MarketInfoMsg {
                slug: Some("btc-updown-15m-1771695900".to_string()),
                question: None,
                token_ids: Some(vec![
                    "up_token_123".to_string(),
                    "down_token_456".to_string(),
                ]),
                end_date: None,
            },
        );

        apply_signal_price_update(
            &state,
            PriceChangeMsg {
                best_bid: Some(0.44),
                best_ask: Some(0.45),
                token_id: None,
                asset_id: Some("up_token_123".to_string()),
                side: Some("SELL".to_string()),
                market_side: None,
            },
        );

        let prices = state.live_prices.lock().clone();
        assert_eq!(prices.up_bid, Some(0.44));
        assert_eq!(prices.up_ask, Some(0.45));
        assert_eq!(prices.down_bid, None);
    }

    #[tokio::test]
    async fn signal_processing_loop_smoke_updates_context_and_prices() {
        let state = test_state();
        let (tx, rx) = mpsc::unbounded_channel();

        let handle = tokio::spawn(signal_processing_loop(Arc::clone(&state), rx));

        tx.send(SignalMessage::NewMarket(MarketInfoMsg {
            slug: Some("btc-updown-15m-1771695900".to_string()),
            question: None,
            token_ids: Some(vec![
                "up_token_123".to_string(),
                "down_token_456".to_string(),
            ]),
            end_date: None,
        }))
        .unwrap();
        tx.send(SignalMessage::PriceChange(PriceChangeMsg {
            best_bid: Some(0.54),
            best_ask: Some(0.55),
            token_id: None,
            asset_id: Some("down_token_456".to_string()),
            side: Some("BUY".to_string()),
            market_side: None,
        }))
        .unwrap();
        drop(tx);

        timeout(Duration::from_secs(1), handle)
            .await
            .unwrap()
            .unwrap();

        let ctx = state
            .market_context
            .lock()
            .clone()
            .expect("market context should be set");
        let prices = state.live_prices.lock().clone();
        assert_eq!(ctx.up_token_id, "up_token_123");
        assert_eq!(prices.down_bid, Some(0.54));
        assert_eq!(prices.down_ask, Some(0.55));
    }

    #[tokio::test]
    async fn price_update_loop_smoke_updates_market_context_and_live_prices() {
        let state = test_state();
        let (tx, rx) = mpsc::unbounded_channel();

        let handle = tokio::spawn(price_update_loop(Arc::clone(&state), rx));

        tx.send(PolymarketMessage::NewMarket {
            slug: Some("btc-updown-15m-1771695900".to_string()),
            question: None,
            token_ids: Some(vec![
                "up_token_123".to_string(),
                "down_token_456".to_string(),
            ]),
            end_date: None,
        })
        .unwrap();
        tx.send(PolymarketMessage::PriceChange {
            best_bid: Some(0.44),
            best_ask: Some(0.45),
            price: None,
            token_id: None,
            asset_id: Some("up_token_123".to_string()),
            side: Some("SELL".to_string()),
            market_side: None,
        })
        .unwrap();
        drop(tx);

        timeout(Duration::from_secs(1), handle)
            .await
            .unwrap()
            .unwrap();

        let ctx = state
            .market_context
            .lock()
            .clone()
            .expect("market context should be set");
        let prices = state.live_prices.lock().clone();
        assert_eq!(ctx.down_token_id, "down_token_456");
        assert_eq!(prices.up_bid, Some(0.44));
        assert_eq!(prices.up_ask, Some(0.45));
    }
}
