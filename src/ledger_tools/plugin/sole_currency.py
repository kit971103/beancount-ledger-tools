"""Where one currency is asserted, check every other one of the account's is zero.

A `balance` directive checks one currency.  A USD broker account may hold CAD and HKD only
in passing -- a deposit arrives, an FX order converts it to USD within days -- and
the ledger asserts its month-end value in USD alone.  A USD figure that
agrees says nothing about CAD left unconverted beside it, and that CAD would be
counted twice: once as itself, once inside the USD market value.  So at every
`balance` in `currency` on a matching account, this plugin checks that each other
currency the account's `open` lists is zero, within `tolerance`, and reports an
error (like a failed balance) where it is not.

It checks rather than generating `balance` directives on purpose: beancount's `pad`
fills EVERY currency asserted after it, so a generated `0 CAD` following one of
those pads would be padded to zero from Income and could never fail.
The check reads transactions only -- pads are expanded after all user plugins, and
since no CAD/HKD balance is ever asserted, a pad never touches those currencies.
Like `balance`, it runs at the start of the day.

The tolerance exists because the ledger is kept at cents while the broker is not: its
sub-cent residual sweeps are not booked, and a dropped sweep's other leg (0.03 HKD
for 0.0038 USD) stays behind.  Over the years that adds up to a few cents.
Anything larger is unconverted cash and fails, which is the point.

Usage, in main.beancount (after exclude_flagged, so scheduled entries are gone):

    plugin "ledger_tools.plugin.sole_currency" "{
     'accounts': '^Assets:Broker:',
     'currency': 'USD',
     'tolerance': '0.10',
    }"

An account whose `open` lists no currencies is not checked.  A currency that has
its own written `balance` on the same date is left to that assertion -- write one
to assert a known non-zero balance.  The plugin adds no entries.
"""

import collections
import re
from decimal import Decimal

from beancount.core import data

__plugins__ = ("sole_currency",)

SoleCurrencyError = collections.namedtuple("SoleCurrencyError", "source message entry")



def sole_currency(entries, options_map, config=None):
    try:
        cfg = eval(config, {}) if isinstance(config, str) else dict(config or {})
        pattern = re.compile(cfg["accounts"])
        currency = cfg["currency"]
        tolerance = Decimal(str(cfg.get("tolerance", "0.10")))
    except Exception as exc:                                   # noqa: BLE001
        return entries, [SoleCurrencyError(
            data.new_metadata("<sole_currency>", 0),
            "bad config %r: %s" % (config, exc), None)]

    currencies = {e.account: e.currencies or ()
                  for e in entries if isinstance(e, data.Open)}
    written = {(e.date, e.account, e.amount.currency)
               for e in entries if isinstance(e, data.Balance)}

    running = collections.defaultdict(Decimal)
    errors = []
    # sorted order puts a date's balances before its transactions: start of day
    for e in data.sorted(entries):
        if isinstance(e, data.Transaction):
            for p in e.postings:
                if pattern.search(p.account) and p.units is not None:
                    running[(p.account, p.units.currency)] += p.units.number
        elif (isinstance(e, data.Balance) and e.amount.currency == currency
              and pattern.search(e.account)):
            for other in currencies.get(e.account, ()):
                if other == currency or (e.date, e.account, other) in written:
                    continue
                held = running[(e.account, other)]
                if abs(held) > tolerance:
                    errors.append(SoleCurrencyError(
                        e.meta,
                        "%s holds %s %s beside its %s assertion; expected 0 +/- %s"
                        % (e.account, held, other, currency, tolerance), e))
    return entries, errors
