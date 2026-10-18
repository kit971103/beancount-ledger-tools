"""Parse Simplii Financial Cash Back Visa statements.

Extraction is `pdftotext -table`, and this is not a free choice. On the busier
statements `-layout` splits the transaction table into separate date /
description / amount streams that no longer line up, and says nothing about it.

The statement has two transaction sections, and which one a row is in decides
its sign: rows above `Total payments` are payments and credits, rows below it
(under `Your new charges and credits`, per card) are charges. A trailing `-`
marks a credit inside the charges section -- a refund.

The amount is the LAST money token on the line: a description carries its own
numbers, and the `Spend Categories` column sits between the description and the
amount.

Five identities are checked per statement:

  1. `Previous balance - Total credits + Total charges = New balance`;
  2. the payment rows sum to `Total payments`, the charge rows to `Total for
     <card>`;
  3. `Purchases + Cash advances + Interest + Fees = Total charges`;
  4. `As at last statement + earnings (4% / 1.5% / 0.5% / Promotions)
     - Cash Back redeemed = Total Cash Back`;
  5. each `Previous balance` equals the previous statement's `New balance`
     (reported as a chain break).

Usage:
    ledger-extract simplii_visa [--docs DIR] [--since 2025-01-01] \
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
SOURCE = "simplii"

ROW = re.compile(r'^([A-Z][a-z]{2})\s+(\d{1,2})\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+(.*)$')
MONEY = re.compile(r'(?<![\d.,])[\d,]+\.\d{2}(?![\d])')
# The cycle that crosses New Year prints a year on BOTH dates
# ("December 18, 2025 to January 17, 2026"); every other one only on the second.
PERIOD = re.compile(r'([A-Z][a-z]+)\s+(\d{1,2})(?:,\s*\d{4})?\s+to\s+'
                    r'([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})')
CASH_LINE = re.compile(r'(As at last statement|[\d.]+% Cash Back|Promotions|'
                       r'Cash Back redeemed|Total Cash Back)\s*([+\-=])\s*(-?[\d,]+\.\d{2})')


def _label(flat, name, sign=r'[+\-=]?'):
    m = re.search(re.escape(name) + r'\s*' + sign + r'\s*\$?(-?[\d,]+\.\d{2})', flat)
    return D(m.group(1)) if m else None


def statement(path, refresh=False):
    if path.lower().endswith('.txt'):
        with open(path, encoding='utf-8', errors='replace') as fh:
            text = fh.read()
    else:
        text = common.pdftotext(path, '-table', refresh=refresh)
    lines = text.split('\n')
    flat = re.sub(r'[ \t]+', ' ', ' '.join(l.strip() for l in lines))

    pm = PERIOD.search(flat)
    if not pm:
        raise ValueError('no statement period line')
    year = int(pm.group(5))
    end_month = common.MONTH_FULL.get(pm.group(3), MON.get(pm.group(3)[:3]))
    period_end = date(year, end_month, int(pm.group(4)))

    stated = dict(
        prev=_label(flat, 'Previous balance'),
        payments=_label(flat, 'Payments'),
        other_credits=_label(flat, 'Other credits'),
        total_credits=_label(flat, 'Total credits'),
        purchases=_label(flat, 'Purchases'),
        advances=_label(flat, 'Cash advances'),
        interest=_label(flat, 'Interest'),
        fees=_label(flat, 'Fees'),
        total_charges=_label(flat, 'Total charges'),
        new=_label(flat, 'New balance'),
        total_payments=_label(flat, 'Total payments'),
    )
    total_for_card = None
    m = re.search(r'Total for \d{4} [X ]+\d{4}\s*\$?([\d,]+\.\d{2})', flat)
    if m:
        total_for_card = D(m.group(1))

    cash = {}
    for lbl, _sign, val in CASH_LINE.findall(flat):
        key = lbl.strip()
        cash[key] = cash.get(key, Decimal(0)) + D(val)

    txns = []
    # The payments table comes first, but it is absent altogether on a statement
    # with no payment in the cycle (e.g. an account's first), so the section is
    # taken from the table header -- only the charges table has Spend Categories.
    section = 'payments'
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if 'Spend Categories' in s or s.startswith('Total payments'):
            section = 'charges'
            continue
        if s.startswith('Total for '):
            section = 'done'
            continue
        m = ROW.match(s)
        if not m or section == 'done':
            continue
        rest = m.group(5)
        hits = list(MONEY.finditer(rest))
        if not hits:
            continue
        last = hits[-1]
        amt = D(last.group(0))
        # A credit inside the charges table is marked either way round: the
        # refunds print a trailing `-`, and `CASH BACK CREDIT` a leading one.
        credit = (rest[last.end():last.end() + 1] == '-'
                  or rest[max(0, last.start() - 1):last.start()] == '-')
        if section == 'payments' or credit:
            amt = -amt
        desc = re.sub(r'\s{2,}', ' ', rest[:last.start()]).strip()
        # the dagger marking a Cash Back Multiplier row is not part of the text
        desc = desc.lstrip('\u2020\u00a0? ').strip()
        txns.append(dict(txn_date=_iso(period_end, m.group(1), int(m.group(2))),
                         date=_iso(period_end, m.group(3), int(m.group(4))),
                         desc=desc, amount=amt, section=section, raw=s))

    errs = []
    paid = -sum((t['amount'] for t in txns if t['section'] == 'payments'), Decimal(0))
    charged = sum((t['amount'] for t in txns if t['section'] == 'charges'), Decimal(0))

    if None not in (stated['prev'], stated['total_credits'],
                    stated['total_charges'], stated['new']):          # identity 1
        got = stated['prev'] - stated['total_credits'] + stated['total_charges']
        if got != stated['new']:
            errs.append('%s - %s + %s != new balance %s'
                        % (stated['prev'], stated['total_credits'],
                           stated['total_charges'], stated['new']))
    if stated['total_payments'] is not None and paid != stated['total_payments']:
        errs.append('payment rows sum to %s, Total payments %s'
                    % (paid, stated['total_payments']))               # identity 2
    if total_for_card is not None and charged != total_for_card:
        errs.append('charge rows sum to %s, Total for card %s' % (charged, total_for_card))
    parts = [stated[k] for k in ('purchases', 'advances', 'interest', 'fees')]
    if None not in parts and stated['total_charges'] is not None:      # identity 3
        if sum(parts) != stated['total_charges']:
            errs.append('purchases+advances+interest+fees %s != Total charges %s'
                        % (sum(parts), stated['total_charges']))
    if 'Total Cash Back' in cash:                                      # identity 4
        earned = sum(v for k, v in cash.items()
                     if k.endswith('% Cash Back') or k == 'Promotions')
        got = (cash.get('As at last statement', Decimal(0)) + earned
               - cash.get('Cash Back redeemed', Decimal(0)))
        if got != cash['Total Cash Back']:
            errs.append('cash back %s + %s - %s != %s'
                        % (cash.get('As at last statement'), earned,
                           cash.get('Cash Back redeemed'), cash['Total Cash Back']))

    return dict(path=path, period_end=str(period_end), stated=stated, cash=cash,
                total_for_card=total_for_card, txns=txns, errs=errs)


def _iso(period_end, mon3, day):
    """A cycle spans about a month, so a date more than 40 days after the period
    end belongs to the year before."""
    d = date(period_end.year, MON[mon3], day)
    if (d - period_end).days > 40:
        d = date(period_end.year - 1, MON[mon3], day)
    return str(d)


def to_json(st):
    return dict(stmt=os.path.basename(st['path']), period_end=st['period_end'],
                stated=st['stated'], cash=st['cash'],
                rows=[dict(date=t['date'], txn_date=t['txn_date'], desc=t['desc'],
                           amount=t['amount'], section=t['section'])
                      for t in st['txns']])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    common.add_common_args(ap)
    args = ap.parse_args(argv)
    docs = common.docs_dir(args.docs)
    files = common.statements(docs, common.doc_folder(SOURCE), "*.statement.pdf", args.since, args.until)
    _, rc = common.run_all(
        lambda f: statement(f, args.refresh), files, "Simplii Cash Back Visa",
        chain=("balance", lambda s: s["stated"].get("new"),
               lambda s: s["stated"].get("prev")),
        json_path=args.json, to_json=to_json)
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
