# Test platform version — format: YYYY.MM.NNNNN
# NNNNN is the sequential build number.
#
# BUMP THIS for ANY change under `testplatform/` or `packages/`. The distributed GA workers
# decide whether to self-update by comparing this string alone (see
# `backend/app/services/worker_client.py:ensure_synced`, which deliberately does NOT key on the
# git commit so that ordinary pushes don't churn every worker mid-run). A change to the shared
# `packages/` code that does NOT bump this leaves workers running different `ba2_common` code
# from the master — which silently breaks trial reproducibility.
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
TEST_APP_VERSION = "2026.08.0034"
