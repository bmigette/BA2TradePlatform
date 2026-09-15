"""Historical audit probes; all HTTP is mocked and all cache data is temporary.

Recorded before commit 317276ba changed screener reuse and empty-response caching.
Assertions describe the pre-fix behavior, not current regression expectations;
see fmp_live_cache_audit_2026-09-08.md for the snapshot and subsequent changes.
"""
from __future__ import annotations

import ast
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def main():
    with tempfile.TemporaryDirectory(prefix="fmp-cache-audit-") as temporary:
        work = Path(temporary)
        os.environ.update(BA2_HOME=str(work), CACHE_FOLDER=str(work / "cache"),
                          DB_FILE=str(work / "unused.sqlite"), LOG_FOLDER=str(work / "logs"),
                          BA2_FILE_LOGGING="0", BA2_STDOUT_LOGGING="0")
        for path in (ROOT, ROOT / "packages/common", ROOT / "packages/providers", ROOT / "packages/experts"):
            sys.path.insert(0, str(path))
        import pandas as pd
        from ba2_common.core import native_cache
        from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider
        import ba2_providers.fmp_common as fc
        mdp = importlib.import_module("ba2_common.core.interfaces.MarketDataProviderInterface")
        senate = importlib.import_module("ba2_experts.FMPSenateTraderWeight")
        ds = importlib.import_module("ba2_experts.DeterministicScorer.data")
        screen = importlib.import_module("ba2_providers.StockScreener")
        assert Path(native_cache.CACHE_FOLDER) == work / "cache"

        class Clock(datetime):
            current = datetime(2026, 8, 30, 12)

            @classmethod
            def now(cls, tz=None):
                return cls.current.replace(tzinfo=timezone.utc).astimezone(tz) if tz else cls.current

            @classmethod
            def utcnow(cls):
                return cls.current

        def frame(days, closes=None):
            if closes is None:
                closes = [100.] * len(days)
            return pd.DataFrame({"Date": pd.to_datetime(days, utc=True), "Open": closes,
                                 "High": [v + 1 for v in closes], "Low": [v - 1 for v in closes],
                                 "Close": closes, "Volume": [100] * len(days)})

        def seed(symbol, rows, age_hours=48, interval="1d"):
            out = rows.copy()
            out["effective_date"] = out["Date"]
            native_cache.write_timeseries("FMPOHLCVProvider", symbol, interval, out)
            path = native_cache.find_timeseries_path("FMPOHLCVProvider", symbol, interval)
            stamp = (Clock.current - timedelta(hours=age_hours)).timestamp()
            os.utime(path, (stamp, stamp))
            return path

        result = {}
        # Any accidental real HTTP, including an unexpected lower-level path, aborts.
        with patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected real HTTP")), \
                patch.object(mdp, "datetime", Clock):
            p = FMPOHLCVProvider(api_key="audit-fake-key")
            seed("FRESH", frame(["2026-08-28"]), age_hours=0)
            with patch.object(p, "_get_ohlcv_data_impl", side_effect=AssertionError("Unexpected warm fetch")):
                for _ in range(3):
                    assert len(p.get_ohlcv_data("FRESH", interval="1d")) == 1
            result["fresh_daily_cache"] = {"reads": 3, "http_calls": 0}

            seed("WEEKEND", frame(["2026-08-28"]))
            calls = []
            def empty_response(url, params, **kwargs):
                calls.append({k: v for k, v in params.items() if k != "apikey"})
                return SimpleNamespace(json=lambda: {"historical": []})
            with patch.object(fc, "fmp_http_get", side_effect=empty_response), patch("time.sleep"):
                for _ in range(3):
                    p.get_ohlcv_data("WEEKEND", interval="1d")
            assert len(calls) == 9
            result["weekend_empty_topup"] = {"reads": 3, "http_calls": len(calls), "windows": calls}

            Clock.current = datetime(2026, 8, 31, 10)
            path = seed("PARTIAL", frame(["2026-08-28"]))
            calls = []
            def partial_daily(symbol, start, end, interval):
                calls.append({"start": str(start), "end": str(end)})
                return frame([Clock.current.date().isoformat()], [101. if Clock.current.day == 31 else 201.])
            with patch.object(p, "_get_ohlcv_data_impl", side_effect=partial_daily):
                p.get_ohlcv_data("PARTIAL", interval="1d")
                os.utime(path, (Clock.current.timestamp(), Clock.current.timestamp()))
                Clock.current = datetime(2026, 8, 31, 17)
                evening = p.get_ohlcv_data("PARTIAL", interval="1d")
                assert len(calls) == 1  # today's morning partial is still served
                Clock.current = datetime(2026, 9, 1, 17)
                following = p.get_ohlcv_data("PARTIAL", interval="1d")
                old = following[following.Date.dt.date == datetime(2026, 8, 31).date()]
                assert old.Close.iloc[0] == 101. and len(calls) == 2
            result["partial_daily_never_revisited"] = {"http_fetches": calls,
                "evening_cached_close": float(evening.Close.iloc[-1]),
                "prior_day_close_after_next_refresh": float(old.Close.iloc[0]),
                "observation": "Refresh starts the following day, so morning partial remains in history."}

            seed("LEGACY", frame(["2026-08-31", "2026-09-01"]), age_hours=0)
            ranges = []
            def legacy_fetch(symbol, start, end, interval):
                ranges.append((end - start).days)
                return frame(["2026-08-31", "2026-09-01"])
            with patch.object(p, "_get_ohlcv_data_impl", side_effect=legacy_fetch):
                p.get_data("LEGACY", interval="1d", lookback_days=30)
            assert len(ranges) == 1 and ranges[0] > 1000
            result["legacy_get_data_ignores_warm_parquet"] = {"fetch_spans_days": ranges}

            # A later date needs fresher data, but DS only checks the left edge.
            ds.reset_caches()
            source_calls = []
            def daily_source(**kwargs):
                source_calls.append(str(kwargs["end_date"]))
                return frame([ds._utcnow().date().isoformat()])
            bundle = SimpleNamespace(ohlcv=lambda: SimpleNamespace(get_ohlcv_data=daily_source))
            with patch.object(ds, "_utcnow", return_value=datetime(2026, 8, 31, 21, tzinfo=timezone.utc)):
                ds.fetch_ohlcv(bundle, "STALE", None)
            with patch.object(ds, "_utcnow", return_value=datetime(2026, 9, 2, 21, tzinfo=timezone.utc)):
                stale = ds.fetch_ohlcv(bundle, "STALE", None)
            assert len(source_calls) == 1 and stale.Date.iloc[-1].date() == datetime(2026, 8, 31).date()
            result["deterministic_scorer_live_memo_never_refreshes"] = {
                "requests_two_days_apart": 2, "provider_calls": len(source_calls),
                "latest_returned_bar": str(stale.Date.iloc[-1])}

            # A real prewarmed JSON exists, but the live helper bypasses it.
            hist_dir = work / "cache/fmp_history"
            hist_dir.mkdir(parents=True)
            disk_history = [{"date": "2026-08-31", "open": 10.}]
            (hist_dir / "historical_price_full__AUDIT.json").write_text(json.dumps(disk_history))
            seed("AUDIT", frame(["2026-08-31"]), age_hours=0)
            price_calls = []
            payload = [{"date": "2026-08-31", "open": 11.}]
            def senate_http(url, params, **kwargs):
                price_calls.append({k: v for k, v in params.items() if k != "apikey"})
                return SimpleNamespace(json=lambda: {"historical": list(payload)})
            def expert():
                ex = senate.FMPSenateTraderWeight.__new__(senate.FMPSenateTraderWeight)
                ex._api_key = "audit-fake-key"
                ex.logger = logging.getLogger("audit-senate")
                return ex
            with patch.object(fc, "fmp_http_get", side_effect=senate_http):
                senate.clear_price_map_memo()
                ex = expert()
                assert ex._get_price_at_date("AUDIT", datetime(2026, 8, 31)) == 11.
                payload.append({"date": "2026-09-01", "open": 12.})
                new_date = expert()._get_price_at_date("AUDIT", datetime(2026, 9, 1))
                assert new_date is None and len(price_calls) == 1
                result["senate_live_bypasses_disk_and_pins_map"] = {
                    "warmed_json_and_parquet": True, "network_requests": list(price_calls),
                    "later_published_date_returned": new_date}

                # Execute only the old method from the commit before Sep 5's memo change.
                old_text = subprocess.run(["git", "-c", f"safe.directory={ROOT.as_posix()}", "show",
                    "eb4d4c71^:packages/experts/ba2_experts/FMPSenateTraderWeight.py"],
                    capture_output=True, text=True, check=True).stdout
                node = next(n for n in ast.walk(ast.parse(old_text))
                            if isinstance(n, ast.FunctionDef) and n.name == "_get_price_at_date")
                code = ast.Module(body=[node], type_ignores=[])
                namespace = {"datetime": datetime, "Optional": __import__("typing").Optional}
                exec(compile(ast.fix_missing_locations(code), "historical_senate_method", "exec"), namespace)
                old_get = namespace["_get_price_at_date"]
                price_calls.clear()
                for _ in range(2):
                    old_get(expert(), "AUDIT", datetime(2026, 8, 31))
                assert len(price_calls) == 2
                result["senate_before_sep5"] = {"fresh_instances": 2, "full_history_calls": len(price_calls),
                    "source": "eb4d4c71^", "disk_cache_ignored_in_live": True}

            # Screener cache keys describe batches, not each symbol's reusable history.
            fc._LIVE_BULK_CACHES.clear()
            batch_calls = []
            def batch_http(url, params, **kwargs):
                symbols = url.rsplit("/", 1)[1].split(",")
                batch_calls.append({"symbols": symbols, "from": params["from"], "to": params["to"]})
                return SimpleNamespace(json=lambda: {"historicalStockList": [
                    {"symbol": s, "historical": [{"date": "2026-08-28", "close": 100., "open": 100., "volume": 100}]}
                    for s in symbols]})
            seed("AAA", frame(["2026-08-28"]), age_hours=0)
            seed("BBB", frame(["2026-08-28"]), age_hours=0)
            with patch.object(screen, "get_app_setting", return_value="audit-fake-key"), \
                    patch.object(fc, "fmp_http_get", side_effect=batch_http), patch.object(screen, "datetime", Clock):
                screener = screen.StockScreener({})
                screener._fetch_history_bulk(["AAA", "BBB"], 30, max_workers=1)
                screener._fetch_history_bulk(["AAA", "BBB"], 30, max_workers=1)
                assert len(batch_calls) == 1
                screener._fetch_history_bulk(["BBB", "AAA"], 30, max_workers=1)
                screener._fetch_history_bulk(["AAA", "BBB"], 29, max_workers=1)
                assert len(batch_calls) == 3
            result["screener_exact_batch_only"] = {"logical_calls": 4, "http_calls": batch_calls,
                "warmed_parquet_ignored": True}

            # Six simultaneous first readers all execute the same fetch.
            fc._LIVE_BULK_CACHES.clear()
            barrier = threading.Barrier(6)
            concurrent_calls = []
            def fake_fetch():
                concurrent_calls.append(1)
                barrier.wait(timeout=10)
                return "same-payload"
            with ThreadPoolExecutor(max_workers=6) as pool:
                values = list(pool.map(lambda _: fc.fmp_live_cached("audit-one-key", fake_fetch), range(6)))
            assert len(concurrent_calls) == 6 and len(set(values)) == 1
            result["live_ttl_concurrent_miss"] = {"simultaneous_readers": 6, "identical_fetches": len(concurrent_calls)}

        output = Path(__file__).with_name("reproductions_2026-09-08.json")
        output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2))
        print(f"All {len(result)} audit scenarios reproduced; zero real HTTP. Evidence: {output}")


if __name__ == "__main__":
    main()
