from logging.config import fileConfig
import os
import sys

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# Add the parent directory to sys.path so we can import ba2_trade_platform
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

# ...and the three Phase 6 packages the same way. ba2_trade_platform.config imports
# ba2_common at module scope, and the venv's editable installs of
# ba2_common/ba2_providers/ba2_experts point at absolute paths that need not exist
# in this checkout (they resolve to the MAIN worktree, or to nothing at all). Only
# pytest.ini's `pythonpath` made those imports work, so `alembic <anything>` and
# `python migrate.py upgrade` died with ModuleNotFoundError unless the caller
# remembered a PYTHONPATH prefix.
#
# PREPENDED, not appended -- like pytest.ini's `pythonpath` -- so THIS checkout's
# packages win over a stale editable install pointing at another worktree. The cost
# is that these directories, and the repo root, sit ahead of site-packages: any
# top-level module under packages/*/ (its `tests` package), or under the repo root
# (`tools`, `test_files`, `logs`), now shadows a same-named installed distribution.
# Nothing collides today, but that is the surface being traded for correctness.
#
# Sliced in one go rather than insert(0) in a loop: three separate insert(0) calls
# would leave the reversed order (experts, providers, common) on sys.path, which is
# not what the tuple reads like.
_PACKAGE_PATHS = [
    os.path.join(_REPO_ROOT, "packages", _pkg)
    for _pkg in ("common", "providers", "experts")
]
sys.path[0:0] = [_p for _p in _PACKAGE_PATHS if _p not in sys.path]

# Import SQLModel metadata and configure database
from sqlmodel import SQLModel
from ba2_trade_platform.config import DB_FILE as _DEFAULT_DB_FILE
from ba2_trade_platform.core import models  # Import all models to register them with SQLModel

# Allow targeting a non-default DB (e.g. prod) without editing config.py.
# Set BA2_DB_FILE to point alembic at any sqlite file. Falls back to the
# default config path otherwise.
DB_FILE = os.environ.get("BA2_DB_FILE", _DEFAULT_DB_FILE)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Set the database URL dynamically
config.set_main_option("sqlalchemy.url", f"sqlite:///{DB_FILE}")

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# Use SQLModel metadata which contains all our table definitions
target_metadata = SQLModel.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True  # Enable batch mode for SQLite
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
