# Test platform version — format: YYYY.MM.NNNNN
# NNNNN is the sequential build number.
#
# BUMP THIS for ANY change under `testplatform/`. The distributed GA workers decide whether to
# self-update by comparing this string (see `backend/app/services/worker_client.py:ensure_synced`,
# which deliberately does NOT key on the git commit so that ordinary pushes don't churn every
# worker mid-run) AND, since 2026-10-03, by checking each shared package's version against
# `testplatform/required_package_versions.py`. Changes under `packages/` therefore no longer bump
# this file by default: they bump the package's own `PACKAGE_VERSION`, and raise the required
# minimum (workers then re-sync) only if the change can affect GA results; otherwise the path goes
# in `testplatform/ga_neutral_package_paths.py`. Edits to `required_package_versions.py` and
# `ga_neutral_package_paths.py` ALONE are exempt from this bump (a raised minimum itself syncs older
# workers). See CLAUDE.md "Versioning" and
# docs/plans/2026-10-03-package-versioning-design.md.
#
# Changes confined to `ba2_trade_platform/` bump `ba2_trade_platform/version.py` instead. The two
# sequences are INDEPENDENT and deliberately so: before the split, a test-platform-only change
# could not reach the workers without a cosmetic trade-app bump, and a trade-only bump made every
# worker re-sync for nothing.
#
# Why this sequence starts at 0001 rather than mirroring the trade app's number: the trade app's
# APP_VERSION has only ever been a 3-4 digit unpadded counter (651 ... 1071+), so a zero-padded
# NNNNN can never collide with a trade version string. That matters during the migration window,
# when a worker that has not pulled yet still reports `ba2_trade_platform`'s APP_VERSION under the
# same `app_version` key. A collision there would look like "already converged" while the worker
# ran stale code. (`ensure_synced` also detects pre-split workers positively, via the
# `version_scheme` field — this padding is the second line of defence, not the only one.)
TEST_APP_VERSION = "2026.09.0135"
