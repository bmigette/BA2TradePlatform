# Equity short selling — design

Status: **implemented on `feat/short-selling` (2026-09-25)**; approved by the operator 2026-09-24.

## Implemented differences

What landed differs from the sections below in these points:

- **Closes are gated by the permission that opened the position** (operator, 2026-09-25): a sell
  closing a long needs `enable_buy`, a buy covering a short needs `enable_sell`. One reader,
  `trading_permission`, serves every path.
- **Close percent:** sell and buy take an optional percent (1–100, default 100) when they close.
  It rounds to whole shares and is refused on a partly filled or fractional position. A percent
  rule re-trims every time it matches (100 → 50 → 25), so its conditions must stop it.
- **TP/SL direction comes from the position**, not the recommendation. Of the 7 exposed
  forward-test backtests, only 1030 (+0.44 CAR) and 1439 (−1.28 CAR) moved. The live prod account
  is unaffected (bt1330 re-runs identical).
- **Same-pass TP/SL adjusts are skipped** on a transaction the pass closed or reduced, on option
  transactions, and on the symbol when a close raised or its outcome is uncertain. This removed a
  stray SHARE stop that O_CC wrote onto its covered-call option transaction; stored O_CC runs may
  change where it fired.
- **Borrow cost exemption:** short stock from an assigned short call, queued for next-open
  liquidation, is not charged, so stored option runs stay identical.
- **Open BT/live gap:** a backtest does not rebase the stop to the fill price, as live does. It is
  a known item, row 6 of
  [the engine unification plan](2026-07-02-live-backtest-engine-unification.md).

## Goal and hard constraint

Let rules open short equity positions in live trading and in backtests.

**No current rule, expert or stored backtest may change behaviour.** The data backs this up:
- prod has 0 of 32 rules with a `sell` action, dev 0 of 76;
- every live expert has `enable_sell=false`;
- the test DB has 0 of 701 backtests, 0 of 181 optimizations and 0 of 669 strategies with a sell
  entry or `enable_short`;
- `allow_hedging` is false on every stored prod instance, and dev has none.

## 1. Semantics: sell is sell, as at the broker

| Order | Position held | Result |
|---|---|---|
| `sell` | long | Reduces or closes the long. Capped at the position size, so it never flips to short. |
| `sell` | flat | **Opens a short**, only if the expert's `enable_sell` is true. Otherwise it is refused, as today, with a clear message. |
| `buy` | short | Covers or reduces the short. Never flips to long. |
| `buy` | flat or long | Unchanged. |
| `close` | short | Already buys to cover (`close_transaction`). |

- **Permission gates (operator, 2026-09-25):** closing is gated by the permission that opened the
  position.

  | Order | Position | Allowed when |
  |---|---|---|
  | sell | flat | `enable_sell` on (opens a short) |
  | sell | long | `enable_buy` on (close, or reduce by an optional percent 1–100, default 100) |
  | buy | flat | `enable_buy` on (opens a long, unchanged) |
  | buy | short | `enable_sell` on (cover, or reduce by an optional percent) |

  No account-level setting is added. The old inert "sell with enable_sell off on a long" behaviour
  is replaced; no live or stored rule used it.
- **TP/SL direction (bug found 2026-09-25):** adjustments take long vs short from the POSITION'S side,
  and use the recommendation only when there is no position. Before, a SELL bar on a held long
  computed a short-style stop. Up to 220 stored backtests were exposed; the operator accepted that
  they change.
- **Backtests:** the existing `enable_short` config keeps feeding `enable_sell` (`deploy_parity`),
  so the long/short seed rulesets and the pullback_rsi short jobs start working.
- **Already short-ready, and reused as is:**
  - sizing and safeguard stops: symmetric in `position_sizing` and `TradeRiskManagement`;
  - Alpaca `_target_exit_spec` (stop above, TP below for a short);
  - TastyTrade `SELL_TO_OPEN`;
  - the backtest account's short lots and margin call on shorts.
- **The only blocker** is `SellAction.execute` (TradeActions.py ~442-510), which refuses without a
  long.

## 2. Netting rule replaces `allow_hedging`

A US equity account nets positions per symbol, so "hedging" (a long and a short in the same symbol
at once) cannot exist; Alpaca already raises on it (AlpacaAccount.py ~1407/1410).
- **Remove `allow_hedging` everywhere:**
  - the UI checkbox;
  - the interface setting (MarketExpertInterface.py ~213);
  - the Smart Risk Manager prompt text (SmartRiskManagerGraph.py ~710-757, ~2339-2351);
  - its toolkit guard (SmartRiskManagerToolkit.py ~2132).
- **One rule everywhere,** in the rule engine and the Smart Risk Manager: an order in the opposite
  direction to an open position only reduces or closes it. A new opposite position opens only from
  flat.
- **Stored `allow_hedging` values** stay in the DB and are ignored.
- **The Smart Risk Manager** can already open live shorts via `open_sell_position`, gated on
  `enable_sell`, which is consistent with section 1.

## 3. Broker checks (live)

- **Alpaca:** before submitting an order that OPENS a short, check the asset is `shortable` and
  `easy_to_borrow`. If not, refuse loudly with the reason; never let the broker reject silently
  downstream.
- **TastyTrade:** already submits sell-to-open; broker short approval applies.
- **The wash-trade lock** already treats opposing orders symmetrically; it is unchanged.

## 4. Backtest cost

- **Borrow cost:** a flat annual rate, charged daily on open short market value, defaulting to
  **0.5%/yr** (easy-to-borrow large caps). It is configurable per run (`short_borrow_rate_pa`) and
  echoed in results; it must survive the trial-config whitelist.
- **Reporting:** borrow cost is its own results line, separate from spread.
- **Stored backtests:** a long-only run has zero shorts, so its results stay byte-identical.

## 5. Tests and parity

- **No impact:** a stored long-only backtest re-run gives byte-identical trades and equity.
- **Backtest engine:**
  - a short opens from flat on a bearish signal;
  - its stop sits above the entry and its TP below;
  - a stop-out, a cover and borrow charged daily.
- **Live path** (TradeManager plus a fake Alpaca account): the same short entry, with the same
  protective legs.
- **Netting, in both paths:**
  - a sell while long reduces or closes and never flips;
  - a buy while short covers and never flips;
  - a sell from flat with `enable_sell` off is refused.
- **Alpaca shortability:** a stock that is not shortable is refused before submit.
- **Smart Risk Manager:** the toolkit applies the netting rule, and no `allow_hedging` remains.
- **pullback_rsi:** the short-jobs xfail flips to a pass, and the preflight refusal is removed.
- **Docs:** fix `rules_documentation.py`'s SELL text and the settings help.
