# Utilities

Helper scripts for data management, analysis, and infrastructure.

## Structure

### /data_management
Scripts for fetching, checking, and maintaining the database.
- `fetch_db.py` - Fetches the latest database snapshot.
- `check_db.py` - Quick check of database integrity.
- `check_db_stats.py` - Detailed statistics of the database content.
- `save_models.py` - Utilities for saving/loading ML models.

### /analysis
Scripts for deep diving into market data and model performance.
- `inspect_markets.py` - Inspect specific market details.
- `inspect_trades.py` - Inspect raw trade data.
- `deep_inspect.py` - Advanced analysis of market microstructure.
- `ensemble_shap.py` - SHAP value analysis for feature importance.

### /infrastructure
Infrastructure and testing utilities.
- `check_s3_data.py` - verify S3 connectivity/data.
- `test_proxy.py` - Test proxy configuration.
