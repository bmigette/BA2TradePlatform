# Tests

This folder contains test files for the BA2ML Trading Platform.

## Running Tests

### Backend Tests

```bash
cd backend
source venv/bin/activate
python -m pytest ../tests/
```

### Quick Test

```bash
cd backend
source venv/bin/activate
python ../tests/test_dataproviders.py
```

## Test Files

- `test_dataproviders.py` - Tests for data provider classes (YFinance, Alpha Vantage, etc.)

## Adding New Tests

1. Create test files with the prefix `test_`
2. Use pytest conventions for test functions (`test_*`)
3. Import modules from the backend path

Example:
```python
import sys
sys.path.insert(0, './backend')

def test_my_feature():
    from app.services import MyService
    assert MyService().validate()
```
