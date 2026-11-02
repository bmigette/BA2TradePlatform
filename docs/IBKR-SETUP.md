# Interactive Brokers (IBKR) setup

The IBKR account talks to **IB Gateway** (or Trader Workstation) over IBKR's socket API through the
`ib_async` library. The platform never logs in to IBKR itself: you run and log in to the Gateway, and the
platform connects to it.

> **Status: untested against a real IBKR account.** The adapter was built without one (design and every
> unverified assumption: `docs/plans/2026-10-03-ibkr-support-design.md`). Treat the first runs as a
> verification exercise on a PAPER account, with `read_only` on, and run `tools/ibkr_paper_smoke.py`
> first (section 5).

## 1. IB Gateway in paper-trading mode

1. Create/enable a **paper trading** account in IBKR Client Portal (Settings > Paper Trading). Its login
   and account id are separate from the live ones; the account id starts with **`DU`**.
2. Install **IB Gateway** (the stable or latest build) and log in with the *paper* credentials, selecting
   **IB API** mode and **Paper Trading**. (TWS works too; the ports differ, below.)
3. In the Gateway: **Configure > Settings > API > Settings**
   - tick **Enable ActiveX and Socket Clients**;
   - **Socket port**: leave the default (**4002** Gateway paper, **7497** TWS paper; live is 4001 / 7496);
   - **Read-Only API**: tick it for the first runs (IB then refuses every order; the platform also has its
     own `read_only` setting);
   - **Trusted IPs**: add `127.0.0.1` (and the address of the machine running the platform if it is a
     different one; the Gateway otherwise asks for a manual confirmation of every new client);
   - leave **Allow connections from localhost only** ticked when the platform runs on the same machine.
4. **Auto restart / re-login.** IB Gateway restarts itself once a day (Configure > Settings > Lock and Exit:
   *Auto restart*, default around midnight New York) and needs a full re-login about weekly (Sunday) unless
   you use a tool such as IBC. While it is down the platform sees failed reads (`None`/empty with an ERROR
   log) and refuses orders (the order row goes to `ERROR`); it reconnects by itself on the next call after
   a 15 s cooldown. Orders resting at IBKR (GTC stops) are unaffected.
5. **Market data.** The adapter requests *frozen/live* data and **refuses delayed data**: a missing market
   data subscription means "no price", never a stale one. A paper account shares the market-data
   subscriptions of the live account that owns it; US stock quotes need the relevant subscription and
   options need OPRA. Fractional-share trading must be enabled for the account if you want fractions.

## 2. Add the account in the platform

Settings > Account Settings > add account > provider **IBKR**:

| Setting | Meaning |
|---|---|
| `host` | Gateway host, default `127.0.0.1` |
| `port` | `4002` Gateway paper (default), `4001` Gateway live, `7497` TWS paper, `7496` TWS live |
| `client_id` | API client id (default 1). **One session per id.** Use a different id for each platform instance and for the smoke script; the platform never changes it for you. Id `0` is IBKR's "master" client (the only one that can see/cancel orders typed in TWS by hand). |
| `account_id` | Your IBKR account id (`DU...` paper). Required: a login can manage several accounts and the platform will not guess |
| `paper_account` | Ticked by default. **Safety rail**: ticked means the account id must start with `DU`, unticked means it must not. A mismatch refuses to connect, so a forgotten tick can never route a live account |
| `read_only` | Ticked by default: every order/cancel is refused before it is sent. Untick to trade |
| `flex_token`, `flex_query_id` | Optional, see section 4 |

Each account definition owns **one connection** that is shared by every part of the platform (the trade
manager, the UI, the lifecycle jobs). Changing the host/port/client id/account/flags in the dialog replaces
it; no restart needed.

Margin and sizing: the platform reads `AvailableFunds` (not IBKR's own 4x day-trading `BuyingPower`) and
applies a Reg-T 2x multiplier only when IBKR's figures show a margin account; `margin_enabled` is still
your switch. Portfolio-margin accounts are treated as Reg-T (conservative).

## 3. What works, and what does not

Works (against the fake; confirm with the smoke script): equity market/limit/stop/stop-limit orders, bracket
protection (a take-profit and a stop-limit in one OCA group, moved in place when the price changes),
cancel/modify, partial fills, refresh from IBKR's open/completed orders and executions, shorts (needs
shortable and easy to borrow), fractional shares for symbols IBKR publishes a sub-share step for, option
chains/quotes/greeks, single-leg and multi-leg (combo) option orders, option positions.

Not available, by IBKR's API rather than by choice:

- **Dividends, cash transfers and balance (NAV) history** need an IBKR **Flex query** (section 4). Without
  it those pages are empty and the log says so once.
- **Option assignment / exercise / expiry feed**: the TWS API has none, so the platform learns of an
  assignment only from the option position vanishing and the shares appearing. The Alpaca-style settlement
  bookkeeping is not done. See the design doc, section 7.3.
- **Executions older than about 7 days** are not returned by the API (fills history is partial).
- Option `volume` is not reported (IBKR streams today's partial volume, not the prior session's), so
  volume-gated option strategies will see "unknown".

## 4. Optional: Flex query for dividends / cash / NAV

In Client Portal: Performance & Reports > Flex Queries > create an **Activity Flex Query** containing
**Cash Transactions** and **Equity Summary by Report Date in Base**, period "Last 365 calendar days",
format XML. Create a **Flex Web Service token** (same page, Flex Web Service Configuration). Put the token
and the query id in the account's `flex_token` / `flex_query_id`. Statements are cached for 10 minutes.
`python tools/ibkr_paper_smoke.py --flex-token T --flex-query Q` fetches and prints what was parsed.

## 5. Verify on paper before trusting it

```
python tools/ibkr_paper_smoke.py                 # read-only; prints every unverified IBKR fact as [CHECK]
python tools/ibkr_paper_smoke.py --option --symbol AAPL
python tools/ibkr_paper_smoke.py --dump-executions
python tools/ibkr_paper_smoke.py --place-test-order   # PAPER ONLY: 1-share limit at half price, placed then cancelled
```

The script always connects **read-only first**; `--place-test-order` reconnects writable only after the
account id is confirmed to start with `DU`, and refuses (exit 2) otherwise. Use a client id no running
platform instance uses (default 98). Send back the "Summary: facts to confirm" block: each line is an
assumption the design rests on (margin multiplier heuristic, fractional eligibility fields, shortable
scale, option contract/OCC mapping, greeks and open-interest ticks, what-if margin figures).

## 6. Troubleshooting

| Symptom | Cause |
|---|---|
| `cannot connect to IBKR ... clientId=N` | Gateway not running/logged in, API not enabled, wrong port, host not trusted |
| `error 326 ... client id already in use` | another session (a second platform, the smoke script, TWS itself) holds that `client_id`; pick another |
| `paper_account=True but account 'U...' is a LIVE account` | the flag and the account disagree; fix the flag (this is the safety rail working) |
| `account ... is not among the accounts this IBKR login manages` | wrong `account_id` for this login |
| orders end `ERROR` with `[unauthorized] ... read-only` | `read_only` is ticked (or the Gateway's Read-Only API is) |
| positions/orders read `None`/empty with ERROR logs | Gateway down or in its daily restart; it recovers by itself |
| no prices | no market-data subscription, or only delayed data (refused by design) |
| a `WARNING ... blocking a thread that is running an asyncio loop` | a UI handler called the account synchronously; wrap it in `run.io_bound` |
