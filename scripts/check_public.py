"""Refuse a commit that would publish something about the ledger this code serves.

This repository is public; the ledger is not.  Two kinds of check run over every
file given (pre-commit passes the staged ones; `--all` checks every tracked file):

  * built-in patterns for what this code's own ledger shape would leak: IBKR
    account ids, country-numbered account names (`Assets:<NN><CC>:...`), long
    reference numbers;
  * a private denylist -- names, account numbers, merchants -- kept OUTSIDE this
    repository, in the ledger's repo: `$LEDGER_TOOLS_DENYLIST`, default
    `../beancount-ledgers/.public-denylist`.  One term per line, matched
    case-insensitively as a whole word; `re:` starts a regular expression; `#`
    starts a comment.

A missing denylist is an error, not a pass: the check fails closed.

    python scripts/check_public.py --all
"""

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DENYLIST = os.path.join(os.path.dirname(HERE), "beancount-ledgers", ".public-denylist")

BUILTIN = [
    ("IBKR account id", re.compile(r"\bU(?!0000)\d{7,8}\b")),   # U0000001: a placeholder
    ("country-numbered account", re.compile(
        r"\b(?:Assets|Liabilities|Income|Expenses|Equity):\d\d[A-Z]{2}\b")),
    ("long reference number", re.compile(r"(?<![\d.])\d{12,}(?![\d.])")),
]


def denylist(path):
    if not os.path.exists(path):
        sys.exit("check_public: no denylist at %s -- set LEDGER_TOOLS_DENYLIST" % path)
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            term = line.strip()
            if not term or term.startswith("#"):
                continue
            if term.startswith("re:"):
                rx = re.compile(term[3:], re.IGNORECASE)
            else:
                rx = re.compile(r"(?<!\w)%s(?!\w)" % re.escape(term), re.IGNORECASE)
            out.append(("denylist", rx))
    return out


def tracked():
    r = subprocess.run(["git", "ls-files"], cwd=HERE, capture_output=True, text=True, check=True)
    return [os.path.join(HERE, f) for f in r.stdout.splitlines()]


def main(argv):
    files = tracked() if argv == ["--all"] else argv
    checks = BUILTIN + denylist(os.environ.get("LEDGER_TOOLS_DENYLIST", DEFAULT_DENYLIST))
    hits = 0
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for n, line in enumerate(lines, 1):
            for what, rx in checks:
                m = rx.search(line)
                if m:
                    hits += 1
                    # the denylist's own terms are private: name the line, not the term
                    shown = m.group(0) if what != "denylist" else "<private term>"
                    print("%s:%d: %s: %s" % (os.path.relpath(f, HERE), n, what, shown))
    if hits:
        print("check_public: %d hit(s); this repository is public" % hits)
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
