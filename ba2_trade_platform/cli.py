"""Console-script entry point for the live trading platform.

Installed as the ``ba2-trade`` command (see ``pyproject.toml`` ``[project.scripts]``).
It runs the repo-root ``main.py`` as ``__main__`` so the existing argument parsing
and ``initialize_system()`` startup execute unchanged — the command is a thin,
behaviour-preserving wrapper, not a second entry path.

The repo root (and thus ``main.py``) is resolved from this module's location, so
``ba2-trade`` always runs the code of the install it belongs to — the editable
dev clone. Prod runs the same code with prod data simply by passing prod paths:
``ba2-trade --db-file <prod.sqlite> --cache-folder <prod cache> --port 8081``.
All CLI args are forwarded untouched to ``main.py``'s ``parse_arguments()`` — so
the command takes the SAME options as ``python main.py`` does today.
"""
from __future__ import annotations


def main() -> None:
    import os
    import runpy
    import sys

    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(pkg_dir)
    main_py = os.path.join(repo_root, "main.py")
    if not os.path.isfile(main_py):
        sys.exit(
            f"ba2-trade: cannot find {main_py}. The console command requires an "
            f"editable/source install of the platform repo."
        )
    sys.argv[0] = main_py
    runpy.run_path(main_py, run_name="__main__")


if __name__ == "__main__":
    main()
