"""Shared plumbing for the bank-export extractors in this package.

Everything here exists because the extractors are run months apart, against
documents that are git-ignored: the caller says where the documents are, the
extraction is cached so a re-run is cheap, and a parse that cannot prove itself
against the figures the document states is reported as a problem rather than
silently used.

`documents/` is git-ignored and does NOT exist inside a git worktree, so a run
from a worktree must be given `--docs <path to the main checkout's documents>`.
Never create a junction to it from a worktree: `git worktree remove` follows the
junction and deletes the real files.
"""

import os
import re
import shutil
import subprocess
import sys
from decimal import Decimal

from ledger_tools import config

#: Where extracted text is cached. Outside the repo: it is derived data.
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "beancount-extract")

MONEY = re.compile(r'(?<![\d.,])-?\d{1,3}(?:,\d{3})*\.\d{2}(?![\d])')

MON3 = {m: i + 1 for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}
MONTH_FULL = {'January': 1, 'February': 2, 'March': 3, 'April': 4, 'May': 5,
              'June': 6, 'July': 7, 'August': 8, 'September': 9, 'October': 10,
              'November': 11, 'December': 12}


def D(s):
    """'1,234.56' / '$1,234.56' / '-$4.00' -> Decimal."""
    return Decimal(str(s).replace(',', '').replace('$', '').replace(' ', ''))


def pdftotext_bin():
    """The pdftotext that supports -table.

    The one shipped with Git for Windows is Xpdf 4.00: it has -table but not
    -bbox-layout, which is why the Scotia statements needed pdfplumber instead.
    """
    env = os.environ.get("PDFTOTEXT")
    if env and os.path.exists(env):
        return env
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    bundled = os.path.join(pf, "G" + "it", "mingw64", "bin", "pdftotext.exe")
    if os.path.exists(bundled):
        return bundled
    found = shutil.which("pdftotext")
    if found:
        return found
    raise RuntimeError(
        "no pdftotext found. Install poppler/xpdf, or set PDFTOTEXT to its path.")


def pdftotext(pdf, mode="-table", refresh=False):
    """Extract `pdf` to text in `mode`, caching the result. -> the text.

    Which mode a source needs is not a free choice and is recorded per source in
    this package's README; getting it wrong shifts amounts against their labels
    without saying so.
    """
    if mode not in ("-table", "-layout", "-raw", "-simple"):
        raise ValueError("unexpected pdftotext mode: %r" % mode)
    key = os.path.abspath(pdf).replace(":", "").replace("\\", "/")
    dst = os.path.join(CACHE, mode.lstrip("-"), key.lstrip("/") + ".txt")
    if refresh or not os.path.exists(dst) \
            or os.path.getmtime(dst) < os.path.getmtime(pdf):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        r = subprocess.run([pdftotext_bin(), mode, pdf, dst],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("pdftotext failed on %s: %s"
                               % (pdf, r.stderr.strip()[:300]))
    with open(dst, encoding="utf-8", errors="replace") as fh:
        return fh.read().replace("\r", "")


def docs_dir(arg=None):
    """Resolve the documents directory, with a message that says what to do."""
    d = (arg or os.environ.get("LEDGER_DOCS")
         or os.path.join(config.ledger_root(), "documents"))
    if not os.path.isdir(d):
        raise SystemExit(
            "documents directory not found: %s\n"
            "It is git-ignored, so it does not exist inside a worktree.\n"
            "Pass --docs <path>, or set LEDGER_DOCS." % d)
    return d


def doc_folder(source):
    """The documents sub-folder holding `source`'s files, from `[extract.docs]`.

    Folders mirror the ledger's account names, which are the owner's, so the
    mapping lives in the ledger's config, not here.
    """
    return config.get("extract", "docs", source)


def label(source, default):
    """How reports name `source`: `[extract.labels]`, else `default`."""
    return config.get("extract", "labels", source, default=default)


def statements(docs, subpath, pattern="*.pdf", since=None, until=None):
    """Source documents under `docs/subpath`, oldest first.

    Files are named `YYYY-MM-DD.<description>.<ext>`, so the date prefix sorts
    and filters them; `since`/`until` are ISO dates compared against it.
    """
    import glob
    root = os.path.join(docs, subpath.replace("/", os.sep))
    if not os.path.isdir(root):
        raise SystemExit("no such document folder: %s" % root)
    out = []
    for p in sorted(glob.glob(os.path.join(root, pattern))):
        base = os.path.basename(p)
        m = re.match(r'(\d{4}-\d{2}-\d{2})\.', base)
        d = m.group(1) if m else None
        if since and (d is None or d < since):
            continue
        if until and (d is None or d > until):
            continue
        out.append(p)
    return out


def add_common_args(ap):
    ap.add_argument("--docs", help="path to documents/ (default: <ledger>/documents)")
    ap.add_argument("--since", help="only documents dated on/after this ISO date")
    ap.add_argument("--until", help="only documents dated on/before this ISO date")
    ap.add_argument("--json", metavar="FILE", help="write the parsed rows here")
    ap.add_argument("--refresh", action="store_true",
                    help="re-run pdftotext even if the cached text is current")
    return ap


def write_json(path, obj):
    import json

    def enc(o):
        if isinstance(o, Decimal):
            return str(o)
        raise TypeError(type(o))

    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=1, default=enc)
        print("wrote", path)


def report(label, parsed, chain=None):
    """Print per-document problems and any break in a chained figure.

    `parsed` is a list of dicts carrying at least `path` and `errs`.
    `chain`, if given, is (name, closing_of, opening_of): consecutive documents
    must agree, which is the check that catches a missing statement.
    """
    bad = 0
    for st in parsed:
        if st["errs"]:
            bad += 1
            print("---", os.path.basename(st["path"]))
            for e in st["errs"]:
                print("   ", e)
    breaks = 0
    if chain:
        name, close_of, open_of = chain

        def fig(fn, st):
            """A document that failed to parse has no figures to chain."""
            try:
                return fn(st)
            except (KeyError, TypeError, IndexError):
                return None

        for a, b in zip(parsed, parsed[1:]):
            if fig(close_of, a) is None or fig(open_of, b) is None:
                continue
            if close_of(a) != open_of(b):
                breaks += 1
                print("%s CHAIN BREAK: %s ends %s -> %s starts %s"
                      % (name, os.path.basename(a["path"]), close_of(a),
                         os.path.basename(b["path"]), open_of(b)))
    n = sum(len(st.get("txns", ())) for st in parsed)
    print("%s: %d documents, %d rows, %d with problems, %d chain breaks"
          % (label, len(parsed), n, bad, breaks))
    return bad + breaks


def run_all(parse, files, label, chain=None, json_path=None, to_json=None):
    """Parse every file, report, optionally dump. -> (parsed, exit code)."""
    parsed = []
    for f in files:
        try:
            parsed.append(parse(f))
        except Exception as ex:
            print("PARSE FAIL", os.path.basename(f), repr(ex), file=sys.stderr)
            parsed.append(dict(path=f, errs=["parse failed: %r" % ex], txns=[]))
    problems = report(label, parsed, chain)
    if json_path:
        write_json(json_path, [to_json(s) for s in parsed] if to_json else parsed)
    return parsed, (1 if problems else 0)
