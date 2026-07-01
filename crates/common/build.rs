/// build.rs — writes version.json to the repo root so non-Rust tooling can read
/// the same signal constants.
///
/// Runs automatically on `cargo build` for any crate that depends on btc-common.
use std::env;
use std::fs;
use std::path::PathBuf;

fn main() {
    // version.rs is the source of truth — keep these in sync manually
    // (we read the constants rather than parsing the .rs file)
    let signal_version = "v8-fix-validation";
    let signal_method = "drift_estimator_v8_fix_validation";

    let json = format!(
        r#"{{
  "signal_version": "{}",
  "signal_method": "{}"
}}"#,
        signal_version, signal_method
    );

    // Walk up from CARGO_MANIFEST_DIR to find the workspace root.
    let out_dir = env::var("CARGO_MANIFEST_DIR").unwrap();
    let manifest_dir = PathBuf::from(&out_dir);

    // common lives at crates/common, so repo root is ../../.
    if let Some(repo_root) = manifest_dir.parent().and_then(|p| p.parent()) {
        let dest = repo_root.join("version.json");
        // Only write if changed to avoid unnecessary rebuilds
        let current = fs::read_to_string(&dest).unwrap_or_default();
        if current.trim() != json.trim() {
            fs::write(&dest, &json).expect("Failed to write version.json");
            eprintln!("cargo:warning=Wrote {}", dest.display());
        }
    }

    println!("cargo:rerun-if-changed=src/version.rs");
}
