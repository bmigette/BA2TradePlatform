"""Turn an observe-mode run's WOULD-RAISE lines into a per-site decision table.

Workflow for flipping BA2_ERROR_MODE from `observe` to `enforce` on evidence rather than guesswork
(see ba2_common.core.failure_modes):

    set BA2_ERROR_MODE=observe
    <run a few representative backtests / a grid strategy>
    python tools/summarize_would_raise.py <logfile> [<logfile> ...]

Each row is one HANDLER site and one exception type, with a count. That is exactly the unit of
decision:

    frequent + genuinely a data condition   -> name it:  absorb_if_benign(e, KeyError)
    rare / never seen                       -> leave it: it will propagate under enforce
    frequent + actually a bug               -> FIX IT, and it stops appearing

Sites that never appear are the point: under `enforce` they propagate, which is the whole
objective. A site appearing with a type you cannot justify is a bug the old swallow was hiding --
that is how the ATR tz TypeError should have been caught years earlier.
"""
import argparse
import collections
import re
import sys

# WOULD-RAISE <Type> at=<file:line> from=<file:line>: <message>
LINE = re.compile(r"WOULD-RAISE (?P<exc>\w+) at=(?P<at>\S+) from=(?P<frm>\S+): (?P<msg>.*)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--top-messages", type=int, default=1,
                    help="sample messages to show per row (helps tell data from bug)")
    a = ap.parse_args()

    counts = collections.Counter()
    samples = collections.defaultdict(list)
    frm = collections.defaultdict(collections.Counter)
    total = 0

    for path in a.logs:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = LINE.search(line)
                    if not m:
                        continue
                    total += 1
                    key = (m.group("at"), m.group("exc"))
                    counts[key] += 1
                    frm[key][m.group("frm")] += 1
                    if len(samples[key]) < a.top_messages:
                        samples[key].append(m.group("msg").strip()[:110])
        except OSError as e:
            print(f"!! cannot read {path}: {e}", file=sys.stderr)

    if not total:
        print("no WOULD-RAISE lines found. Before concluding 'nothing was absorbed', rule out\n"
              "the two boring explanations: the run was not in observe mode (BA2_ERROR_MODE),\n"
              "or the records never reached THIS file.\n"
              "\n"
              "PROVE the pipe works before trusting an empty result -- fire a canary:\n"
              "    BA2_ERROR_MODE=observe python -c \"\\\n"
              "        from ba2_common.core.failure_modes import absorb_if_benign\\n"
              "        try: raise TypeError('canary')\\n"
              "        except Exception as e: absorb_if_benign(e)\"\n"
              "and confirm a WOULD-RAISE line appears where you are grepping. An absent line\n"
              "proves nothing on its own -- that assumption is exactly how the ATR tz bug\n"
              "survived for months.\n"
              "\n"
              "(For the record: GA pool workers run at logging.disable(INFO), NOT (ERROR), so\n"
              "WARNING/ERROR/CRITICAL do pass the level filter; and although _worker_init clears\n"
              "the ba2 handlers, logging.lastResort still writes WARNING+ to stderr, which a grid\n"
              "captures via 2>&1. Check the worker logs too, not just the master's.)")
        return 0

    print(f"{total} absorbed exception(s) that WOULD propagate under BA2_ERROR_MODE=enforce\n")
    print(f"{'count':>7}  {'handler site':<34} {'exception':<22} raised at")
    print("-" * 100)
    for (at, exc), n in counts.most_common():
        top_from = frm[(at, exc)].most_common(1)[0][0]
        print(f"{n:>7}  {at:<34} {exc:<22} {top_from}")
        for s in samples[(at, exc)]:
            print(f"{'':>7}  ->  {s}")
    print("\nFor each row: name it benign at that site if it is genuinely a data condition,\n"
          "otherwise treat it as a bug to fix before flipping to enforce.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
