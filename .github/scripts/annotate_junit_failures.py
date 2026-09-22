"""Echo a junit XML's failures as GitHub ``::error::`` annotations.

WHY THIS EXISTS. When this workflow goes red, the useful text lives in the run LOG and in the
junit ARTIFACT -- and downloading either requires ADMIN rights on the repository, returning 403
and 401 respectively to everyone else. The annotations API has no such restriction on a public
repo. So the one channel a non-admin can actually read is an annotation, and that is what this
prints. Without it a failing build says only "Process completed with exit code 1", which is how
a five-run outage went undiagnosed.

Usage: python annotate_junit_failures.py <junit.xml> [more.xml ...]
Always exits 0: it is a reporter, and must never turn a green build red or mask a red one.
"""
import os
import sys
import textwrap
import xml.etree.ElementTree as ET

#: GitHub truncates a long annotation, and the LAST frames of a traceback are the informative
#: ones, so the tail is kept and split across several annotations rather than sent as one.
_TAIL_CHARS = 3000
_CHUNK = 900


def annotate(path: str) -> int:
    if not os.path.exists(path):
        print(f"::error::no junit XML at {path} -- the test step produced none")
        return 0
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        print(f"::error::junit XML at {path} is unparsable: {e}")
        return 0
    found = 0
    for case in root.iter("testcase"):
        for bad in list(case.findall("failure")) + list(case.findall("error")):
            found += 1
            where = f"{case.get('classname')}::{case.get('name')}"
            body = (bad.text or bad.get("message") or "").strip()
            for i, chunk in enumerate(textwrap.wrap(body[-_TAIL_CHARS:], _CHUNK)):
                flat = chunk.replace("\r", " ").replace("\n", " ")
                print(f"::error title={where} ({i})::{flat}")
    return found


def main(argv: list) -> int:
    total = sum(annotate(p) for p in argv[1:]) if len(argv) > 1 else 0
    print(f"::error::{total} failing test(s) reported above")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
