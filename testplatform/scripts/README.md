# Scripts

Utility scripts for development and maintenance.

## Database Scripts

- `init_db.py` - Initialize the root database
- `init_backend_db.py` - Initialize the backend database with all tables

## Feature Tracking Scripts (historical)

These read `feature_list.json` from the working directory: the checklist from the platform's
first build, now kept at `docs/feature_list.json`.


- `view_features.py` - View all features
- `view_feature.py` - View a specific feature
- `find_next_failing.py` - Find next failing feature
- `find_ui_failing.py` - Find failing UI features
- `update_feature.py` - Update feature status
- `view_next_features.py` - View next features to implement
- `view_ui_features.py` - View UI features
- `view_ui_next.py` - View next UI features
- `show_next.py` - Show next task

## Data and Grid Scripts

- `migrate_cache_layout.py` - Move data from the old `~/Documents/ba2_trade_platform` / in-repo layout to `BA2_HOME` (dry run by default, `--apply` to move)
- `run_phase1_grid.sh` - Reproduce the Phase-1 strategy-optimization grid (`ba2-test optimize-batch`)
- `run_options_grid.sh` - Options strategy grid (builds the options cache, then one `optimize-batch`)

## Development Scripts

- `check_routes.py` - Check API routes
- `update_zoom_feature.py` - Update zoom feature (specific fix)

## Usage

Run from the project root:

```bash
python scripts/init_backend_db.py
python scripts/view_features.py
```
