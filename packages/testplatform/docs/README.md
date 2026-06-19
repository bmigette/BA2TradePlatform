# Documentation

This folder contains all project documentation.

## Structure

```
docs/
├── README.md               # This file
├── feature_list.json       # Complete list of implemented features (229/231)
├── implementation/         # Development session notes and progress
│   ├── SESSION_*_HANDOFF.md   # Individual session summaries
│   └── claude-progress.txt     # Detailed progress log
└── spec/                   # Project specifications
    ├── app_spec.txt           # Original application specification
    └── test_export_manually.md # Manual testing notes
```

## Feature Status

Current feature implementation: **229/231 (99.1%)**

See `feature_list.json` for the complete breakdown of all features and their status.

### Remaining Features (Need API Keys)
1. Polygon.io OHLCV Provider - Requires `POLYGON_API_KEY`
2. EODHD OHLCV Provider - Requires `EODHD_API_KEY`

## Quick Links

- [Main README](../README.md) - Project overview and setup
- [Quick Start](../QUICK_START.md) - Getting started guide
- [Tests](../tests/README.md) - Running tests
- [Backend](../backend/) - FastAPI backend
- [Frontend](../frontend/) - React frontend
