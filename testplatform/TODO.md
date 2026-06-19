# TODO - Exploration Topics

## Quality Diversity & MapElites

### Overview
Quality Diversity (QD) algorithms aim to find a diverse collection of high-performing solutions rather than a single optimal solution. This approach is particularly valuable for neural network training where multiple effective architectures/weights may exist.

### MapElites
- [ ] Investigate MapElites algorithm for parallel network training
- [ ] MapElites divides the solution space into a grid of niches based on behavioral descriptors
- [ ] Each cell stores the best-performing solution for that behavior
- [ ] Enables exploration of diverse solutions simultaneously

### Key Areas to Explore

1. **MapElites with Neural Networks**
   - [ ] How to define behavioral descriptors for trading networks
   - [ ] Grid resolution and dimensionality trade-offs
   - [ ] Mutation operators for network weights

2. **DCRL (Differentiable QD with RL)**
   - [ ] Research DCRL enhancements that combine MapElites with Reinforcement Learning
   - [ ] Evaluate if DCRL achieves better results than vanilla MapElites
   - [ ] Implementation complexity vs performance gains

3. **Shinka Framework**
   - [ ] Check if Shinka already uses MapElites by default
   - [ ] Document Shinka's QD implementation details
   - [ ] Identify any configuration options for QD parameters
   - [ ] Compare Shinka's approach with other QD libraries

### References to Research
- [ ] Original MapElites paper: Mouret & Clune (2015)
- [ ] DCRL papers and implementations
- [ ] Shinka documentation and source code

### Implementation Notes
- Consider behavioral descriptors: risk metrics, drawdown patterns, trade frequency
- GPU parallelization for fitness evaluation
- Storage strategy for elite archive


Check price that is used for target predictions

## Backtest Strategy Engine
- [ ] **Multiple open trades**: Support `max_open_trades` setting to allow opening new positions while existing ones are active. Currently limited to 1 position at a time.
- [ ] **Trade open delay**: Configurable minimum bars/time between trades to avoid overtrading.
- [ ] **Per-bar evaluation for multi-trade**: When multiple trades enabled, condition evaluation on every execution bar is needed (currently optimized to skip non-prediction bars in single-trade mode).
- [ ] **Backtest optimization engine**: Backend endpoint that sweeps TP/SL/strategy params in a single request (load model once, predict once, sweep params) instead of one backtest per combination.

## Performance
- [ ] **SQLite → PostgreSQL**: Large blob columns (equity_curve, drawdown_curve) cause page fragmentation. PostgreSQL with TOAST would handle better.
- [ ] **Backtest curve storage**: Store equity/drawdown curves in files or compress them instead of raw JSON blobs in DB.
- [ ] **Uvicorn workers**: Test `--workers N` on Windows for API concurrency.

---

## TradingAgents integration of two arXiv methods (from BA2TradePlatform brainstorm, 2026-06)

Goal: bring two 2025 methods into our own TradingAgents / strategy stack. Parked here
(this is the ML/research platform); the live trade platform (BA2TradePlatform) focuses
on the deterministic factor strategies for now.

### Phase 0 — Backtester (prerequisite for BOTH papers)
- [ ] Build a backtest/replay harness: replay historical OHLCV/fundamentals → run an
      expert/strategy's logic → simulate fills + transaction costs → output metrics
      (returns, Sharpe, max drawdown, turnover). NOTE: this likely extends the existing
      "Backtest Strategy Engine" work above rather than starting from scratch.
- [ ] Without this there is no RL training environment and no evolution fitness function;
      also lets us validate FactorRanker factors before risking capital.

### Paper 1 — Adaptive Alpha Weighting with PPO (arXiv:2509.01393)
LLM generates N formulaic alphas → PPO learns a weight vector → composite alpha →
vol-scaled, quintile-thresholded position; reward = next-period P&L − turnover cost.
State = OHLCV + prev position + regime (20/100-MA crossover) + 63d annualized vol.
- [ ] Treat each TradingAgents analyst/researcher output (and/or formulaic alphas) as an
      "alpha" signal.
- [ ] Implement the composite → quintile → vol-scaled position machinery (deterministic).
- [ ] Ship robust deterministic weighting first (equal / rolling-IC / risk-parity over alphas).
- [ ] Add PPO weighting as an OPT-IN, trained on the Phase 0 backtester. Never deploy
      un-validated RL to live money.
- [ ] Local PDF: Downloads/2509.01393v1.pdf

### Paper 2 — QuantEvolve: multi-agent evolutionary strategy discovery (arXiv:2510.18569)
Hypothesis → generate → backtest → select loop with quality-diversity (MAP-Elites) over a
feature map of investor preferences (strategy type, risk, turnover, return profile).
Discovers a DIVERSE population of strategies, not one optimum. (Ties directly to the
MapElites work above.)
- [ ] Multi-agent loop proposing new factor formulas / expert configs.
- [ ] Use the Phase 0 backtester as the fitness function; keep a QD archive keyed on the
      feature map.
- [ ] Output = candidate factors for BA2TradePlatform's FactorRanker, or candidate experts
      for human review. Research tool only — never auto-deploys to live.
- [ ] Local PDF: Downloads/2510.18569v1.pdf

### Sequencing
Phase 0 backtester → Paper 1 alpha-weighting → Paper 2 evolver. Each independently useful.
