"""``app.services.warm`` is a thin host, and must stay one (spec section 8).

The warm implementation lives in the three installable packages, because the LIVE
trading application runs the same warm and may not import this backend tree. The
first delivery got that wrong and bridged it with ``sys.path``; this test is what
stops it coming back: every name the backend exposes must BE the package's object,
not a copy of it.
"""
import app.services.warm as backend_warm


def test_the_queue_and_budget_are_the_ba2_common_objects():
    from ba2_common.core.warm import WarmBudget, WarmQueue

    assert backend_warm.WarmQueue is WarmQueue
    assert backend_warm.WarmBudget is WarmBudget


def test_the_planner_and_pinned_roots_are_the_ba2_providers_modules():
    from ba2_providers.warm import planner, roots

    assert backend_warm.planner is planner
    assert backend_warm.roots is roots
    assert backend_warm.plan is planner.plan
    assert backend_warm.materialize_pinned_root is roots.materialize_pinned_root


def test_the_fetcher_is_the_ba2_experts_object():
    from ba2_experts.warm_fetchers import DefaultWarmFetcher, NamespaceFetchers

    assert backend_warm.DefaultWarmFetcher is DefaultWarmFetcher
    assert backend_warm.NamespaceFetchers is NamespaceFetchers


def test_the_backend_holds_no_warm_implementation_of_its_own():
    """A module here would be a second copy that only the test platform can run."""
    import os

    package_dir = os.path.dirname(backend_warm.__file__)
    modules = sorted(f for f in os.listdir(package_dir)
                     if f.endswith(".py") and f != "__init__.py")

    assert modules == [], f"warm implementation modules left in the backend: {modules}"
