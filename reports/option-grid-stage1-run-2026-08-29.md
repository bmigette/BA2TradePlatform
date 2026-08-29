# BA2 Option-Grid Stage 1 Run — 2026-08-29

36-job GA matrix (18 option structures x 2 experts, pop 200, gen 60, early-stop 8)
under ba2-stage1.service (MemoryMax=24G, CPUQuota=250%, --parallel 2).
Branch: stage1-trial-metrics @ /home/debian/ba2-grid/repo.

## Progress log
- 2026-08-29 17:01 UTC | active=yes mem=8.0GB | job 1/36 optm-FMPRating-O_LC-st1 gen 1/60 best=7.1042 ret=n/a dd=n/a | completed=0 failed=0
- 2026-08-29 17:31 UTC | active=yes mem=8.9GB | job 1/36 optm-FMPRating-O_LC-st1 gen 1/60 best=13.8222 ret=n/a dd=n/a | completed=0 failed=0
- 2026-08-29 18:00 UTC | active=yes mem=7.8GB | job 1/36 optm-FMPRating-O_LC-st1 gen 1/60 ind 98/200 best=13.8222 ret=n/a dd=n/a | completed=0 failed=0
- 2026-08-29 18:31 UTC | active=yes mem=8.2GB | job 1/36 optm-FMPRating-O_LC-st1 gen 1/60 ind 133/200 best=13.8222 ret=n/a dd=n/a | completed=0 failed=0
- 2026-08-29 19:01 UTC | active=yes mem=7.4GB | job 1/36 optm-FMPRating-O_LC-st1 gen 1/60 ind 164/200 best=13.8222 ret=n/a dd=n/a | completed=0 failed=0
- 2026-08-29 19:30 UTC | active=yes mem=9.5GB | job 1/36 optm-FMPRating-O_LC-st1 gen 1/60 ind 180/200 best=13.8222 ret=n/a dd=n/a | completed=0 failed=0
- 2026-08-29 20:01 UTC | active=yes mem=8.7GB | job 1/36 optm-FMPRating-O_LC-st1 gen 1/60 ind 198/200 best=13.8222 ret=n/a dd=n/a | completed=0 failed=0
- 2026-08-29 20:30 UTC | active=yes mem=7.9GB | job 1/36 optm-FMPRating-O_LC-st1 gen 2/60 (gen1 done best=13.8222) ret=n/a dd=n/a | completed=0 failed=0
- 2026-08-29 21:02 UTC | active=yes mem=7.1GB | job 1/36 optm-FMPRating-O_LC-st1 gen 1/60 (mem gen 1/60 ind 6/200) best=n/a (fresh run, no gen completed) ret=n/a dd=n/a | completed=0 failed=0
- 2026-08-29 21:02 UTC | NOTE: service stopped+restarted twice 20:50-20:56 UTC (deliberate, NRestarts=0). Before 1st stop: repeated 'FMP API key not configured' worker errors (~20:50:47). Current incarnation (since 20:55:46 UTC) started job 1 from scratch (strategy #1, no checkpoint resume); earlier gen 1-2 progress discarded. No OOM (mem.events max=0), fleet untouched.
- 2026-08-29 21:31 UTC | active=yes mem=7.5GB | job 1/36 optm-FMPRating-O_LC-st1 gen 1/60 ind 56/200 best=n/a (gen1 not done yet) ret=n/a dd=n/a | completed=0 failed=0
- 2026-08-29 22:02 UTC | active=yes mem=7.8GB | job 1/36 optm-FMPRating-O_LC-st1 gen 1/60 ind 72/200 best=13.8222 (gen1 in progress) ret=n/a dd=n/a | completed=0 failed=0
- 2026-08-29 22:31 UTC | active=yes mem=3.4GB | job 1/36 optm-FMPRating-O_LC-st1 gen 1/60 ind 120/200 best=13.8222 (gen1 in progress, no ckpt history yet) ret=n/a dd=n/a | completed=0 failed=0
- 2026-08-29 23:01 UTC | active=yes mem=7.0GB | job 1/36 optm-FMPRating-O_LC-st1 gen 1/60 ind 150/200 best=13.8222 (gen1 in progress) ret=n/a dd=n/a | completed=0 failed=0
- 2026-08-29 23:31 UTC | active=yes mem=7.4GB | job 1/36 optm-FMPRating-O_LC-st1 gen 1/60 ind ~173/200 best=13.8222 (gen1 in progress, no ckpt history yet) ret=n/a dd=n/a | completed=0 failed=0
