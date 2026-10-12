# Package version -- format: YYYY.MM.NNNNN (NNNNN = zero-padded 5-digit sequential build number).
#
# Single source of truth for this package's version: `__init__.py` re-exports it as
# `__version__` and `pyproject.toml`'s `version` must equal it (pinned by a test; there is no
# build backend available at check time, so pyproject stays static rather than dynamic).
#
# BUMP THIS on EVERY change to the shipped code under `packages/experts/ba2_experts/`. Whether the test-platform
# GA workers must also re-sync is a SEPARATE decision (`testplatform/required_package_versions.py`);
# see CLAUDE.md "Versioning" and docs/plans/2026-10-03-package-versioning-design.md.
PACKAGE_VERSION = "2026.10.00003"
