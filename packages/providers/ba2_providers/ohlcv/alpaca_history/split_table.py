"""Our own split table over the RAW Alpaca daily store: every candidate split is judged by the raw price.

Runbook: docs/REPRODUCE-BACKTESTS.md section 4 (data preparation).  Run it through the pipeline CLI, step ``split-table``
(offline once the FMP calendar windows are cached):

    python tools/prepare_history.py --source alpaca --steps split-table

This module is the RULES (pure: no network, no writes) plus this spec; the loaders, the FMP calendar fetch, the writers and
``build_split_table`` are in ``split_build.py``.

WHY.  Neither vendor's split list is trusted: Alpaca lists spurious events (HON, NVS, STRR, SNEX, BIP, DRS, IR) and misses
real ones (KOD, UHAL, SNX, IAC, RTX); FMP has mixed-basis series (CRWD, META before 2022-06).  The bars here are RAW, so a
real split shows as an overnight jump ``close(D-1) / open(D)`` about equal to the factor ``f`` (new shares / old shares; a
reverse split has f < 1).  Task 4 (the cache build) reads only ``_meta/split_table.json``.

INPUTS (all read-only)
  * ``--store`` (default ~/Documents/ba2/alpaca_store_daily2016): ``bars/1Day/symbol=<S>/all.parquet`` (t UTC bar start, o,
    h, l, c, v, n, vw; folder names use '-' as in BRK-B).  PLACEHOLDER bars (v == 0 AND n == 0: Alpaca's no-trade day, flat price) are DROPPED
    on reading (like the build does), so a jump across a dropped run is measured between the real bars on either side and a long dropped run
    becomes a data gap (the pair is skipped beyond MAX_GAP_DAYS unless it jumps beyond x5, a series break); ``corporate_actions/ym=YYYY-MM.json.gz``, ``STORE_INFO.json``.  Every daily ``t`` must
    be exactly 00:00 New York time (04:00Z in summer, 05:00Z in winter; the session is its date) and each session may appear
    once, else the build STOPS (SplitTableBuildError); the corporate actions must hold at least one forward or reverse split.
    The calendar is fetched from ``--fmp-from`` to ``--fmp-to`` (default: the store's asof + 1 day; earlier than the asof is
    refused).
  * FMP split calendar ``/api/v3/stock_split_calendar`` (date, symbol, numerator, denominator), fetched in 30-day windows
    (a window of 1000+ rows is halved down to one day; a row more than a day outside its window is refused) and cached raw
    in ``<out>/_meta/fmp_split_calendar/<from>_<to>.json``.  The key is the AppSetting FMP_API_KEY of ``--db`` (read-only),
    read only when a window is not cached, never printed.
  * FMP ADJUSTED daily files ``<FMP daily dir>/<SYM>_1d.parquet`` (Date, Close; '.' written '-') for the FMP step.
  * ``split_overrides.json`` (below).

LISTINGS -> EVENTS.  Alpaca: forward_splits and reverse_splits (ex_date, f = new_rate/old_rate), stock_dividends (ex_date,
f = rate), unit_splits ONLY when old_symbol == new_symbol (effective_date, f = new_rate/old_rate; a unit separated into a
different symbol is ignored and counted).  spin_offs are NOT splits: they become SPIN-OFF events (below).  IGNORED:
cash_dividends, mergers (cash, stock, stock_and_cash), rights_distributions, redemptions, worthless_removals.  FMP:
numerator/denominator (a row with a missing or zero side is counted, never read as a split; a boolean or negative side stops the build).  An absent kind in a monthly
file means none that month.  f == 1 events are counted and dropped.
NAME CHANGES.  The bars were fetched as-of 2026-10-12, so a renamed ticker's whole history sits under its CURRENT symbol,
while a split before the rename is listed under the OLD one: an event is mapped to the current symbol by following the
``name_changes`` chain old -> new in process_date order, using only renames whose process_date is AFTER the event date (a
same-day rename is ambiguous -- tickers swap on a date -- and is not followed); a rename to the same ticker (CUSIP-only) is
skipped.  Symbols are upper-cased with '.' written '-'.  Events of symbols not in the store are counted, not judged.

THE RULES (tolerances are module constants)
  * Event placement: listings of one symbol whose first sessions on/after their dates are at most NEAR_SESSIONS = 3
    sessions apart (chained) form a cluster.  A cluster where each vendor lists at most one ratio is ONE event; where a
    vendor lists several ratios it is one event per ratio (AMC 2023-08: a 1.13333 distribution and the 1:10 reverse split).
    Duplicate listings (ratio within RATIO_TOL = 1 %) are one (``dup_listings_collapsed``).
  * An event dated on/before the symbol's first bar or after its last cannot affect its prices: counted
    (``outside_range_<source>``), not reviewed.
  * ``jump`` = close(D-1) / open(D), D = the first session on/after the event date.  The jump needs a bar before D and at
    most MAX_GAP_DAYS = 30 days between the two bars; else the price cannot observe the event (``no_price_data``).
  * BIG event: ``|ln f| >= ln 1.25`` (BIG_MIN).  PROVEN BY JUMP when ``|ln jump - ln f| <= ln 1.15`` (PROVEN_TOL), looked
    for at the listed dates first, then up to 3 sessions either side (a vendor's ex-date can be a day or two off); the
    accepted date is the session that jumped.  CONTRADICTED when every jump is within ``ln 1.10`` of 1 (CONTRADICT_TOL).
  * SMALL event (``|ln f| < ln 1.25``: stock dividends, small unit splits): the price cannot prove it.  Accepted when BOTH
    vendors list it (ratios within 1 %), or when ONE vendor lists it and Yahoo lists a split within 3 sessions with a ratio
    within 0.5 % (``small_confirmed_by_yahoo``, source ``<vendor>+yahoo``; CBSH stock dividends); otherwise review, not
    applied (also without Yahoo).  The FMP step is NOT used for these (it also
    reflects spin-off adjustments such as HON's, which our convention does not apply).
  * THE FMP STEP (second arbiter).  r(t) = FMP ADJUSTED close / Alpaca RAW close on the sessions both have.  FMP divides
    its history by the factors it applied, so across an event the market move cancels in r and
    ``step = r(D) / r(D-1)`` equals the factor FMP applied (1.0 if none); D-1 = the last common session before D, D the first
    on/after it.  STABILITY: also take the 2 common sessions before D-1 and the 2 after D; if r varies by more than 2 %
    (STEP_STABLE) within either 3-session side the vote is ``unstable`` -- unless another LISTED event (of another unit, an
    override) lies in those sessions, then only D-1 and D count.  (Not "accepted": the verdicts of the neighbours are not
    known yet.)  VOTE: ``equals_f`` when ``|ln step - ln f| <= ln 1.02`` (STEP_TOL), ``flat`` when ``|ln step| <= ln 1.02``,
    ``other``, ``none`` (no FMP data on both sides), ``unstable``.  Column ``fmp_step`` = "<step> <vote>" (or "none"; empty
    when the arbiter is off).  Candidate sessions for the step are searched like the jump's.  CAVEAT: step == f shows FMP
    APPLIED the factor, not that the market had the split; that is why a step-proven accept is GUARDED (below) and the step is
    recorded per event (``fmp_step``).  GUARD: ``proven_by_fmp_step`` needs the step equal to f within 2 % AND the raw jump
    EITHER (i) within x1.5 of f (STEP_JUMP_MAX) and not within ln 1.10 of 1, OR (ii) BOTH vendors list f (within 1 %) and the
    jump is closer to f than to 1 in log terms (|ln jump - ln f| < |ln jump|: a reverse split on a day the stock also moved
    hard); otherwise the event is NOT applied and gets the review verdict
    ``fmp_step_without_jump`` (FMP adjusted for a separation / spin-off the raw bars barely show: ADEA, JBIO, NEUP, RGC, TJGC).
  * both vendors list DIFFERENT ratios (not within 1 %): the ratio the jump proves wins if exactly one is proven
    (``ratio_mismatch_resolved``); else the ratio FMP's step equals wins if exactly one does and the jump does not contradict
    it (``ratio_mismatch_resolved_by_fmp_step``); else ``ratio_mismatch``.
  * UNEXPLAINED JUMPS: an overnight close(t-1)/open(t) > 1.8 or < 0.55 with no listed event (or override) within 3 sessions
    is a review row, unless the FMP step there is ``flat`` (``jump_explained_by_market``: a real price move, counted).
    A pair of bars further apart than MAX_GAP_DAYS observes no overnight and is skipped.
  * SPIN-OFFS (verdicts ``spinoff_*``).  The backtest engine does not credit the spun-off shares, so an unadjusted parent
    series shows a fake loss on the ex-date.  Every Alpaca ``spin_offs`` listing (parent = source_symbol, spinco = new_symbol,
    both mapped through the name changes) gets a COMPUTED factor
        f = 1 + (new_rate / source_rate) * spinco_close(D) / parent_close(D)
    with D = the parent's ex-date session, parent_close its raw close on D, spinco_close the raw close of the spinco's first
    session on/after D within 3 sessions, from the same store.  Prices BEFORE D are divided by f; VOLUME IS NOT multiplied
    (the share count is unchanged, unlike a split): spin-offs are therefore a separate event kind, the top-level
    ``spinoffs`` of split_table.json (``{symbol: [[date, f], ...]}``), never mixed into ``splits``.  The jump and big-factor
    rules do not apply (the factor is computed, not listed); small factors (HON/Garrett ~1.01-1.03) are adjusted too.
    FMP CROSS-CHECK on its step across D: FLAT is classified first (|ln step| <= ln 1.02, whatever the factor: FMP did not
    adjust it).  A non-flat step within 10 % of f (SPINOFF_FMP_TOL; gaps and jumps around the ex-date explain
    the near misses) -> ``spinoff_fmp_agrees``, OUR computed factor is applied and the deviation is recorded in ``fmp_step``; ``flat`` (FMP did not adjust it,
    e.g. CTVA) -> applied anyway, ``spinoff_fmp_flat`` + an informational row; no or unstable FMP data -> applied,
    ``spinoff_fmp_unverified`` + row; any other step -> ``spinoff_fmp_mismatch`` review row, NOT applied.  No spinco bar within
    3 sessions -> if FMP's step across the ex-date is NOT flat (and above 1) it is the factor (``spinoff_from_fmp_step``,
    applied, informational row: the OTC spinco names Alpaca does not carry); otherwise ``spinoff_no_spinco_price`` review row,
    not applied.  The spinco's bars are read for ANY symbol present in the store's bars dir (not only the judged / listed
    ones); a ``-WI`` (when-issued) suffix maps to the base ticker (AIRC-WI -> AIRC).  A same-ticker spin-off (the spinco maps
    to the parent itself: CR, INBX, MSGE) is refused with ``spinoff_no_spinco_price``, no fallback.  CVRs, warrants and
    rights are NOT adjusted (like cash dividends): ``spinoff_not_adjusted_cvr_warrant``, counted, no review row.  A spun-off
    security is one when its id is a 9-character CUSIP-like code containing a digit (092CVR018, 67012E104), ends in ``-WS``,
    is the parent ticker + W / Z / L (OPENW, OPENZ, OPENL), or is any other 3-5 letter ticker ending in W that has no bars
    (SAVAW, XRXDW).  The real spun-off tickers still lacking bars are written to ``_meta/spinco_symbols_missing.txt`` (below).  A spin-off position explains nearby jumps.  A split
    listing within 3 sessions of an APPLIED spin-off whose ratio is within 2 % of its factor is that same event listed as a
    split by a vendor: it is absorbed (``listings_absorbed_by_spinoff``), never adjusted twice; a split that still gets
    accepted with the spin-off's factor (e.g. through an override) stops the build.  Split overrides that REJECT a listing
    (NVS, HON, ADEA, BIP, CTVA, AMC) reject the SPLIT listing only; they never block a spin-off adjustment.
  * SPLIT + SPIN-OFF, JOINT DECISION.  When a split is LISTED within 3 sessions of a spin-off's ex-date (HON 2026-06-29: a 1:2
    reverse split and the HONA spin-off, 1 HONA per 2 HON), the price shows one combined move.  Three candidates are
    scored by FMP's step across the date: the split alone (step = f), the spin-off alone (step = S), and the two COMPOSED
    (step = f x S', where the spin rate is converted to the post-split share basis: S' = 1 + (rate / f) x spinco_close /
    parent_close).  The ONE candidate within 10 % of the step is kept (several within 10 %: the closest wins only when it is
    within 2 % and the others are not); none or an ambiguous tie -> ``split_spinoff_conflict`` review row, neither applied.
    Split alone: the listed split is decided by the normal rules and the spin-off is absorbed
    (``spinoff_absorbed_by_split``).  Spin-off alone: the listing was the spin-off listed as a split (a listed ratio within 2 %
    of the spin-off factor is always that).  Composed: BOTH are applied -- the split (``split_composed_with_spinoff``) and the
    spin-off at S' (``spinoff_composed_with_split``) -- because volume is multiplied by the split and not by the spin-off.
    Only Alpaca's listed ratios are split candidates when Alpaca lists any (FMP's own composite "split" is not).  A reviewed
    override on the listing decides it (the split is kept, the spin-off absorbed).  An override decision always leaves a row
    (``override_accept`` / ``override_reject``): a listed event never disappears silently.
  * Evidence for the joint decision is FMP's step; with no FMP data on both sides the raw jump stands in (VGR: jump 1.519 vs
    the spin-off 1.5367 and the 1.41 split).  A listed split whose own price step (jump proof or FMP step) lies on ANOTHER
    session than the spin-off's -- an ADR ratio change the session after the ex-date (UL: the 8:9 consolidation on 12-09, the
    Magnum spin-off on 12-08) -- is a SEPARATE event and goes through the normal rules, which already put a split on the
    session that moved (``splits_moved_to_jump_session`` counts accepted dates no vendor listed).  When FMP's step IS the
    spin-off's factor (a spin-off with no spinco price, factor taken from the step: FTI) a split listed there is that
    spin-off listed as a split: absorbed, adjusted once.
  * SPIN-OFF OVERRIDES: an override entry with ``"kind": "spinoff"`` decides the SPIN-OFF of its symbol / ex-date: reject drops
    the adjustment, accept applies ``ratio`` as the factor (and absorbs split listings there).  The default kind is "split".
    A split override and a spin-off override on one symbol / date are independent (CENT 2024-02-09 rejects both).  An ACCEPT of each on
    one date (ENOV 2022-04-05: the 1:3 reverse split and the ESAB spin-off 1.743) applies BOTH as decided, each factor on its own basis
    (prices divided by the split factor and by the spin-off factor); neither is absorbed by the other.
  * SPLIT + SPIN-OFF ON ONE EX-DATE (leftovers).  A split and a spin-off of one symbol within 3 sessions are ONE price event (AIV 2020-12-15:
    the vendors list the spin-off as a 9.295 split, Alpaca lists the spin-off itself): never both.  The SPLIT is kept and the
    spin-off absorbed (``spinoff_absorbed_by_split``, informational row; recorded in the split's ``absorbed`` /
    ``absorbed_factor`` columns and in ``absorbed_spinoffs`` of split_table.json) when the split is a reviewed override or FMP's
    step agrees (within 10 %) with the split alone or with split x spin-off.  Otherwise NEITHER is applied and the pair goes
    to the review file as ``split_spinoff_conflict`` (decide it with an override).
  * STORE SYMBOLS.  An event is mapped to the LAST symbol of its name-change chain whose bars cover its date, not blindly to
    the current ticker: OP was renamed SVRN on 2025-11-17, after its bars end (2025-11-14), so its 1:10, 1:20 and 1:25 reverse
    splits stay on OP (counted: ``events_mapped_to_older_symbol``); a symbol "has bars" for an event when its first bar is on/before
    the event date and its last bar on/after it.
  * SMALL BOTH-LISTED FACTORS need the price to agree: FMP's step equals the factor (within 2 %) -> ``small_both``; else Yahoo
    lists it (3 sessions, 0.5 %) -> ``small_confirmed_by_yahoo`` (source ``both+yahoo``); else the raw jump is closer to f than to 1
    in log space (``|ln(j/f)| < |ln j|``) -> ``small_both_jump_agrees``; else ``small_both_price_disagrees``
    review row, NOT applied (AIV 2019-02-21: flat raw jump, flat FMP series, Yahoo's series matches ours only without it).
  * BOTH-VENDORS RULE (``both_listed_jump_within_1_5``): a big event with ``|ln f| >= ln 2`` that BOTH vendors list at the
    same ratio (within 1 %) is accepted when the raw jump is within x1.5 of f, with or without a matching FMP step (tried
    after the jump proof and the guarded step proof).
  * INFERRED SPLITS (candidates).  An unexplained jump (no listed event near) whose FMP step is stable and within 0.5 % of
    a CLEAN ratio r -- k or 1/k for an integer k in 2..1000, or 3/2, 2/3 -- and whose raw jump is within x1.5 of r is a
    CANDIDATE unlisted split.  A split that NEITHER vendor list contains is suspicious (FMP data errors turn market gaps into
    "splits": SM 2020-03-09, DBVT 2020-03-17), so a candidate needs a THIRD source: Yahoo (``yfinance`` ``Ticker.splits``).
    It is accepted at r only when Yahoo lists a split within 3 sessions of the date with a ratio within 1 % of r
    (``inferred_confirmed_by_yahoo``, source ``fmp_step+yahoo``, + informational row); Yahoo lists none ->
    ``inferred_unconfirmed`` review row, NOT applied; Yahoo unreachable / symbol unknown ->  ``yahoo_unavailable`` review row,
    NOT applied.  A not-applied candidate beyond x5 is still a series break.  YAHOO IS USED FOR THIS and to confirm a SMALL single-vendor event
    (above), never for the other listed events (both-vendor, FMP-only proven, big events, ...).  Yahoo uses CURRENT tickers: a symbol it does not know is retried under its
    renamed successors from the Alpaca name-change chain (TPX -> SGI) and the answering ticker is recorded.  An answer from a
    ticker that is NOT the requested one or a rename successor of it (renamed on/after the symbol's last bar: OP -> SVRN, same
    CUSIP) is never accepted: the symbol is treated as unavailable (status ``foreign_ticker`` in yahoo_inputs.csv).  Requests are
    paced (~1/s) and retried; the raw result per symbol is cached in ``<out>/_meta/yahoo_splits/<SYM>.json`` (with
    ``fetched_at``), so a rerun is offline; ``--refresh-yahoo`` fetches again.  A transient failure is not cached.
  * SERIES BREAKS (``series_break``): any unexplained raw overnight jump still left after all of the above with
    ``|ln jump| >= ln 5`` (beyond x5 either way) is a spliced series (a reused ticker, new equity after a bankruptcy), never
    a split: review row (applied False) and an entry in the top-level ``breaks`` of split_table.json.  A jump that FMP's step
    explains as a market move (``flat``) and a jump a split, spin-off or override explains is never a break.  The scan also
    covers bar pairs across a data gap longer than MAX_GAP_DAYS (the unexplained-jump review rows skip them).  The cache build keeps only the segment AFTER the last break of a
    symbol: a ticker means the company it names at the store's asof, which is also the company FMP's fundamentals describe.
  * Two accepted BIG events in the same direction within 3 sessions would adjust one split twice (the vendors list slightly
    different ratios, both "proven"): both are retracted (``overlapping_accepts``).
  * OVERRIDES (``split_overrides.json``, reviewed, same rules as screener/panel_exclusions.json): ``_doc`` + ``entries``
    with symbol, event_date (ISO), decision accept|reject, reason, added, reviewed_by, and ``ratio`` for accept (> 0, != 1).
    An entry decides the event of its symbol that lists event_date (any date of the cluster) outright; one matching no listing
    ADDS the split (accept) or silences the unexplained-jump row at its date (reject).  Two overrides matching one listing group are
    ambiguous and stop the build; one override decides every listing group dated on its event_date (the same event listed
    twice, e.g. with an inverted ratio) once, and the listings it absorbs are counted (``listings_absorbed_by_override``).  A reviewed_by starting with
    'pending' REFUSES the build (``SplitOverridesPending``; the pipeline exits 2).  The file's sha256 is in the table.

VERDICT CODES (``VERDICTS``; counted in the printed summary as ``verdict_<code>``)
  accepted:  proven                  big, jump proves it (any source; FMP-only gets fmp_only_proven)
             fmp_only_proven         FMP-only and proven by the jump: accepted + review row "Alpaca missed"
             proven_by_fmp_step      big, jump does not prove it but FMP's step equals f and the guard holds: accepted (FMP-only: + review row)
             ratio_mismatch_resolved           vendors disagree; the jump proves one ratio
             ratio_mismatch_resolved_by_fmp_step  vendors disagree; the step equals one, the jump does not contradict it
             small_both              small, both vendors list it AND FMP's step equals it
             small_confirmed_by_yahoo  small, one vendor lists it and Yahoo lists it within 0.5 % (+ Yahoo is consulted)
             override_accept         decided by an override entry
             both_listed_jump_within_1_5  both vendors list a big (>= x2) ratio and the jump is within x1.5 of it (no step needed)
             inferred_confirmed_by_yahoo  an UNLISTED split: clean FMP step + raw jump within x1.5 + Yahoo lists it (+ info row)
             spinoff_fmp_agrees / spinoff_fmp_flat / spinoff_fmp_unverified   applied spin-off (kind "spinoff"), by FMP vote
             spinoff_from_fmp_step   spin-off whose spinco has no price: FMP's non-flat step is the factor (+ info row)
  rejected without a review row:
             spinoff_not_adjusted_cvr_warrant  a spun-off CVR / warrant / right (counted, never adjusted)
             override_reject         decided by an override entry
             rejected_confirmed      listing contradicted by the raw jump AND flat in FMP's step: no split anywhere
             jump_explained_by_market (an unexplained jump, not an event) flat FMP step: a real price move
  review rows (``split_review.csv``; applied False unless noted):
             alpaca_contradicted / fmp_contradicted / both_contradicted   listed by that vendor(s), the raw price does not
                                     jump and FMP's step is not flat (or is unstable/none)
             unresolved              listed, the jump neither proves nor contradicts it, the step does not equal f
             fmp_step_without_jump   the step equals f but neither guard condition (i) nor (ii) holds
             small_single_vendor     small, only one vendor lists it
             ratio_mismatch          vendors disagree and neither jump nor step settles it (or both are proven)
             no_price_data           no usable bars, or none within 30 days on both sides of the event
             unexplained_jump        raw overnight jump > x1.8 / < x0.55 with no event near and the step not flat
             inferred_unconfirmed    candidate unlisted split that Yahoo does not list (not applied)
             yahoo_unavailable       candidate unlisted split Yahoo could not confirm (not applied)
             spinoff_absorbed_by_split  informational (applied): the spin-off was absorbed by the split on the same ex-date
             split_composed_with_spinoff / spinoff_composed_with_split  the composed pair, both applied (see JOINT DECISION)
             small_both_price_disagrees  small, both vendors list it but the price does not support it (not applied)
             small_both_jump_agrees  small, both vendors list it, FMP's series is flat but the raw jump is closer to f than to 1
             split_absorbed_by_spinoff  informational (NOT applied): the evidence supports the spin-off alone, the split was it
             override_accept / override_reject  informational row of every override decision (listings it decided)
             split_spinoff_conflict  split and spin-off on one ex-date that FMP's step does not support: neither applied
             spinoff_fmp_mismatch    spin-off whose computed factor FMP's step contradicts (not applied)
             spinoff_no_spinco_price spin-off whose spinco has no bar within 3 sessions of the ex-date (not applied)
             overlapping_accepts     see above (both retracted)
             series_break            unexplained raw jump beyond x5 (see SERIES BREAKS); also listed in split_table.json "breaks"
  (applied True review rows are the informational ones of fmp_only_proven, proven_by_fmp_step and the two resolved verdicts)

OUTPUTS (``<out>/_meta``, atomic temp + os.replace; read the CSVs with ``read_csv_frame`` -- pandas' default NA parsing
turns the symbol "NA" into NaN)
  split_table.json     {run_id, asof, absorbed_spinoffs {symbol: [[date, f], ...]} (spin-offs absorbed by a split), yahoo_files {dir, count, content_sha256 over (symbol, sha256 of the cache file), inputs_csv}, spinoffs {symbol: [[date, f], ...]} (prices before date / f, volume unchanged),
                       breaks {symbol: [session date, ...]}, inputs_sha256, overrides_sha256, fmp_daily_files {dir, count, sha256 over (name,
                       size, mtime_ns), content_sha256 over (name, size, sha256 of the bytes), inputs_csv},
                       splits {symbol: [[date, factor], ...]}} events sorted; factor = new/old shares, prices BEFORE the
                       date are divided by it.  Written LAST.  Two accepted events of one symbol on one date stop the build (decide them with an
                       override).
  RUN COHERENCE        every run has a run_id (UTC timestamp + 8 hex of a hash of the inputs); it is in split_table.json and
                       the last column ("run_id") of every CSV below.  The CSVs are written first, the table last, so a
                       crash never leaves a table newer than its CSVs; the cache build checks that the run_ids match.
  spinco_symbols_missing.txt  the real spun-off tickers (parent in the store, not a CVR / warrant / right, not the parent's own
                       ticker) that still have NO daily bars in the store, one per line after two ``#`` comment lines (the first is
                       ``# run_id: <id>``).  The pipeline ``download`` step reads this list and fetches those symbols (Alpaca
                       carries no OTC names: they stay on the list and get the FMP-step fallback).
  yahoo_inputs.csv     symbol, ticker (the one that answered), status, n_splits, fetched_at, sha256 of every Yahoo cache file
                       consulted by this run
  fmp_daily_inputs.csv file, size, mtime_ns, mtime_utc, sha256 (of the bytes) of every FMP daily file read: the record of
                       exactly which bytes were used (that cache is archived after the build)
  split_review.csv     REVIEW_COLUMNS (+ run_id): symbol, event_date, alpaca_ratio, fmp_ratio, jump, fmp_step, verdict, applied, reason
  split_accepted.csv   ACCEPTED_COLUMNS: symbol, date, kind (split|spinoff), factor, source (alpaca|fmp|both|override|fmp_step|alpaca_spinoff|<vendor>+yahoo), verdict, jump, fmp_step, absorbed, absorbed_factor
  split_vs_fmp.csv     COMPARE_COLUMNS: symbol, date, our_factor, fmp_ratio, alpaca_ratio, jump, fmp_step, verdict, class;
                       class agree | ratio_differs | ours_only | fmp_only_accepted (ours and FMP, Alpaca does not list it)
                       | fmp_only_rejected (verdict = our review verdict, outside_range, rejected_confirmed or no_row)
  fmp_split_calendar/  the raw calendar windows (reruns are offline)
"""

from __future__ import annotations

import gzip
import json
import math
import os
import re
from collections import Counter
from datetime import date, timedelta
from typing import Any, Callable, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

import numpy as np
import pandas as pd


OVERRIDES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "split_overrides.json")

PROVEN_TOL = math.log(1.15)        # |ln jump - ln f| within this: the price shows the split
BIG_MIN = math.log(1.25)           # |ln f| below this is "small": price cannot prove it
CONTRADICT_TOL = math.log(1.10)    # |ln jump| within this (for a big f): the price shows no split
RATIO_TOL = 0.01                   # two listings of one event agree when their ratios are within 1 %
NEAR_SESSIONS = 3                  # listings / unexplained jumps this close are the same place
JUMP_HI = 1.8                      # unexplained overnight jump bounds
JUMP_LO = 0.55
MAX_GAP_DAYS = 30                  # bars further apart than this do not observe an event between them
STEP_TOL = math.log(1.02)          # FMP step: |ln step - ln f| within this -> "equals_f"; |ln step| within this -> "flat"
STEP_JUMP_MAX = math.log(1.5)      # a step-proven accept also needs the raw jump within this of ln f (and not flat)
BOTH_BIG_MIN = math.log(2)         # the both-vendors-listed rule needs |ln f| of at least this
SERIES_BREAK = math.log(5)         # an unexplained overnight jump beyond x5 either way is a series break
SPINOFF_FMP_TOL = math.log(1.10)   # FMP's step within 10 % of a spin-off's computed factor agrees with it (we apply OUR factor)
CLEAN_TOL = math.log(1.005)        # the FMP step must be this close (0.5 %) to a clean ratio for an inferred split
CLEAN_K_MAX = 1000                 # clean ratios: k or 1/k for integer k in 2..CLEAN_K_MAX, and 3/2, 2/3
STEP_STABLE = 0.02                 # FMP step: r may vary by at most 2 % within each 3-session side of the event

VERDICTS = ("proven", "fmp_only_proven", "proven_by_fmp_step", "ratio_mismatch_resolved",
            "ratio_mismatch_resolved_by_fmp_step", "small_both", "override_accept", "override_reject",
            "rejected_confirmed", "jump_explained_by_market", "alpaca_contradicted", "fmp_contradicted",
            "both_contradicted", "unresolved", "fmp_step_without_jump", "small_single_vendor", "small_confirmed_by_yahoo", "spinoff_absorbed_by_split",
            "split_spinoff_conflict", "split_absorbed_by_spinoff", "small_both_jump_agrees", "split_composed_with_spinoff",
            "spinoff_composed_with_split", "small_both_price_disagrees", "ratio_mismatch", "no_price_data",
            "unexplained_jump", "overlapping_accepts", "both_listed_jump_within_1_5", "inferred_confirmed_by_yahoo", "inferred_unconfirmed", "yahoo_unavailable", "series_break",
            "spinoff_fmp_agrees", "spinoff_fmp_flat", "spinoff_fmp_unverified", "spinoff_fmp_mismatch",
            "spinoff_no_spinco_price", "spinoff_not_adjusted_cvr_warrant", "spinoff_from_fmp_step")
REVIEW_COLUMNS = ["symbol", "event_date", "alpaca_ratio", "fmp_ratio", "jump", "fmp_step", "verdict", "applied", "reason"]
ACCEPTED_COLUMNS = ["symbol", "date", "kind", "factor", "source", "verdict", "jump", "fmp_step", "absorbed", "absorbed_factor"]


class ListedSplit(NamedTuple):
    symbol: str
    date: date
    ratio: float            # new shares / old shares: 4-for-1 -> 4.0, 1-for-10 -> 0.1


class SpinOff(NamedTuple):
    symbol: str             # the PARENT (source) symbol, mapped to its current ticker
    date: date              # ex-date
    spinco: str             # the spun-off company's current ticker (its bars are in the same store)
    ratio: float            # new_rate / source_rate: spinco shares received per parent share
    security: str = ""      # the listed id BEFORE the name-change mapping (a CUSIP stays a CUSIP); "" = the spinco ticker


def is_cvr_warrant_right(spinco: str, parent: str, has_bars: bool) -> bool:
    """A spun-off SECURITY that is a CVR, warrant or right, not a company: not adjusted (like a cash dividend).  Its id is
    a 9-character CUSIP-like code containing a digit (092CVR018, 67012E104); or it ends in ``-WS``; or it is the parent
    ticker plus W / Z / L (OPENW, OPENZ, OPENL); or any other 3-5 letter ticker ending in W that has no bars (SAVAW)."""
    if re.fullmatch(r"[A-Z0-9]{9}", spinco) and any(ch.isdigit() for ch in spinco):
        return True
    if spinco.endswith("-WS") or spinco in (parent + "W", parent + "Z", parent + "L"):
        return True
    return bool(re.fullmatch(r"[A-Z]{2,4}W", spinco)) and not has_bars


class SplitTableBuildError(RuntimeError):
    """An input is missing or has changed shape: the build stops (the pipeline step fails)."""


class SplitOverridesPending(ValueError):
    """An override entry is still pending review: the build must not run."""


def _norm_symbol(s: str) -> str:
    return s.strip().upper().replace(".", "-")        # the store's folder names use '-' (BRK-B)


# ---------------------------------------------------------------------------
# overrides
# ---------------------------------------------------------------------------
BAR_CAP_KIND = "bar_cap"


def _ov_kind(o: Mapping[str, Any]) -> str:
    """"split" (default) | "spinoff" | "bar_cap" (a daily High/Low cap, applied by the build, never seen by ``decide_splits``): which listing the override decides.  A spinoff override REJECTS the spin-off adjustment
    of that symbol / ex-date, or ACCEPTS it with ``ratio`` = the factor (prices before the date are divided by it)."""
    return o["kind"] if "kind" in o else "split"


def _pick(v: float, cands: Sequence[Tuple[str, float]]) -> Optional[str]:
    """The ONE candidate (kind, predicted step) that FMP's step ``v`` supports: within SPINOFF_FMP_TOL (10 %); several within
    10 %: the closest wins only when it is within STEP_TOL (2 %) and the others are not.  None = none / ambiguous."""
    agree = sorted((abs(math.log(v) - math.log(t)), k) for k, t in cands if abs(math.log(v) - math.log(t)) <= SPINOFF_FMP_TOL)
    if len(agree) == 1 or (len(agree) > 1 and agree[0][0] <= STEP_TOL < agree[1][0]):
        return agree[0][1]
    return None


def _load_entries(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every entry of the reviewed overrides file (split, spinoff and bar_cap kinds), validated.  Raises ``SplitOverridesPending`` while any
    entry's ``reviewed_by`` starts with 'pending', ``ValueError`` for a malformed entry."""
    with open(path or OVERRIDES_PATH, encoding="utf-8") as f:
        entries = json.load(f)["entries"]
    seen = set()
    for e in entries:
        miss = [k for k in ("symbol", "event_date", "decision", "reason", "added", "reviewed_by") if not e.get(k)]
        if miss:
            raise ValueError(f"split override entry {e!r} lacks {miss}: needs symbol, event_date, decision, reason, "
                             f"a date and a reviewer")
        if e["decision"] not in ("accept", "reject"):
            raise ValueError(f"split override {e['symbol']} {e['event_date']}: decision must be accept|reject, "
                             f"got {e['decision']!r}")
        date.fromisoformat(e["event_date"])
        if "kind" in e and e["kind"] not in ("split", "spinoff", BAR_CAP_KIND):
            raise ValueError(f"split override {e['symbol']} {e['event_date']}: kind must be split|spinoff|{BAR_CAP_KIND}, got {e['kind']!r}")
        if _ov_kind(e) == BAR_CAP_KIND:
            if e["decision"] != "accept" or e.get("field") not in ("High", "Low"):
                raise ValueError(f"bar_cap override {e['symbol']} {e['event_date']}: needs decision 'accept' and field High|Low, "
                                 f"got {e['decision']!r} / {e.get('field')!r}")
        elif e["decision"] == "accept":
            r = e.get("ratio")
            if not isinstance(r, (int, float)) or not math.isfinite(r) or r <= 0 or r == 1:
                raise ValueError(f"split override {e['symbol']} {e['event_date']}: 'accept' needs a ratio > 0 and != 1, "
                                 f"got {r!r}")
        key = (_norm_symbol(e["symbol"]), e["event_date"], _ov_kind(e)) + ((e["field"],) if _ov_kind(e) == BAR_CAP_KIND else ())
        if key in seen:
            raise ValueError(f"split override {key} is listed twice")
        seen.add(key)
    pending = [e for e in entries if str(e["reviewed_by"]).lower().startswith("pending")]
    if pending:
        raise SplitOverridesPending(
            f"{len(pending)} split override(s) pending review ({', '.join(e['symbol'] + ' ' + e['event_date'] for e in pending)}): "
            f"set reviewed_by in {path or OVERRIDES_PATH} before building")
    return entries


def load_overrides(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """The SPLIT and SPIN-OFF entries of the reviewed overrides file, validated (``decide_splits`` decides these).  The ``bar_cap`` entries
    are validated with them (a pending one refuses here too) but belong to the build: ``load_bar_caps``."""
    return [e for e in _load_entries(path) if _ov_kind(e) != BAR_CAP_KIND]


def load_bar_caps(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """The ``bar_cap`` entries of the reviewed overrides file: cap one daily High / Low at the 5-minute all-session envelope bound (the build
    applies them, witness ``override``)."""
    return [e for e in _load_entries(path) if _ov_kind(e) == BAR_CAP_KIND]


# ---------------------------------------------------------------------------
# vendor listings -> ListedSplit
# ---------------------------------------------------------------------------
def _kind(month: Mapping[str, Any], kind: str) -> List[Mapping[str, Any]]:
    """The actions of one kind in a monthly file.  Alpaca OMITS a kind with no action that month (measured: 'forward_splits'
    is absent from 4 of 130 files, 'name_changes' from 42), so an absent key is an empty list -- for the kinds we read only."""
    return month[kind] if kind in month else []


def build_rename_map(corporate_actions: Iterable[Mapping[str, Any]]) -> Dict[str, List[Tuple[date, str]]]:
    """old symbol -> [(process_date, new symbol)] sorted by date, from the ``name_changes`` of the monthly files."""
    out: Dict[str, List[Tuple[date, str]]] = {}
    for month in corporate_actions:
        for nc in _kind(month, "name_changes"):
            old, new = _norm_symbol(nc["old_symbol"]), _norm_symbol(nc["new_symbol"])
            if old == new:                       # a CUSIP-only change: the ticker stays
                continue
            out.setdefault(old, []).append((date.fromisoformat(nc["process_date"]), new))
    for lst in out.values():
        lst.sort()
    return out


def map_to_current(symbol: str, on: date, renames: Mapping[str, Sequence[Tuple[date, str]]],
                   in_store: Optional[Callable[[str, date], bool]] = None) -> str:
    """The symbol the bars of ``symbol`` as of ``on`` sit under: follow every rename effective AFTER ``on`` (a same-day one is ambiguous: tickers swap on a date),
    in date order (the store was fetched as-of a later date, so a renamed ticker's whole history is under its
    CURRENT symbol).  With ``in_store(symbol, day)`` the result is the LAST symbol of the chain whose bars COVER ``on``: a ticker
    renamed after the store's bars end (OP -> SVRN, 2025-11-17; OP's bars end 2025-11-14 and SVRN's start 2025-11-17) keeps its
    history under the OLD symbol, and its events must follow it there."""
    cur, at = _norm_symbol(symbol), on
    best = cur if (in_store is not None and in_store(cur, on)) else None
    for _ in range(25):
        nxt = next(((d, n) for d, n in renames.get(cur, ()) if d > at), None)
        if nxt is None:
            return best if best is not None else cur
        at, cur = nxt
        if in_store is not None and in_store(cur, on):
            best = cur
    raise ValueError(f"rename chain of {symbol} from {on} does not end (cycle?)")


def rename_successors(symbol: str, renames: Mapping[str, Sequence[Tuple[date, str]]],
                      not_before: Optional[date] = None) -> List[str]:
    """The tickers ``symbol`` was renamed to LATER, oldest first (the name-change chain followed to its end): a vendor that
    uses current tickers (Yahoo) may only know the last of them.  With ``not_before`` only renames dated on/after it count: a
    rename BEFORE the symbol's last bar belongs to an earlier company that held the ticker (a reused ticker), not to this one."""
    out: List[str] = []
    cur, at = _norm_symbol(symbol), (date.min if not_before is None else not_before - timedelta(days=1))
    for _ in range(25):
        nxt = next(((d, n) for d, n in renames.get(cur, ()) if d > at), None)
        if nxt is None:
            return out
        at, cur = nxt
        out.append(cur)
    raise ValueError(f"rename chain of {symbol} does not end (cycle?)")


def _map(symbol: str, day: date, renames: Mapping[str, Sequence[Tuple[date, str]]],
         in_store: Optional[Callable[[str, date], bool]], stats: Counter) -> str:
    """map_to_current, counting the events that stay on an OLDER symbol because the store holds no bars under the current one."""
    sym = map_to_current(symbol, day, renames, in_store)
    if in_store is not None and sym != map_to_current(symbol, day, renames):
        stats["events_mapped_to_older_symbol"] += 1
    return sym


def alpaca_events_from_actions(corporate_actions: Iterable[Mapping[str, Any]],
                               renames: Mapping[str, Sequence[Tuple[date, str]]],
                               stats: Optional[Counter] = None,
                               in_store: Optional[Callable[[str, date], bool]] = None) -> List[ListedSplit]:
    """Factor events of the Alpaca corporate-action files, mapped to current symbols.  Spin-offs, cash dividends and
    mergers are not price adjustments here (same convention as FMP close) and are ignored; a unit split into a
    DIFFERENT symbol (SPAC unit separation) is not a factor event of either symbol."""
    stats = stats if stats is not None else Counter()
    out: List[ListedSplit] = []

    def add(sym: str, d: str, ratio: float, kind: str) -> None:
        if not (isinstance(ratio, (int, float)) and math.isfinite(ratio) and ratio > 0):
            raise ValueError(f"alpaca {kind} {sym} {d}: unusable ratio {ratio!r}")
        day = date.fromisoformat(d)
        stats[f"alpaca_{kind}"] += 1
        out.append(ListedSplit(_map(sym, day, renames, in_store, stats), day, float(ratio)))

    for month in corporate_actions:
        for e in _kind(month, "forward_splits"):
            add(e["symbol"], e["ex_date"], e["new_rate"] / e["old_rate"], "forward_splits")
        for e in _kind(month, "reverse_splits"):
            add(e["symbol"], e["ex_date"], e["new_rate"] / e["old_rate"], "reverse_splits")
        for e in _kind(month, "stock_dividends"):
            add(e["symbol"], e["ex_date"], e["rate"], "stock_dividends")
        for e in _kind(month, "unit_splits"):
            if _norm_symbol(e["old_symbol"]) != _norm_symbol(e["new_symbol"]):
                stats["alpaca_unit_splits_other_symbol_ignored"] += 1
                continue
            add(e["old_symbol"], e["effective_date"], e["new_rate"] / e["old_rate"], "unit_splits")
    return out


def spinoff_events_from_actions(corporate_actions: Iterable[Mapping[str, Any]],
                                renames: Mapping[str, Sequence[Tuple[date, str]]],
                                stats: Optional[Counter] = None,
                                in_store: Optional[Callable[[str, date], bool]] = None) -> List[SpinOff]:
    """Alpaca ``spin_offs`` (source_symbol, new_symbol, ex_date, new_rate, source_rate), parent and spinco both mapped to
    their current symbols through the name changes (like every other event)."""
    stats = stats if stats is not None else Counter()
    out: List[SpinOff] = []
    for month in corporate_actions:
        for e in _kind(month, "spin_offs"):
            nr, sr = e["new_rate"], e["source_rate"]
            if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0 for x in (nr, sr)):
                raise SplitTableBuildError(f"alpaca spin_off {e['source_symbol']} {e['ex_date']}: unusable rates {nr!r}/{sr!r}")
            day = date.fromisoformat(e["ex_date"])
            stats["alpaca_spin_offs"] += 1
            new = _norm_symbol(e["new_symbol"])
            if new.endswith("-WI"):                  # when-issued line (AIRC.WI, BAM.WI): the bars sit under the base ticker
                new = new[:-3]
            out.append(SpinOff(_map(e["source_symbol"], day, renames, in_store, stats), day,
                               _map(new, day, renames, in_store, stats), float(nr) / float(sr), new))
    return out


def fmp_events_from_rows(rows: Iterable[Mapping[str, Any]],
                         renames: Mapping[str, Sequence[Tuple[date, str]]],
                         stats: Optional[Counter] = None,
                         in_store: Optional[Callable[[str, date], bool]] = None) -> List[ListedSplit]:
    """Events of FMP split-calendar rows (``date, symbol, numerator, denominator``).  A row with a missing or zero
    side has no ratio and is counted, never read as a 0-for-1 split."""
    stats = stats if stats is not None else Counter()
    out: List[ListedSplit] = []
    for r in rows:
        num, den = r.get("numerator"), r.get("denominator")
        if isinstance(num, bool) or isinstance(den, bool) or (isinstance(num, (int, float)) and num < 0) \
                or (isinstance(den, (int, float)) and den < 0):
            raise SplitTableBuildError(f"FMP split row {dict(r)!r}: a boolean or negative numerator/denominator")
        if not isinstance(num, (int, float)) or not isinstance(den, (int, float)) or not den or num <= 0:
            stats["fmp_rows_without_ratio"] += 1
            continue
        day = date.fromisoformat(r["date"][:10])
        stats["fmp_rows"] += 1
        out.append(ListedSplit(_map(r["symbol"], day, renames, in_store, stats), day, float(num) / float(den)))
    return out


def corporate_action_paths(store: str) -> List[str]:
    """The monthly corporate-action files of a store (``ym=*.json.gz``, a stray temp file is not one), oldest first --
    the ONE listing both the reader and the input hash use."""
    d = os.path.join(store, "corporate_actions")
    paths = [os.path.join(d, n) for n in sorted(os.listdir(d)) if n.startswith("ym=") and n.endswith(".json.gz")]
    if not paths:
        raise FileNotFoundError(f"no corporate_actions/ym=*.json.gz under {store}")
    return paths


def read_corporate_actions(store: str) -> List[Dict[str, Any]]:
    """The monthly corporate-action files of a store, oldest first."""
    out = []
    for path in corporate_action_paths(store):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            out.append(json.load(f))
    return out


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------
class _Bars(NamedTuple):
    days: np.ndarray        # datetime64[D], ascending, one per session with a usable bar
    o: np.ndarray
    c: np.ndarray


def _bars(df: pd.DataFrame, stats: Counter, symbol: str = "?") -> _Bars:
    """Usable daily bars as sessions.  Alpaca's daily ``t`` is the bar start; converted to New York time EVERY daily bar must
    be exactly 00:00 there (04:00Z in summer, 05:00Z in winter) -- the session is then its date -- and a session may appear
    once: anything else means the store's timestamps are not what this module assumes, and the build stops."""
    t = pd.to_datetime(df["t"])
    if t.dt.tz is not None:
        t = t.dt.tz_convert("America/New_York").dt.tz_localize(None)
    off = (t - t.dt.normalize()) != pd.Timedelta(0)
    if off.any():
        raise SplitTableBuildError(f"{symbol}: {int(off.sum())} daily bar(s) are not at 00:00 New York time "
                                   f"(first: {t[off].iloc[0]}): the store's timestamps are not the assumed bar starts")
    days = t.dt.normalize().to_numpy().astype("datetime64[D]")
    if len(np.unique(days)) != len(days):
        raise SplitTableBuildError(f"{symbol}: the daily bars hold the same session more than once")
    o = df["o"].to_numpy(dtype=float)
    c = df["c"].to_numpy(dtype=float)
    ok = np.isfinite(o) & np.isfinite(c) & (o > 0) & (c > 0)
    if not ok.all():
        stats["bars_unusable_skipped"] += int((~ok).sum())
    order = np.argsort(days[ok], kind="stable")
    return _Bars(days[ok][order], o[ok][order], c[ok][order])


class _RatioSeries:
    """r(t) = FMP ADJUSTED daily close / Alpaca RAW daily close on the sessions both have.  FMP divides its history
    by the factors it applied, so across an event the market move cancels in r: ``step = r(D) / r(D-1)`` equals the
    factor FMP applied (1.0 when it applied none)."""

    def __init__(self, days: np.ndarray, vals: np.ndarray):
        self.days, self.vals = days, vals

    @classmethod
    def build(cls, bars: _Bars, fmp_df: pd.DataFrame) -> Optional["_RatioSeries"]:
        cols = {c.lower(): c for c in fmp_df.columns}
        if "date" not in cols or "close" not in cols:
            raise ValueError(f"FMP daily frame needs Date and Close columns, has {list(fmp_df.columns)}")
        idx = pd.DatetimeIndex(pd.to_datetime(fmp_df[cols["date"]])).tz_localize(None).normalize()
        f = pd.Series(fmp_df[cols["close"]].to_numpy(dtype=float), index=idx)
        a = pd.Series(bars.c, index=pd.DatetimeIndex(bars.days))
        r = (f[~f.index.duplicated()] / a[~a.index.duplicated()]).dropna()
        r = r[np.isfinite(r) & (r > 0)].sort_index()
        if not len(r):
            return None
        return cls(r.index.to_numpy().astype("datetime64[D]"), r.to_numpy(dtype=float))

    def step(self, on: date, others: Sequence[date]) -> Tuple[Optional[float], bool]:
        """``(r(D) / r(D-1), stable)`` with D-1 = the last common session before ``on`` and D the first on/after it;
        ``(None, True)`` when there is none on a side.  ``stable`` is False when r varies by more than STEP_STABLE
        within the 3 common sessions before D or the 3 from D on -- unless another event lies in those sessions (then
        only D-1 and D count)."""
        pos = int(np.searchsorted(self.days, np.datetime64(on, "D"), side="left"))
        if pos < 1 or pos >= len(self.days):
            return None, True
        if int((self.days[pos] - self.days[pos - 1]).astype(int)) > MAX_GAP_DAYS:
            return None, True
        lo, hi = max(0, pos - 3), min(len(self.days), pos + 3)
        waived = any(self.days[lo] <= np.datetime64(o, "D") <= self.days[hi - 1] for o in others)
        stable = waived or all(side.max() / side.min() - 1.0 <= STEP_STABLE
                               for side in (self.vals[lo:pos], self.vals[pos:hi]))
        return float(self.vals[pos] / self.vals[pos - 1]), stable


def _step_vote(step: Optional[float], stable: bool, f: Optional[float]) -> str:
    """none (no FMP data on both sides) | unstable | equals_f (|ln step - ln f| <= ln 1.02) | flat | other"""
    if step is None:
        return "none"
    if not stable:
        return "unstable"
    ls = math.log(step)
    if f is not None and abs(ls - math.log(f)) <= STEP_TOL:
        return "equals_f"
    if abs(ls) <= STEP_TOL:
        return "flat"
    return "other"


def _fmt_step(step: Optional[float], vote: str) -> str:
    return "none" if step is None else f"{step:.6g} {vote}"


def _nearest_clean_ratio(step: float) -> Optional[float]:
    """The clean ratio (k or 1/k for an integer k in 2..CLEAN_K_MAX, or 3/2, 2/3) within CLEAN_TOL (0.5 %) of ``step``."""
    cands = [1.5, 2.0 / 3.0]
    x = step if step >= 1.0 else 1.0 / step
    for k in {int(x), int(x) + 1}:
        if 2 <= k <= CLEAN_K_MAX:
            cands.append(float(k) if step >= 1.0 else 1.0 / k)
    ok = [c for c in cands if abs(math.log(step) - math.log(c)) <= CLEAN_TOL]
    return min(ok, key=lambda c: abs(math.log(step) - math.log(c))) if ok else None


def _same_ratio(a: float, b: float) -> bool:
    return abs(a / b - 1.0) <= RATIO_TOL


def _jump_idx(bars: _Bars, i: int) -> Optional[float]:
    """close(D-1) / open(D) for the session at index ``i``; None when the two bars do not observe an overnight
    (no bar before it, or the two are more than MAX_GAP_DAYS apart)."""
    if i <= 0 or i >= len(bars.days):
        return None
    if int((bars.days[i] - bars.days[i - 1]).astype(int)) > MAX_GAP_DAYS:
        return None
    return float(bars.c[i - 1] / bars.o[i])


def _first_session_on_or_after(bars: _Bars, on: date) -> int:
    return int(np.searchsorted(bars.days, np.datetime64(on, "D"), side="left"))


def _row(sym: str, d: date, a: Optional[float], f: Optional[float], jump: Optional[float], verdict: str,
         applied: bool, reason: str, step: str = "") -> Dict[str, Any]:
    return {"symbol": sym, "event_date": d.isoformat(), "alpaca_ratio": a, "fmp_ratio": f,
            "jump": None if jump is None else round(jump, 6), "fmp_step": step, "verdict": verdict,
            "applied": applied, "reason": reason}


def _cluster(listings: List[Tuple[str, ListedSplit, int]]) -> List[List[Tuple[str, ListedSplit, int]]]:
    """Group (source, listing, position) by position: members at most NEAR_SESSIONS apart (chained) are one event."""
    listings = sorted(listings, key=lambda m: (m[2], m[1].date))
    groups: List[List[Tuple[str, ListedSplit, int]]] = []
    for m in listings:
        if groups and m[2] - groups[-1][-1][2] <= NEAR_SESSIONS:
            groups[-1].append(m)
        else:
            groups.append([m])
    return groups


def _units(groups: List[List[Tuple[str, ListedSplit, int]]]) -> List[List[Tuple[str, ListedSplit, int]]]:
    """Events to decide.  A cluster where each vendor lists at most one ratio is ONE event (the vendors may disagree on
    its ratio: that is a mismatch to review).  A cluster where a vendor lists several ratios holds several distinct
    events (AMC 2023-08: a 1.13333 distribution and the 1:10 reverse split), so it splits into one event per ratio."""
    out: List[List[Tuple[str, ListedSplit, int]]] = []
    for grp in groups:
        per_vendor = [_distinct([m[1].ratio for m in grp if m[0] == v]) for v in ("alpaca", "fmp")]
        if all(len(r) <= 1 for r in per_vendor):
            out.append(grp)
            continue
        classes = _distinct([m[1].ratio for m in grp])
        for rep in classes:
            out.append([m for m in grp if _same_ratio(m[1].ratio, rep)])
    return out


def _distinct(ratios: List[float]) -> List[float]:
    out: List[float] = []
    for r in ratios:
        if not any(_same_ratio(r, o) for o in out):
            out.append(r)
    return out


class _EventContext:
    """What the decision of ONE event unit reads: the symbol's bars and FMP ratio series, the unit's listed dates, the first
    session on/after each, the raw jump there, and the other listed events (they waive the step's stability check)."""

    def __init__(self, bars: _Bars, rser: Optional[_RatioSeries], step_on: bool, dates: Sequence[date],
                 others: Sequence[date]):
        self.bars, self.rser, self.step_on, self.dates, self.others = bars, rser, step_on, list(dates), list(others)
        self.n = len(bars.days)
        self.idxs = {d: _first_session_on_or_after(bars, d) for d in self.dates}
        self.jumps = {d: _jump_idx(bars, i) for d, i in self.idxs.items()}

    def observable(self) -> bool:
        """some listed date has a bar before it within MAX_GAP_DAYS"""
        return any(j is not None for j in self.jumps.values())

    @property
    def jump_contra(self) -> bool:
        """every observable jump is within CONTRADICT_TOL of 1"""
        return all(j is None or abs(math.log(j)) <= CONTRADICT_TOL for j in self.jumps.values())

    def proving(self, ratio: float) -> Optional[date]:
        """the SESSION whose overnight jump proves ``ratio`` (big events only): a listed date first, then the sessions up
        to NEAR_SESSIONS either side of it (a vendor's ex-date can sit a day or two off the jump)"""
        if abs(math.log(ratio)) < BIG_MIN:
            return None
        for off in (0, 1, -1, 2, -2, 3, -3):
            for d in self.dates:
                i = self.idxs[d] + off
                j = _jump_idx(self.bars, i)
                if j is not None and abs(math.log(j) - math.log(ratio)) <= PROVEN_TOL:
                    return self.bars.days[i].astype(object)
        return None

    def shown(self, d: Optional[date] = None) -> float:
        if d is not None:                                      # a proving session: its own jump
            return _jump_idx(self.bars, _first_session_on_or_after(self.bars, d))
        return next(j for j in self.jumps.values() if j is not None)

    def step_str(self, ratio: Optional[float], d: date) -> str:
        if self.rser is None:
            return "none" if self.step_on else ""
        v, ok = self.rser.step(d, self.others)
        return _fmt_step(v, _step_vote(v, ok, ratio))

    def step_proving(self, ratio: float, guard: bool = False, both: bool = False) -> Optional[date]:
        """the session whose FMP step equals ``ratio`` (big events only), searched like ``proving``; with ``guard`` also the
        raw jump there must (i) be within STEP_JUMP_MAX of ``ratio`` and not within CONTRADICT_TOL of 1, or (ii) ``both``
        vendors list it and the jump is closer to ``ratio`` than to 1 in log terms"""
        if self.rser is None or abs(math.log(ratio)) < BIG_MIN:
            return None
        for off in (0, 1, -1, 2, -2, 3, -3):
            for d in self.dates:
                i = self.idxs[d] + off
                if 0 <= i < self.n:
                    day = self.bars.days[i].astype(object)
                    v, ok = self.rser.step(day, self.others)
                    if _step_vote(v, ok, ratio) == "equals_f":
                        j = _jump_idx(self.bars, i)
                        if guard and not (j is not None and (
                                (abs(math.log(j) - math.log(ratio)) <= STEP_JUMP_MAX and abs(math.log(j)) > CONTRADICT_TOL)
                                or (both and abs(math.log(j) - math.log(ratio)) < abs(math.log(j))))):
                            continue
                        return day
        return None

    def step_flat(self) -> bool:
        """the FMP step is flat at every listed date that has FMP data on both sides (and at one at least)"""
        if self.rser is None:
            return False
        votes = []
        for d in self.dates:
            v, ok = self.rser.step(self.bars.days[self.idxs[d]].astype(object), self.others)
            votes.append(_step_vote(v, ok, None))
        return "flat" in votes and all(x in ("flat", "none") for x in votes)


def decide_splits(raw_daily: Mapping[str, pd.DataFrame],
                  alpaca_events: Iterable[ListedSplit],
                  fmp_events: Iterable[ListedSplit],
                  overrides: Sequence[Mapping[str, Any]],
                  stats: Optional[Counter] = None,
                  fmp_daily: Optional[Mapping[str, pd.DataFrame]] = None,
                  breaks: Optional[Dict[str, List[str]]] = None,
                  spinoffs: Iterable[SpinOff] = (),
                  yahoo: Optional[Callable[[str], Mapping[str, Any]]] = None,
                  spinco_frame: Optional[Callable[[str], Optional[pd.DataFrame]]] = None,
                  missing_spincos: Optional[set] = None
                  ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Judge every candidate event by the raw price jump across it, and by the FMP step when ``fmp_daily`` is given.

    ``raw_daily``: symbol -> frame with ``t`` (bar start), ``o``, ``c`` of RAW bars.  Events: current symbols.
    Returns ``(accepted, review_rows)``: accepted = ``[{symbol, date, factor, source, verdict}]`` sorted;
    review rows have REVIEW_COLUMNS.  Everything set aside or decided is counted in ``stats`` (verdict_<code>,
    outside_range, dup_listings_collapsed, events_symbol_not_in_store, overrides_unused, ...).

    ``fmp_daily``: symbol -> FMP daily frame (``Date``, ``Close`` ADJUSTED).  None = the FMP-step arbiter is off (every
    vote is "none"); a symbol missing from it is counted in ``fmp_daily_missing_symbol`` and votes "none".
    ``spinoffs``: the Alpaca spin-offs; each becomes an accepted event of ``kind`` "spinoff" with a COMPUTED factor (or a review
    row), see SPIN-OFFS in the module docstring.  ``yahoo``: ``symbol -> {"available": bool, "ticker": answering ticker, "splits": [[iso date, ratio], ...]}``, the
    confirmation source of the UNLISTED (inferred) splits only; None = no Yahoo, every inferred candidate is
    ``yahoo_unavailable``.  ``spinco_frame``: ``symbol -> daily frame or None``, read for a spun-off company whose bars exist in the store
    but that is not among ``raw_daily`` (a subset run); ``missing_spincos``: filled with the real spun-off tickers that still
    have no bars anywhere (the ``spinco_symbols_missing.txt`` list).  ``breaks``: filled with ``{symbol: [session date, ...]}`` of the series breaks (verdict ``series_break``)."""
    stats = stats if stats is not None else Counter()
    by_sym: Dict[str, List[Tuple[str, ListedSplit]]] = {}
    for src, evs in (("alpaca", alpaca_events), ("fmp", fmp_events)):
        for ev in evs:
            if not (math.isfinite(ev.ratio) and ev.ratio > 0):
                raise ValueError(f"unusable ratio in {src} listing {ev!r}")
            if ev.symbol not in raw_daily:
                stats[f"events_symbol_not_in_store_{src}"] += 1
                continue
            if abs(math.log(ev.ratio)) < 1e-9:
                stats[f"events_ratio_one_{src}"] += 1
                continue
            by_sym.setdefault(ev.symbol, []).append((src, ev))
    ov_by_sym: Dict[str, List[Mapping[str, Any]]] = {}
    for o in overrides:
        sym = _norm_symbol(o["symbol"])
        if sym not in raw_daily:
            stats["overrides_unused"] += 1
            continue
        ov_by_sym.setdefault(sym, []).append(o)

    so_by_sym: Dict[str, List[SpinOff]] = {}
    for so in spinoffs:
        if so.symbol not in raw_daily:
            stats["spinoffs_symbol_not_in_store"] += 1
            continue
        so_by_sym.setdefault(so.symbol, []).append(so)
    bars_cache: Dict[str, Optional[_Bars]] = {}

    def bars_of(sym: str) -> Optional[_Bars]:
        if sym not in bars_cache:
            frame = raw_daily[sym] if sym in raw_daily else (spinco_frame(sym) if spinco_frame is not None else None)
            bars_cache[sym] = _bars(frame, stats, sym) if frame is not None else None
        return bars_cache[sym]

    accepted: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    for sym in sorted(raw_daily):
        bars = bars_of(sym)
        rser = None
        if fmp_daily is not None:
            if sym in fmp_daily:
                rser = _RatioSeries.build(bars, fmp_daily[sym])
            if rser is None:
                stats["fmp_daily_missing_symbol"] += 1
        a, r = _decide_symbol(sym, bars, by_sym.get(sym, []), ov_by_sym.get(sym, []), stats, rser,
                              fmp_daily is not None, breaks, so_by_sym.get(sym, []), bars_of, yahoo,
                              missing_spincos)
        accepted += a
        rows += r
    accepted.sort(key=lambda x: (x["symbol"], x["date"]))
    rows.sort(key=lambda x: (x["symbol"], x["event_date"], x["verdict"]))
    return accepted, rows


def _decide_symbol(sym: str, bars: _Bars, listed: List[Tuple[str, ListedSplit]], overrides: List[Mapping[str, Any]],
                   stats: Counter, rser: Optional[_RatioSeries] = None, step_on: bool = False,
                   breaks: Optional[Dict[str, List[str]]] = None, spinoffs: Sequence[SpinOff] = (),
                   bars_of: Optional[Callable[[str], Optional[_Bars]]] = None,
                   yahoo: Optional[Callable[[str], Mapping[str, Any]]] = None,
                   missing_spincos: Optional[set] = None
                   ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    acc: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    n = len(bars.days)

    def accept(d: date, f: float, source: str, verdict: str, step: str = "", kind: str = "split") -> None:
        stats[f"verdict_{verdict}"] += 1
        j = _jump_idx(bars, _first_session_on_or_after(bars, d)) if n else None
        acc.append({"symbol": sym, "date": d.isoformat(), "kind": kind, "factor": f, "source": source, "verdict": verdict,
                    "jump": None if j is None else round(j, 6), "fmp_step": step, "absorbed": "", "absorbed_factor": None})

    def review(d: date, a: Optional[float], fm: Optional[float], jump: Optional[float], verdict: str,
               applied: bool, reason: str, count: bool = True, step: str = "") -> None:
        if count:
            stats[f"verdict_{verdict}"] += 1
        rows.append(_row(sym, d, a, fm, jump, verdict, applied, reason, step))

    # every listed date of the symbol: an event of ANOTHER unit near a step's sessions waives the stability check
    listed_dates = sorted({ev.date for _, ev in listed})
    ov_dates = [date.fromisoformat(o["event_date"]) for o in overrides]

    # position of each listing; a listing the bars cannot be affected by is counted, not judged
    placed: List[Tuple[str, ListedSplit, int]] = []
    for src, ev in listed:
        if n == 0:
            placed.append((src, ev, ev.date.toordinal()))
            continue
        first, last = bars.days[0].astype(object), bars.days[-1].astype(object)
        if ev.date <= first or ev.date > last:
            stats[f"outside_range_{src}"] += 1
            continue
        placed.append((src, ev, _first_session_on_or_after(bars, ev.date)))

    units = _units(_cluster(placed))
    consumed: set = set()                                # ids of the listing groups a spin-off decision already settled
    pending_absorb: List[Tuple[SpinOff, float, int, date]] = []
    composed_ids: set = set()                            # ids of the accepted dicts of a composed split + spin-off pair

    def idx_of(a: Mapping[str, Any]) -> int:
        return _first_session_on_or_after(bars, date.fromisoformat(a["date"]))

    def yahoo_lists(ratio: float, i0: int) -> bool:
        """Yahoo lists a split within NEAR_SESSIONS sessions of session ``i0`` with a ratio within 0.5 % (CLEAN_TOL)"""
        y = yahoo(sym) if yahoo is not None else None
        return bool(y is not None and y["available"]
                    and any(abs(_first_session_on_or_after(bars, date.fromisoformat(yd)) - i0) <= NEAR_SESSIONS
                            and abs(math.log(float(yr)) - math.log(ratio)) <= CLEAN_TOL for yd, yr in y["splits"]))

    # ---- spin-offs: the factor is COMPUTED, f = 1 + (new_rate / source_rate) * spinco_close(D) / parent_close(D), D = the
    # ex-date session, spinco_close = the close of the spinco's first session on/after D within NEAR_SESSIONS sessions.  The
    # jump and big-factor rules do not apply.  FMP's step across D is a cross-check, not the source.
    so_positions: List[int] = []
    so_applied: List[Tuple[int, float]] = []          # (position, f) of the applied ones
    so_dates = sorted({so.date for so in spinoffs})
    so_ov_used: set = set()
    for so in spinoffs:
        has_bars = bars_of is not None and so.spinco != sym and bars_of(so.spinco) is not None
        if is_cvr_warrant_right(so.security or so.spinco, sym, has_bars):
            stats["verdict_spinoff_not_adjusted_cvr_warrant"] += 1      # a CVR / warrant / right: not adjusted, no row
            continue
        if not has_bars and so.spinco != sym and missing_spincos is not None:
            missing_spincos.add(so.spinco)                             # a real ticker the store still lacks
        if n == 0 or so.date <= bars.days[0].astype(object) or so.date > bars.days[-1].astype(object):
            stats["outside_range_spinoff"] += 1
            continue
        iD = _first_session_on_or_after(bars, so.date)
        D = bars.days[iD].astype(object)
        so_positions.append(iD)
        ov_so = next((o for o in overrides if _ov_kind(o) == "spinoff" and date.fromisoformat(o["event_date"]) in (so.date, D)), None)
        if ov_so is not None:                         # a reviewed override decides this spin-off outright
            so_ov_used.add(id(ov_so))
            if ov_so["decision"] == "accept":
                accept(D, float(ov_so["ratio"]), "override", "override_accept", "", kind="spinoff")
                so_applied.append((iD, float(ov_so["ratio"])))
                consumed.update(id(g) for g in units if any(abs(m[2] - iD) <= NEAR_SESSIONS for m in g)
                                and not any(_ov_kind(o) == "split" and date.fromisoformat(o["event_date"]) in {m[1].date for m in g}
                                            for o in overrides))
            else:
                stats["verdict_override_reject"] += 1
            review(D, None, None, None, "override_" + ov_so["decision"], ov_so["decision"] == "accept",
                   f"reviewed spin-off override ({ov_so['reviewed_by']}) of {so.spinco} (ratio {so.ratio:.6g}): {ov_so['reason']}",
                   count=False)
            continue
        sc = bars_of(so.spinco) if bars_of is not None and so.spinco != sym else None   # spinco == parent: a reused / renamed ticker
        k = int(np.searchsorted(sc.days, bars.days[iD])) if sc is not None else 0
        if sc is None or k >= len(sc.days) or sc.days[k] > bars.days[min(iD + NEAR_SESSIONS, n - 1)]:
            if so.spinco != sym and rser is not None:
                # no price for the spun-off company (an OTC name): FMP's step, when it is NOT flat (and above 1), is the factor
                v, ok = rser.step(D, [x for x in listed_dates + ov_dates + so_dates if x != D])
                if v is not None and ok and _step_vote(v, ok, None) == "other" and v > 1.0:
                    # FMP's step IS this spin-off's factor, so it supports the spin-off alone: a split a vendor lists on the same
                    # ex-date is that spin-off listed as a split (FTI 1.344 vs the 1.2216 step), adjusted ONCE
                    absorbed = [g for g in units if id(g) not in consumed and any(abs(m[2] - iD) <= NEAR_SESSIONS for m in g)
                                and not any(_ov_kind(o) == "split" and date.fromisoformat(o["event_date"]) in {m[1].date for m in g}
                                            for o in overrides)]
                    consumed.update(id(g) for g in absorbed)
                    stats["listings_absorbed_by_spinoff"] += sum(len(g) for g in absorbed)
                    accept(D, v, "fmp_step", "spinoff_from_fmp_step", _fmt_step(v, "other"), kind="spinoff")
                    so_applied.append((iD, v))
                    review(D, None, None, None, "spinoff_from_fmp_step", True,
                           f"spin-off of {so.spinco} (ratio {so.ratio:.6g}) has no {so.spinco} price; FMP's step {v:.6g} across "
                           f"the ex-date is used as the factor", count=False, step=_fmt_step(v, "other"))
                    continue
            review(D, None, None, None, "spinoff_no_spinco_price", False,
                   f"spin-off of {so.spinco} (ratio {so.ratio:.6g}): no {so.spinco} bar within {NEAR_SESSIONS} sessions of "
                   f"the ex-date (or the spinco's current ticker is the parent's own), the factor cannot be computed", step="")
            continue
        pc = float(sc.c[k]) / float(bars.c[iD])
        f = 1.0 + so.ratio * pc
        # ---- a split LISTED within NEAR_SESSIONS sessions of the ex-date: one price event, never both counted.  Candidates
        # (FMP's step across the date must be within 10 % of one, decisively): the split alone, the spin-off alone, or the
        # two composed -- the spin-off rate then converted to the POST-split share basis (rate / split ratio: Honeywell's 1 HONA
        # per 2 HON, after the 1:2 reverse split, is 1 per new share).  Units a reviewed override covers are left to it.
        near = [g for g in units if id(g) not in consumed and any(abs(m[2] - iD) <= NEAR_SESSIONS for m in g)
                and not any(_ov_kind(o) == "split" and date.fromisoformat(o["event_date"]) in {m[1].date for m in g}
                            for o in overrides)]

        def on_another_session(g) -> bool:
            """a listed split whose own price step lies on ANOTHER session than the spin-off's (an ADR ratio change the day
            after: UL) is a separate event: the normal rules put it on the session that moved."""
            uc = _EventContext(bars, rser, step_on, sorted({m[1].date for m in g}), [])
            for r in _distinct([m[1].ratio for m in g]):
                dd = uc.proving(r) or uc.step_proving(r)
                if dd is not None and dd != D:
                    return True
                if rser is not None:                          # a small ratio (8:9) has no jump proof: FMP's step on a nearby session
                    for off in (1, -1, 2, -2, 3, -3):
                        i = iD + off
                        if 0 <= i < n:
                            sv, sok = rser.step(bars.days[i].astype(object), [])
                            if sv is not None and sok and abs(math.log(sv) - math.log(r)) <= STEP_TOL:
                                return True
            return False
        apart = {id(g) for g in near if on_another_session(g)}
        stats["splits_on_another_session_than_a_spinoff"] += len(apart)
        near = [g for g in near if id(g) not in apart]
        if near:
            members = [m for g in near for m in g]
            R = _distinct([m[1].ratio for m in members if m[0] == "alpaca"]) or _distinct([m[1].ratio for m in members])
            if any(abs(math.log(r) - math.log(f)) <= STEP_TOL for r in R):
                # a vendor lists the spin-off itself as a split (GE, MMM): adjusted ONCE, as the spin-off
                stats["listings_absorbed_by_spinoff"] += len(members)
                consumed.update(id(g) for g in near)
            else:
                cands = []                                        # (kind, step it predicts, split ratio, spin-off factor)
                for r in R:
                    sprime = 1.0 + (so.ratio / r) * pc
                    cands.append(("split", r, r, None))
                    cands.append(("composed", r * sprime, r, sprime))
                cands.append(("spinoff", f, None, f))
                v, ok = rser.step(D, [x for x in listed_dates + ov_dates + so_dates if x != D]) if rser is not None else (None, True)
                evidence = "FMP's step"
                if v is None:                                     # no FMP data on both sides: the raw jump stands in (VGR)
                    v = _jump_idx(bars, iD)
                    evidence = "the raw jump (no FMP data)"
                labels = {"split": 0, "composed": 1, "spinoff": 2}
                chosen = _pick(v, [(f"{c[0]}:{i}", c[1]) for i, c in enumerate(cands)]) if (v is not None and ok) else None
                agree_n = sum(1 for c in cands if v is not None and ok and abs(math.log(v) - math.log(c[1])) <= SPINOFF_FMP_TOL)
                if chosen is None:
                    consumed.update(id(g) for g in near)
                    review(D, None, None, None, "split_spinoff_conflict", False,
                           f"listed split(s) {R} and the spin-off of {so.spinco} (factor {f:.6g}, ratio {so.ratio:.6g}) fall on "
                           f"one ex-date; {evidence} {('none' if v is None else format(v, '.6g'))} agrees with "
                           + ("none of" if agree_n == 0 else "several of") + " the candidates (split alone, spin-off alone, composed "
                           "with the spin rate on the post-split basis): NEITHER is applied (decide it with an override)",
                           step=_fmt_step(v, "other") if v is not None else "none")
                    continue
                kind, _target, r_pick, s_pick = cands[int(chosen.split(":")[1])]
                step_txt = f"{v:.6g} agrees with {kind} (target {_target:.6g}; {evidence})"
                if kind == "split":
                    pending_absorb.append((so, f, iD, D))        # the units stay with the loop below; the spin-off is absorbed
                    continue
                consumed.update(id(g) for g in near)
                if kind == "composed":
                    sd = min(m[1].date for m in members if m[0] == "alpaca" or not any(mm[0] == "alpaca" for mm in members)
                             if abs(math.log(m[1].ratio) - math.log(r_pick)) <= RATIO_TOL)
                    accept(bars.days[_first_session_on_or_after(bars, sd)].astype(object), r_pick, "alpaca", "split_composed_with_spinoff",
                           step_txt)
                    composed_ids.add(id(acc[-1]))
                    accept(D, s_pick, "alpaca_spinoff", "spinoff_composed_with_split", step_txt, kind="spinoff")
                    composed_ids.add(id(acc[-1]))
                    so_applied.append((iD, s_pick))
                    review(D, None, None, None, "spinoff_composed_with_split", True,
                           f"split {r_pick:.6g} and spin-off of {so.spinco} on one ex-date, composed: spin rate {so.ratio:.6g} per old "
                           f"share = {so.ratio / r_pick:.6g} per new share, factor {s_pick:.6g}; {evidence} {v:.6g} vs split x spin-off "
                           f"{_target:.6g}", count=False, step=step_txt)
                    continue
                stats["listings_absorbed_by_spinoff"] += len(members)         # kind == "spinoff": the evidence supports the spin-off alone
        step = "none" if step_on else ""
        vote = "none"
        if rser is not None:
            v, ok = rser.step(D, [x for x in listed_dates + ov_dates + so_dates if x != D])
            vote = _step_vote(v, ok, None)                      # flat / other / none / unstable
            if vote != "flat" and v is not None and ok and abs(math.log(v) - math.log(f)) <= SPINOFF_FMP_TOL:
                vote = "equals_f"                                # within 10 %: gaps and jumps around the ex-date explain it
                step = f"{v:.6g} agrees (deviation {v / f - 1.0:+.1%} from f)"
            else:
                step = _fmt_step(v, vote)
        info = (f"spin-off of {so.spinco} (ratio {so.ratio:.6g}), factor {f:.6g} = 1 + ratio * {so.spinco} close "
                f"{float(sc.c[k]):.6g} / {sym} close {float(bars.c[iD]):.6g}; FMP step vote {vote}")
        if vote == "other":
            review(D, None, None, None, "spinoff_fmp_mismatch", False,
                   info + ": FMP adjusted by a different factor, not applied", step=step)
            continue
        verdict = {"equals_f": "spinoff_fmp_agrees", "flat": "spinoff_fmp_flat"}.get(vote, "spinoff_fmp_unverified")
        accept(D, f, "alpaca_spinoff", verdict, step, kind="spinoff")
        so_applied.append((iD, f))
        if verdict != "spinoff_fmp_agrees":
            review(D, None, None, None, verdict, True, info + (": FMP did not adjust it, applied anyway"
                                                               if vote == "flat" else ": FMP cannot confirm it"),
                   count=False, step=step)

    ov_used = set()
    ov_positions: List[int] = []
    event_positions: List[int] = []
    for grp in units:
        event_positions += [m[2] for m in grp]
        if id(grp) in consumed:
            continue
        dates = sorted({m[1].date for m in grp})
        # ---- override: decides the event outright
        matches = [o for o in overrides if _ov_kind(o) == "split" and date.fromisoformat(o["event_date"]) in dates]
        if len(matches) > 1:
            raise SplitTableBuildError(f"{sym}: overrides {[o['event_date'] for o in matches]} all match the listings around "
                                       f"{dates[0]}: ambiguous, keep one entry per event")
        if matches:
            ov = matches[0]
            stats["listings_absorbed_by_override"] += len(grp)
            what = "; ".join(f"{m[0]} lists {m[1].ratio:.6g} on {m[1].date}" for m in grp)
            if id(ov) in ov_used:                     # the same event listed twice (e.g. with an inverted ratio): decided once
                continue
            ov_used.add(id(ov))
            if ov["decision"] == "accept":
                accept(date.fromisoformat(ov["event_date"]), float(ov["ratio"]), "override", "override_accept")
            else:
                stats["verdict_override_reject"] += 1
            review(date.fromisoformat(ov["event_date"]), None, None, None, "override_" + ov["decision"],
                   ov["decision"] == "accept", f"reviewed override ({ov['reviewed_by']}) decides the listing(s) [{what}]: {ov['reason']}",
                   count=False)
            continue
        if any(abs(m[2] - p) <= NEAR_SESSIONS and abs(math.log(m[1].ratio) - math.log(f)) <= STEP_TOL
               for m in grp for p, f in so_applied):
            # a vendor lists the applied spin-off as a split (HON, NVS, IAC): it is adjusted ONCE, as the spin-off
            stats["listings_absorbed_by_spinoff"] += len(grp)
            continue
        a_all = _distinct([m[1].ratio for m in grp if m[0] == "alpaca"])
        f_all = _distinct([m[1].ratio for m in grp if m[0] == "fmp"])
        stats["dup_listings_collapsed"] += len(grp) - len(a_all) - len(f_all)
        d0 = dates[0]
        a_r = a_all[0] if a_all else None
        f_r = f_all[0] if f_all else None
        if n == 0:
            review(d0, a_r, f_r, None, "no_price_data", False, "the symbol has no usable bars")
            continue
        ctx = _EventContext(bars, rser, step_on, dates, [d for d in listed_dates if d not in dates] + ov_dates)
        if not ctx.observable():
            review(d0, a_r, f_r, None, "no_price_data", False,
                   f"no bars on both sides within {MAX_GAP_DAYS} days of the event")
            continue

        if a_r is not None and f_r is not None and not _same_ratio(a_r, f_r):
            pa, pf = ctx.proving(a_r), ctx.proving(f_r)
            if (pa is None) != (pf is None):
                d, r, src = (pa, a_r, "alpaca") if pa is not None else (pf, f_r, "fmp")
                accept(d, r, src, "ratio_mismatch_resolved", ctx.step_str(r, d))
                review(d, a_r, f_r, ctx.shown(d), "ratio_mismatch_resolved", True,
                       f"vendors disagree ({src} ratio {r:.6g} is the one the jump proves)", count=False,
                       step=ctx.step_str(r, d))
                continue
            sa, sf = ctx.step_proving(a_r), ctx.step_proving(f_r)
            if (sa is None) != (sf is None) and not ctx.jump_contra:
                d, r, src = (sa, a_r, "alpaca") if sa is not None else (sf, f_r, "fmp")
                accept(d, r, src, "ratio_mismatch_resolved_by_fmp_step", ctx.step_str(r, d))
                review(d, a_r, f_r, ctx.shown(d), "ratio_mismatch_resolved_by_fmp_step", True,
                       f"vendors disagree ({src} ratio {r:.6g} is the one FMP's step equals; the jump does not contradict it)",
                       count=False, step=ctx.step_str(r, d))
            else:
                review(d0, a_r, f_r, ctx.shown(), "ratio_mismatch", False,
                       "vendors list different ratios and the jump proves " + ("both" if pa is not None else "neither"),
                       step=ctx.step_str(a_r, d0))
            continue

        ratio = a_r if a_r is not None else f_r
        src = "alpaca" if a_r is not None else "fmp"
        both = a_r is not None and f_r is not None
        if abs(math.log(ratio)) < BIG_MIN:
            if both:
                sday = None                                  # the session (D first, then +-3) where FMP's own series steps by it
                if ctx.rser is not None:
                    for off in (0, 1, -1, 2, -2, 3, -3):
                        i = ctx.idxs[d0] + off
                        if 0 <= i < ctx.n:
                            day = bars.days[i].astype(object)
                            sv, sok = ctx.rser.step(day, ctx.others)
                            if _step_vote(sv, sok, ratio) == "equals_f":
                                sday = day
                                break
                if sday is not None:
                    if sday != d0 and sday not in dates:
                        stats["splits_moved_to_jump_session"] += 1    # ADR ratio change one session after the listed date
                    accept(sday, ratio, "both", "small_both", ctx.step_str(ratio, sday))
                elif yahoo_lists(ratio, ctx.idxs[d0]):
                    accept(d0, ratio, "both+yahoo", "small_confirmed_by_yahoo", ctx.step_str(ratio, d0))
                elif ctx.jumps[d0] is not None and abs(math.log(ctx.jumps[d0] / ratio)) < abs(math.log(ctx.jumps[d0])):
                    # the raw jump is exactly the factor, closer to f than to 1 (BBDO 1.101 vs f 1.1), though FMP's series is flat
                    accept(d0, ratio, "both", "small_both_jump_agrees", ctx.step_str(ratio, d0))
                else:
                    review(d0, a_r, f_r, ctx.shown(), "small_both_price_disagrees", False,
                           f"small ratio {ratio:.6g} listed by both vendors, but FMP's own series does not step by it, Yahoo "
                           f"does not list it and the raw jump is not closer to it than to 1: NOT applied", step=ctx.step_str(ratio, d0))
            else:
                if yahoo_lists(ratio, ctx.idxs[d0]):
                    # the price cannot prove a small ratio and only one vendor lists it, but Yahoo lists it too (0.5 %)
                    accept(d0, ratio, f"{src}+yahoo", "small_confirmed_by_yahoo", ctx.step_str(ratio, d0))
                    continue
                review(d0, a_r, f_r, ctx.shown(), "small_single_vendor", False,
                       f"small ratio {ratio:.6g}: the price cannot prove it and only {src} lists it",
                       step=ctx.step_str(ratio, d0))
            continue
        pd_ = ctx.proving(ratio)
        if pd_ is not None:
            if pd_ not in dates:
                stats["splits_moved_to_jump_session"] += 1           # no vendor date is the session that jumped (ADR: D+1)
            if both or src == "alpaca":
                accept(pd_, ratio, "both" if both else "alpaca", "proven", ctx.step_str(ratio, pd_))
            else:
                accept(pd_, ratio, "fmp", "fmp_only_proven", ctx.step_str(ratio, pd_))
                review(pd_, a_r, f_r, ctx.shown(pd_), "fmp_only_proven", True,
                       "Alpaca missed this split; the jump proves it", count=False, step=ctx.step_str(ratio, pd_))
            continue
        ps = ctx.step_proving(ratio, guard=True, both=both)
        if ps is None and both and abs(math.log(ratio)) >= BOTH_BIG_MIN:
            j = ctx.shown()
            if abs(math.log(j) - math.log(ratio)) <= STEP_JUMP_MAX:       # both vendors list it, the jump is within x1.5
                accept(d0, ratio, "both", "both_listed_jump_within_1_5", ctx.step_str(ratio, d0))
                continue
        if ps is None:
            pe = ctx.step_proving(ratio)
            if pe is not None:                       # FMP applied it, but the raw bars barely show it: not applied
                review(pe, a_r, f_r, ctx.shown(pe), "fmp_step_without_jump", False,
                       f"FMP's step equals the listed ratio {ratio:.6g} but the raw jump {ctx.shown(pe):.4g} does not support it "
                       f"(not within x{math.exp(STEP_JUMP_MAX):.2g} of it, nor, for a both-vendor listing, closer to it than to 1): "
                       f"a separation or spin-off adjustment, not a split",
                       step=ctx.step_str(ratio, pe))
                continue
        if ps is not None:
            if ps not in dates:
                stats["splits_moved_to_jump_session"] += 1
            accept(ps, ratio, "both" if both else src, "proven_by_fmp_step", ctx.step_str(ratio, ps))
            if not both and src == "fmp":
                review(ps, a_r, f_r, ctx.shown(ps), "proven_by_fmp_step", True,
                       "Alpaca missed this split; FMP's step equals it (the jump alone does not prove it)",
                       count=False, step=ctx.step_str(ratio, ps))
            continue
        if ctx.jump_contra:
            if ctx.step_flat():
                stats["verdict_rejected_confirmed"] += 1       # price flat in the raw bars AND in FMP's: no split
                continue
            v = "both_contradicted" if both else f"{src}_contradicted"
            review(d0, a_r, f_r, ctx.shown(), v, False,
                   f"listed ratio {ratio:.6g} but the raw price does not jump (jump {ctx.shown():.4g})",
                   step=ctx.step_str(ratio, d0))
        else:
            review(d0, a_r, f_r, ctx.shown(), "unresolved", False,
                   f"listed ratio {ratio:.6g}, jump {ctx.shown():.4g}: neither proves nor contradicts it",
                   step=ctx.step_str(ratio, d0))

    # ---- overrides no listing led to: an 'accept' ADDS the split, a 'reject' silences the jump row at its date
    for o in overrides:
        if _ov_kind(o) == "spinoff":
            if id(o) not in so_ov_used:
                stats["spinoff_overrides_unused"] += 1       # no spin-off listing at that date: counted, never silent
            continue
        if id(o) in ov_used:
            continue
        d = date.fromisoformat(o["event_date"])
        if n:
            ov_positions.append(_first_session_on_or_after(bars, d))
        if o["decision"] == "accept":
            accept(d, float(o["ratio"]), "override", "override_accept")
        else:
            stats["verdict_override_reject"] += 1
        review(d, None, None, None, "override_" + o["decision"], o["decision"] == "accept",
               f"reviewed override ({o['reviewed_by']}), no vendor lists it: {o['reason']}", count=False)
    positions = event_positions + ov_positions + so_positions

    # ---- two big accepts in the same direction within NEAR_SESSIONS sessions would adjust one split twice (the
    # vendors list slightly different ratios for one event and each is "proven"): retract both, for a human
    if n:
        big = [a for a in acc if a["kind"] == "split" and a["source"] != "override" and abs(math.log(a["factor"])) >= BIG_MIN]
        big_idx = {id(a): _first_session_on_or_after(bars, date.fromisoformat(a["date"])) for a in big}
        retract = set()
        for i, x in enumerate(big):
            for y in big[i + 1:]:
                if (abs(big_idx[id(x)] - big_idx[id(y)]) <= NEAR_SESSIONS
                        and (x["factor"] > 1) == (y["factor"] > 1)):
                    retract.update((id(x), id(y)))
        for a in [a for a in big if id(a) in retract]:
            acc.remove(a)
            stats[f"verdict_{a['verdict']}"] -= 1
            rows[:] = [r for r in rows if not (r["applied"] and r["event_date"] == a["date"]
                                               and r["verdict"] == a["verdict"])]       # its "applied" row
            review(date.fromisoformat(a["date"]), a["factor"], None, None, "overlapping_accepts", False,
                   f"accepted ratio {a['factor']:.6g} lies within {NEAR_SESSIONS} sessions of another accepted big "
                   f"split in the same direction: one split would be applied twice")

    # ---- a split and a spin-off within NEAR_SESSIONS sessions are ONE price event: never apply both.  Keep the SPLIT, absorb
    # the spin-off (recorded in provenance), when the split is an override, or when the FMP step agrees (within 10 %) with
    # the split alone or with split x spin-off; otherwise refuse both into the review file.
    for so, f_so, iD, D in pending_absorb:           # FMP's step supports the listed SPLIT alone: keep it, absorb the spin-off
        x = next((a for a in acc if a["kind"] == "split" and abs(idx_of(a) - iD) <= NEAR_SESSIONS), None)
        if x is None:
            review(D, None, None, None, "split_spinoff_conflict", False,
                   f"FMP's step supports the listed split alone, but no split was accepted at the spin-off of {so.spinco} "
                   f"(factor {f_so:.6g}): neither is applied", step="")
            continue
        x["absorbed"] = f"spin-off of {so.spinco} factor {f_so:.6g} on {D.isoformat()} (FMP's step supports the split alone)"
        x["absorbed_factor"] = f_so
        review(D, None, None, None, "spinoff_absorbed_by_split", True,
               f"the spin-off of {so.spinco} (factor {f_so:.6g}) and the split {x['factor']:.6g} ({x['verdict']}) fall on the same "
               f"ex-date: ONE event is applied, the split (FMP's step supports it alone)", step=x["fmp_step"])
    if n and any(a["kind"] == "spinoff" for a in acc):
        for sp in [a for a in acc if a["kind"] == "spinoff" and id(a) not in composed_ids]:
            for x in [a for a in acc if a["kind"] == "split" and id(a) not in composed_ids and idx_of(a) == idx_of(sp)]:
                if sp not in acc:
                    break
                if sp["source"] == "override" and x["source"] == "override":
                    continue                       # BOTH reviewed (ENOV 2022-04-05): independent events, both applied as decided
                xd, sd = date.fromisoformat(x["date"]), date.fromisoformat(sp["date"])
                v, ok = rser.step(xd, [d for d in listed_dates + ov_dates + so_dates if d != xd]) if rser is not None else (None, True)
                if v is None:
                    v = _jump_idx(bars, idx_of(x))
                pick = None
                if v is not None and ok:
                    pick = _pick(v, [("split", x["factor"]), ("spinoff", sp["factor"]),
                                     ("composed", x["factor"] + sp["factor"] - 1.0)])
                if x["source"] == "override":
                    pick = "split"                                   # a reviewed override on the split decides it
                if pick == "split":
                    acc.remove(sp)
                    stats[f"verdict_{sp['verdict']}"] -= 1
                    rows[:] = [r for r in rows if not (r["applied"] and r["event_date"] == sp["date"]
                                                        and r["verdict"] == sp["verdict"])]
                    x["absorbed"] = f"spin-off factor {sp['factor']:.6g} on {sp['date']} ({sp['verdict']})"
                    x["absorbed_factor"] = sp["factor"]
                    review(sd, None, None, None, "spinoff_absorbed_by_split", True,
                           f"the spin-off (factor {sp['factor']:.6g}) and the split {x['factor']:.6g} ({x['verdict']}) fall on the "
                           f"same ex-date: ONE event is applied, the split; "
                           + ("it is a reviewed override" if x["source"] == "override" else "FMP's step supports it"),
                           step=x["fmp_step"])
                elif pick == "spinoff":                              # the evidence supports the spin-off alone: the split was it
                    acc.remove(x)
                    stats[f"verdict_{x['verdict']}"] -= 1
                    rows[:] = [r for r in rows if not (r["applied"] and r["event_date"] == x["date"] and r["verdict"] == x["verdict"])]
                    review(xd, None, None, None, "split_absorbed_by_spinoff", False,
                           f"the split {x['factor']:.6g} ({x['verdict']}) and the spin-off {sp['factor']:.6g} fall on the same "
                           f"ex-date and the evidence supports the spin-off alone: the split listing is that spin-off, NOT applied",
                           step=x["fmp_step"])
                elif pick == "composed":                             # both are real: the spin rate on the post-split basis
                    sp["factor"] = 1.0 + (sp["factor"] - 1.0) / x["factor"]
                    composed_ids.update((id(x), id(sp)))
                    review(sd, None, None, None, "spinoff_composed_with_split", True,
                           f"split {x['factor']:.6g} and spin-off on one ex-date, composed: spin factor {sp['factor']:.6g} on the "
                           f"post-split basis", count=False, step=sp["fmp_step"])
                else:
                    acc.remove(sp)
                    acc.remove(x)
                    stats[f"verdict_{sp['verdict']}"] -= 1
                    stats[f"verdict_{x['verdict']}"] -= 1
                    rows[:] = [r for r in rows if not (r["applied"] and r["event_date"] in (sp["date"], x["date"])
                                                        and r["verdict"] in (sp["verdict"], x["verdict"]))]
                    review(sd, None, None, None, "split_spinoff_conflict", False,
                           f"a split {x['factor']:.6g} ({x['verdict']}) and a spin-off {sp['factor']:.6g} ({sp['verdict']}) fall on "
                           f"the same ex-date and the evidence supports none of them alone or composed (or several): NEITHER is "
                           f"applied (decide it with an override)", step=x["fmp_step"])
        so_applied = [(idx_of(a), a["factor"]) for a in acc if a["kind"] == "spinoff"]

    # ---- one event is adjusted ONCE: a split near an applied spin-off with (about) the spin-off's factor is a double adjustment
    for a in acc:
        if a["kind"] == "split" and n:
            ia = _first_session_on_or_after(bars, date.fromisoformat(a["date"]))
            for p, f in so_applied:
                if abs(ia - p) <= NEAR_SESSIONS and abs(math.log(a["factor"]) - math.log(f)) <= STEP_TOL:
                    raise SplitTableBuildError(f"{sym}: the split {a['factor']:.6g} on {a['date']} ({a['verdict']}) and the "
                                               f"spin-off factor {f:.6g} are the same event: refusing to adjust it twice")
    positions += [_first_session_on_or_after(bars, date.fromisoformat(a["date"])) for a in acc if a["kind"] == "split" and n]

    # ---- unexplained raw overnight jumps with no event (split, spin-off, override) within NEAR_SESSIONS sessions
    if n > 1:
        ratio_ = bars.c[:-1] / bars.o[1:]
        observed = np.diff(bars.days).astype(int) <= MAX_GAP_DAYS      # a pair further apart than that observes no overnight
        for k in np.flatnonzero(((ratio_ > JUMP_HI) | (ratio_ < JUMP_LO)) & (observed | (np.abs(np.log(ratio_)) >= SERIES_BREAK))):
            t = int(k) + 1
            if any(abs(t - p) <= NEAR_SESSIONS for p in positions):
                continue
            day = bars.days[t].astype(object)
            gap = not observed[k]
            candidate_row = False
            step = "none" if step_on else ""
            if rser is not None:
                v, ok = rser.step(day, listed_dates + ov_dates + so_dates)
                vote = _step_vote(v, ok, None)
                if vote == "flat":                            # FMP's adjusted/raw ratio did not move: a real price move
                    stats["verdict_jump_explained_by_market"] += 1
                    continue
                step = _fmt_step(v, vote)
                clean = _nearest_clean_ratio(v) if (v is not None and vote != "unstable") else None
                if clean is not None and not gap and abs(math.log(float(ratio_[k])) - math.log(clean)) <= STEP_JUMP_MAX:
                    # a CANDIDATE unlisted split: FMP applied a clean ratio (within 0.5 %) and the raw jump is within x1.5 of
                    # it.  A split that NEITHER vendor list contains is suspicious (an FMP data error turns a market gap
                    # into a "split"): only Yahoo listing it (within 3 sessions, ratio within 1 %) makes it one.
                    y = yahoo(sym) if yahoo is not None else None
                    what = (f"no vendor lists it; FMP applied {clean:.6g} (step {v:.6g}) and the raw jump {ratio_[k]:.4g} is "
                            f"within x{math.exp(STEP_JUMP_MAX):.2g} of it")
                    if y is None or not y["available"]:
                        review(day, None, None, float(ratio_[k]), "yahoo_unavailable", False,
                               what + "; Yahoo could not be consulted, so it is NOT applied", step=step)
                        candidate_row = True
                    elif any(abs(_first_session_on_or_after(bars, date.fromisoformat(yd)) - t) <= NEAR_SESSIONS
                             and _same_ratio(float(yr), clean) for yd, yr in y["splits"]):
                        accept(day, clean, "fmp_step+yahoo", "inferred_confirmed_by_yahoo", step)
                        review(day, None, None, float(ratio_[k]), "inferred_confirmed_by_yahoo", True,
                               what + f"; Yahoo ({y['ticker']}) lists a {clean:.6g} split within {NEAR_SESSIONS} sessions",
                               count=False, step=step)
                        continue
                    else:
                        review(day, None, None, float(ratio_[k]), "inferred_unconfirmed", False,
                               what + f"; Yahoo ({y['ticker']}) lists no such split: NOT applied (an FMP data error?)",
                               step=step)
                        candidate_row = True
            if abs(math.log(float(ratio_[k]))) >= SERIES_BREAK:
                # beyond x5 and nothing above explains it: a spliced series (reused ticker, new equity after a bankruptcy);
                # this includes pairs across a data gap longer than MAX_GAP_DAYS
                review(day, None, None, float(ratio_[k]), "series_break", False,
                       f"overnight close/open {ratio_[k]:.4g} beyond x{math.exp(SERIES_BREAK):.2g}"
                       f"{' across a data gap' if gap else ''}, unlisted and not explained by FMP's step: the series is "
                       f"spliced here; only the segment after the last break is used", step=step)
                if breaks is not None:
                    breaks.setdefault(sym, []).append(day.isoformat())
                continue
            if candidate_row:
                continue                                      # the candidate's own row already explains it
            review(day, None, None, float(ratio_[k]), "unexplained_jump", False,
                   f"overnight close/open {ratio_[k]:.4g} with no listed split within {NEAR_SESSIONS} sessions",
                   step=step)
    return acc, rows


def splits_by_symbol(accepted: Sequence[Mapping[str, Any]], kind: str = "split") -> Dict[str, List[List[Any]]]:
    """``{symbol: [[date, factor], ...]}`` of the accepted events of one ``kind`` ("split" or "spinoff"), sorted by date
    (the split_table.json layout)."""
    out: Dict[str, List[List[Any]]] = {}
    for a in sorted((x for x in accepted if x["kind"] == kind), key=lambda x: (x["symbol"], x["date"])):
        rows = out.setdefault(a["symbol"], [])
        if rows and rows[-1][0] == a["date"]:
            raise SplitTableBuildError(f"{a['symbol']}: two accepted events on {a['date']} ({rows[-1][1]} and {a['factor']}): "
                                       f"decide them with an override")
        rows.append([a["date"], a["factor"]])
    return out


# ---------------------------------------------------------------------------
# comparison with FMP's split list
# ---------------------------------------------------------------------------
COMPARE_COLUMNS = ["symbol", "date", "our_factor", "fmp_ratio", "alpaca_ratio", "jump", "fmp_step", "verdict", "class"]
COMPARE_DAYS = 5          # an FMP listing and one of our events this many calendar days apart are the same event


def near(d1: date, d2: date) -> bool:
    return abs((d1 - d2).days) <= COMPARE_DAYS


def compare_with_fmp(raw_daily: Mapping[str, pd.DataFrame], accepted: Sequence[Mapping[str, Any]],
                     review_rows: Sequence[Mapping[str, Any]], alpaca_events: Iterable[ListedSplit],
                     fmp_events: Iterable[ListedSplit],
                     fmp_daily: Optional[Mapping[str, pd.DataFrame]] = None) -> List[Dict[str, Any]]:
    """One row per (symbol, event) across our table and FMP's list (symbols of ``raw_daily`` only).

    class: ``agree`` (ours and FMP, ratios within 1 %), ``ratio_differs``, ``ours_only`` (applied, FMP does not list
    it), ``fmp_only_accepted`` (ours and FMP, and Alpaca does not list it: Alpaca missed it), ``fmp_only_rejected``
    (FMP lists it, we did not apply it; ``verdict`` = our review verdict, ``outside_range`` for an event no bar
    can show, ``rejected_confirmed`` when no row exists and FMP's step is flat there, else ``no_row``).  ``jump`` =
    close(D-1)/open(D) at the event, when observable; ``fmp_step`` = FMP step value and vote (see _RatioSeries)."""
    stats: Counter = Counter()
    by_sym: Dict[str, Dict[str, list]] = {}
    for key, evs in (("fmp", fmp_events), ("alp", alpaca_events)):
        for e in evs:
            if e.symbol in raw_daily:
                dup = any(near(x.date, e.date) and _same_ratio(x.ratio, e.ratio)
                          for x in by_sym.get(e.symbol, {"fmp": [], "alp": []})[key])
                if dup:                                  # a vendor's duplicate listing is one event
                    continue
                by_sym.setdefault(e.symbol, {"fmp": [], "alp": [], "ours": [], "rev": []})[key].append(e)
    spin_dates: Dict[str, List[date]] = {}
    for a in accepted:
        if a["kind"] == "spinoff":
            spin_dates.setdefault(a["symbol"], []).append(date.fromisoformat(a["date"]))
            continue
        by_sym.setdefault(a["symbol"], {"fmp": [], "alp": [], "ours": [], "rev": []})["ours"].append(a)
    for r in review_rows:
        by_sym.setdefault(r["symbol"], {"fmp": [], "alp": [], "ours": [], "rev": []})["rev"].append(r)

    out: List[Dict[str, Any]] = []
    for sym, g in sorted(by_sym.items()):
        bars = _bars(raw_daily[sym], stats, sym)
        rser = (_RatioSeries.build(bars, fmp_daily[sym]) if fmp_daily is not None and sym in fmp_daily else None)
        all_dates = ([e.date for e in g["fmp"]] + [e.date for e in g["alp"]]
                     + [date.fromisoformat(o["date"]) for o in g["ours"]])

        def step(d: date, f: Optional[float]) -> Tuple[str, str]:
            if rser is None:
                return "none", "none"
            v, ok = rser.step(d, [x for x in all_dates if x != d])
            vote = _step_vote(v, ok, f)
            return _fmt_step(v, vote), vote

        def jump(d: date) -> Optional[float]:
            if not len(bars.days) or d <= bars.days[0].astype(object) or d > bars.days[-1].astype(object):
                return None
            j = _jump_idx(bars, _first_session_on_or_after(bars, d))
            return None if j is None else round(j, 6)

        def alp_near(d: date) -> Optional[float]:
            return next((e.ratio for e in g["alp"] if near(e.date, d)), None)

        free_fmp = list(g["fmp"])
        for o in g["ours"]:
            od = date.fromisoformat(o["date"])
            m = next((e for e in free_fmp if near(e.date, od)), None)
            if m is not None:
                free_fmp.remove(m)
            a_r = alp_near(od)
            if m is None:
                cls = "ours_only"
            elif not _same_ratio(o["factor"], m.ratio):
                cls = "ratio_differs"
            elif a_r is None:
                cls = "fmp_only_accepted"
            else:
                cls = "agree"
            out.append({"symbol": sym, "date": o["date"], "our_factor": o["factor"],
                        "fmp_ratio": None if m is None else m.ratio, "alpaca_ratio": a_r, "jump": jump(od),
                        "fmp_step": step(od, o["factor"])[0], "verdict": o["verdict"], "class": cls})
        for m in free_fmp:
            row = next((r for r in g["rev"] if near(date.fromisoformat(r["event_date"]), m.date)
                        and not r["verdict"].startswith("spinoff_")), None)
            if row is not None:
                verdict = row["verdict"]
            elif any(near(m.date, sd) for sd in spin_dates.get(sym, [])):
                verdict = "explained_by_spinoff"          # FMP lists the spin-off as a split; adjusted once, as a spin-off
            elif len(bars.days) and (m.date <= bars.days[0].astype(object) or m.date > bars.days[-1].astype(object)):
                verdict = "outside_range"
            else:
                verdict = "rejected_confirmed" if step(m.date, m.ratio)[1] == "flat" else "no_row"
            out.append({"symbol": sym, "date": m.date.isoformat(), "our_factor": None, "fmp_ratio": m.ratio,
                        "alpaca_ratio": alp_near(m.date), "jump": jump(m.date),
                        "fmp_step": step(m.date, m.ratio)[0], "verdict": verdict,
                        "class": "fmp_only_rejected"})
    out.sort(key=lambda r: (r["symbol"], r["date"]))
    return out
