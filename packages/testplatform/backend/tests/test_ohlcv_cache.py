"""
Tests for OHLCV cache handler and cache-related endpoints.
"""

import pytest
from unittest.mock import patch, MagicMock, PropertyMock
from datetime import datetime
import pandas as pd
import os
import tempfile


class TestHandleOHLCVCacheFetch:
    """Tests for handle_ohlcv_cache_fetch handler."""

    @pytest.fixture
    def mock_task_queue(self):
        """Create a mock task queue."""
        tq = MagicMock()
        tq.update_progress = MagicMock()
        return tq

    @pytest.fixture
    def mock_provider(self):
        """Create a mock OHLCV provider."""
        provider = MagicMock()
        _df = pd.DataFrame({
            'Date': pd.date_range('2020-01-01', periods=100),
            'Open': range(100),
            'High': range(100),
            'Low': range(100),
            'Close': range(100),
            'Volume': range(100)
        })
        provider.extend_ohlcv_cache.return_value = _df
        return provider

    def test_successful_cache_fetch(self, mock_task_queue, mock_provider):
        """Test successful OHLCV cache fetch for a symbol."""
        with patch('app.services.ohlcv_cache_handler.get_task_queue', return_value=mock_task_queue), \
             patch('app.api.datasets.get_ohlcv_provider', return_value=mock_provider):
            from app.services.ohlcv_cache_handler import handle_ohlcv_cache_fetch

            result = handle_ohlcv_cache_fetch('task-123', {
                'provider': 'yfinance',
                'symbol': 'AAPL',
                'timeframes': ['1d', '1h']
            })

            assert result['status'] == 'completed'
            assert result['symbol'] == 'AAPL'
            assert '1d' in result['results']
            assert '1h' in result['results']
            assert result['results']['1d']['status'] == 'success'
            assert result['results']['1d']['rows'] == 100

    def test_cache_fetch_with_provider_error(self, mock_task_queue, mock_provider):
        """Test cache fetch handles provider errors gracefully."""
        mock_provider.extend_ohlcv_cache.side_effect = Exception("API limit reached")

        with patch('app.services.ohlcv_cache_handler.get_task_queue', return_value=mock_task_queue), \
             patch('app.api.datasets.get_ohlcv_provider', return_value=mock_provider):
            from app.services.ohlcv_cache_handler import handle_ohlcv_cache_fetch

            result = handle_ohlcv_cache_fetch('task-123', {
                'provider': 'yfinance',
                'symbol': 'AAPL',
                'timeframes': ['1d']
            })

            assert result['status'] == 'completed'
            assert result['results']['1d']['status'] == 'error'
            assert 'API limit reached' in result['results']['1d']['error']

    def test_cache_fetch_missing_symbol(self, mock_task_queue):
        """Test cache fetch fails without a symbol."""
        with patch('app.services.ohlcv_cache_handler.get_task_queue', return_value=mock_task_queue):
            from app.services.ohlcv_cache_handler import handle_ohlcv_cache_fetch

            result = handle_ohlcv_cache_fetch('task-123', {
                'provider': 'yfinance',
                'symbol': '',
                'timeframes': ['1d']
            })

            assert result['status'] == 'failed'
            assert 'symbol is required' in result['error']

    def test_cache_fetch_progress_updates(self, mock_task_queue, mock_provider):
        """Test that progress updates are called during fetch."""
        with patch('app.services.ohlcv_cache_handler.get_task_queue', return_value=mock_task_queue), \
             patch('app.api.datasets.get_ohlcv_provider', return_value=mock_provider):
            from app.services.ohlcv_cache_handler import handle_ohlcv_cache_fetch

            handle_ohlcv_cache_fetch('task-123', {
                'provider': 'yfinance',
                'symbol': 'AAPL',
                'timeframes': ['1d', '4h', '1h']
            })

            # 1 initial + 2 per timeframe (a "cache status" line then a "done" line) + 1 final
            # = 1 + 2*3 + 1 = 8. (extend_ohlcv_cache is mocked, so its own progress_callback,
            # which would add more, never fires here.)
            assert mock_task_queue.update_progress.call_count == 8

    def test_handler_uses_extend_ohlcv_cache(self, mock_task_queue, mock_provider):
        """Handler must call extend_ohlcv_cache, not get_ohlcv_data."""
        mock_provider.extend_ohlcv_cache = MagicMock(return_value=pd.DataFrame(
            {'Date': [], 'Close': []}
        ))
        with patch('app.services.ohlcv_cache_handler.get_task_queue',
                   return_value=mock_task_queue), \
             patch('app.api.datasets.get_ohlcv_provider',
                   return_value=mock_provider):
            from importlib import reload
            import app.services.ohlcv_cache_handler as mod
            reload(mod)
            mod.handle_ohlcv_cache_fetch('task-1', {
                'provider': 'yfinance',
                'symbol': 'AAPL',
                'timeframes': ['1d'],
            })
        mock_provider.extend_ohlcv_cache.assert_called()
        mock_provider.get_ohlcv_data.assert_not_called()

    def test_handler_passes_custom_date_range(self, mock_task_queue, mock_provider):
        """Handler must parse start_date/end_date strings from payload into datetime."""
        from datetime import datetime
        captured = {}
        def capture(*args, **kwargs):
            captured['start'] = kwargs.get('start_date') or args[1]
            captured['end'] = kwargs.get('end_date') or args[2]
            return pd.DataFrame({'Date': [], 'Close': []})

        mock_provider.extend_ohlcv_cache = MagicMock(side_effect=capture)
        with patch('app.services.ohlcv_cache_handler.get_task_queue',
                   return_value=mock_task_queue), \
             patch('app.api.datasets.get_ohlcv_provider',
                   return_value=mock_provider):
            from importlib import reload
            import app.services.ohlcv_cache_handler as mod
            reload(mod)
            mod.handle_ohlcv_cache_fetch('task-1', {
                'provider': 'yfinance',
                'symbol': 'AAPL',
                'timeframes': ['1d'],
                'start_date': '2023-01-01',
                'end_date': '2024-12-31',
            })
        assert isinstance(captured.get('start'), datetime), "start_date must be datetime"
        assert isinstance(captured.get('end'), datetime), "end_date must be datetime"
        assert captured['start'] == datetime(2023, 1, 1)
        assert captured['end'] == datetime(2024, 12, 31)


class TestOHLCVProviderEndpoint:
    """Tests for the OHLCV providers listing endpoint."""

    def test_providers_list_structure(self):
        """Test that providers endpoint returns expected structure."""
        # Import and call the endpoint function directly
        import asyncio
        from app.api.tools import list_ohlcv_providers

        result = asyncio.get_event_loop().run_until_complete(list_ohlcv_providers())

        assert 'providers' in result
        assert 'default' in result
        assert result['default'] == 'yfinance'
        assert len(result['providers']) >= 1

        # Check yfinance provider
        yf = next(p for p in result['providers'] if p['id'] == 'yfinance')
        assert yf['available'] is True
        assert yf['requires_api_key'] is False


class TestOHLCVCacheStatusEndpoint:
    """Tests for the cache status endpoint."""

    def test_cache_status_empty_dir(self):
        """Test cache status with no native OHLCV provider dirs."""
        import asyncio
        from app.api.tools import get_ohlcv_cache_status

        # Repoint the unified-cache root resolver at nothing (the new scan source).
        with patch('app.api.tools._ohlcv_cache_roots', return_value=[]):
            result = asyncio.get_event_loop().run_until_complete(get_ohlcv_cache_status())

            assert result['count'] == 0
            assert result['cache_files'] == []

    def test_cache_status_with_files(self):
        """Test cache status correctly parses native parquet cache files."""
        import asyncio
        from pathlib import Path as RealPath
        from app.api.tools import get_ohlcv_cache_status

        with tempfile.TemporaryDirectory() as tmpdir:
            # A native OHLCV provider dir IS the scan root; files live directly under it.
            root = RealPath(tmpdir) / "FMPOHLCVProvider"
            root.mkdir()
            pd.DataFrame({
                'Date': pd.to_datetime(['2024-01-01', '2024-01-02']),
                'Open': [100, 103], 'High': [105, 108], 'Low': [99, 102],
                'Close': [103, 107], 'Volume': [1000000, 1200000],
            }).to_parquet(root / "AAPL_1d.parquet", index=False)

            with patch('app.api.tools._ohlcv_cache_roots', return_value=[root]):
                result = asyncio.get_event_loop().run_until_complete(get_ohlcv_cache_status())

                assert result['count'] == 1
                assert result['cache_files'][0]['provider'] == 'FMPOHLCVProvider'
                assert result['cache_files'][0]['symbol'] == 'AAPL'
                assert result['cache_files'][0]['interval'] == '1d'
                assert result['cache_files'][0]['rows'] == 2


import pathlib


class TestCacheFilePerProvider:
    """UNIFIED CACHE: cache files are the native parquet store, keyed by provider CLASS name
    (CACHE_FOLDER/<ProviderClassName>/<SYM>_<interval>.parquet) — the same store get_ohlcv_data uses."""

    def test_cache_file_is_native_classname_parquet(self, monkeypatch):
        """Cache file path is CACHE_FOLDER/<ClassName>/<SYM>_<interval>.parquet (native cache)."""
        from ba2_common.core import native_cache
        from app.services.ohlcv_cache_provider import OHLCVCacheProviderBase as MarketDataProviderInterface

        class _Stub(MarketDataProviderInterface):
            def _get_ohlcv_data_impl(self, *a, **kw):
                return pd.DataFrame()
            def get_provider_name(self):
                return "testprov"
            def get_supported_features(self):
                return []
            def validate_config(self):
                return True

        with tempfile.TemporaryDirectory() as tmp:
            monkeypatch.setattr(native_cache, "CACHE_FOLDER", tmp, raising=False)
            s = _Stub()
            p = s._get_cache_file("AAPL", "1h")
            # Native key is the CLASS name (_Stub), NOT get_provider_name(); parquet, not csv.
            assert p == pathlib.Path(tmp) / "_Stub" / "AAPL_1h.parquet"

    def test_cache_file_creates_directory(self, monkeypatch):
        """_get_cache_file must create the provider subdirectory if it does not exist."""
        from ba2_common.core import native_cache
        from app.services.ohlcv_cache_provider import OHLCVCacheProviderBase as MarketDataProviderInterface

        class _Stub(MarketDataProviderInterface):
            def _get_ohlcv_data_impl(self, *a, **kw):
                return pd.DataFrame()
            def get_provider_name(self):
                return "myprov"
            def get_supported_features(self):
                return []
            def validate_config(self):
                return True

        with tempfile.TemporaryDirectory() as tmp:
            monkeypatch.setattr(native_cache, "CACHE_FOLDER", tmp, raising=False)
            s = _Stub()
            p = s._get_cache_file("MSFT", "1d")
            assert p.parent.exists(), "Provider subdirectory should have been created"


class TestExtendOHLCVCache:
    """Tests for extend_ohlcv_cache extend-only semantics."""

    def _make_df(self, start: str, end: str) -> pd.DataFrame:
        dates = pd.date_range(start, end, freq='D')
        return pd.DataFrame({
            'Date': dates, 'Open': 1.0, 'High': 2.0,
            'Low': 0.5, 'Close': 1.5, 'Volume': 100.0
        })

    def _make_provider(self, tmp_dir: str, monkeypatch):
        """A stub provider whose native cache is isolated to tmp_dir (CACHE_FOLDER repointed)."""
        from ba2_common.core import native_cache
        from app.services.ohlcv_cache_provider import OHLCVCacheProviderBase as MarketDataProviderInterface
        monkeypatch.setattr(native_cache, "CACHE_FOLDER", tmp_dir, raising=False)

        class _Stub(MarketDataProviderInterface):
            def _get_ohlcv_data_impl(self, symbol, start, end, interval):
                dates = pd.date_range(start, end, freq='D')
                return pd.DataFrame({
                    'Date': dates, 'Open': 1.0, 'High': 2.0,
                    'Low': 0.5, 'Close': 1.5, 'Volume': 100.0
                })
            def get_provider_name(self): return "stub"
            def get_supported_features(self): return []
            def validate_config(self): return True

        return _Stub()

    def _seed_cache(self, prov, start: str, end: str):
        """Write existing bars to the native cache via the provider's own writer (parquet+eff_date)."""
        prov._write_cache_df(self._make_df(start, end), "AAPL", "1d")

    def test_no_fetch_when_range_covered(self, monkeypatch):
        """If cache covers the range, _get_ohlcv_data_impl must not be called."""
        with tempfile.TemporaryDirectory() as tmp:
            prov = self._make_provider(tmp, monkeypatch)
            self._seed_cache(prov, "2024-01-01", "2024-12-31")

            with patch.object(prov, '_get_ohlcv_data_impl',
                              wraps=prov._get_ohlcv_data_impl) as mock_impl:
                prov.extend_ohlcv_cache("AAPL", datetime(2024, 3, 1), datetime(2024, 6, 1), "1d")
                mock_impl.assert_not_called()

    def test_full_fetch_when_no_cache(self, monkeypatch):
        """If no cache exists, fetches the full requested range and saves (native parquet)."""
        with tempfile.TemporaryDirectory() as tmp:
            prov = self._make_provider(tmp, monkeypatch)
            prov.extend_ohlcv_cache("AAPL", datetime(2024, 1, 1), datetime(2024, 3, 31), "1d")
            cache_file = prov._get_cache_file("AAPL", "1d")
            assert cache_file.exists()
            df = pd.read_parquet(cache_file)
            assert len(df) > 0
            assert "effective_date" in df.columns  # unified-cache schema

    def test_extends_right_only(self, monkeypatch):
        """Only fetches the right-side gap, not the already-cached portion."""
        with tempfile.TemporaryDirectory() as tmp:
            prov = self._make_provider(tmp, monkeypatch)
            self._seed_cache(prov, "2024-01-01", "2024-06-30")

            with patch.object(prov, '_get_ohlcv_data_impl',
                              wraps=prov._get_ohlcv_data_impl) as mock_impl:
                prov.extend_ohlcv_cache("AAPL", datetime(2024, 1, 1), datetime(2024, 9, 30), "1d")
                assert mock_impl.call_count == 1
                # The fetch should start from around the cache max date
                call_start = mock_impl.call_args[0][1]
                assert call_start >= datetime(2024, 6, 28)

    def test_no_duplicate_rows_after_extend(self, monkeypatch):
        """Merged cache must not have duplicate Date rows."""
        with tempfile.TemporaryDirectory() as tmp:
            prov = self._make_provider(tmp, monkeypatch)
            self._seed_cache(prov, "2024-01-01", "2024-06-30")
            prov.extend_ohlcv_cache("AAPL", datetime(2024, 1, 1), datetime(2024, 9, 30), "1d")
            df = pd.read_parquet(prov._get_cache_file("AAPL", "1d"))
            df['Date'] = pd.to_datetime(df['Date'])
            assert df['Date'].duplicated().sum() == 0
