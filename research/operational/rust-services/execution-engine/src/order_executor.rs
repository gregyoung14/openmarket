use std::str::FromStr;

use alloy::primitives::U256;
use alloy::signers::Signer as _;
use alloy::signers::local::PrivateKeySigner;
use anyhow::{Context, Result, anyhow};
use polymarket_client_sdk::POLYGON;
use polymarket_client_sdk::clob::types::response::PostOrderResponse;
use polymarket_client_sdk::clob::types::{OrderType, Side};
use polymarket_client_sdk::types::Decimal;
use tracing::{info, warn};

use crate::config;
use crate::models::Position;

/// Round up to the nearest whole integer
fn round_up_to_integer(value: f64) -> Result<i64> {
    if !value.is_finite() {
        return Err(anyhow!("size is not finite"));
    }
    let rounded = value.ceil();
    if rounded <= 0.0 {
        return Err(anyhow!("size must be > 0 after rounding: {rounded}"));
    }
    Ok(rounded as i64)
}

/// The order executor uses the official Polymarket Rust SDK to place orders
/// directly on the CLOB — no Python sidecar needed.
pub struct OrderExecutor {
    /// Raw private key hex (without 0x prefix)
    private_key: String,
}

impl OrderExecutor {
    /// Create the executor from a hex private key (with or without 0x prefix).
    pub fn new(private_key: &str) -> Result<Self> {
        let key = private_key
            .strip_prefix("0x")
            .unwrap_or(private_key)
            .to_owned();

        // Validate the key parses
        let signer = PrivateKeySigner::from_str(&key).context("Failed to parse private key")?;

        info!(address = %signer.address(), "Order executor initialized");
        Ok(Self { private_key: key })
    }

    /// Build signer from stored key
    fn signer(&self) -> Result<PrivateKeySigner> {
        let signer = PrivateKeySigner::from_str(&self.private_key)
            .context("Failed to parse private key")?
            .with_chain_id(Some(POLYGON));
        Ok(signer)
    }

    /// Place a BUY limit order (GTC) to enter a position.
    pub async fn buy(&self, token_id: &str, price: f64, size: f64) -> Result<PostOrderResponse> {
        let size_int = round_up_to_integer(size)?;
        let price = config::ceil_decimals(price, config::ORDER_PRICE_DECIMALS);

        info!(
            token_id = token_id,
            price = price,
            size = size_int,
            "Placing BUY order"
        );

        let signer = self.signer()?;
        let client =
            polymarket_client_sdk::clob::Client::new(config::CLOB_BASE_URL, Default::default())?
                .authentication_builder(&signer)
                .authenticate()
                .await
                .context("Failed to authenticate with CLOB")?;

        let price_dec =
            Decimal::from_str(&format!("{price:.4}")).context("Invalid price decimal")?;
        let size_dec = Decimal::from_str(&size_int.to_string()).context("Invalid size decimal")?;

        let tid = U256::from_str(token_id).context("Invalid token_id for U256")?;

        let min_tick = client
            .tick_size(tid)
            .await
            .context("Failed to fetch market tick size")?
            .minimum_tick_size
            .as_decimal();
        let tick_decimals = min_tick.scale();
        let price_dec = price_dec.trunc_with_scale(tick_decimals);

        let order = client
            .limit_order()
            .token_id(tid)
            .size(size_dec)
            .price(price_dec)
            .side(Side::Buy)
            .order_type(OrderType::FOK)
            .build()
            .await?;

        let signed_order = client
            .sign(&signer, order)
            .await
            .context("Failed to sign buy order")?;

        let response = client
            .post_order(signed_order)
            .await
            .context("Failed to post buy order")?;

        info!(
            order_id = %response.order_id,
            success = response.success,
            status = ?response.status,
            "BUY order submitted"
        );
        Ok(response)
    }

    /// Place a SELL limit order to exit a position.
    pub async fn sell(&self, token_id: &str, shares: f64, min_price: f64) -> Result<String> {
        let shares_int = round_up_to_integer(shares)?;
        let min_price = config::ceil_decimals(min_price, config::ORDER_PRICE_DECIMALS);

        info!(
            token_id = token_id,
            shares = shares_int,
            min_price = min_price,
            "Placing SELL order"
        );

        let signer = self.signer()?;
        let client =
            polymarket_client_sdk::clob::Client::new(config::CLOB_BASE_URL, Default::default())?
                .authentication_builder(&signer)
                .authenticate()
                .await
                .context("Failed to authenticate with CLOB")?;

        let price_dec =
            Decimal::from_str(&format!("{min_price:.4}")).context("Invalid price decimal")?;
        let size_dec =
            Decimal::from_str(&shares_int.to_string()).context("Invalid size decimal")?;

        let tid = U256::from_str(token_id).context("Invalid token_id for U256")?;

        let min_tick = client
            .tick_size(tid)
            .await
            .context("Failed to fetch market tick size")?
            .minimum_tick_size
            .as_decimal();
        let tick_decimals = min_tick.scale();
        let price_dec = price_dec.trunc_with_scale(tick_decimals);

        let order = client
            .limit_order()
            .token_id(tid)
            .size(size_dec)
            .price(price_dec)
            .side(Side::Sell)
            .build()
            .await?;

        let signed_order = client
            .sign(&signer, order)
            .await
            .context("Failed to sign sell order")?;

        let response = client
            .post_order(signed_order)
            .await
            .context("Failed to post sell order")?;

        let order_id = response.order_id.clone();
        info!(order_id = %order_id, success = response.success, status = ?response.status, "SELL order placed");
        Ok(order_id)
    }

    /// Entry: buy into a position.
    pub async fn enter_position(&self, position: &Position) -> Result<PostOrderResponse> {
        self.buy(&position.token_id, position.entry_price, position.shares)
            .await
    }

    /// Best-effort single-order cancel; used when an entry wasn't filled instantly.
    pub async fn cancel_order(&self, order_id: &str) -> Result<()> {
        if order_id.is_empty() {
            return Ok(());
        }

        let signer = self.signer()?;
        let client =
            polymarket_client_sdk::clob::Client::new(config::CLOB_BASE_URL, Default::default())?
                .authentication_builder(&signer)
                .authenticate()
                .await
                .context("Failed to authenticate with CLOB")?;
        client
            .cancel_order(order_id)
            .await
            .with_context(|| format!("Failed to cancel order {order_id}"))?;
        Ok(())
    }

    /// Exit: sell a position at the given price.
    pub async fn exit_position(&self, position: &Position, exit_price: f64) -> Result<String> {
        self.sell(
            &position.token_id,
            position.shares,
            (exit_price - config::SLIPPAGE).max(0.01),
        )
        .await
    }

    /// Check if the CLOB API is reachable
    pub async fn health_check(&self) -> bool {
        match polymarket_client_sdk::clob::Client::default().ok().await {
            Ok(_) => true,
            Err(e) => {
                warn!(error = %e, "CLOB health check failed");
                false
            }
        }
    }
}
