import os
import re
import subprocess
import numpy as np
from scipy.optimize import differential_evolution

def run_backtest(weights):
    w_drift, w_ofi, w_score, w_whipsaw = weights
    env = os.environ.copy()
    env["W_DRIFT"] = str(w_drift)
    env["W_OFI_ACCEL"] = str(w_ofi)
    env["W_SCOREBOARD"] = str(w_score)
    env["WHIPSAW_WEIGHT"] = str(w_whipsaw)

    # We use quiet run because cargo is noisy
    cmd = ["./target/release/v14_x_quant_paper", "--db-path", "../../data/polymarket_btc_data.db"]
    
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    
    if result.returncode != 0:
        return float('inf')

    text = result.stdout
    
    try:
        trades_match = re.search(r'\|\s*Total Trades\s*\|\s*(\d+)', text)
        trades = int(trades_match.group(1)) if trades_match else 0
        
        wr_match = re.search(r'\|\s*Win Rate\s*\|\s*([\d\.]+)%', text)
        wr = float(wr_match.group(1)) if wr_match else 0.0
        
        roi_match = re.search(r'\|\s*Total ROI\s*\|\s*([\-\d\.]+)%', text)
        roi = float(roi_match.group(1)) if roi_match else 0.0
        
        alpha_match = re.search(r'\|\s*Strategy Alpha[^\d\-]*([\-\d\.]+)%', text)
        alpha = float(alpha_match.group(1)) if alpha_match else 0.0
        
    except Exception as e:
        return float('inf')

    # Objective: Minimize negative ROI (Maximize ROI)
    # Applying a heavy penalty if it barely trades
    if trades < 50:
        return 1e6 - trades
        
    # Using negative ROI as the loss function
    objective = -roi
    print(f"Weights [{w_drift:7.3f}, {w_ofi:7.3f}, {w_score:7.3f}, {w_whipsaw:7.3f}] -> Trades: {trades:3d}, WR: {wr:5.1f}%, Alpha: {alpha:6.2f}%, ROI: {roi:9.1f}%")
    
    return objective

if __name__ == "__main__":
    print("Building Rust binary...")
    subprocess.run(["cargo", "build", "--release"], check=True)
    
    print("Starting Parallel Differential Evolution Optimization for V14 Quant Paper parameters...")
    
    bounds = [(0.0, 5.0), (0.0, 5.0), (0.0, 5.0), (-3.0, 0.0)]
    
    res = differential_evolution(
        run_backtest,
        bounds,
        maxiter=10, # Keep iteration low to just find a great local area practically
        popsize=5,  # 5*4 = 20 genes per generation
        workers=4,  # Limit workers to 4 to prevent Out-Of-Memory issues on 36GB RAM (M4 Max has too many cores for full parallelization with heavy RAM usage)
        disp=True
    )
    
    print("\n" + "="*50)
    print("OPTIMIZATION FINISHED")
    print("="*50)
    print(res)
    print("\nBest Parameters Found:")
    print(f"W_DRIFT        = {res.x[0]:.4f}")
    print(f"W_OFI_ACCEL    = {res.x[1]:.4f}")
    print(f"W_SCOREBOARD   = {res.x[2]:.4f}")
    print(f"WHIPSAW_WEIGHT = {res.x[3]:.4f}")
