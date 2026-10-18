"""Parse Scotia Momentum VISA Infinite statements -- with pdfplumber, not pdftotext.

**`pdftotext` is the wrong tool for these statements, in both of its useful
modes, and it fails silently.** `-layout` emits the amount column as a separate
stream offset from the rows it belongs to:

    001 Feb 26 Feb 27 PAYMENT SERVICE        TORONTO ON
                                                              120.00-
    002 Feb 27 Feb 28 ONLINE STORE           800-555-0100 ON    15.00

Nothing there looks broken, and a matcher fed from it pairs the wrong merchant
with the wrong amount all the way down. The usual fix is `-bbox-layout`, but the
pdftotext on this machine is Xpdf 4.00 (Glyph & Cog), not poppler: it has
neither that flag nor `-table`, and exits 99.

So: `pdfplumber` -- group `extract_words()` by rounded `top`, sort by `x0`, and
every row reconstructs exactly. Two things bite when reading the result:

  * the header carries footnote daggers as raw `\\x87` bytes *inside* the words,
    so `New Balance\\x87 = $120.00` does not match a plain `New Balance` regex.
    Control bytes are stripped before anything is matched;
  * the words are run together unpredictably -- `Previous balance,Feb 3/25` has
    no space after the comma, `Totalcashbackearned` has none at all. Summary
    figures are therefore matched against a whitespace-stripped copy of the
    text, so spacing cannot change the answer.

Four identities are checked per statement:

  1. `Previous balance + sum(transaction rows) + Interest = New Balance`;
  2. `Payments/credits` equals the negative rows, `Purchases/charges` the
     positive ones;
  3. each `Previous balance` equals the previous statement's New Balance
     (reported as a chain break);
  4. the cash back block chains -- see below.

Scotia changed the reward scheme mid-history and the statements change with it.
Up to early 2026 the statement prints a *cumulative* `Total cash back earned`
which resets on the annual November payout, so a cycle's earning is the
difference between consecutive statements. From then on it prints a proper
ledger: `Previous Balance + Earned -
Redeemed + Bonus, Accelerators & Adjustments = Total Cash Back Balance`. Both
formats are read; the second is checked as an identity, the first only chains.

Usage:
    ledger-extract scotia_visa [--docs DIR] [--since 2025-01-01] \
        [--json out.json]
"""

import argparse
import os
import re
from decimal import Decimal

from ledger_tools.extract import common

D = common.D
SOURCE = "scotia"

CTRL = re.compile(r'[\x00-\x1f\x7f-\xa0]')
MON = common.MON3

# NNN  TRANS-DATE  POSTING-DATE  DESCRIPTION  AMOUNT[-]
ROW = re.compile(r'^(\d{3})\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+(.*)$')
TRAILING_AMT = re.compile(r'([\d,]+\.\d{2})(-?)$')
PERIOD = re.compile(r'([A-Z][a-z]{2})(\d{1,2}),(\d{4})-([A-Z][a-z]{2})(\d{1,2}),(\d{4})'
                    r'StatementPeriod')
STMT_DATE = re.compile(r'([A-Z][a-z]{2})(\d{1,2}),(\d{4})StatementDate')


def lines_of(path):
    """Every page's words, grouped into visual lines, control bytes stripped."""
    import pdfplumber
    out = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            rows = {}
            for w in page.extract_words():
                rows.setdefault(round(w["top"]), []).append(w)
            for top in sorted(rows):
                text = " ".join(w["text"] for w in sorted(rows[top], key=lambda w: w["x0"]))
                out.append(CTRL.sub("", text))
    return out


def _fig(squashed, label, money=r'\$?(-?[\d,]+\.\d{2})'):
    """Read a figure by its label out of the whitespace-stripped text."""
    m = re.search(re.escape(label.replace(" ", "")) + r'[=+\-]?' + money, squashed)
    return D(m.group(1)) if m else None


def statement(path, refresh=False):
    lines = lines_of(path)
    squashed = re.sub(r'\s+', '', ' '.join(lines))

    sd = STMT_DATE.search(squashed)
    if not sd:
        raise ValueError('no Statement Date')
    stmt_date = '%04d-%02d-%02d' % (int(sd.group(3)), MON[sd.group(1)], int(sd.group(2)))
    pm = PERIOD.search(squashed)
    if not pm:
        raise ValueError('no Statement Period')
    p_from = '%04d-%02d-%02d' % (int(pm.group(3)), MON[pm.group(1)], int(pm.group(2)))
    p_to = '%04d-%02d-%02d' % (int(pm.group(6)), MON[pm.group(4)], int(pm.group(5)))

    # `Previous balance,Feb 3/25 $0.00` -- the date sits between the label and the
    # figure with no space, so the date must be consumed explicitly. A lazy
    # `(\S+)\s+([\d.,]+)` captures `3` here: it parses, and it is wrong.
    prev = None
    pb = re.search(r'Previousbalance,?[A-Z][a-z]{2}\d{1,2}/\d{2}\$?(-?[\d,]+\.\d{2})(-?)',
                   squashed)
    if pb:
        prev = D(pb.group(1))
        if pb.group(2) == '-':
            prev = -prev

    stated = dict(
        prev=prev,
        interest=_fig(squashed, 'Interest'),
        payments=_fig(squashed, 'Payments/credits'),
        purchases=_fig(squashed, 'Purchases/charges'),
        account=_fig(squashed, 'Account='),
        new=_fig(squashed, 'New'),
        credit_limit=_fig(squashed, 'Creditlimit'),
    )

    # cash back, both schemes
    cash = dict(
        cumulative=_fig(squashed, 'Totalcashbackearned'),
        prev=_fig(squashed, 'PreviousBalance'),
        earned=_fig(squashed, 'Earnedthisstatementperiod'),
        redeemed=_fig(squashed, 'Redeemedthisstatementperiod'),
        adjustments=_fig(squashed, 'Bonus,Accelerators&Adjustments'),
        total=_fig(squashed, 'TotalCashBackBalance'),
    )

    txns = []
    for ln in lines:
        m = ROW.match(ln.strip())
        if not m:
            continue
        rest = m.group(6).strip()
        am = TRAILING_AMT.search(rest)
        if not am:
            continue
        amt = D(am.group(1))
        if am.group(2) == '-':            # a trailing minus marks a credit
            amt = -amt
        desc = rest[:am.start()].strip()
        txns.append(dict(n=int(m.group(1)),
                         txn_date=_iso(stmt_date, m.group(2), int(m.group(3))),
                         date=_iso(stmt_date, m.group(4), int(m.group(5))),
                         desc=re.sub(r'\s{2,}', ' ', desc), amount=amt, raw=ln.strip()))

    errs = []
    if [t['n'] for t in txns] != list(range(1, len(txns) + 1)):
        errs.append('row numbers are not 1..n: %s' % [t['n'] for t in txns])

    pos = sum((t['amount'] for t in txns if t['amount'] > 0), Decimal(0))
    neg = -sum((t['amount'] for t in txns if t['amount'] < 0), Decimal(0))
    if None not in (stated['prev'], stated['new'], stated['interest']):   # identity 1
        got = stated['prev'] + pos - neg + stated['interest']
        if got != stated['new']:
            errs.append('%s + %s - %s + %s != new balance %s'
                        % (stated['prev'], pos, neg, stated['interest'], stated['new']))
    if stated['purchases'] is not None and pos != stated['purchases']:    # identity 2
        errs.append('positive rows sum to %s, Purchases/charges %s'
                    % (pos, stated['purchases']))
    if stated['payments'] is not None and neg != stated['payments']:
        errs.append('negative rows sum to %s, Payments/credits %s'
                    % (neg, stated['payments']))
    if None not in (cash['prev'], cash['earned'], cash['total']):         # identity 4
        got = (cash['prev'] + cash['earned'] - (cash['redeemed'] or 0)
               + (cash['adjustments'] or 0))
        if got != cash['total']:
            errs.append('cash back %s + %s - %s + %s != %s'
                        % (cash['prev'], cash['earned'], cash['redeemed'],
                           cash['adjustments'], cash['total']))

    return dict(path=path, date=stmt_date, p_from=p_from, p_to=p_to,
                stated=stated, cash=cash, txns=txns, errs=errs)


def _iso(stmt_date, mon3, day):
    """A cycle never spans more than a month, so a date after the statement date
    belongs to the year before it."""
    y = int(stmt_date[:4])
    mo = MON[mon3]
    cand = '%04d-%02d-%02d' % (y, mo, day)
    if cand > stmt_date:
        cand = '%04d-%02d-%02d' % (y - 1, mo, day)
    return cand


def to_json(st):
    return dict(stmt=os.path.basename(st['path']), date=st['date'],
                p_from=st['p_from'], p_to=st['p_to'], stated=st['stated'],
                cash=st['cash'],
                rows=[dict(n=t['n'], date=t['date'], txn_date=t['txn_date'],
                           desc=t['desc'], amount=t['amount']) for t in st['txns']])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    common.add_common_args(ap)
    args = ap.parse_args(argv)
    docs = common.docs_dir(args.docs)
    files = common.statements(docs, common.doc_folder(SOURCE), "*.statement.pdf", args.since, args.until)
    _, rc = common.run_all(
        lambda f: statement(f, args.refresh), files, "Scotia Momentum VISA Infinite",
        chain=("balance", lambda s: s["stated"].get("new"),
               lambda s: s["stated"].get("prev")),
        json_path=args.json, to_json=to_json)
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
