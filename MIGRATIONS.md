# Database Migrations for BA2 Trade Platform

This project uses Alembic for database schema migrations, which is particularly useful for managing schema changes in production environments.

## Setup

Alembic has been configured to work with SQLModel and the BA2 Trade Platform database structure. The configuration files are:

- `alembic.ini` - Main Alembic configuration
- `alembic/env.py` - Environment setup and metadata imports
- `migrate.py` - Convenient wrapper script for common operations

## Usage

### Creating a New Migration

When you make changes to the models in `ba2_trade_platform/core/models.py`:

```bash
python migrate.py create "Description of your changes"
```

This will auto-generate a migration script based on the differences between your current models and the database schema.

### Applying Migrations

To update your database to the latest schema:

```bash
python migrate.py upgrade
```

To upgrade to a specific revision:

```bash
python migrate.py upgrade <revision_id>
```

### Rolling Back Migrations

To downgrade to a previous revision:

```bash
python migrate.py downgrade <revision_id>
```

To downgrade by one revision:

```bash
python migrate.py downgrade -1
```

### Checking Migration Status

To see the current database revision:

```bash
python migrate.py current
```

To see the migration history:

```bash
python migrate.py history
```

## SQLite Considerations

SQLite has limited `ALTER TABLE` support compared to PostgreSQL or MySQL. Some operations that may require special handling:

1. **Changing column types**: SQLite doesn't support changing column types directly
2. **Adding NOT NULL columns**: Must provide default values or use a multi-step process
3. **Dropping columns**: Not supported in older SQLite versions

The migration system handles these limitations by:
- Using table recreation when necessary
- Providing default values for new NOT NULL columns
- Including error handling for SQLite-specific limitations

## Current Schema

The database includes these main tables:
- `expertrecommendation` - AI trading recommendations with risk and time horizon analysis
- `marketanalysis` - Market analysis data linked to recommendations
- `expertinstance` - Running expert configurations
- `tradingorder` - Trading order history
- `instrument` - Financial instrument definitions
- `accountdefinition` - Trading account configurations

## Recent Changes

### Migration 2b4cf753ba81 (2025-09-24)
- Added `risk_level` enum field to `ExpertRecommendation` (LOW, MEDIUM, HIGH)
- Added `time_horizon` enum field to `ExpertRecommendation` (SHORT_TERM, MEDIUM_TERM, LONG_TERM)
- Added `market_analysis_id` foreign key linking recommendations to market analysis
- Added foreign key constraint with CASCADE delete

## Best Practices

1. **Always test migrations** on a copy of production data first
2. **Review auto-generated migrations** before applying them
3. **Use descriptive migration messages** that explain the business purpose
4. **Back up your database** before running migrations in production
5. **Keep migrations small and focused** - one logical change per migration