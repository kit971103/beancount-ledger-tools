"""Parse RBC Cash Back Mastercard statements.

Extraction is `pdftotext -table`.

The statement is TWO columns: transactions on the left, the summary boxes on the
right, both on the same physical lines. Everything is read from the left column,
whose right edge is the `AMOUNT ($)` header; the right column is picked out of
the flattened text by its own labels.

Each transaction is two or three lines:

    TRANSACTION-DATE  POSTING-DATE  DESCRIPTION            AMOUNT
                      <reference number>                       <- unique row id
                      Foreign Currency - USD 10.00 Exchange rate - 1.400000

`POST DATE` is the transaction date for the ledger, not `TRANS DATE`: the
posting date is what decides the statement cycle.

The parse proves itself against the statement's own summary: debits must equal
purchases + cash advances + interest + fees, credits must equal Payments &
credits, previous + debits - credits must equal the Total Account Balance, and
the cash back box must chain. Every row must carry a reference number, and no
reference may repeat.

Usage:
    ledger-extract rbc_mastercard [--docs DIR] [--since 2025-01-01] \
        [--json out.json]
"""

import argparse
import os
import re
from decimal import Decimal

from ledger_tools.extract import common

SOURCE = "rbc-mastercard"

MON = dict(JAN=1, FEB=2, MAR=3, APR=4, MAY=5, JUN=6,
           JUL=7, AUG=8, SEP=9, OCT=10, NOV=11, DEC=12)
ROW = re.compile(r'^\s*([A-Z]{3})\s+(\d{1,2})\s+([A-Z]{3})\s+(\d{1,2})\s')
MONEY = re.compile(r'(-?)\$(-?)([\d,]+\.\d{2})')
REF = re.compile(r'^\s*(\d{15,})\s*$')
FX = re.compile(r'Foreign Currency\s*-\s*(\w+)\s+([\d,]+\.\d{2})\s+Exchange rate\s*-\s*([\d.]+)')
PERIOD = re.compile(r'STATEMENT FROM ([A-Z]{3}) (\d{1,2})(?:, (\d{4}))? TO ([A-Z]{3}) (\d{1,2}), (\d{4})')
NO_REF_OK = re.compile(r'CASH BACK REWARD', re.I)


def D(m):
    """MONEY match -> Decimal (RBC writes credits as -$x, and occasionally $-x)."""
    v = Decimal(m.group(3).replace(',', ''))
    return -v if (m.group(1) or m.group(2)) else v


def label(flat, name):
    m = re.search(re.escape(name) + r'\s+(-?)\$(-?)([\d,]+\.\d{2})', flat)
    if not m:
        return None
    v = Decimal(m.group(3).replace(',', ''))
    return -v if (m.group(1) or m.group(2)) else v


def statement(path, refresh=False):
    if path.lower().endswith('.txt'):
        with open(path, encoding='utf-8', errors='replace') as fh:
            text = fh.read()
    else:
        text = common.pdftotext(path, '-table', refresh=refresh)
    lines = text.split('\n')
    flat = re.sub(r'\s+', ' ', ' '.join(l.strip() for l in lines))

    pm = PERIOD.search(flat)
    if not pm:
        raise ValueError('no STATEMENT FROM line')
    y_to = int(pm.group(6))
    y_from = int(pm.group(3)) if pm.group(3) else y_to
    p_from = (y_from, MON[pm.group(1)], int(pm.group(2)))
    p_to = (y_to, MON[pm.group(4)], int(pm.group(5)))

    stated = dict(
        prev=label(flat, 'PREVIOUS ACCOUNT BALANCE'),
        pay=label(flat, 'Payments & credits'),
        pur=label(flat, 'Purchases & debits'),
        adv=label(flat, 'Cash advances'),
        interest=label(flat, 'Interest'),
        fees=label(flat, 'Fees'),
        total=label(flat, 'Total Account Balance'),
        new=label(flat, 'NEW BALANCE'),   # absent when the balance is a credit
        cb_prev=label(flat, 'Previous Cash Back Balance'),
        cb_new=label(flat, 'New Cash Back Balance'),
    )
    cb_g = re.search(r'Cash Back on Grocery Purchases\s+\$?(-?[\d,]+\.\d{2}|0)\b', flat)
    cb_o = re.search(r'Cash Back on Other Purchases\s+\$?(-?[\d,]+\.\d{2}|0)\b', flat)
    stated['cb_groc'] = Decimal(cb_g.group(1).replace(',', '')) if cb_g else None
    stated['cb_other'] = Decimal(cb_o.group(1).replace(',', '')) if cb_o else None
    cb_rew = re.search(r'--Cash Back Reward(?:s Redeemed|ed to Date)\s+-?\$(-?[\d,]+\.\d{2})', flat)
    stated['cb_redeem'] = Decimal(cb_rew.group(1).replace(',', '')) if cb_rew else None

    c_amt = None
    txns = []
    cur = None
    for ln in lines:
        if 'ACTIVITY DESCRIPTION' in ln and 'AMOUNT' in ln:
            c_amt = ln.index('AMOUNT ($)')
            continue
        if c_amt is None:
            continue
        left = ln[:c_amt + 12]
        m = ROW.match(left)
        if m:
            desc = left[m.end():c_amt].strip() if len(left) > m.end() else ''
            mm = MONEY.search(left[c_amt:])
            if mm is None:
                continue
            if desc.upper().startswith('TOTAL ACCOUNT BALANCE'):
                continue
            cur = dict(txn_date=(MON[m.group(1)], int(m.group(2))),
                       post_date=(MON[m.group(3)], int(m.group(4))),
                       desc=desc, amount=D(mm), ref=None, fx=None, extra=[])
            txns.append(cur)
            continue
        if cur is None:
            continue
        tail = left.strip()
        if REF.match(tail):
            cur['ref'] = tail.strip()
            continue
        fm = FX.search(tail)
        if fm:
            cur['fx'] = dict(ccy=fm.group(1), amount=fm.group(2), rate=fm.group(3))
            continue
        if tail and not tail.upper().startswith('TOTAL ACCOUNT BALANCE'):
            cur['extra'].append(tail)

    # years: the cycle runs forward from p_from
    for t in txns:
        for k in ('txn_date', 'post_date'):
            mo, dd = t[k]
            y = p_from[0] if mo >= p_from[1] else p_to[0]
            if p_from[0] == p_to[0]:
                y = p_from[0]
            t[k[:-5] + '_iso'] = '%04d-%02d-%02d' % (y, mo, dd)

    # RBC prints no NEW BALANCE line at all when the account is in credit
    if stated['new'] is None:
        stated['new'] = stated['total']

    errs = []
    pos = sum((t['amount'] for t in txns if t['amount'] > 0), Decimal(0))
    neg = -sum((t['amount'] for t in txns if t['amount'] < 0), Decimal(0))
    if None not in (stated['pur'], stated['adv'], stated['interest'], stated['fees']):
        exp_pos = stated['pur'] + stated['adv'] + stated['interest'] + stated['fees']
        if pos != exp_pos:
            errs.append('debits %s != purchases+advances+interest+fees %s' % (pos, exp_pos))
    if stated['pay'] is not None and neg != -stated['pay']:
        errs.append('credits -%s != Payments & credits %s' % (neg, stated['pay']))
    if stated['prev'] is not None and stated['total'] is not None:
        if stated['prev'] + pos - neg != stated['total']:
            errs.append('%s + %s - %s != total %s'
                        % (stated['prev'], pos, neg, stated['total']))
    if stated['total'] is not None and stated['new'] is not None \
            and stated['total'] != stated['new']:
        errs.append('Total Account Balance %s != NEW BALANCE %s'
                    % (stated['total'], stated['new']))
    if None not in (stated['cb_prev'], stated['cb_groc'], stated['cb_other'], stated['cb_new']):
        got = stated['cb_prev'] + stated['cb_groc'] + stated['cb_other'] \
            - (stated['cb_redeem'] or 0)
        if got != stated['cb_new']:
            errs.append('cash back %s+%s+%s-%s != %s'
                        % (stated['cb_prev'], stated['cb_groc'], stated['cb_other'],
                           stated['cb_redeem'], stated['cb_new']))
    refs = [t['ref'] for t in txns if t['ref']]
    if len(set(refs)) != len(refs):
        errs.append('duplicate reference numbers')
    # A cash back redemption is posted by RBC itself, not by a card transaction, so
    # it carries no reference number. That is the only row that legitimately may
    # lack one.
    missing = [t for t in txns if not t['ref'] and not NO_REF_OK.match(t['desc'])]
    if missing:
        errs.append('%d rows with no reference number: %s'
                    % (len(missing), [t['desc'][:30] for t in missing]))
    return dict(path=path, p_from=p_from, p_to=p_to, stated=stated, txns=txns,
                errs=errs)


def to_json(st):
    return dict(stmt=os.path.basename(st['path']),
                p_from='%04d-%02d-%02d' % st['p_from'],
                p_to='%04d-%02d-%02d' % st['p_to'],
                stated=st['stated'],
                rows=[dict(post=t['post_iso'], txn=t['txn_iso'], desc=t['desc'],
                           amount=t['amount'], ref=t['ref'], fx=t['fx'],
                           extra=t['extra']) for t in st['txns']])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    common.add_common_args(ap)
    args = ap.parse_args(argv)
    docs = common.docs_dir(args.docs)
    files = common.statements(docs, common.doc_folder(SOURCE), "*.statement.pdf", args.since, args.until)
    _, rc = common.run_all(
        lambda f: statement(f, args.refresh), files, common.label(SOURCE, "RBC Mastercard"),
        chain=("balance", lambda s: s["stated"].get("new"),
               lambda s: s["stated"].get("prev")),
        json_path=args.json, to_json=to_json)
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
