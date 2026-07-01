# Version Management Guide

> **Single source of truth:** `crates/common/src/version.rs`

All version strings, service names, and signal method identifiers live in one
place. Every service — Rust or Python — reads from this crate, either at
compile time or via a generated JSON file.

---

## Constants

| Constant | Current Value | Used For |
|---|---|---|
| `SIGNAL_VERSION` | `v14` | Trade ledger entries, `/health` responses, WS messages |
| `SIGNAL_METHOD` | `drift_estimator_v14_quant_paper` | `/health` and WS `connected` payloads |
| `SERVICE_SIGNAL_ENGINE` | `signal-engine` | Service name in JSON responses and logs |
| `SERVICE_EXECUTION_ENGINE` | `execution-engine` | Service name in JSON responses and logs |
| `SERVICE_POLYMARKET_WS` | `polymarket-websocket` | Service name in JSON responses and logs |

---

## How It Flows

```
crates/common/src/version.rs   (source of truth)
        │
        ├──► Rust services (compile-time)
        │      signal-engine:    use btc_common::version::SIGNAL_VERSION
        │      execution-engine: use btc_common::version::SIGNAL_VERSION
        │
        ├──► build.rs → version.json  (auto-generated at repo root)
        │      Written on every `cargo build` of any crate that depends
        │      on btc-common. Only re-written when content changes.
        │
        └──► Python redeem-positions service
               Reads version.json on startup.
```

---

## How to Bump the Version

Two files need editing, both in the same crate:

### 1. `crates/common/src/version.rs`

```rust
pub const SIGNAL_VERSION: &str = "v12";           // ← bump here
pub const SIGNAL_METHOD: &str = "drift_estimator_v12_best_candidate";
```

### 2. `crates/common/build.rs`

```rust
let signal_version = "v12";                        // ← match here
let signal_method = "drift_estimator_v12_best_candidate";
```

> `build.rs` duplicates the strings because it runs at build time before the
> library is compiled — it can't import the constants it's about to build.

### 3. Build any dependent crate

```bash
cargo build -p signal-engine --release
cargo build -p execution-engine --release
```

This will:
- Compile the new constants into both Rust binaries
- Auto-write `version.json` at the repo root

### 4. Update execution engine test fixtures

The execution engine has a guard test (`test_fixtures_match_current_version`)
that asserts the current `SIGNAL_VERSION` matches the version strings
hardcoded in test JSON fixtures. If you bumped the version, these tests will
**fail** until you update the raw JSON strings in:

- `crates/execution-engine/src/models.rs` — `ENTRY_JSON` constant and assertions
- `crates/execution-engine/src/position.rs` — test helper `make_entry_signal` and JSON fixtures

Search for the old version string and replace:
```bash
grep -rn '"<old_version>"' crates/execution-engine/src/ --include="*.rs"
```

### 5. Restart services

```bash
cargo build -p signal-engine -p execution-engine --release
```

Python picks up `version.json` on restart. Rust services use the new binary.

### 6. Verify

```bash
curl -s http://127.0.0.1:8003/health | jq .version
# → "v12"
```

---

## LLM Instructions

When asked to bump or change the signal version:

1. Edit **`crates/common/src/version.rs`** — update `SIGNAL_VERSION` and `SIGNAL_METHOD`
2. Edit **`crates/common/build.rs`** — update the matching string literals (`signal_version`, `signal_method`)
3. Search for the **old version string** in `crates/execution-engine/src/` and replace all occurrences in test fixtures
4. Run `cargo test` in both `signal-engine/` and `execution-engine/` to verify
5. Run `cargo build --release` in both `signal-engine/` and `execution-engine/`
6. Verify `version.json` at the repo root was updated
7. Restart any local services that use the changed binaries
8. Confirm via `/health` endpoints

**Do NOT** add version strings anywhere else. If a new service needs the
version, add `btc-common = { path = "../common" }` to its `Cargo.toml`
and import `btc_common::version::SIGNAL_VERSION`. For Python services, read
`version.json`.
