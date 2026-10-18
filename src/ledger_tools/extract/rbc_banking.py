"""Parse RBC personal-banking statements (chequing and savings).

Extraction is `pdftotext -table`. `-layout` splits a transaction table into
separate date / description / amount streams that no longer line up, and says
nothing about it.

Two facts make the extraction self-checking:

  * money is right-aligned under its column, so on any one page the token END
    positions fall into at most three tight clusters (jitter <= 3 chars, gaps
    >= 15). The RIGHTMOST cluster is always Balance ($) -- the Opening/Closing
    Balance rows guarantee it is non-empty on every page. Of the rest, left =
    Withdrawals, right = Deposits. Where only one non-balance cluster exists the
    page is ambiguous and both readings are tried, and the one that reproduces
    the stated totals wins.
  * every printed Balance ($) must equal the running balance, and the signed
    rows must reproduce the stated Total deposits / Total withdrawals / closing
    balance.

Do NOT map tokens onto the column-header offsets: the headers sit 5-10
characters right of the numbers under them, by different amounts per column, so
"nearest header" silently reads deposits as withdrawals.

A negative balance prints as `- 4.00` / `- $4.00` -- minus sign, space, number.

Usage:
    ledger-extract rbc_banking [--docs DIR] [--since 2025-01-01] \
        [--account daily|savings|both] [--json out.json]
"""

import argparse
import itertools
import os
import re
from decimal import Decimal

from ledger_tools.extract import common

D = common.D
MONEY = re.compile(r'[\d,]+\.\d{2}')
DATE = re.compile(r'^(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b')
MONTHS = common.MON3
FULLMONTH = common.MONTH_FULL
PERIOD = re.compile(r'From\s+(\w+)\s+(\d{1,2}),\s+(\d{4})\s+to\s+(\w+)\s+(\d{1,2}),\s+(\d{4})')
OPEN_RE = re.compile(r'Your opening balance on \w+ \d{1,2}, \d{4}\s+(-\s*)?\$?([\d,]+\.\d{2})')
CLOSE_RE = re.compile(r'Your closing balance on \w+ \d{1,2}, \d{4}\s+=?\s*(-\s*)?\$?([\d,]+\.\d{2})')
DEP_RE = re.compile(r'Total deposits into your account\s+\+?\s*\$?([\d,]+\.\d{2})')
WD_RE = re.compile(r'Total withdrawals from your account\s+-?\s*\$?([\d,]+\.\d{2})')

GAP = 8            # chars; smaller than the real column gap, bigger than the jitter

ACCOUNTS = {
    "daily": ("rbc-daily", "RBC chequing"),        # (source, default label)
    "savings": ("rbc-savings", "RBC savings"),
}


def neg_before(line, start):
    """True if the token at `start` is printed with a detached minus: `- 4.00`."""
    return re.search(r'-\s*\$?\s*$', line[:start]) is not None


def scan(text):
    """-> (period, stated totals, [page]) where a page is a list of raw detail lines."""
    lines = text.split('\n')
    flat = ' '.join(l.strip() for l in lines)
    m = PERIOD.search(flat)
    if not m:
        raise ValueError('no period line')
    period = ((int(m.group(3)), FULLMONTH[m.group(1)], int(m.group(2))),
              (int(m.group(6)), FULLMONTH[m.group(4)], int(m.group(5))))

    def one(rx, signed=False):
        mm = rx.search(flat)
        if not mm:
            return None
        if signed:
            v = D(mm.group(2))
            return -v if mm.group(1) else v
        return D(mm.group(1))

    stated = dict(opening=one(OPEN_RE, True), closing=one(CLOSE_RE, True),
                  deposits=one(DEP_RE), withdrawals=one(WD_RE))

    pages, cur, off, inside = [], None, 0, False
    for ln in lines:
        if 'Withdrawals ($)' in ln and 'Balance ($)' in ln and 'Description' in ln:
            if cur is not None:
                pages.append(cur)
            off = ln.index('Date')
            cur, inside = [], True
            continue
        if not inside:
            continue
        cut = ln[off:] if len(ln) > off else ''
        if re.match(r'^\s*\d+ of \d+\s*$', cut) or 'Important information' in ln \
                or 'Please check this Account Statement' in ln:
            pages.append(cur)
            cur, inside = None, False
            continue
        if cut.strip():
            cur.append(cut)
    if cur is not None:
        pages.append(cur)
    return period, stated, pages


def columns(page):
    """Cluster money-token end positions on one page -> (balance_cut, split_candidates)."""
    ends = sorted({m.end() for ln in page for m in MONEY.finditer(ln)})
    if not ends:
        return None, []
    groups = []
    for e in ends:
        if groups and e - groups[-1][-1] <= GAP:
            groups[-1].append(e)
        else:
            groups.append([e])
    if len(groups) > 3:
        raise ValueError('%d column clusters: %s' % (len(groups), groups))
    bal = groups[-1]
    rest = groups[:-1]
    bal_cut = (rest[-1][-1] + bal[0]) / 2 if rest else bal[0] - 1
    if len(rest) == 2:
        return bal_cut, [(rest[0][-1] + rest[1][0]) / 2]      # w < cut <= d
    if len(rest) == 1:
        return bal_cut, ['AMBIG']
    return bal_cut, []


def rows_of(page, bal_cut, wd_cut):
    """Yield dicts: date?, desc, w, d, bal -- one per detail line that carries money."""
    out = []
    pending = []
    cur_date = None
    for ln in page:
        toks = [(m.group(0), m.start(), m.end()) for m in MONEY.finditer(ln)]
        head = ln.strip()
        dm = DATE.match(head)
        if dm:
            cur_date = (int(dm.group(1)), MONTHS[dm.group(2)])
        desc = (ln[:toks[0][1]] if toks else ln).strip()
        if dm:
            desc = DATE.sub('', desc, count=1).strip()
        w = d = bal = None
        for t, s, e in toks:
            v = D(t)
            if bal_cut is not None and e > bal_cut:
                bal = -v if neg_before(ln, s) else v
            elif wd_cut == 'AMBIG':
                w = v                      # placeholder; caller flips
            elif wd_cut is not None and e <= wd_cut:
                w = v
            else:
                d = v
        kind = 'open' if 'Opening Balance' in desc else \
               'close' if 'Closing Balance' in desc else 'txn'
        if kind != 'txn':
            out.append(dict(kind=kind, bal=bal))
            pending = []
            continue
        if desc:
            pending.append(desc)
        if w is None and d is None and bal is None:
            continue
        out.append(dict(kind='txn', date=cur_date, desc=list(pending), w=w, d=d,
                        bal=bal, raw=ln.rstrip()))
        pending = []
    return out


def verify(rows, period, stated):
    errs = []
    (yf, mf, _), _ = period
    txns = [r for r in rows if r['kind'] == 'txn']
    opens = [r for r in rows if r['kind'] == 'open']
    closes = [r for r in rows if r['kind'] == 'close']

    # RBC prints the date only when it changes, so a continuation page can open with
    # rows that still belong to the last date on the page before: forward-fill.
    last = None
    for t in txns:
        if t['date'] is None:
            t['date'] = last
        else:
            last = t['date']

    y, prev_m = yf, mf
    for t in txns:
        if t['date'] is None:
            errs.append("row with no date: %r" % t['raw'])
            t['iso'] = None
            continue
        dd, mm = t['date']
        if mm < prev_m:
            y += 1
        prev_m = mm
        t['iso'] = '%04d-%02d-%02d' % (y, mm, dd)

    opening = opens[0]['bal'] if opens and opens[0]['bal'] is not None else stated['opening']
    if opens and opens[0]['bal'] is not None and stated['opening'] is not None \
            and opens[0]['bal'] != stated['opening']:
        errs.append("Opening Balance row %s != summary %s"
                    % (opens[0]['bal'], stated['opening']))

    bal = opening
    for t in txns:
        amt = (t['d'] or 0) - (t['w'] or 0)
        t['signed'] = amt
        bal += amt
        if t['bal'] is not None and t['bal'] != bal:
            errs.append("running %s != printed %s at %s :: %r"
                        % (bal, t['bal'], t['iso'], t['raw'][:80]))
            bal = t['bal']

    closing = closes[0]['bal'] if closes and closes[0]['bal'] is not None else stated['closing']
    if closing is not None and bal != closing:
        errs.append('chain ends %s, closing balance %s' % (bal, closing))
    dep = sum((t['d'] for t in txns if t['d']), Decimal(0))
    wd = sum((t['w'] for t in txns if t['w']), Decimal(0))
    if stated['deposits'] is not None and dep != stated['deposits']:
        errs.append("deposits %s != stated %s" % (dep, stated['deposits']))
    if stated['withdrawals'] is not None and wd != stated['withdrawals']:
        errs.append("withdrawals %s != stated %s" % (wd, stated['withdrawals']))
    if opening is not None and closing is not None and opening - wd + dep != closing:
        errs.append('%s - %s + %s != %s' % (opening, wd, dep, closing))
    return dict(txns=txns, opening=opening, closing=closing, errs=errs)


def statement(path, refresh=False):
    """Parse one statement PDF (or an already-extracted .txt)."""
    if path.lower().endswith('.txt'):
        with open(path, encoding='utf-8', errors='replace') as fh:
            text = fh.read()
    else:
        text = common.pdftotext(path, '-table', refresh=refresh)
    period, stated, pages = scan(text)
    cols = [columns(p) for p in pages]
    choices = []
    for bal_cut, cands in cols:
        choices.append([None] if not cands else
                       (['w', 'd'] if cands[0] == 'AMBIG' else [cands[0]]))
    best = None
    for combo in itertools.product(*choices):
        rows = []
        for p, (bal_cut, _), pick in zip(pages, cols, combo):
            if pick == 'w':
                rows += rows_of(p, bal_cut, 10 ** 6)
            elif pick == 'd':
                rows += rows_of(p, bal_cut, -1)
            else:
                rows += rows_of(p, bal_cut, pick)
        res = verify(rows, period, stated)
        if not res['errs']:
            best = res
            break
        if best is None or len(res['errs']) < len(best['errs']):
            best = res
    best['path'] = path
    best['period'] = period
    best['stated'] = stated
    return best


def to_json(st):
    return dict(stmt=os.path.basename(st['path']),
                p_from='%04d-%02d-%02d' % st['period'][0],
                p_to='%04d-%02d-%02d' % st['period'][1],
                opening=st['opening'], closing=st['closing'],
                rows=[dict(date=t.get('iso'), desc=' | '.join(t['desc']),
                           amount=t.get('signed'), bal=t['bal'])
                      for t in st['txns']])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    common.add_common_args(ap)
    ap.add_argument("--account", choices=["daily", "savings", "both"], default="both")
    args = ap.parse_args(argv)
    docs = common.docs_dir(args.docs)

    rc = 0
    for key in (["daily", "savings"] if args.account == "both" else [args.account]):
        src, label = ACCOUNTS[key]
        label = common.label(src, label)
        files = common.statements(docs, common.doc_folder(src), "*.statement.pdf", args.since, args.until)
        parsed, code = common.run_all(
            lambda f: statement(f, args.refresh), files, label,
            chain=("balance", lambda s: s.get("closing"), lambda s: s.get("opening")),
            json_path=(args.json and args.json.replace(".json", ".%s.json" % key)),
            to_json=to_json)
        rc |= code
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
