"""Stage-2 work must not move a single stage-1 job name or command.

The discovery name is the resume/SKIP key of every stage-1 checkpoint (tools/run_options_matrix.py
discovery_name). This pins the names and commands of the -st1fix-shaped campaign, computed
machine-independently: the launcher is passed as the entry-point name "ba2-test" (so the digest
reads this checkout's _EXPERT_OPT through _CHECKOUT_LAUNCHER), os.path.abspath is the identity, and
the store env keys are cleared.

PINNED was filled ONCE from the baseline commit (plan/option-stage2 @ f9e537c4, i.e. dev 3b3465c8
plus plan docs) and must never be edited afterwards: a diff here means stage-2 work moved a stage-1
job identity, which would orphan (or wrongly SKIP) a running stage-1 checkpoint.
"""
import importlib.util
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("rom_pin", ROOT / "tools/run_options_matrix.py")
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)

_ENV = ("BA2_HOME", "BACKTEST_OPTIONS_STORE", "BACKTEST_OPTIONS_PARQUET_ROOT",
        "BACKTEST_OPTIONS_RISK_FREE_RATE", "TASTYTRADE_OPTIONS_HISTORY_FLOOR")

_ARGV = ["--profile", "discovery", "--screener-gate-store", "STORE", "--max-stock-price", "0",
         "--option-size-within-fill-volume", "--experts", "DeterministicScorer",
         "--market-condition-profile", "ohlcv-v1,ta-structure-v1",
         "--market-condition-manifest", "ohlcv-v1=aaaa,ta-structure-v1=bbbb",
         "--fitness", "option_car_target_soft30", "--name-suffix=-st1fix"]

#: Filled ONCE from the baseline commit and never edited afterwards.
PINNED: dict = {
    'optm-DeterministicScorer-O_LC-spow0922-st1fix-d9053ae2cb6ea':
        'ba2-test optimize --expert DeterministicScorer --universe AAPL,MSFT,KO '
        '--strategy O_LC --start 2020-01-01 --end 2025-12-31 --interval 1d '
        '--population 200 --generations 60 --initial-capital 20000.0 --run-schedule daily '
        '--name optm-DeterministicScorer-O_LC-spow0922-st1fix-d9053ae2cb6ea --parallel 4 '
        '--seed 42 --elitism-percent 10.0 --options-store thetadata '
        '--option-spread-model pow-2026-09-22 --screener-gate-store STORE '
        '--max-stock-price 0.0 --market-condition-profile ohlcv-v1,ta-structure-v1 '
        '--market-condition-manifest ohlcv-v1=aaaa,ta-structure-v1=bbbb '
        '--fitness option_car_target_soft30 --early-stop 8 --mutation-prob 0.3 '
        '--profit-cap-pct 2000.0 --profit-share-cap-pct 25.0 '
        '--option-size-within-fill-volume',
    'optm-DeterministicScorer-O_LP-spow0922-st1fix-d9a350b6ad336':
        'ba2-test optimize --expert DeterministicScorer --universe AAPL,MSFT,KO '
        '--strategy O_LP --start 2020-01-01 --end 2025-12-31 --interval 1d '
        '--population 200 --generations 60 --initial-capital 20000.0 --run-schedule daily '
        '--name optm-DeterministicScorer-O_LP-spow0922-st1fix-d9a350b6ad336 --parallel 4 '
        '--seed 42 --elitism-percent 10.0 --options-store thetadata '
        '--option-spread-model pow-2026-09-22 --screener-gate-store STORE '
        '--max-stock-price 0.0 --market-condition-profile ohlcv-v1,ta-structure-v1 '
        '--market-condition-manifest ohlcv-v1=aaaa,ta-structure-v1=bbbb '
        '--fitness option_car_target_soft30 --early-stop 8 --mutation-prob 0.3 '
        '--profit-cap-pct 2000.0 --profit-share-cap-pct 25.0 '
        '--option-size-within-fill-volume',
    'optm-DeterministicScorer-O_VERT-spow0922-st1fix-d7692214c1372':
        'ba2-test optimize --expert DeterministicScorer --universe AAPL,MSFT,KO '
        '--strategy O_VERT --start 2020-01-01 --end 2025-12-31 --interval 1d '
        '--population 200 --generations 60 --initial-capital 20000.0 --run-schedule daily '
        '--name optm-DeterministicScorer-O_VERT-spow0922-st1fix-d7692214c1372 '
        '--parallel 4 --seed 42 --elitism-percent 10.0 --options-store thetadata '
        '--option-spread-model pow-2026-09-22 --screener-gate-store STORE '
        '--max-stock-price 0.0 --market-condition-profile ohlcv-v1,ta-structure-v1 '
        '--market-condition-manifest ohlcv-v1=aaaa,ta-structure-v1=bbbb '
        '--fitness option_car_target_soft30 --early-stop 8 --mutation-prob 0.3 '
        '--profit-cap-pct 2000.0 --profit-share-cap-pct 25.0 '
        '--option-size-within-fill-volume',
    'optm-DeterministicScorer-O_BULLCS-spow0922-st1fix-d86e813987bc9':
        'ba2-test optimize --expert DeterministicScorer --universe AAPL,MSFT,KO '
        '--strategy O_BULLCS --start 2020-01-01 --end 2025-12-31 --interval 1d '
        '--population 200 --generations 60 --initial-capital 20000.0 --run-schedule daily '
        '--name optm-DeterministicScorer-O_BULLCS-spow0922-st1fix-d86e813987bc9 '
        '--parallel 4 --seed 42 --elitism-percent 10.0 --options-store thetadata '
        '--option-spread-model pow-2026-09-22 --screener-gate-store STORE '
        '--max-stock-price 0.0 --market-condition-profile ohlcv-v1,ta-structure-v1 '
        '--market-condition-manifest ohlcv-v1=aaaa,ta-structure-v1=bbbb '
        '--fitness option_car_target_soft30 --early-stop 8 --mutation-prob 0.3 '
        '--profit-cap-pct 2000.0 --profit-share-cap-pct 25.0 '
        '--option-size-within-fill-volume',
    'optm-DeterministicScorer-O_BULLPS-spow0922-st1fix-dd759ddacbd6f':
        'ba2-test optimize --expert DeterministicScorer --universe AAPL,MSFT,KO '
        '--strategy O_BULLPS --start 2020-01-01 --end 2025-12-31 --interval 1d '
        '--population 200 --generations 60 --initial-capital 20000.0 --run-schedule daily '
        '--name optm-DeterministicScorer-O_BULLPS-spow0922-st1fix-dd759ddacbd6f '
        '--parallel 4 --seed 42 --elitism-percent 10.0 --options-store thetadata '
        '--option-spread-model pow-2026-09-22 --screener-gate-store STORE '
        '--max-stock-price 0.0 --market-condition-profile ohlcv-v1,ta-structure-v1 '
        '--market-condition-manifest ohlcv-v1=aaaa,ta-structure-v1=bbbb '
        '--fitness option_car_target_soft30 --early-stop 8 --mutation-prob 0.3 '
        '--profit-cap-pct 2000.0 --profit-share-cap-pct 25.0 '
        '--option-size-within-fill-volume',
    'optm-DeterministicScorer-O_BEARCS-spow0922-st1fix-d6f6fc8b24fd1':
        'ba2-test optimize --expert DeterministicScorer --universe AAPL,MSFT,KO '
        '--strategy O_BEARCS --start 2020-01-01 --end 2025-12-31 --interval 1d '
        '--population 200 --generations 60 --initial-capital 20000.0 --run-schedule daily '
        '--name optm-DeterministicScorer-O_BEARCS-spow0922-st1fix-d6f6fc8b24fd1 '
        '--parallel 4 --seed 42 --elitism-percent 10.0 --options-store thetadata '
        '--option-spread-model pow-2026-09-22 --screener-gate-store STORE '
        '--max-stock-price 0.0 --market-condition-profile ohlcv-v1,ta-structure-v1 '
        '--market-condition-manifest ohlcv-v1=aaaa,ta-structure-v1=bbbb '
        '--fitness option_car_target_soft30 --early-stop 8 --mutation-prob 0.3 '
        '--profit-cap-pct 2000.0 --profit-share-cap-pct 25.0 '
        '--option-size-within-fill-volume',
    'optm-DeterministicScorer-O_BF-spow0922-st1fix-df4b675ac93c7':
        'ba2-test optimize --expert DeterministicScorer --universe AAPL,MSFT,KO '
        '--strategy O_BF --start 2020-01-01 --end 2025-12-31 --interval 1d '
        '--population 200 --generations 60 --initial-capital 20000.0 --run-schedule daily '
        '--name optm-DeterministicScorer-O_BF-spow0922-st1fix-df4b675ac93c7 --parallel 4 '
        '--seed 42 --elitism-percent 10.0 --options-store thetadata '
        '--option-spread-model pow-2026-09-22 --screener-gate-store STORE '
        '--max-stock-price 0.0 --market-condition-profile ohlcv-v1,ta-structure-v1 '
        '--market-condition-manifest ohlcv-v1=aaaa,ta-structure-v1=bbbb '
        '--fitness option_car_target_soft30 --early-stop 8 --mutation-prob 0.3 '
        '--profit-cap-pct 2000.0 --profit-share-cap-pct 25.0 '
        '--option-size-within-fill-volume',
    'optm-DeterministicScorer-O_IC-spow0922-st1fix-dd60bda6fb77b':
        'ba2-test optimize --expert DeterministicScorer --universe AAPL,MSFT,KO '
        '--strategy O_IC --start 2020-01-01 --end 2025-12-31 --interval 1d '
        '--population 200 --generations 60 --initial-capital 20000.0 --run-schedule daily '
        '--name optm-DeterministicScorer-O_IC-spow0922-st1fix-dd60bda6fb77b --parallel 4 '
        '--seed 42 --elitism-percent 10.0 --options-store thetadata '
        '--option-spread-model pow-2026-09-22 --screener-gate-store STORE '
        '--max-stock-price 0.0 --market-condition-profile ohlcv-v1,ta-structure-v1 '
        '--market-condition-manifest ohlcv-v1=aaaa,ta-structure-v1=bbbb '
        '--fitness option_car_target_soft30 --early-stop 8 --mutation-prob 0.3 '
        '--profit-cap-pct 2000.0 --profit-share-cap-pct 25.0 '
        '--option-size-within-fill-volume',
    'optm-DeterministicScorer-O_JL-spow0922-st1fix-d03ee8a9c684a':
        'ba2-test optimize --expert DeterministicScorer --universe AAPL,MSFT,KO '
        '--strategy O_JL --start 2020-01-01 --end 2025-12-31 --interval 1d '
        '--population 200 --generations 60 --initial-capital 20000.0 --run-schedule daily '
        '--name optm-DeterministicScorer-O_JL-spow0922-st1fix-d03ee8a9c684a --parallel 4 '
        '--seed 42 --elitism-percent 10.0 --options-store thetadata '
        '--option-spread-model pow-2026-09-22 --screener-gate-store STORE '
        '--max-stock-price 0.0 --market-condition-profile ohlcv-v1,ta-structure-v1 '
        '--market-condition-manifest ohlcv-v1=aaaa,ta-structure-v1=bbbb '
        '--fitness option_car_target_soft30 --early-stop 8 --mutation-prob 0.3 '
        '--profit-cap-pct 2000.0 --profit-share-cap-pct 25.0 '
        '--option-size-within-fill-volume',
    'optm-DeterministicScorer-O_RS-spow0922-st1fix-d78c0b7e73d15':
        'ba2-test optimize --expert DeterministicScorer --universe AAPL,MSFT,KO '
        '--strategy O_RS --start 2020-01-01 --end 2025-12-31 --interval 1d '
        '--population 200 --generations 60 --initial-capital 20000.0 --run-schedule daily '
        '--name optm-DeterministicScorer-O_RS-spow0922-st1fix-d78c0b7e73d15 --parallel 4 '
        '--seed 42 --elitism-percent 10.0 --options-store thetadata '
        '--option-spread-model pow-2026-09-22 --screener-gate-store STORE '
        '--max-stock-price 0.0 --market-condition-profile ohlcv-v1,ta-structure-v1 '
        '--market-condition-manifest ohlcv-v1=aaaa,ta-structure-v1=bbbb '
        '--fitness option_car_target_soft30 --early-stop 8 --mutation-prob 0.3 '
        '--profit-cap-pct 2000.0 --profit-share-cap-pct 25.0 '
        '--option-size-within-fill-volume',
    'optm-DeterministicScorer-O_CSP-spow0922-st1fix-dbae8d85abcea':
        'ba2-test optimize --expert DeterministicScorer --universe AAPL,MSFT,KO '
        '--strategy O_CSP --start 2020-01-01 --end 2025-12-31 --interval 1d '
        '--population 200 --generations 60 --initial-capital 20000.0 --run-schedule daily '
        '--name optm-DeterministicScorer-O_CSP-spow0922-st1fix-dbae8d85abcea --parallel 4 '
        '--seed 42 --elitism-percent 10.0 --options-store thetadata '
        '--option-spread-model pow-2026-09-22 --screener-gate-store STORE '
        '--max-stock-price 0.0 --market-condition-profile ohlcv-v1,ta-structure-v1 '
        '--market-condition-manifest ohlcv-v1=aaaa,ta-structure-v1=bbbb '
        '--fitness option_car_target_soft30 --early-stop 8 --mutation-prob 0.3 '
        '--profit-cap-pct 2000.0 --profit-share-cap-pct 25.0 '
        '--option-size-within-fill-volume',
    'optm-DeterministicScorer-O_STRD-spow0922-st1fix-deb0b75c249df':
        'ba2-test optimize --expert DeterministicScorer --universe AAPL,MSFT,KO '
        '--strategy O_STRD --start 2020-01-01 --end 2025-12-31 --interval 1d '
        '--population 200 --generations 60 --initial-capital 20000.0 --run-schedule daily '
        '--name optm-DeterministicScorer-O_STRD-spow0922-st1fix-deb0b75c249df '
        '--parallel 4 --seed 42 --elitism-percent 10.0 --options-store thetadata '
        '--option-spread-model pow-2026-09-22 --screener-gate-store STORE '
        '--max-stock-price 0.0 --market-condition-profile ohlcv-v1,ta-structure-v1 '
        '--market-condition-manifest ohlcv-v1=aaaa,ta-structure-v1=bbbb '
        '--fitness option_car_target_soft30 --early-stop 8 --mutation-prob 0.3 '
        '--profit-cap-pct 2000.0 --profit-share-cap-pct 25.0 '
        '--option-size-within-fill-volume',
    'optm-DeterministicScorer-O_STRG-spow0922-st1fix-d321d2b19a406':
        'ba2-test optimize --expert DeterministicScorer --universe AAPL,MSFT,KO '
        '--strategy O_STRG --start 2020-01-01 --end 2025-12-31 --interval 1d '
        '--population 200 --generations 60 --initial-capital 20000.0 --run-schedule daily '
        '--name optm-DeterministicScorer-O_STRG-spow0922-st1fix-d321d2b19a406 '
        '--parallel 4 --seed 42 --elitism-percent 10.0 --options-store thetadata '
        '--option-spread-model pow-2026-09-22 --screener-gate-store STORE '
        '--max-stock-price 0.0 --market-condition-profile ohlcv-v1,ta-structure-v1 '
        '--market-condition-manifest ohlcv-v1=aaaa,ta-structure-v1=bbbb '
        '--fitness option_car_target_soft30 --early-stop 8 --mutation-prob 0.3 '
        '--profit-cap-pct 2000.0 --profit-share-cap-pct 25.0 '
        '--option-size-within-fill-volume',
    'optm-DeterministicScorer-O_CC-spow0922-st1fix-de113aeb6f6cf':
        'ba2-test optimize --expert DeterministicScorer --universe AAPL,MSFT,KO '
        '--strategy O_CC --start 2020-01-01 --end 2025-12-31 --interval 1d '
        '--population 200 --generations 60 --initial-capital 20000.0 --run-schedule daily '
        '--name optm-DeterministicScorer-O_CC-spow0922-st1fix-de113aeb6f6cf --parallel 4 '
        '--seed 42 --elitism-percent 10.0 --options-store thetadata '
        '--option-spread-model pow-2026-09-22 --screener-gate-store STORE '
        '--max-stock-price 0.0 --market-condition-profile ohlcv-v1,ta-structure-v1 '
        '--market-condition-manifest ohlcv-v1=aaaa,ta-structure-v1=bbbb '
        '--fitness option_car_target_soft30 --early-stop 8 --mutation-prob 0.3 '
        '--profit-cap-pct 2000.0 --profit-share-cap-pct 25.0 '
        '--option-size-within-fill-volume',
    'optm-DeterministicScorer-O_PP-spow0922-st1fix-d4aa7a04b3301':
        'ba2-test optimize --expert DeterministicScorer --universe AAPL,MSFT,KO '
        '--strategy O_PP --start 2020-01-01 --end 2025-12-31 --interval 1d '
        '--population 200 --generations 60 --initial-capital 20000.0 --run-schedule daily '
        '--name optm-DeterministicScorer-O_PP-spow0922-st1fix-d4aa7a04b3301 --parallel 4 '
        '--seed 42 --elitism-percent 10.0 --options-store thetadata '
        '--option-spread-model pow-2026-09-22 --screener-gate-store STORE '
        '--max-stock-price 0.0 --market-condition-profile ohlcv-v1,ta-structure-v1 '
        '--market-condition-manifest ohlcv-v1=aaaa,ta-structure-v1=bbbb '
        '--fitness option_car_target_soft30 --early-stop 8 --mutation-prob 0.3 '
        '--profit-cap-pct 2000.0 --profit-share-cap-pct 25.0 '
        '--option-size-within-fill-volume',
    'optm-DeterministicScorer-O_WHEEL-spow0922-st1fix-db7b7f2631351':
        'ba2-test optimize --expert DeterministicScorer --universe AAPL,MSFT,KO '
        '--strategy O_WHEEL --start 2020-01-01 --end 2025-12-31 --interval 1d '
        '--population 200 --generations 60 --initial-capital 20000.0 --run-schedule daily '
        '--name optm-DeterministicScorer-O_WHEEL-spow0922-st1fix-db7b7f2631351 '
        '--parallel 4 --seed 42 --elitism-percent 10.0 --options-store thetadata '
        '--option-spread-model pow-2026-09-22 --screener-gate-store STORE '
        '--max-stock-price 0.0 --market-condition-profile ohlcv-v1,ta-structure-v1 '
        '--market-condition-manifest ohlcv-v1=aaaa,ta-structure-v1=bbbb '
        '--fitness option_car_target_soft30 --early-stop 8 --mutation-prob 0.3 '
        '--profit-cap-pct 2000.0 --profit-share-cap-pct 25.0 '
        '--option-size-within-fill-volume',
}


def _jobs(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(M.os.path, "abspath", lambda p: p)
    args = M.resolve_args(M.build_parser(), _ARGV)
    uni = "AAPL,MSFT,KO"
    out = {}
    for name, expert, strat, mode in M.planned_jobs(args, "ba2-test", ["DeterministicScorer"],
                                                    args.strategies.split(","), uni):
        out[name] = shlex.join(M.build_cmd(args, "ba2-test", name, expert, strat, uni, mode))
    return out


def test_stage1_discovery_names_and_commands_are_unchanged(monkeypatch):
    got = _jobs(monkeypatch)
    assert len(PINNED) == 16
    assert got == PINNED, "a stage-1 job name or command moved -- stage 2 must not touch stage 1"
