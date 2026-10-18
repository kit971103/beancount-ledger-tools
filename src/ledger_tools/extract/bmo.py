"""Parse BMO chequing and line of credit statements.

Extraction is `pdftotext -table` for both. On the chequing statements `-layout`
is also usable, but `-table` survives the column shift that happens on
continuation pages, where BMO re-lays the `Amounts deducted` / `Amounts added` /
`Balance` columns at different offsets from page 1.

Neither mode should be trusted on column *position*: the balance column is
right-aligned, so a four-digit balance sits two characters left of a three-digit
one. **Parse by the running balance instead** -- the money tokens on a line are
"the amount, then the new balance", and the sign comes from
`new balance - previous balance`. That is what catches an overdraft to
e.g. -17.00, which a money regex without an optional leading `-` reads as 17.00
and which then breaks the chain a few rows later.

Four identities are checked on every chequing statement:

 1. each row's amount equals the change in the printed running balance;
 2. `Opening balance - Total deducted + Total added = Closing balance`;
 3. the deducted rows sum to `Total deducted`, the added rows to `Total added`;
 4. each statement's opening balance equals the previous statement's closing
    balance (reported as a chain break).

The line of credit statement is TWO columns -- transactions on the left, the
"at a glance" summary on the right, on the same physical lines. The left column
is cut at the `Amount ($)` header; the summary is read from the right of that
cut by its own labels, and must satisfy

    Previous balance - Payments - Credit adjustments
        + Purchases + Advances + Interest + Fees + Debit adjustments
        = New account balance

with the parsed rows summing to the stated payments and advances. A credit
prints as a trailing `CR`.

Usage:
    ledger-extract bmo [--docs DIR] [--account main|loc|both] \
        [--since 2025-01-01] [--json out.json]
"""

import argparse
import os
import re
from datetime import date
from decimal import Decimal

from ledger_tools.extract import common

D = common.D
MON = common.MON3

MAIN_SOURCE, LOC_SOURCE = "bmo-main", "bmo-loc"

MONEY = re.compile(r'(?<![\d.,])-?\d{1,3}(?:,\d{3})*\.\d{2}(?![\d])')
ROW = re.compile(r'^(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+'
                 r'(?P<day>\d{1,2})\s{2,}(?P<rest>.*\S)\s*$')
PERIOD_END = re.compile(r'For the period ending (\w+) (\d{1,2}), (\d{4})')


# --------------------------------------------------------------------------- #
# chequing
# --------------------------------------------------------------------------- #

def _rows(text):
    end_m = PERIOD_END.search(text)
    if not end_m:
        raise ValueError('no "For the period ending" line')
    end = date(int(end_m.group(3)), MON[end_m.group(1)[:3]], int(end_m.group(2)))
    rows = []
    for i, ln in enumerate(text.split('\n')):
        m = ROW.match(ln)
        if not m:
            continue
        rest = m.group('rest')
        toks = MONEY.findall(rest)
        desc = re.sub(r'\s{2,}', ' ', MONEY.sub('', rest).strip()).strip()
        rows.append(dict(mon=MON[m.group('mon')], day=int(m.group('day')),
                         desc=desc, toks=toks, line=i, raw=ln.rstrip()))
    return end, rows


def _assign_year(end, mon, day):
    """A statement never spans more than ~a month, so a row more than 20 days
    after the period end belongs to the previous year."""
    d = date(end.year, mon, day)
    if (d - end).days > 20:
        d = date(end.year - 1, mon, day)
    return d


def chequing(path, refresh=False):
    if path.lower().endswith('.txt'):
        with open(path, encoding='utf-8', errors='replace') as fh:
            text = fh.read()
    else:
        text = common.pdftotext(path, '-table', refresh=refresh)
    end, rows = _rows(text)

    errs = []
    opening = tot_ded = tot_add = None
    txns = []
    bal = None
    for r in rows:
        d = r['desc']
        if d.startswith('Opening balance'):
            if len(r['toks']) != 1:
                errs.append('opening balance row has %d money tokens: %r'
                            % (len(r['toks']), r['raw']))
                continue
            opening = D(r['toks'][0])
            bal = opening
            continue
        if d.startswith('Closing totals'):
            if len(r['toks']) != 2:
                errs.append('closing totals row has %d money tokens: %r'
                            % (len(r['toks']), r['raw']))
                continue
            tot_ded, tot_add = D(r['toks'][0]), D(r['toks'][1])
            continue
        if len(r['toks']) != 2:
            errs.append('detail row has %d money tokens: %r'
                        % (len(r['toks']), r['raw']))
            continue
        if bal is None:
            errs.append('detail row before the opening balance: %r' % r['raw'])
            continue
        amt, newbal = D(r['toks'][0]), D(r['toks'][1])
        delta = newbal - bal
        if abs(delta) != amt:                                   # identity 1
            errs.append('%s -> %s is not +/-%s :: %r' % (bal, newbal, amt, r['raw']))
        bal = newbal
        txns.append(dict(date=str(_assign_year(end, r['mon'], r['day'])),
                         desc=d, amount=delta, balance=newbal, raw=r['raw']))

    closing = bal
    if None not in (opening, tot_ded, tot_add, closing):
        if opening - tot_ded + tot_add != closing:              # identity 2
            errs.append('%s - %s + %s != %s' % (opening, tot_ded, tot_add, closing))
    ded = sum((-t['amount'] for t in txns if t['amount'] < 0), Decimal(0))
    add = sum((t['amount'] for t in txns if t['amount'] > 0), Decimal(0))
    if tot_ded is not None and ded != tot_ded:                  # identity 3
        errs.append('deducted rows sum to %s, stated %s' % (ded, tot_ded))
    if tot_add is not None and add != tot_add:
        errs.append('added rows sum to %s, stated %s' % (add, tot_add))

    return dict(path=path, end=str(end), opening=opening, closing=closing,
                tot_ded=tot_ded, tot_add=tot_add, txns=txns, errs=errs)


# --------------------------------------------------------------------------- #
# line of credit
# --------------------------------------------------------------------------- #

# The row number is optional on purpose: the extraction loses it on some
# statements (it lands in the page furniture instead), so a row must be
# recognised by its two dates, not by being numbered.
LOC_ROW = re.compile(r'^\s*(?:(\d+)\s+)?([A-Z][a-z]{2})\.?\s+(\d{1,2})\s+'
                     r'([A-Z][a-z]{2})\.?\s+(\d{1,2})\b')
STMT_DATE = re.compile(r'Stmt\.\s*date:\s*(\w+)\.?\s+(\d{1,2}),\s+(\d{4})')


def _loc_label(right, name):
    """Read one figure out of the flattened right-hand summary column."""
    m = re.search(re.escape(name) + r'[^$\n]*\$\s*([\d,]+\.\d{2})\s*(CR)?', right)
    if not m:
        return None
    v = D(m.group(1))
    return -v if m.group(2) else v


def line_of_credit(path, refresh=False):
    if path.lower().endswith('.txt'):
        with open(path, encoding='utf-8', errors='replace') as fh:
            text = fh.read()
    else:
        text = common.pdftotext(path, '-table', refresh=refresh)
    lines = text.split('\n')

    sm = STMT_DATE.search(re.sub(r'\s+', ' ', text))
    if not sm:
        raise ValueError('no "Stmt. date" line')
    stmt_date = date(int(sm.group(3)), MON[sm.group(1)[:3]], int(sm.group(2)))

    cut = None
    for ln in lines:
        if 'Description' in ln and 'Amount ($)' in ln:
            cut = ln.index('Amount ($)') + len('Amount ($)') + 4
            break
    if cut is None:
        raise ValueError('no transaction-table header line')

    left = [ln[:cut] for ln in lines]
    right = re.sub(r'\s+', ' ', ' '.join(ln[cut:].strip() for ln in lines))

    stated = dict(
        prev=_loc_label(right, 'Previous balance'),
        payments=_loc_label(right, 'Payments received'),
        credit_adj=_loc_label(right, 'Credit adjustments'),
        purchases=_loc_label(right, 'Card purchases'),
        advances=_loc_label(right, 'Cash advances/Cheques'),
        interest=_loc_label(right, 'Interest'),
        fees=_loc_label(right, 'Fees'),
        debit_adj=_loc_label(right, 'Debit adjustments'),
        new=_loc_label(right, 'New account balance'),
    )

    txns, cur = [], None
    for ln in left:
        m = LOC_ROW.match(ln)
        if m:
            tail = ln[m.end():]
            # The amount is the LAST money token on the line, never the first: a
            # description carries its own numbers (`INTEREST ADVANCES @ 10.00000%
            # TO 09FEB`), and only the guarded money pattern refuses to read
            # `10.00` out of `10.00000`.
            hits = list(common.MONEY.finditer(tail))
            amt, desc = None, tail
            if hits:
                last = hits[-1]
                amt = D(last.group(0))
                if tail[last.end():last.end() + 2].upper() == 'CR':
                    amt = -amt              # `CR` -- a credit, i.e. a payment
                desc = tail[:last.start()]
            cur = dict(n=int(m.group(1)) if m.group(1) else None,
                       txn_date=_loc_iso(stmt_date, m.group(2), int(m.group(3))),
                       date=_loc_iso(stmt_date, m.group(4), int(m.group(5))),
                       desc=re.sub(r'\s{2,}', ' ', desc).strip(), amount=amt,
                       raw=ln.rstrip())
            txns.append(cur)
            continue
        if cur is None or cur['desc']:
            continue
        tail = ln.strip()                       # description wrapped to its own line
        if tail and tail != '.' and not common.MONEY.fullmatch(tail):
            cur['desc'] = re.sub(r'\s{2,}', ' ', tail)

    errs = []
    for t in txns:
        if t['amount'] is None:
            errs.append('row has no amount: %r' % t['raw'])
        if not t['desc']:
            errs.append('row has no description: %r' % t['raw'])

    amts = [t['amount'] for t in txns if t['amount'] is not None]
    paid = -sum((a for a in amts if a < 0), Decimal(0))
    charged = sum((a for a in amts if a > 0), Decimal(0))
    if stated['payments'] is not None and paid != -stated['payments']:
        errs.append('payment rows sum to %s, stated %s' % (paid, -stated['payments']))
    charge_labels = [stated[k] for k in ('purchases', 'advances', 'interest', 'fees')]
    if None not in charge_labels and charged != sum(charge_labels):
        errs.append('charge rows sum to %s, stated purchases+advances+interest+fees %s'
                    % (charged, sum(charge_labels)))
    if None not in (stated['prev'], stated['new']):
        got = (stated['prev'] + (stated['payments'] or 0) + (stated['credit_adj'] or 0)
               + sum(x or 0 for x in charge_labels) + (stated['debit_adj'] or 0))
        if got != stated['new']:
            errs.append('summary box does not balance: %s != new %s'
                        % (got, stated['new']))

    return dict(path=path, date=str(stmt_date), stated=stated, txns=txns, errs=errs)


def _loc_iso(stmt_date, mon3, day):
    return str(_assign_year(stmt_date, MON[mon3[:3]], day))


# --------------------------------------------------------------------------- #

def _chq_json(st):
    return dict(stmt=os.path.basename(st['path']), end=st['end'],
                opening=st['opening'], closing=st['closing'],
                rows=[dict(date=t['date'], desc=t['desc'], amount=t['amount'],
                           bal=t['balance']) for t in st['txns']])


def _loc_json(st):
    return dict(stmt=os.path.basename(st['path']), date=st['date'],
                stated=st['stated'],
                rows=[dict(date=t['date'], txn_date=t['txn_date'], desc=t['desc'],
                           amount=t['amount']) for t in st['txns']])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    common.add_common_args(ap)
    ap.add_argument("--account", choices=["main", "loc", "both"], default="both")
    args = ap.parse_args(argv)
    docs = common.docs_dir(args.docs)
    rc = 0

    if args.account in ("main", "both"):
        files = common.statements(docs, common.doc_folder(MAIN_SOURCE), "*.statement.pdf",
                                  args.since, args.until)
        _, code = common.run_all(
            lambda f: chequing(f, args.refresh), files, "BMO chequing",
            chain=("balance", lambda s: s.get("closing"), lambda s: s.get("opening")),
            json_path=(args.json and args.json.replace(".json", ".main.json")),
            to_json=_chq_json)
        rc |= code

    if args.account in ("loc", "both"):
        files = common.statements(docs, common.doc_folder(LOC_SOURCE), "*.statement.pdf",
                                  args.since, args.until)
        _, code = common.run_all(
            lambda f: line_of_credit(f, args.refresh), files, "BMO line of credit",
            chain=("balance", lambda s: s["stated"].get("new"),
                   lambda s: s["stated"].get("prev")),
            json_path=(args.json and args.json.replace(".json", ".loc.json")),
            to_json=_loc_json)
        rc |= code

    return rc


if __name__ == '__main__':
    raise SystemExit(main())
