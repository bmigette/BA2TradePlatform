"""One-off: redact credentials from OLD log files (the live loggers redact new lines since the
log-redaction change). Writes ``<name>.redacted`` next to every ``*.log*`` file and prints per-file
COUNTS only (never a value). ``--in-place --yes-overwrite`` replaces the originals; run it with the
app stopped. The operator decides: nothing is run automatically.

    python tools/redact_old_logs.py <log folder> [--in-place --yes-overwrite]
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ba2_trade_platform.log_redaction import REDACTED, redact_text  # noqa: E402


def redact_file(path: str, in_place: bool = False) -> tuple:
    """Returns (lines, redacted_lines, output_path)."""
    out_path = path if in_place else path + '.redacted'
    tmp = out_path + '.tmp'
    lines = changed = 0
    with open(path, 'r', encoding='utf-8', errors='replace', newline='') as src, \
            open(tmp, 'w', encoding='utf-8', newline='') as dst:
        for line in src:
            lines += 1
            new = redact_text(line)
            if new != line:
                changed += 1
            dst.write(new)
    os.replace(tmp, out_path)
    return lines, changed, out_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('folder')
    ap.add_argument('--in-place', action='store_true')
    ap.add_argument('--yes-overwrite', action='store_true',
                    help='required with --in-place: confirms replacing the original logs')
    args = ap.parse_args(argv)
    if args.in_place and not args.yes_overwrite:
        print('--in-place needs --yes-overwrite (originals are replaced, not backed up)')
        return 2
    total = 0
    for name in sorted(os.listdir(args.folder)):
        path = os.path.join(args.folder, name)
        if not os.path.isfile(path) or '.log' not in name or name.endswith(('.redacted', '.tmp')):
            continue
        lines, changed, out = redact_file(path, args.in_place)
        total += changed
        print(f'{name}: {lines} lines, {changed} redacted -> {os.path.basename(out)}')
    print(f'total redacted lines: {total} (marker {REDACTED})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
