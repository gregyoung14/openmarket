# V14 Quant Paper Strategy

## Evaluation of "How I'd Become a Quant" vs Older Strategies (e.g., V13)

Based on the provided article, there are several deeply meaningful upgrades we can extract for V14 when compared to our current approaches like V13.

### 1. Replacing Normal Distributions with Fat-Tailed (Student-t) Distributions
**Current (V13):** The Bayesian Posterior module computes likelihoods using a normal distribution: `ll_up = -(r_t - expected_move).powi(2) / (2.0 * local_vol.powi(2))`.
**The New Edge:** The article emphasizes that financial returns invariably fail normality tests because they exhibit "fat tails." It recommends fitting a Student-t distribution via Maximum Likelihood Estimation (MLE). If BTC exhibits fat tails, our normal distribution assumption is mispricing the likelihood of sudden spikes, either keeping us out of good trades or keeping us in bad ones.

### 2. Black-Scholes Greeks vs. Linear Time Decay
**Current (V13):** Time decay (Theta equivalent) is modeled as a simple linear penalty (`MAX_PENALTY * decay_progress`) at the end of the market window.
**The New Edge:** Polymarket YES/NO tokens are Binary Options on BTC price. The article focuses heavily on Stochastic Calculus, Itô's Lemma, and Option Greeks. Instead of a linear penalty, we should implement a rigorous **Theta curve** (since time decay of binary options is nonlinear and accelerates greatly near expiry) and adjust our entry thresholds dynamically using **Vega** (sensitivity to real-time volatility) rather than static offsets.

### 3. Rigorous Statistical Backtesting (BS Testing)
**Current (V13):** Backtesting generally assesses total PnL or Win Rate (e.g., "71% win rate") to validate the system.
**The New Edge:** Testing multiple variations of a strategy leads to the "multiple comparisons problem" where some pass purely by chance. The article introduces using **Bonferroni corrections** or **Benjamini-Hochberg** false discovery rate controls to weed out "NOISY BS". Implementing factor regressions with **Newey-West standard errors** (to correct for autocorrelation) on our backtest trade logs would objectively prove if our edge is real or just systemic noise disguised as alpha.

### 4. LMSR directly mapped to Softmax
**The New Edge:** The article formally connects Polymarket's LMSR to the softmax classifier used in neural networks. This validates using cross-entropy and MLE when training parameter weights (like `W_DRIFT`, `W_OFI_ACCEL`) instead of merely guessing or grid-searching them, establishing a mathematically sound foundation for optimizing our signal weights.

### Summary
V14 should introduce:
1. **Student-t Likelihoods** in the Bayesian Updater instead of Normal.
2. **Options Greeks (Theta & Vega)** for dynamic time-decay and volatility scaling.
3. **P-value & Factor Regression** tests directly inside the Backtester suite to invalidate noisy edges.
