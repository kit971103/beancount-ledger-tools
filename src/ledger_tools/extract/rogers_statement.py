"""Parse Rogers Red World Elite Mastercard statements.

Extraction is `pdftotext -raw`. `-layout` puts the `Calculating your balance`
figures one line off their labels (a `Previous balance` that is really the
payments figure) and splits the
transaction table into a date stream and a description stream; `-raw` keeps
each label with its figure and each row on one line.

A row is `Mon D Mon D <description> <amount>` -- transaction date, then posting
date, which is the ledger date. A credit
prints with a leading `-`. A foreign purchase is followed by a line
`FOREIGN CURRENCY USD 10.00 @ 1.400000000`, attached to the row above it.

Four identities are checked per statement:

  1. `Previous balance - Payments & credits + New purchases & debits + Cash
     advances + Promotional balances + Fees + Interest = New Balance`;
  2. the negative rows sum to `Payments & credits`;
  3. the positive rows sum to purchases + cash advances + promotional + fees +
     interest (fee and interest rows are rows in the table);
  4. each `Previous balance` equals the previous statement's `New Balance`
     (reported as a chain break).

Usage:
    ledger-extract rogers_statement [--docs DIR] [--since 2025-01-01] \
        [--json out.json]
"""

import argparse
import os
import re
from datetime import date
from decimal import Decimal

from ledger_tools.extract import common

D = common.D
MON = common.MON3
SOURCE = "rogers"

ROW = re.compile(r'^([A-Z][a-z]{2}) (\d{1,2}) ([A-Z][a-z]{2}) (\d{1,2}) (.*?) '
                 r'(-?[\d,]+\.\d{2})$')
FX = re.compile(r'^FOREIGN CURRENCY ([A-Z]{3}) ([\d,]+\.\d{2}) @ ([\d.]+)$')
PERIOD = re.compile(r'Statement Period ([A-Z][a-z]{2}) (\d{1,2}), (\d{4}) - '
                    r'([A-Z][a-z]{2}) (\d{1,2}), (\d{4})')
LABELS = dict(prev='Previous balance', credits='Payments & credits',
              purchases='New purchases & debits', advances='Cash advances',
              promo='Promotional balances', fees='Fees', interest='Interest',
              new='New Balance')


def statement(path, refresh=False):
    text = common.pdftotext(path, '-raw', refresh=refresh)
    lines = [l.strip() for l in text.split('\n')]

    pm = next((PERIOD.search(l) for l in lines if PERIOD.search(l)), None)
    if not pm:
        raise ValueError('no statement period line')
    period_end = date(int(pm.group(6)), MON[pm.group(4)], int(pm.group(5)))

    stated = {}
    for key, label in LABELS.items():
        rx = re.compile(r'^' + re.escape(label) + r' \$(-?[\d,]+\.\d{2})$')
        for l in lines:
            m = rx.match(l)
            if m:
                stated[key] = D(m.group(1))
                break

    txns = []
    for l in lines:
        m = ROW.match(l)
        if m:
            txns.append(dict(txn_date=_iso(period_end, m.group(1), int(m.group(2))),
                             date=_iso(period_end, m.group(3), int(m.group(4))),
                             desc=m.group(5).strip(), amount=D(m.group(6)), fx=None))
            continue
        m = FX.match(l)
        if m and txns:
            txns[-1]['fx'] = dict(currency=m.group(1), amount=D(m.group(2)),
                                  rate=m.group(3))

    errs = []
    missing = [k for k in LABELS if k not in stated]
    if missing:
        errs.append('summary figures not found: %s' % ', '.join(missing))
    else:
        charges = sum(stated[k] for k in ('purchases', 'advances', 'promo', 'fees',
                                          'interest'))
        if stated['prev'] - stated['credits'] + charges != stated['new']:  # identity 1
            errs.append('%s - %s + %s != New Balance %s'
                        % (stated['prev'], stated['credits'], charges, stated['new']))
        neg = -sum((t['amount'] for t in txns if t['amount'] < 0), Decimal(0))
        pos = sum((t['amount'] for t in txns if t['amount'] > 0), Decimal(0))
        if neg != stated['credits']:                                     # identity 2
            errs.append('credit rows sum to %s, Payments & credits %s'
                        % (neg, stated['credits']))
        if pos != charges:                                               # identity 3
            errs.append('charge rows sum to %s, charges stated %s' % (pos, charges))

    return dict(path=path, period_end=str(period_end), stated=stated, txns=txns,
                errs=errs)


def _iso(period_end, mon3, day):
    """A cycle spans about a month, so a date more than 40 days after the period
    end belongs to the year before."""
    d = date(period_end.year, MON[mon3], day)
    if (d - period_end).days > 40:
        d = date(period_end.year - 1, MON[mon3], day)
    return str(d)


def to_json(st):
    return dict(stmt=os.path.basename(st['path']), period_end=st['period_end'],
                stated=st['stated'],
                rows=[dict(date=t['date'], txn_date=t['txn_date'], desc=t['desc'],
                           amount=t['amount'], fx=t['fx']) for t in st['txns']])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    common.add_common_args(ap)
    args = ap.parse_args(argv)
    docs = common.docs_dir(args.docs)
    files = common.statements(docs, common.doc_folder(SOURCE), "*.statement.pdf", args.since, args.until)
    _, rc = common.run_all(
        lambda f: statement(f, args.refresh), files, "Rogers Red World Elite",
        chain=("balance", lambda s: s["stated"].get("new"),
               lambda s: s["stated"].get("prev")),
        json_path=args.json, to_json=to_json)
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
