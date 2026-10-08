# Minimum shared-package versions a test-platform worker must run -- format YYYY.MM.NNNNN.
#
# Distributed GA workers must produce results identical to the master's, so a shared-package
# change that can alter GA results has to reach every worker. Each package carries its own
# `PACKAGE_VERSION` (packages/<dir>/<pkg>/version.py); a worker re-syncs only when
#   (a) its TEST_APP_VERSION differs from the master's, or
#   (b) its reported version of a package is BELOW the entry here (or it reports none).
# A package version that is merely HIGHER on the master than on a worker, while the worker is
# still at/above the minimum, does NOT force a re-sync (the master logs it as drift instead).
#
# RAISING an entry is the explicit act meaning "this package change can affect GA results; make
# every worker take it". Set it EQUAL to the package's new PACKAGE_VERSION in the same commit. No
# TEST_APP_VERSION bump is needed: the raised minimum itself makes older workers sync, and an edit to
# this file alone is exempt from the `testplatform/` -> bump-TEST rule. A change that cannot affect GA results (a
# broker-only module, tests, docs) leaves this file alone and instead adds its path to
# `ga_neutral_package_paths.py`. `tools/check_package_versions.py` enforces the choice (the `package-version-guard` CI job).
#
# This file is read by TEXT (ast.literal_eval of the assignment), never imported: keep the value
# a plain dict literal of str -> str.
REQUIRED_PACKAGE_VERSIONS = {
    "ba2_common": "2026.10.00008",
    "ba2_providers": "2026.10.00005",
    "ba2_experts": "2026.10.00002",
}
