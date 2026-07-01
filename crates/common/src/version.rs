// ─── Single source of truth for all version / naming constants ───
//
// Every service (signal-engine, execution-engine, polymarket-ws, redeem-positions)
// imports from here.  When you bump a version, change it in ONE place.
//
// The build.rs script writes `version.json` to the repo root so the Python
// redeem-positions service can read the same values without any Rust dependency.

/// Current signal-strategy version tag (written into every trade ledger entry).
pub const SIGNAL_VERSION: &str = "v8-fix-validation";

/// Human-readable signal method description.
pub const SIGNAL_METHOD: &str = "drift_estimator_v8_fix_validation";

/// Service names (used in /health responses, logs, systemd descriptions).
pub const SERVICE_SIGNAL_ENGINE: &str = "signal-engine";
pub const SERVICE_EXECUTION_ENGINE: &str = "execution-engine";
pub const SERVICE_POLYMARKET_WS: &str = "polymarket-websocket";

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_is_not_empty() {
        assert!(!SIGNAL_VERSION.is_empty());
    }

    #[test]
    fn method_contains_version() {
        assert!(
            SIGNAL_METHOD.contains(SIGNAL_VERSION)
                || SIGNAL_METHOD.contains(&SIGNAL_VERSION.replace('v', ""))
                || SIGNAL_METHOD.contains(&SIGNAL_VERSION.replace('-', "_")),
            "SIGNAL_METHOD should reference the current version"
        );
    }
}
