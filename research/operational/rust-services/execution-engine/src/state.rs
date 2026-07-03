use std::sync::Arc;
use std::sync::atomic::AtomicBool;
use std::time::Instant;

use parking_lot::Mutex;
use tokio::sync::broadcast;

use crate::config;
use crate::models::{ExecutionEvent, ExitStrategy, LivePrices, MarketContext};
use crate::order_executor::OrderExecutor;
use crate::position::PositionManager;
use crate::wallet::WalletBalances;

/// Shared application state accessible from all tasks.
pub struct AppState {
    /// Broadcast channel for execution events → WS clients
    pub event_tx: broadcast::Sender<ExecutionEvent>,

    /// Position manager (bankroll, open/closed positions)
    pub position_manager: Mutex<PositionManager>,

    /// Live bid/ask prices from polymarket-websocket
    pub live_prices: Mutex<LivePrices>,

    /// Current market context (slug, token IDs, end time)
    pub market_context: Mutex<Option<MarketContext>>,

    /// Order executor (Polymarket CLOB SDK)
    pub order_executor: Arc<OrderExecutor>,

    /// Whether the CLOB API is reachable
    pub clob_healthy: AtomicBool,

    /// Wallet address derived from private key
    pub wallet_address: String,

    /// On-chain wallet balances (USDC.e, native USDC, MATIC)
    pub wallet_balances: Mutex<WalletBalances>,

    /// Engine start time (for uptime)
    pub start_time: Instant,
}

impl AppState {
    pub fn new(
        order_executor: OrderExecutor,
        strategy: ExitStrategy,
        wallet_address: String,
    ) -> Self {
        let (event_tx, _) = broadcast::channel(config::BROADCAST_CHANNEL_SIZE);

        Self {
            event_tx,
            position_manager: Mutex::new(PositionManager::new(strategy)),
            live_prices: Mutex::new(LivePrices::default()),
            market_context: Mutex::new(None),
            order_executor: Arc::new(order_executor),
            clob_healthy: AtomicBool::new(false),
            wallet_address,
            wallet_balances: Mutex::new(WalletBalances::default()),
            start_time: Instant::now(),
        }
    }

    /// Broadcast an execution event to all WS subscribers
    pub fn broadcast(&self, event: ExecutionEvent) {
        // Ignore send errors (no subscribers)
        let _ = self.event_tx.send(event);
    }
}
