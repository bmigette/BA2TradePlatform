"""Insert ``raise_if_defect(e)`` at the top of broad, swallowing except handlers.

An AST-guided rewriter for the pattern the ATR incident exposed::

    except Exception as e:
        logger.error(...)
        return None          # <- a TypeError now looks exactly like "no data"

becomes::

    except Exception as e:
        raise_if_defect(e)
        logger.error(...)
        return None

CONSERVATIVE BY CONSTRUCTION — it only touches a handler that is ALL of:
  * broad (``except Exception``/``BaseException``/bare) — narrow handlers already say what they
    expect, and their author's intent is explicit;
  * binds the exception to a name (``as e``) — without a binding there is nothing to inspect;
  * contains NO ``raise`` anywhere in its body — a handler that already re-raises is fine;
  * does not already call ``raise_if_defect``;
  * is NOT a best-effort telemetry guard. Handlers binding a name matching /log/i (the
    project's own convention, e.g. ``except Exception as log_error``) wrap activity-log writes
    where swallowing is CORRECT: a failed audit-log write must not abort risk management or
    trading. Escalating those would let a logging hiccup crash live trades — strictly worse
    than the bug this whole exercise is fixing.

Everything else is left alone. Run with --check to list candidates without editing.

    python tools/apply_raise_if_defect.py --check <files...>
    python tools/apply_raise_if_defect.py <files...>
"""
import argparse
import ast
import pathlib
import sys

IMPORT_LINE = "from ba2_common.core.failure_modes import raise_if_defect"


def candidates(tree: ast.AST, skipped=None):
    out = []
    skipped = skipped if skipped is not None else []
    for n in ast.walk(tree):
        if not isinstance(n, ast.ExceptHandler):
            continue
        t = n.type
        broad = t is None or (isinstance(t, ast.Name) and t.id in ("Exception", "BaseException"))
        if not broad or not n.name:
            continue
        if any(isinstance(x, ast.Raise) for x in ast.walk(n)):
            continue
        if "log" in n.name.lower():          # best-effort telemetry guard — see module docstring
            skipped.append(n)
            continue
        first = n.body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
                and isinstance(first.value.func, ast.Name)
                and first.value.func.id == "raise_if_defect"):
            continue
        out.append(n)
    return out


def rewrite(path: pathlib.Path, check: bool) -> int:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    skipped = []
    cands = candidates(tree, skipped)
    if not cands:
        return 0
    if check:
        for h in cands:
            print(f"  {path}:{h.lineno} (except ... as {h.name})")
        for h in skipped:
            print(f"  SKIP {path}:{h.lineno} (telemetry guard: as {h.name})")
        return len(cands)

    lines = src.split("\n")
    # bottom-up so earlier line numbers stay valid as we insert
    for h in sorted(cands, key=lambda x: x.lineno, reverse=True):
        first = h.body[0]
        indent = " " * (first.col_offset)
        lines.insert(first.lineno - 1, f"{indent}raise_if_defect({h.name})")

    out = "\n".join(lines)
    if IMPORT_LINE not in out:
        # place after the last top-level import so it cannot land inside a docstring
        tree2 = ast.parse(out)
        last = 0
        for n in tree2.body:
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                last = max(last, n.end_lineno or n.lineno)
        l2 = out.split("\n")
        l2.insert(last, IMPORT_LINE)
        out = "\n".join(l2)

    ast.parse(out)  # never write a file we just broke
    path.write_text(out, encoding="utf-8")
    return len(cands)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("files", nargs="+")
    a = ap.parse_args()
    total = 0
    for f in a.files:
        p = pathlib.Path(f)
        n = rewrite(p, a.check)
        if n:
            print(f"{'would patch' if a.check else 'patched'} {n:>3} in {p}")
        total += n
    print(f"\ntotal: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
