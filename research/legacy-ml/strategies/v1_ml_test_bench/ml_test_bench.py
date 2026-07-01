import sqlite3
import pandas as pd
import numpy as np
import os
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import TensorDataset, DataLoader
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import accuracy_score, classification_report
import statsmodels.api as sm
import xgboost as xgb
import matplotlib.pyplot as plt
import seaborn as sns

# --- Step 1: Data Loading ---
def load_data(db_path='polymarket_btc_data.db'):
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database file {db_path} not found. Please run fetch_db.py first.")
    
    conn = sqlite3.connect(db_path)
    query = """
    SELECT 
        candle_start, 
        open_price, 
        high_price, 
        low_price, 
        close_price, 
        volume, 
        quote_volume, 
        trade_count 
    FROM binance_candles_15m 
    ORDER BY candle_start ASC
    """
    df = pd.read_sql_query(query, conn)
    conn.close()

    if len(df) < 50:
        print(f"Warning: Table binance_candles_15m has only {len(df)} rows. Trying binance_candles_1m for better testing...")
        conn = sqlite3.connect(db_path)
        query = query.replace('binance_candles_15m', 'binance_candles_1m')
        df = pd.read_sql_query(query, conn)
        conn.close()

    if len(df) == 0:
        raise ValueError("No data found in binance_candles_15m or binance_candles_1m")

    # Convert timestamp to datetime (assuming Unix milliseconds)
    df['candle_start'] = pd.to_datetime(df['candle_start'], unit='ms')
    df.sort_values('candle_start', inplace=True)
    df.set_index('candle_start', inplace=True)
    
    print(f"Loaded {len(df)} rows from {df.index.min()} to {df.index.max()}")
    return df

# --- Step 2: Target Definition ---
def define_target(df):
    df = df.copy()
    # Create target: 1 if next close > current close, else 0
    df['next_close'] = df['close_price'].shift(-1)
    df['target'] = (df['next_close'] > df['close_price']).astype(int)

    # Drop the last row (no future data) and the helper column
    df.dropna(subset=['target'], inplace=True)
    df.drop(columns=['next_close'], inplace=True)

    print("Target distribution:")
    print(df['target'].value_counts(normalize=True))
    return df

# --- Step 3: Feature Engineering ---
def calculate_sma(series, window):
    return series.rolling(window=window).mean()

def calculate_ema(series, window):
    return series.ewm(span=window, adjust=False).mean()

def calculate_rsi(series, window=14):
    delta = series.diff(1)
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_macd(series, fast=12, slow=26, signal=9):
    ema_fast = calculate_ema(series, fast)
    ema_slow = calculate_ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = calculate_ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def calculate_bollinger(series, window=20, std_dev=2):
    sma = calculate_sma(series, window)
    std = series.rolling(window=window).std()
    upper = sma + (std * std_dev)
    lower = sma - (std * std_dev)
    return upper, lower

def add_features(df):
    df = df.copy()
    # Check if we have enough rows for default windows
    n_rows = len(df)
    sma_win = min(20, n_rows // 2) if n_rows > 4 else 2
    ema_win = min(12, n_rows // 2) if n_rows > 4 else 2
    rsi_win = min(14, n_rows // 2) if n_rows > 4 else 2
    
    print(f"Using windows: SMA={sma_win}, EMA={ema_win}, RSI={rsi_win}")

    df['sma_20'] = calculate_sma(df['close_price'], sma_win)
    df['ema_12'] = calculate_ema(df['close_price'], ema_win)
    df['rsi_14'] = calculate_rsi(df['close_price'], rsi_win)
    macd_line, signal_line, _ = calculate_macd(df['close_price']) # Uses default 12, 26, 9
    df['macd_line'] = macd_line
    df['macd_signal'] = signal_line
    df['bb_upper'], df['bb_lower'] = calculate_bollinger(df['close_price'], window=min(20, n_rows // 2) if n_rows > 4 else 2)

    df['price_change'] = df['close_price'].pct_change()
    df['volume_change'] = df['volume'].pct_change()
    df['high_low_range'] = (df['high_price'] - df['low_price']) / df['open_price']

    # Drop rows with NaN from indicators
    df.dropna(inplace=True)
    print(f"Rows remaining after feature engineering: {len(df)}")
    return df

# --- Step 4: Baseline Logic Regression ---
def run_logistic_regression(X_train, X_test, y_train, y_test):
    print("\n--- Running Logistic Regression ---")
    X_train_const = sm.add_constant(X_train)
    X_test_const = sm.add_constant(X_test)

    model = sm.Logit(y_train, X_train_const).fit(disp=0)
    y_pred_prob = model.predict(X_test_const)
    y_pred = (y_pred_prob > 0.5).astype(int)
    
    accuracy = accuracy_score(y_test, y_pred)
    print(f"Accuracy: {accuracy:.4f}")
    print(classification_report(y_test, y_pred))
    # print(model.summary()) # Can be very long
    return model

# --- Step 5: XGBoost ---
def run_xgboost(X_train, X_test, y_train, y_test):
    print("\n--- Running XGBoost ---")
    xgb_model = xgb.XGBClassifier(objective='binary:logistic', eval_metric='logloss')
    xgb_model.fit(X_train, y_train)

    y_pred = xgb_model.predict(X_test)
    accuracy = accuracy_score(y_test, y_pred)
    print(f"XGBoost Accuracy: {accuracy:.4f}")
    print(classification_report(y_test, y_pred))
    
    # Feature importance
    importances = pd.Series(xgb_model.feature_importances_, index=X_train.columns).sort_values(ascending=False)
    print("Feature Importances:")
    print(importances.head(10))
    return xgb_model

# --- Step 6: PyTorch MLP ---
class MLP(nn.Module):
    def __init__(self, input_size):
        super(MLP, self).__init__()
        self.network = nn.Sequential(
            nn.Linear(input_size, 64),
            nn.ReLU(),
            nn.Linear(64, 32),
            nn.ReLU(),
            nn.Linear(32, 1),
            nn.Sigmoid()
        )

    def forward(self, x):
        return self.network(x)

def run_pytorch_mlp(X_train, X_test, y_train, y_test):
    print("\n--- Running PyTorch MLP ---")
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)

    train_dataset = TensorDataset(torch.tensor(X_train_scaled, dtype=torch.float32), 
                                torch.tensor(y_train.values, dtype=torch.float32))
    train_loader = DataLoader(train_dataset, batch_size=32, shuffle=False)

    model = MLP(X_train.shape[1])
    criterion = nn.BCELoss()
    optimizer = optim.Adam(model.parameters(), lr=0.001)

    model.train()
    for epoch in range(20):
        epoch_loss = 0
        for data, target in train_loader:
            optimizer.zero_grad()
            output = model(data).squeeze()
            loss = criterion(output, target)
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item()
        if (epoch + 1) % 5 == 0:
            print(f"Epoch {epoch+1}, Avg Loss: {epoch_loss/len(train_loader):.4f}")

    model.eval()
    with torch.no_grad():
        y_pred_prob = model(torch.tensor(X_test_scaled, dtype=torch.float32)).squeeze().numpy()
        y_pred = (y_pred_prob > 0.5).astype(int)
    
    accuracy = accuracy_score(y_test, y_pred)
    print(f"PyTorch MLP Accuracy: {accuracy:.4f}")
    print(classification_report(y_test, y_pred))
    return model

# --- Main Execution ---
def main():
    try:
        df = load_data()
        df = define_target(df)
        df = add_features(df)

        features = ['sma_20', 'ema_12', 'rsi_14', 'macd_line', 'macd_signal', 'bb_upper', 'bb_lower', 
                    'price_change', 'volume_change', 'high_low_range', 'volume', 'quote_volume', 'trade_count']
        
        X = df[features]
        y = df['target']

        # Chronological split: 80% train, 20% test
        split_idx = int(len(df) * 0.8)
        X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
        y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]

        run_logistic_regression(X_train, X_test, y_train, y_test)
        run_xgboost(X_train, X_test, y_train, y_test)
        run_pytorch_mlp(X_train, X_test, y_train, y_test)

    except Exception as e:
        print(f"An error occurred during binary classification pipeline: {e}")

if __name__ == "__main__":
    main()
