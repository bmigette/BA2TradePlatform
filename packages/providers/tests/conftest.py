import os, tempfile, pathlib
import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolated_db():
    """Point ba2_common's DB seam at a throwaway sqlite + isolate the native cache.

    Configures the DB seam (ba2_common.core.db.configure_db) so the provider_cache
    table is created in a temp sqlite, and redirects CACHE_FOLDER (read at import by
    native_cache) so parquet/JSON cache writes never touch the real
    ~/Documents/.../cache tree.
    """
    workdir = pathlib.Path(tempfile.mkdtemp())
    tmp_db = workdir / "test.sqlite"

    # Redirect the cache root BEFORE init_db / any provider import that binds it.
    cache_dir = str(workdir / "cache")
    import ba2_common.config as cfg
    cfg.CACHE_FOLDER = cache_dir
    # The native_cache substrate now lives in ba2_common; timeseries_path reads the
    # SOURCE module's CACHE_FOLDER and _spill_path reads its _CACHE_ROOT, so rebind
    # BOTH on the source module (rebinding the ba2_providers shim alone has no effect)
    # or parquet/JSON cache writes leak to the real ~/Documents cache.
    import ba2_common.core.native_cache as nc_src
    nc_src.CACHE_FOLDER = cache_dir
    nc_src._CACHE_ROOT = os.path.join(cache_dir, "datasets", "cache")
    # Keep the shim's view consistent for any test reading nc.CACHE_FOLDER directly.
    import ba2_providers.cache.native_cache as nc
    nc.CACHE_FOLDER = cache_dir
    nc._CACHE_ROOT = os.path.join(cache_dir, "datasets", "cache")

    from ba2_common.core import db
    db.configure_db(str(tmp_db))   # defined in Task 3 (db seam)
    db.init_db()                   # create_all registers provider_cache
    yield
