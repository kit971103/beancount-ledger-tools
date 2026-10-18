"""Suggest a counter-account for export rows the ledger does not have yet.

The rule for rows built from an export with no other record of them: accept a
category as `*` only when >= 3 prior rows for the merchant agree >= 60% on a
counter-account, else flag `!`.  This applies that rule and prints the evidence;
it writes nothing.  "The merchant" is the row's description, matched against each
prior transaction's `bank-desc` (else its narration) on the same account, after
collapsing whitespace and case.  A leg on the receivable account (`[extract]
receivable` in the config, e.g. `Assets:AR`) counts per person, since `person:` is
what decides the receivable.

Usage:
    ledger-extract categorise --source eqbank-main \\
        --file sub-ledgers/eq_bank --account Assets:Bank:Checking --since 2025-01-01

Only rows the ledger lacks on (date, amount) are listed, as
`date flag amount description hits/prior counter-accounts`.
"""

import argparse
import re
import sys
from collections import Counter

from ledger_tools import config
from ledger_tools.extract import bank_csv, common, ledger

MIN_ROWS, MIN_SHARE = 3, 0.60


def key(desc):
    return re.sub(r'\s+', ' ', (desc or '').strip().strip('"')).lower()


def counters(txn, account):
    """The legs other than `account`, as labels (AR legs carry their person)."""
    out = []
    for p in txn['postings']:
        if p['account'] == account:
            continue
        label = p['account']
        if label == config.get('extract', 'receivable', default=None):
            label += ':' + txn.get('person_of', {}).get(p['line'], 'Misc')
        out.append(label)
    return tuple(sorted(set(out)))


def _persons(txn):
    """Map each posting line to its `person:` (posting meta, else the transaction's)."""
    default = txn['meta'].get('person', '').strip('"') or 'Misc'
    raw, res, last = txn['raw'], {}, None
    for p in txn['postings']:
        res[p['line']] = default
    # a posting's own `person:` is the indented meta line right after it
    lines = {p['line'].rsplit(':', 1)[1]: p['line'] for p in txn['postings']}
    for i, line in enumerate(raw):
        n = str(txn['start'] + i)
        if n in lines:
            last = lines[n]
            continue
        m = re.match(r'^\s{4,}person:\s*"([^"]*)"', line)
        if m and last:
            res[last] = m.group(1)
    return res


def history(txns, account):
    """-> {merchant key: Counter(counter-account tuple)} over every prior posting."""
    hist = {}
    for t in txns:
        if not any(p['account'] == account for p in t['postings']):
            continue
        t['person_of'] = _persons(t)
        desc = t['meta'].get('bank-desc', '').strip('"') or t['narration']
        hist.setdefault(key(desc), Counter())[counters(t, account)] += 1
    return hist


def suggest(hist, desc):
    c = hist.get(key(desc), Counter())
    n = sum(c.values())
    if not n:
        return None, 0, 0, '!'
    best, k = c.most_common(1)[0]
    ok = n >= MIN_ROWS and k / n >= MIN_SHARE
    return best, k, n, '*' if ok else '!'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--source', required=True, choices=sorted(bank_csv.SOURCES))
    ap.add_argument('--file', required=True, help='sub-ledger folder or file')
    ap.add_argument('--account', required=True)
    ap.add_argument('--csv', help="one export file instead of the source's glob "
                                  "(e.g. a `-partial.csv` the glob leaves out)")
    common.add_common_args(ap)
    a = ap.parse_args(argv)
    if a.csv:
        rows = [r for r in bank_csv.SOURCES[a.source]['read'](a.csv)
                if (not a.since or r['date'] >= a.since) and (not a.until or r['date'] <= a.until)]
    else:
        rows, _ = bank_csv.load(a.source, a.docs, a.since, a.until)
    txns = ledger.parse(a.file)
    have = ledger.postings_to(txns, a.account, a.since, a.until)
    sign = -1 if a.source in ledger.CARD_SOURCES else 1   # export -> ledger sign
    missing, _ = ledger.compare([dict(r, amount=r['amount'] * sign) for r in rows], have)
    missing = Counter(missing)
    hist = history(txns, a.account)
    for r in rows:
        k = (r['date'], r['amount'] * sign)
        if not missing[k]:
            continue
        missing[k] -= 1
        best, hit, n, flag = suggest(hist, r['desc'])
        print('%s %s %10s  %-45s %s/%s %s' % (r['date'], flag, k[1], r['desc'][:45], hit, n,
                                             ', '.join(best) if best else '-'))


if __name__ == '__main__':
    sys.exit(main())
