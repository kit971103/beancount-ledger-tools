"""Read a sub-ledger back as rows, and compare it with an export.

This is the other half of a reconciliation: the extractors in this package turn
a bank's own document into checked rows, and this turns the ledger into rows of
the same shape so the two can be compared as **multisets in both directions** --
which is the claim a reconciliation makes for every reconciled sub-ledger, and it
is not the same claim as "I spot-checked a few".

The parse is deliberately textual and plugin-free: it reads what is written in
the file, including the `&` scheduled entries that `ledger_tools.plugin.exclude_flagged`
drops from every report, and it is quote-aware because a metadata string may
span several lines.

Usage:
    ledger-extract ledger \\
        --file sub-ledgers/amex \\
        --account Liabilities:Card:Amex --source amex --since 2025-01-01
    ledger-extract ledger \\
        --file sub-ledgers/rbc \\
        --account Assets:Bank:Checking --statements rbc-daily --since 2025-01-01
"""

import argparse
import datetime
import os
import re
from collections import Counter
from decimal import Decimal

from ledger_tools.extract import common

Q = '"'
TXN_RE = re.compile(r'^(\d{4}-\d{2}-\d{2}) ([*!?&]) (.*)$')
# an optional `@ x CCY` / `@@ x CCY` price follows the amount (the IBKR FX trades)
POST_RE = re.compile(r'^\s\s+([A-Z][A-Za-z0-9:\-]+)\s+(-?[\d.,]+)\s+([A-Z_][A-Z0-9_.\-]*)'
                     r'(?:\s+@@?\s+-?[\d.,]+\s+[A-Z_][A-Z0-9_.\-]*)?\s*(?:;.*)?$')
META_RE = re.compile(r'^\s\s+([a-z][a-z0-9\-_]*):\s*(.*)$')
# bank_csv sources that state a charge as positive; the ledger posts it negative
CARD_SOURCES = ("amex", "rogers", "scotia", "simplii")


def parse(path):
    """-> list of transactions: date, flag, payee, narration, tags, postings, meta.

    `path` is a sub-ledger file, or a sub-ledger folder -- the bank sub-ledgers are
    split one file per statement -- in which case every `*.beancount` in it is read.
    """
    if os.path.isdir(path):
        txns = []
        for f in sorted(os.listdir(path)):
            if f.endswith('.beancount'):
                txns.extend(parse(os.path.join(path, f)))
        return txns
    with open(path, encoding='utf-8') as fh:
        lines = fh.read().split('\n')
    txns, cur, open_str = [], None, False
    for n, line in enumerate(lines, 1):
        if open_str:
            cur['raw'].append(line)
            cur['end'] = n
            if line.count(Q) % 2 == 1:
                open_str = False
            continue
        m = TXN_RE.match(line)
        if m:
            if cur:
                txns.append(cur)
            cur = dict(date=datetime.date.fromisoformat(m.group(1)), flag=m.group(2),
                       head=m.group(3), start=n, end=n, meta={}, postings=[],
                       raw=[line])
            continue
        if cur is None:
            continue
        if not line.startswith(' '):
            txns.append(cur)
            cur = None
            continue
        cur['raw'].append(line)
        cur['end'] = n
        mp = POST_RE.match(line)
        if mp:
            cur['postings'].append(dict(account=mp.group(1),
                                        amount=Decimal(mp.group(2).replace(',', '')),
                                        currency=mp.group(3),
                                        line='%s:%d' % (os.path.basename(path), n)))
            continue
        mm = META_RE.match(line)
        if mm:
            cur['meta'][mm.group(1)] = mm.group(2).strip()
            if mm.group(2).count(Q) % 2 == 1:
                open_str = True
    if cur:
        txns.append(cur)

    for t in txns:
        quoted = re.findall(Q + '([^' + Q + ']*)' + Q, t['head'])
        t['payee'] = quoted[0] if len(quoted) > 1 else None
        t['narration'] = quoted[1] if len(quoted) > 1 else (quoted[0] if quoted else '')
        bare = re.sub(Q + '[^' + Q + ']*' + Q, '', t['head'])
        t['tags'] = re.findall(r'#([A-Za-z0-9\-_/.]+)', bare)
    return txns


def postings_to(txns, account, since=None, until=None):
    """Every posting to one account, as (date, amount, label) rows."""
    out = []
    for t in txns:
        for p in t['postings']:
            if p['account'] != account:
                continue
            d = t['date'].isoformat()
            if since and d < since:
                continue
            if until and d > until:
                continue
            out.append(dict(date=d, amount=p['amount'],
                            desc=t['meta'].get('bank-desc', '').strip('"') or t['narration'],
                            ref=t['meta'].get('bank-ref', '').strip("'\""),
                            flag=t['flag'], line=p['line']))
    return out


def compare(export_rows, ledger_rows, key=lambda r: (r['date'], r['amount'])):
    """Multiset equality in both directions. -> (only_in_export, only_in_ledger)."""
    a, b = Counter(key(r) for r in export_rows), Counter(key(r) for r in ledger_rows)
    return sorted((a - b).elements()), sorted((b - a).elements())


# rows an extractor produces, addressed by name for the CLI
STATEMENT_SOURCES = {
    "rbc-daily": ("rbc_banking", dict(account="daily")),
    "rbc-savings": ("rbc_banking", dict(account="savings")),
    "rbc-mastercard": ("rbc_mastercard", {}),
    "bmo-main": ("bmo", dict(account="main")),
    "bmo-loc": ("bmo", dict(account="loc")),
    "scotia": ("scotia_visa", {}),
    "simplii": ("simplii_visa", {}),
    "rogers": ("rogers_statement", {}),
}


def statement_rows(name, docs=None, since=None, until=None):
    """Rows from one of the PDF statement extractors, in the export-row shape."""
    import importlib
    mod_name, opts = STATEMENT_SOURCES[name]
    mod = importlib.import_module("ledger_tools.extract." + mod_name)
    docs = common.docs_dir(docs)

    if mod_name == "rbc_banking":
        parse_one, sign = mod.statement, 1
    elif mod_name == "bmo":
        parse_one = mod.chequing if opts["account"] == "main" else mod.line_of_credit
        # a line of credit charge grows a liability, which the ledger writes negative
        sign = 1 if opts["account"] == "main" else -1
    else:
        parse_one, sign = mod.statement, -1        # a card charge is a negative posting

    rows = []
    for f in common.statements(docs, common.doc_folder(name), "*.statement.pdf"):
        st = parse_one(f)
        for t in st["txns"]:
            d = t.get("iso") or t.get("date") or t.get("post_iso")
            amt = t.get("signed", t.get("amount"))
            if d is None or amt is None:
                continue
            rows.append(dict(date=d, amount=sign * amt,
                             desc=t.get("desc") if isinstance(t.get("desc"), str)
                             else ' | '.join(t.get("desc") or ()),
                             ref=t.get("ref"), src=os.path.basename(f)))
    if since:
        rows = [r for r in rows if r["date"] >= since]
    if until:
        rows = [r for r in rows if r["date"] <= until]
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    common.add_common_args(ap)
    ap.add_argument("--file", required=True, help="the sub-ledger file or folder to read")
    ap.add_argument("--account", required=True, help="the account its postings hit")
    ap.add_argument("--source", help="a CSV export to compare with (see bank_csv)")
    ap.add_argument("--statements", choices=sorted(STATEMENT_SOURCES),
                    help="a set of statement PDFs to compare with")
    args = ap.parse_args(argv)

    txns = parse(args.file)
    led = postings_to(txns, args.account, args.since, args.until)
    print("%s: %d transactions, %d postings to %s"
          % (os.path.basename(os.path.normpath(args.file)), len(txns), len(led), args.account))
    flags = Counter(r["flag"] for r in led)
    print("   flags: %s" % ", ".join("%s %d" % (f, n) for f, n in sorted(flags.items())))

    export = None
    if args.source:
        from ledger_tools.extract import bank_csv
        rows, _ = bank_csv.load(args.source, args.docs, args.since, args.until)
        # a card export states a charge as positive; the ledger posts it negative
        flip = -1 if args.source in CARD_SOURCES else 1
        export = [dict(r, amount=flip * r["amount"]) for r in rows]
    elif args.statements:
        export = statement_rows(args.statements, args.docs, args.since, args.until)

    if export is None:
        return 0

    only_export, only_ledger = compare(export, led)
    print("   export rows %d, ledger postings %d" % (len(export), len(led)))
    for label, rows in (("in the export, not the ledger", only_export),
                        ("in the ledger, not the export", only_ledger)):
        print("   %s: %d" % (label, len(rows)))
        for d, a in rows[:15]:
            print("       %s %12s" % (d, a))
    return 1 if (only_export or only_ledger) else 0


if __name__ == '__main__':
    raise SystemExit(main())
