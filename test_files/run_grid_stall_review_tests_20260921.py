"""Run the remote patch's six tests plus existing pool tests in a temporary overlay.

Only the reviewed two modules are overlaid in this test process. The repository,
running applications, and databases are not modified. Pass the snapshot directory.
"""
import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path


def main():
    repo = Path(__file__).resolve().parents[1]
    snapshot = Path(sys.argv[1])
    sandbox = Path(tempfile.mkdtemp(prefix='ba2-stall-pytest-'))
    source_dir = sandbox / 'app/services'
    tests_dir = sandbox / 'tests'
    source_dir.mkdir(parents=True)
    tests_dir.mkdir()
    for filename in ('strategy_fitness.py', 'strategy_optimization_handler.py'):
        shutil.copyfile(snapshot / filename, source_dir / filename)
    shutil.copyfile(snapshot / 'test_local_pool_stall_recovery.py', tests_dir / 'test_local_pool_stall_recovery.py')
    # This test reads handler source relative to itself, so copy it into the same overlay.
    shutil.copyfile(repo / 'testplatform/backend/tests/test_pool_recycle.py', tests_dir / 'test_pool_recycle.py')
    sys.path.insert(0, str(repo / 'testplatform/backend'))
    import app.services
    for name in ('strategy_fitness', 'strategy_optimization_handler'):
        full_name = 'app.services.' + name
        spec = importlib.util.spec_from_file_location(full_name, source_dir / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)
        setattr(app.services, name, module)
    import pytest
    return pytest.main([str(tests_dir), '-q', '--confcutdir=' + str(sandbox)])


if __name__ == '__main__':
    raise SystemExit(main())
