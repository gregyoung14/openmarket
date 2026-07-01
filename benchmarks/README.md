# Benchmarks

Benchmark categories:

| Benchmark | Metric |
|---|---|
| WebSocket ingest | messages/sec |
| Tick normalization | ns/event |
| Lag pairing | pairs/sec |
| Feature generation | windows/sec |
| Backtest | markets/sec |
| Inference | microseconds/prediction |
| Memory | peak RSS |
| CPU | average and p99 utilization |

Initial benchmark command targets:

```bash
cargo bench --workspace
cargo run -p v15_brier_calibration --release -- --db-path data/openmarket.db
```
