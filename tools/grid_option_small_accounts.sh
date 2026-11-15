#!/usr/bin/env bash
# Small-account option grid: stage-2 style jobs at $10k, $20k and $50k, run one after the other on the grid host.
# PREPARED 2026-11-11 (owner decisions 2026-11-11), NOT LAUNCHED: needs the owner's go.
#
#   bash tools/grid_option_small_accounts.sh            (detached: systemd-run --unit ba2-s3small ... bash <this file>)
#
# One job per step, each through tools/option_style_job.sh (the recipe is pinned there; host paths, worker name,
# digests are its environment variables):
#   1. $10k  long10k      s3L10   bundle_jobL10.json     --fitness option_long_dd40_50   redo of job L10 (opt 44)
#   2. $20k  premium_stop s3P20   bundle_jobPstop.json   (default fitness soft30)
#   3. $20k  mid_share    s3A20   bundle_jobAshare.json  (default fitness soft30)
#   4. $20k  long         s3C20   bundle_jobC.json       --fitness option_long           redo of job C (opt 43)
#   5. $10k  burst10k     s3B10   bundle_jobBurst.json   --fitness option_long_dd40_50   new (owner idea 2026-11-12)
#   6. $50k  wheel50k_ndx s3N50   bundle_jobNdx.json     (default fitness soft30)        new (owner 2026-11-12);
#            UFILE=tools/options_universe_nasdaq.txt SCREEN="--screen-preset ndx30"
#   7. $10k  cheap10k     s3W10   bundle_jobCheap.json   (default fitness soft30)        new (owner 2026-11-13);
#            SCREEN="--screen-preset cheap50" (the stage-2 list, large caps priced $10..$50 at each scan)
# EVERY job reads the ALPHA VANTAGE option store (owner 2026-11-12, "Use alpha for opt data"): OPTION_HISTORY_SOURCE=
# alphavantage gives each job `--options-store alphavantage --option-history-source alphavantage` and the name suffix
# -optalphavantage (optm-DeterministicScorer-O_STYLE-<style>-<suffix>-optalphavantage). A NEW RESULT FAMILY: not
# comparable with any ThetaData run (stage 1, stage 2, the 2026-11-05..09 small-account runs); the stage-1 seeds were
# optimised on ThetaData and are starting points only (docs/REPRODUCE-BACKTESTS.md 6.3).
# Differences from the 2026-11-05..08 stage-2 jobs (A, P, P50k, C, L10):
#   - the option store (above);
#   - the 1-contract floor is OFF (TEST >= 0187): those jobs still carried it;
#   - a capped run that gives back the whole cap is stopped and scored as wiped out (TEST >= 0200,
#     account_wiped_out_reason = equity_cap_drawdown): results before and after are not comparable;
#   - premium_stop (owner 2026-11-11): the premium-based loss exit is ALWAYS ON for WHEEL/CSP/RS/JL (level still
#     searched, 0.5x..2x the credit); the wheel entry is always on with a capital-share gene 30..60%; every other
#     member 10..60% (O_CC / O_PP included, enforced where their stock is sized: ba2_common 2026.11.00051);
#   - mid_share = mid + a 10..60% structure_share gene on every member;
#   - burst10k (owner idea 2026-11-12, added as job 5 the same day): O_LC > O_BULLCS at 7..25 DTE (never below 7),
#     3..5% per structure, take-profit 60..200% / loss exit 30..70% of the premium / time stop 3..7 days ALWAYS ON
#     (levels searched), at most 10 structures; the $10k flags of job 1;
#   - wheel50k_ndx (owner 2026-11-12, job 6): premium_stop reduced to WHEEL > CSP (same mandatory loss exit, same
#     always-on wheel entry and 30..60% wheel share, a 10..60% CSP share), $50k, the 30 largest names by market cap of
#     the 201 NASDAQ-listed large caps of tools/options_universe_nasdaq.txt (a Nasdaq-100 PROXY; caveats in its header);
#   - cheap10k (owner 2026-11-13, job 7): the wheel at $10k. WHEEL > CSP (wheel50k_ndx's members verbatim) > BULLPS
#     (premium_stop's) > LC (long10k's DTE, a 20..80% share), per-instrument cap 50..100 and deployment 50..100 so one
#     $50 put ($5,000 = 50% of $10k) always fits and two fit at the top; the stage-2 list under the FIXED screen
#     --screen-preset cheap50 (the xl floors + a $50 share-price CEILING evaluated at each weekly scan on the price
#     knowable then, the 50 largest by market cap that pass); the $10k flags without job 1's long-book fitness.
#
# PRE-LAUNCH CHECKLIST (do not skip):
#   1. $REPO is DETACHED at the dev commit that carries the points above (TEST >= 0200, ba2_common >= 00051) and
#      the fleet worker is on the same commit (the master's pre-flight syncs it; check GET /version).
#   2. No other master uses the fleet worker on another TEST version: the goal2027 grid must have ended or be on
#      this same commit, otherwise the worker re-syncs back and forth.
#   3. The Alpha Vantage download is COMPLETE for both universes, 2020-01-02..2025-12-31 (on 2026-11-12 the owner-PC
#      download of the 856-name owner universe was at symbol 247, about 12 more days at 62 calls/min). Jobs 1-5 read
#      tools/options_universe_stage2.txt (750 names, all in the owner universe). Job 6 reads
#      tools/options_universe_nasdaq.txt: 191 names are in the owner universe, the other ten (ADBE ADP ATVI CDNS CHTR
#      INTU ISRG REGN SNPS VRTX, tools/options_universe_nasdaq_av_extra.txt) are downloaded SEPARATELY into the same
#      store, with a share of the key's 75 calls/min while the main download runs (the budget file keeps the sum under
#      --key-limit 75):
#        python tools/download_av_option_chains.py --symbols-file tools/options_universe_nasdaq_av_extra.txt \
#            --start 2020-01-01 --db ~/Documents/ba2/test/dl_forecasting.db --calls-per-minute 13 \
#            --report av_options_nasdaq_extra_report.json
#      Check (read-only, no network; exit 0 = complete, 3 = sessions still owed, the names listed):
#        python tools/download_av_option_chains.py --symbols-file <universe file> --start 2020-01-02 --end 2025-12-31 \
#            --require-complete --db <the test DB>
#      This chain runs that check on the grid host before jobs 1-5, before job 6 and before job 7 (the stage-2 list
#      again), and SKIPS a job whose universe is incomplete (reason in the status file). `ba2-test prewarm
#      --option-history-source alphavantage ...` prints the same coverage as a report.
#      (tools/option_universe_preflight.py reads only the ThetaData tree: not this check.)
#   4. The AV store is on the grid host, in the cache the master and the worker share ($BA2_HOME/common/cache/
#      AlphaVantageOptionsProvider/): from the machine holding the complete store,
#        ba2-test sync-cache --worker remote227 --ohlcv-history-source alpaca
#      (it copies every missing file and PRUNES worker files that machine's cache does not list; check the drift first,
#      docs/REPRODUCE-BACKTESTS.md 5.4 / 5.8). Then the host-local shared arrays of the AV store, run TWICE per
#      universe on the grid host (the second run must report 0 built):
#        python tools/build_shared_arrays.py --options-store alphavantage --universe-file tools/options_universe_stage2.txt \
#            --ohlcv-provider AlpacaHistoryOHLCVProvider --interval 1d --start 2020-01-02 --end 2025-12-31 --jobs 4
#        python tools/build_shared_arrays.py --options-store alphavantage --universe-file tools/options_universe_nasdaq.txt \
#            --ohlcv-provider AlpacaHistoryOHLCVProvider --interval 1d --start 2020-01-02 --end 2025-12-31 --jobs 4
#   5. The seed bundles are in $GRID_DIR: build them from the test DB with tools/build_style_seed_bundle.py
#      (docs/REPRODUCE-BACKTESTS.md 5.7) and upload them (jobL10 jobPstop jobAshare jobC jobBurst jobNdx jobCheap).
#   6. Every job passes its seed check first (writes nothing), with the chain's environment:
#        OPTION_HISTORY_SOURCE=alphavantage bash tools/option_style_job.sh <style> <suffix> <bundle> [the job's extra args] \
#            --seed-check-only
#      (job 6 also with its UFILE / SCREEN environment as below; its `--print-universe` dry run with the same arguments
#      shows the restriction resolved against the screener panel: a listed name the panel cannot select refuses).
#      Job 7 with its SCREEN, e.g.
#        OPTION_HISTORY_SOURCE=alphavantage SCREEN="--screen-preset cheap50" bash tools/option_style_job.sh cheap10k s3W10 \
#            bundle_jobCheap.json --estimates-source fmp --initial-capital 10000 --equity-cap 10000 --seed-check-only
#      and its `--print-universe` dry run (same arguments) shows how many listed names the $10..$50 screen can ever select.
# Every job continues after a failed one (the exit codes are appended to $GRID_DIR/s3chain_small.status).
REPO="${REPO:-/home/debian/ba2-grid/repo}"
GRID_DIR="${GRID_DIR:-/home/debian/ba2-grid}"
JOB="$REPO/tools/option_style_job.sh"
cd "$GRID_DIR" || exit 1
B="$GRID_DIR"
export OPTION_HISTORY_SOURCE=alphavantage            # owner 2026-11-12: every job of this chain reads the AV store

# av_complete <universe file> <tag>: exit 0 when the AV store holds every session of 2020-01-02..2025-12-31 for every
# listed name (read-only, no network), with the job script's own host defaults; the log names what is owed.
av_complete() {
    local home="${BA2_HOME:-/home/debian/ba2-grid/home}"
    (cd "$REPO" && BA2_HOME="$home" \
        PYTHONPATH="$REPO/packages/common:$REPO/packages/providers:$REPO/packages/experts:$REPO/testplatform/backend:$REPO/testplatform" \
        "${PY:-/opt/ba2worker/ba2-venvs/test/bin/python}" tools/download_av_option_chains.py --symbols-file "$1" \
        --start 2020-01-02 --end 2025-12-31 --require-complete --db "$home/test/dl_forecasting.db") > "$2_avcheck.log" 2>&1
}

STAGE2="$REPO/tools/options_universe_stage2.txt"
if av_complete "$STAGE2" s3stage2; then
    # 1. $10k: long calls / call spreads / convex / LEAPS. Redo of job L10 (opt 44).
    bash "$JOB" long10k s3L10 "$B/bundle_jobL10.json" --fitness option_long_dd40_50 --estimates-source fmp \
        --initial-capital 10000 --equity-cap 10000 > s3L10_run.log 2>&1
    echo "jobL10 exit $? $(date -Is)" >> s3chain_small.status

    # 2. $20k: premium with the wheel first (WHEEL > CSP > BULLPS > JL > RS > CC > PP > IC), style premium_stop.
    bash "$JOB" premium_stop s3P20 "$B/bundle_jobPstop.json" --estimates-source fmp > s3P20_run.log 2>&1
    echo "jobP20 exit $? $(date -Is)" >> s3chain_small.status

    # 3. $20k: mid-term mix (STRG/STRD > BULLCS > BF > LC), style mid_share.
    bash "$JOB" mid_share s3A20 "$B/bundle_jobAshare.json" --estimates-source fmp > s3A20_run.log 2>&1
    echo "jobA20 exit $? $(date -Is)" >> s3chain_small.status

    # 4. $20k: long-dated (CBS > LEAP > PMCC > CONVEX). Redo of job C (opt 43).
    bash "$JOB" long s3C20 "$B/bundle_jobC.json" --fitness option_long --estimates-source fmp > s3C20_run.log 2>&1
    echo "jobC20 exit $? $(date -Is)" >> s3chain_small.status

    # 5. $10k: momentum burst (LC > BULLCS, short-dated, exits always on), style burst10k. Same account flags as job 1.
    bash "$JOB" burst10k s3B10 "$B/bundle_jobBurst.json" --fitness option_long_dd40_50 --estimates-source fmp \
        --initial-capital 10000 --equity-cap 10000 > s3B10_run.log 2>&1
    echo "jobB10 exit $? $(date -Is)" >> s3chain_small.status
else
    echo "jobs L10 P20 A20 C20 B10 SKIPPED: AV store incomplete for options_universe_stage2.txt, check exit $? (see s3stage2_avcheck.log) $(date -Is)" >> s3chain_small.status
fi

# 6. $50k: the wheel family (WHEEL > CSP, covered calls only after an assignment) on the 30 largest NASDAQ-listed
#    names, style wheel50k_ndx (owner 2026-11-12). The screen is FIXED (--screen-preset ndx30: the 30 largest by
#    market cap at each scan, no screener genes) and restricted to tools/options_universe_nasdaq.txt; default fitness.
#    GUARD: the AV store must hold the whole list, the ten names downloaded separately included (checklist 3).
NDX="$REPO/tools/options_universe_nasdaq.txt"
if av_complete "$NDX" s3N50; then
    UFILE="$NDX" SCREEN="--screen-preset ndx30" bash "$JOB" wheel50k_ndx s3N50 "$B/bundle_jobNdx.json" \
        --estimates-source fmp --initial-capital 50000 --equity-cap 50000 > s3N50_run.log 2>&1
    echo "jobN50 exit $? $(date -Is)" >> s3chain_small.status
else
    echo "jobN50 SKIPPED: AV store incomplete for options_universe_nasdaq.txt, check exit $? (see s3N50_avcheck.log) $(date -Is)" >> s3chain_small.status
fi

# 7. $10k: the wheel family on CHEAP large caps (WHEEL > CSP > BULLPS > LC), style cheap10k (owner 2026-11-13). A $10k
#    account cannot secure one put on a $170 stock ($17,000); the FIXED screen --screen-preset cheap50 keeps, at each
#    weekly scan, the 50 largest of the stage-2 names priced $10..$50 on that scan's price (point in time, as-traded
#    basis: the panel's split factor), so one contract reserves at most about $5,000. Default fitness (the premium
#    family's soft30, as jobs 2 and 6). Same universe as jobs 1-5, re-checked here because job 6 ran in between.
if av_complete "$STAGE2" s3W10; then
    SCREEN="--screen-preset cheap50" bash "$JOB" cheap10k s3W10 "$B/bundle_jobCheap.json" \
        --estimates-source fmp --initial-capital 10000 --equity-cap 10000 > s3W10_run.log 2>&1
    echo "jobW10 exit $? $(date -Is)" >> s3chain_small.status
else
    echo "jobW10 SKIPPED: AV store incomplete for options_universe_stage2.txt, check exit $? (see s3W10_avcheck.log) $(date -Is)" >> s3chain_small.status
fi
