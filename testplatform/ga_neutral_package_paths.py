# Paths under `packages/` whose changes CANNOT affect GA / backtest results.
#
# `tools/check_package_versions.py` requires that a diff touching `packages/<pkg>/` (a) bumps that
# package's PACKAGE_VERSION, and (b) EITHER raises `testplatform/required_package_versions.py` for
# the package (or bumps TEST_APP_VERSION) OR touches only paths matched here. Listing a path is
# therefore the reviewed, in-diff statement "workers need not take this change".
#
# HOW TO ADD: append a glob (fnmatch syntax, `/` separators, `*` also crosses `/`) in the SAME
# commit as the change, and say in the commit message why the code is unreachable from the GA /
# backtest path (e.g. broker-only module never imported by the backtest engine). Prefer a narrow
# glob for one module over a wide one; never list a module the backtest engine imports.
#
# Read by TEXT (ast.literal_eval), never imported: keep it a plain list of str literals.
GA_NEUTRAL_GLOBS = [
    "packages/common/ba2_common/core/ibkr_*.py",
    "packages/common/ba2_common/core/protective_legs.py",
    "packages/common/tests/test_ibkr_*.py",
    # Tests, docs and READMEs never ship to GA code paths.
    "packages/*/tests/*",
    "packages/*/test_files/*",
    "packages/*/docs/*",
    "packages/*/README.md",
    "packages/*/*.md",
]
