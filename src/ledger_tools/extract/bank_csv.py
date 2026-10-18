"""Read the CSV exports -- EQ Bank, AMEX, Rogers, Scotia, Simplii.

Every export is read into the same row shape:

    dict(date, post_date, desc, amount, ref, src, extra)

`date` is the date the ledger uses for that source, which is the bank's posting
date wherever the export has one: the posting date is what decides a card's
statement cycle, so it, not the merchant's transaction date, is the transaction
date. Where the export carries both, the other one is kept as `post_date`'s
counterpart in `extra['txn_date']` rather than thrown away -- `documents/` is
git-ignored, so a column no one wrote down is gone.

`amount` keeps the export's own sign, and what a positive number means is a
property of the source, recorded in each spec's `sign` note:

  * EQ Bank (asset):     + money in, - money out
  * AMEX / Rogers / Scotia / Simplii (cards): + charge (debt up), - payment or
    refund. Simplii states this as two columns and is normalised to match.

`desc` is the export's description **verbatim**. Where a source splits it over
two columns (Scotia), the parts are joined with two spaces and nothing else is
touched: it is source text, not something to tidy up.

Usage:
    ledger-extract bank_csv --source eqbank-main [--docs DIR]
    ledger-extract bank_csv --source all --json rows.json
"""

import argparse
import csv
import datetime
import os
from decimal import Decimal, InvalidOperation

from ledger_tools.extract import common

D = common.D


def _money(s):
    """'$5,109.18' / '-$9.35' / '"$4.50"' / '1288.06' -> Decimal, or None if blank."""
    s = (s or '').strip().strip('"').strip()
    if not s:
        return None
    neg = s.startswith('-') or s.endswith('CR')
    s = s.lstrip('-').rstrip('CR').replace('$', '').replace(',', '').strip()
    if not s:
        return None
    try:
        v = Decimal(s)
    except InvalidOperation:
        raise ValueError('not a money value: %r' % s)
    return -v if neg else v


def _iso(s):
    """The four date spellings the exports use -> 'YYYY-MM-DD'."""
    s = (s or '').strip().strip('"').strip()
    for fmt in ('%Y-%m-%d', '%d %b %Y', '%m/%d/%Y', '%d/%m/%Y'):
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError('unrecognised date: %r' % s)


# --------------------------------------------------------------------------- #
# one reader per export shape
# --------------------------------------------------------------------------- #

def _eqbank_main(path):
    out = []
    for r in csv.DictReader(open(path, encoding='utf-8-sig', newline='')):
        out.append(dict(date=_iso(r['Transfer date']), post_date=None,
                        desc=r['Description'].strip(), amount=_money(r['Amount']),
                        ref=None, extra=dict(balance=_money(r.get('Balance')))))
    out.reverse()                      # the export is newest-first
    return out


def _eqbank_card(path):
    out = []
    for r in csv.DictReader(open(path, encoding='utf-8-sig', newline='')):
        out.append(dict(date=_iso(r['Date']), post_date=None,
                        desc=r['Description'].strip().strip('"'),
                        amount=_money(r['Amount']), ref=None, extra={}))
    out.reverse()
    return out


def _amex(path):
    out = []
    for r in csv.DictReader(open(path, encoding='utf-8-sig', newline='')):
        ref = (r.get('Reference') or '').strip().strip("'")
        out.append(dict(date=_iso(r['Date Processed']), post_date=_iso(r['Date Processed']),
                        desc=r['Description'], amount=_money(r['Amount']),
                        ref=ref or None,
                        extra=dict(txn_date=_iso(r['Date']),
                                   merchant=(r.get('Merchant') or '').strip() or None,
                                   fx=(r.get('Foreign Spend Amount') or '').strip() or None)))
    out.reverse()
    return out


def _rogers(path):
    out = []
    for r in csv.DictReader(open(path, encoding='utf-8-sig', newline='')):
        amt = _money(r['Amount'])
        out.append(dict(date=_iso(r['Posted Date']), post_date=_iso(r['Posted Date']),
                        desc=(r.get('Merchant Name') or '').strip(), amount=amt,
                        ref=(r.get('Reference Number') or '').strip() or None,
                        extra=dict(txn_date=_iso(r['Date']),
                                   category=(r.get('Merchant Category Description') or '').strip()
                                   or None,
                                   city=(r.get('Merchant City') or '').strip() or None,
                                   status=(r.get('Activity Status') or '').strip() or None)))
    out.reverse()
    return out


def _scotia(path):
    out = []
    for r in csv.DictReader(open(path, encoding='utf-8-sig', newline='')):
        # Scotia splits one description over two columns: the export truncates
        # `Description` at 25 characters and overflows the rest into
        # `Sub-description`. Joined with two spaces, never re-wrapped.
        desc = r['Description'].strip()
        sub = (r.get('Sub-description') or '').strip()
        out.append(dict(date=_iso(r['Date']), post_date=_iso(r['Date']),
                        desc=(desc + '  ' + sub).strip() if sub else desc,
                        amount=_money(r['Amount']), ref=None,
                        extra=dict(type=(r.get('Type of Transaction') or '').strip(),
                                   status=(r.get('Status') or '').strip(),
                                   card=(r.get('Associated card number') or '').strip())))
    out.reverse()
    return out


def _simplii(path):
    out = []
    rd = csv.reader(open(path, encoding='utf-8-sig', newline=''))
    header = [h.strip() for h in next(rd)]
    ix = {h: i for i, h in enumerate(header)}
    for row in rd:
        if not any(c.strip() for c in row):
            continue
        fout = _money(row[ix['Funds Out']])
        fin = _money(row[ix['Funds In']])
        # Two columns, normalised to the card convention used everywhere else:
        # a charge is positive, a payment or refund negative.
        amount = (fout or Decimal(0)) - (fin or Decimal(0))
        out.append(dict(date=_iso(row[ix['Date']]), post_date=_iso(row[ix['Date']]),
                        desc=row[ix['Transaction Details']].strip(), amount=amount,
                        ref=None,
                        extra=dict(funds_out=fout, funds_in=fin,
                                   card=row[ix['Credit Card']].strip()
                                   if 'Credit Card' in ix else None)))
    out.reverse()
    return out


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #

def _check_balance_chain(rows):
    """EQ Bank Main prints a running balance: the strongest check an export offers."""
    errs = []
    prev = None
    for r in rows:
        bal = r['extra'].get('balance')
        if bal is None:
            continue
        if prev is not None and prev + r['amount'] != bal:
            errs.append('%s: %s + %s != %s :: %s'
                        % (r['date'], prev, r['amount'], bal, r['desc'][:40]))
        prev = bal
    return errs


def _check_signs(rows):
    """Scotia states the direction in its own column; it must agree with the sign."""
    errs = []
    for r in rows:
        kind = r['extra'].get('type')
        if kind == 'Debit' and r['amount'] < 0:
            errs.append('%s Debit but negative: %s %s' % (r['date'], r['amount'], r['desc'][:40]))
        if kind == 'Credit' and r['amount'] > 0:
            errs.append('%s Credit but positive: %s %s' % (r['date'], r['amount'], r['desc'][:40]))
    return errs


def _check_one_column(rows):
    """Simplii writes a row in exactly one of Funds Out / Funds In."""
    errs = []
    for r in rows:
        fout, fin = r['extra']['funds_out'], r['extra']['funds_in']
        if (fout is None) == (fin is None):
            errs.append('%s has %s out / %s in :: %s'
                        % (r['date'], fout, fin, r['desc'][:40]))
    return errs


def _check_refs(rows):
    """The row key is the reference number PLUS the amount, not the reference alone.

    Rogers shares one reference between a cash advance and the fee it incurred,
    so matching on the reference alone silently pairs the wrong rows; with the
    amount it is unique.
    """
    seen = {}
    errs = []
    for r in rows:
        if not r['ref']:
            continue
        key = (r['ref'], r['amount'])
        if key in seen:
            errs.append('reference %s at %s repeats: %s / %s'
                        % (r['ref'], r['amount'], seen[key][:40], r['desc'][:40]))
        seen[key] = r['desc']
    return errs


def _check_dates_sorted(rows):
    bad = [(a['date'], b['date']) for a, b in zip(rows, rows[1:]) if a['date'] > b['date']]
    return ['rows are not oldest-first: %s then %s' % p for p in bad[:3]]


# --------------------------------------------------------------------------- #

SOURCES = {
    "eqbank-main": dict(
        pattern="*.csv", read=_eqbank_main,
        checks=[_check_balance_chain, _check_dates_sorted], dedupe=True,
        sign="+ money in, - money out",
        note="Exports may overlap (rows repeated across files are dropped); the "
             "balance column chains across all of them."),
    "eqbank-card": dict(
        pattern="*.csv", read=_eqbank_card,
        checks=[_check_dates_sorted],
        sign="+ money in, - money out",
        note="No balance column: this export supports no assertion beyond the opening one."),
    "amex": dict(
        pattern="*.activity.csv", read=_amex,
        checks=[_check_refs, _check_dates_sorted], dedupe=True,
        sign="+ charge, - payment or refund",
        note="`Date Processed` is the ledger date. Only the later export has Reference."),
    "rogers": dict(
        pattern="*.transactions.csv", read=_rogers,
        checks=[_check_refs, _check_dates_sorted],
        sign="+ charge, - cash back or payment",
        note="One export per statement cycle. Bank-generated rows (cash interest, overlimit "
             "fee) may carry no reference, and a cash advance shares its reference with "
             "its fee -- the key is reference + amount."),
    "scotia": dict(
        pattern="*.transactions.csv",
        read=_scotia, checks=[_check_signs, _check_dates_sorted],
        sign="+ Debit (charge), - Credit (payment, refund, cash back)",
        note="Description is truncated at 25 chars and overflows into Sub-description."),
    "simplii": dict(
        pattern="*.transactions.csv",
        read=_simplii, checks=[_check_one_column, _check_dates_sorted],
        sign="+ Funds Out (charge), - Funds In (payment or refund)",
        note="Dates are MM/DD/YYYY. No row id, so matching falls back on date+amount+desc."),
}


def _drop_overlap(rows):
    """Drop rows a later export repeats from an earlier one.

    EQ Bank and AMEX export any date range, so two downloads overlap.  A row's
    (date, description, amount, balance) -- balance None for AMEX -- repeated in
    *another* file is the same row; a repeat inside one file is kept, since there
    it is a real second row (two identical charges on one day).
    Whitespace is collapsed: one export may write `received from  A NAME` where a
    later one has a single space.
    """
    first, out = {}, []
    for r in rows:
        k = (r["date"], " ".join(r["desc"].split()), r["amount"], r["extra"].get("balance"))
        if first.setdefault(k, r["src"]) != r["src"]:
            continue
        out.append(r)
    return out


def load(source, docs=None, since=None, until=None):
    """-> rows, oldest first, across every export file for that source."""
    spec = SOURCES[source]
    docs = common.docs_dir(docs)
    files = common.statements(docs, common.doc_folder(source), spec["pattern"])
    rows = []
    for f in files:
        for r in spec["read"](f):
            r["src"] = os.path.basename(f)
            rows.append(r)
    if spec.get("dedupe"):
        rows = _drop_overlap(rows)
    rows.sort(key=lambda r: (r["date"], r["src"]))
    if since:
        rows = [r for r in rows if r["date"] >= since]
    if until:
        rows = [r for r in rows if r["date"] <= until]
    return rows, files


def check(source, rows):
    errs = []
    for fn in SOURCES[source]["checks"]:
        errs += fn(rows)
    return errs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    common.add_common_args(ap)
    ap.add_argument("--source", default="all",
                    choices=sorted(SOURCES) + ["all"])
    args = ap.parse_args(argv)

    names = sorted(SOURCES) if args.source == "all" else [args.source]
    rc = 0
    dump = {}
    for name in names:
        rows, files = load(name, args.docs, args.since, args.until)
        errs = check(name, rows)
        for e in errs:
            print("   ", e)
        print("%-12s %d files, %d rows, %s (%s), %d problems"
              % (name, len(files), len(rows),
                 (rows[0]["date"] + " .. " + rows[-1]["date"]) if rows else "empty",
                 SOURCES[name]["sign"], len(errs)))
        dump[name] = rows
        rc |= 1 if errs else 0
    common.write_json(args.json, dump if len(names) > 1 else dump[names[0]])
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
