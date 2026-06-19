# Monorepo migration — the 5 BA2 repos are now one

As of this commit, the BA2 codebase lives in **one git repo** (this one, `bmigette/BA2TradePlatform`)
instead of 5 separate repos. The package boundaries are unchanged — each is still its own installable
package with its own `pyproject.toml` — they just live under `packages/` now:

```
BA2TradePlatform/                 (this repo)
  ba2_trade_platform/             live trade APP  (package: ba2trade-app)   ← root level
  testplatform/                   test/backtest APP (package: ba2test-app)  ← root level
                                    (backend/ + frontend/ + ba2test_launcher.py)
  packages/                       shared LIBRARIES (used by both apps)
    common/                       package: ba2trade-common   (ba2_common)
    providers/                    package: ba2trade-providers (ba2_providers)
    experts/                      package: ba2trade-experts   (ba2_experts)
  install.sh                      builds both venvs (editable: chain from packages/, apps from root + testplatform/)
```

The 4 former repos — **BA2TradeCommon, BA2TradeProviders, BA2TradeExperts, BA2TestPlatform** — are now
**frozen archives** on GitHub (their history is preserved there). Do NOT push to them anymore. All work
happens in this repo, on `dev`.

## How to migrate a machine (Mac / Windows / the other dev session)

1. **Clone just this repo** (or `git pull` if you already have it):
   ```
   git clone git@github.com:bmigette/BA2TradePlatform.git
   cd BA2TradePlatform
   ```
2. **Rebuild the venvs** (editable, from `packages/`):
   ```
   ./install.sh --editable            # both venvs;  add --ui for the experts[ui] extra
   # or: --test-only / --trade-only
   ```
   This recreates `~/ba2-venvs/{trade,test}` with the `common → providers → experts` chain installed
   editable from `packages/`, the trade app (`ba2-trade`) from the repo root, and the test app
   (`ba2-test`) from `testplatform/`. Windows: run under Git Bash / WSL, or replicate the
   `uv pip install -e packages/common packages/providers packages/experts testplatform` +
   `-e .` (root) steps.
3. **Verify**: `~/ba2-venvs/test/bin/ba2-test --help` and `~/ba2-venvs/trade/bin/python -c "import ba2_trade_platform"`.
4. **Delete the old sibling clones** (`BA2TradeCommon/`, `BA2TradeProviders/`, `BA2TradeExperts/`,
   `BA2TestPlatform/`) once the venvs point at `packages/` — otherwise editable installs may still
   resolve to the stale copies. Check: the venv's
   `…/site-packages/_editable_impl_ba2trade_*.pth` files should contain `…/BA2TradePlatform/packages/…`.

## Notes
- **A run already in flight is unaffected** — a live trade app or a `ba2-test optimize` already running has
  its modules loaded; the file move + editable re-point only changes `.pth` (no process restart).
- **On-disk data is unaffected** — caches/DBs/metric-store live under `~/Documents/ba2/...`, outside the repo.
- **Editable `.pth` staleness is the one gotcha** — if a venv still points at an old sibling dir, your edits
  land in the wrong place. Re-run `./install.sh --editable` (or the targeted `uv pip install -e packages/*`)
  on each machine, then remove the old clones.
