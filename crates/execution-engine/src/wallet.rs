//! On-chain wallet balance polling via Polygon JSON-RPC.
//!
//! Queries:
//! - USDC.e (bridged) balance — this is what Polymarket uses
//! - Native USDC balance
//! - MATIC balance (for gas)
//!
//! Uses raw `eth_call` / `eth_getBalance` so we don't need web3 crate.

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use tracing::warn;

// ─── Constants ─────────────────────────────────────────────────────────────

/// Polygon public RPCs (tried in order; first success wins)
const POLYGON_RPCS: &[&str] = &[
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon-rpc.com",
];

/// USDC.e (bridged from Ethereum) — the one Polymarket actually uses
const USDC_E_ADDRESS: &str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174";

/// Native USDC on Polygon
const USDC_NATIVE_ADDRESS: &str = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359";

/// ERC20 `balanceOf(address)` selector
const BALANCE_OF_SELECTOR: &str = "70a08231";

// ─── Types ─────────────────────────────────────────────────────────────────

/// On-chain wallet balances (all denominated in human-readable units)
#[derive(Debug, Clone, Serialize, Default)]
pub struct WalletBalances {
    /// USDC.e (bridged) — Polymarket's collateral token
    pub usdc_e: f64,
    /// Native USDC on Polygon
    pub usdc_native: f64,
    /// MATIC for gas
    pub matic: f64,
}

/// Standard JSON-RPC response
#[derive(Debug, Deserialize)]
struct RpcResponse {
    result: Option<String>,
}

// ─── Public API ────────────────────────────────────────────────────────────

/// Pick the first working RPC from the list.
/// Returns the URL that succeeded so callers reuse it within the same poll.
async fn pick_rpc(client: &reqwest::Client) -> Result<&'static str> {
    for rpc in POLYGON_RPCS {
        let body = serde_json::json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_blockNumber",
            "params": []
        });
        match client.post(*rpc).json(&body).send().await {
            Ok(resp) => {
                if let Ok(parsed) = resp.json::<RpcResponse>().await
                    && parsed.result.is_some()
                {
                    return Ok(*rpc);
                }
            }
            Err(_) => continue,
        }
    }
    anyhow::bail!("All Polygon RPCs failed health-check")
}

/// Fetch all on-chain balances for the given wallet address.
pub async fn fetch_balances(wallet_address: &str) -> Result<WalletBalances> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()?;
    let addr_clean = wallet_address
        .strip_prefix("0x")
        .unwrap_or(wallet_address)
        .to_lowercase();

    // Pad address to 32 bytes for ERC20 call data
    let padded_addr = format!("{:0>64}", addr_clean);

    // Pick a working RPC
    let rpc_url = pick_rpc(&client).await?;

    // 1. USDC.e balance (6 decimals)
    let usdc_e = erc20_balance_of(&client, rpc_url, USDC_E_ADDRESS, &padded_addr, 6)
        .await
        .unwrap_or_else(|e| {
            warn!(error = %e, "Failed to fetch USDC.e balance");
            0.0
        });

    // 2. Native USDC balance (6 decimals)
    let usdc_native = erc20_balance_of(&client, rpc_url, USDC_NATIVE_ADDRESS, &padded_addr, 6)
        .await
        .unwrap_or_else(|e| {
            warn!(error = %e, "Failed to fetch native USDC balance");
            0.0
        });

    // 3. MATIC balance (18 decimals)
    let matic = native_balance(&client, rpc_url, wallet_address)
        .await
        .unwrap_or_else(|e| {
            warn!(error = %e, "Failed to fetch MATIC balance");
            0.0
        });

    Ok(WalletBalances {
        usdc_e,
        usdc_native,
        matic,
    })
}

// ─── Helpers ───────────────────────────────────────────────────────────────

/// Call ERC20 `balanceOf(address)` via eth_call
async fn erc20_balance_of(
    client: &reqwest::Client,
    rpc_url: &str,
    contract: &str,
    padded_addr: &str,
    decimals: u32,
) -> Result<f64> {
    let call_data = format!("0x{BALANCE_OF_SELECTOR}{padded_addr}");

    let body = serde_json::json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{
            "to": contract,
            "data": call_data
        }, "latest"]
    });

    let resp: RpcResponse = client
        .post(rpc_url)
        .json(&body)
        .send()
        .await
        .context("RPC request failed")?
        .json()
        .await
        .context("RPC response parse failed")?;

    let hex = resp.result.unwrap_or_default();
    let hex_clean = hex.strip_prefix("0x").unwrap_or(&hex);

    if hex_clean.is_empty() || hex_clean.chars().all(|c| c == '0') {
        return Ok(0.0);
    }

    let raw = u128::from_str_radix(hex_clean.trim_start_matches('0'), 16).unwrap_or(0);
    let divisor = 10u128.pow(decimals) as f64;
    Ok(raw as f64 / divisor)
}

/// Get native MATIC balance via eth_getBalance
async fn native_balance(client: &reqwest::Client, rpc_url: &str, address: &str) -> Result<f64> {
    let body = serde_json::json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getBalance",
        "params": [address, "latest"]
    });

    let resp: RpcResponse = client
        .post(rpc_url)
        .json(&body)
        .send()
        .await
        .context("RPC request failed")?
        .json()
        .await
        .context("RPC response parse failed")?;

    let hex = resp.result.unwrap_or_default();
    let hex_clean = hex.strip_prefix("0x").unwrap_or(&hex);

    if hex_clean.is_empty() || hex_clean.chars().all(|c| c == '0') {
        return Ok(0.0);
    }

    let raw = u128::from_str_radix(hex_clean.trim_start_matches('0'), 16).unwrap_or(0);
    Ok(raw as f64 / 1e18)
}
