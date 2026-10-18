"""Remove the prices `implicit_prices` derived from selected transactions.

`beancount.plugins.implicit_prices` turns every `@`/`@@` posting into a
`price` directive.  That is right for a real conversion, but wrong for a broker's
nightly residual sweep: its legs are rounded to the cent, so

    Assets:Broker:Taxable   0.01 USD
    Assets:Broker:Taxable  -0.01 CAD @@ 0.01 USD

implies `price CAD 1.00 USD`.  Beancount values a balance at the latest price
on or before the report date, so such a price overrides the monthly block in
price.beancount until the next block lands -- every CAD/USD conversion in that
window runs at 1:1.

Usage, in main.beancount, directly below implicit_prices:

    plugin "beancount.plugins.implicit_prices"
    plugin "ledger_tools.plugin.drop_implied_prices" "{'bank-desc': 'IBKR residual sweep'}"

The config is a dict of transaction metadata.  A price is dropped when
implicit_prices made it (it carries `__implicit_prices__`) from a transaction
whose metadata holds every key with exactly that value.  The transactions
themselves are untouched.  Explicit `price` directives are never dropped.
"""

import collections

from beancount.core import data
from beancount.plugins.implicit_prices import METADATA_FIELD

__plugins__ = ("drop_implied_prices",)

DropImpliedPricesError = collections.namedtuple(
    "DropImpliedPricesError", "source message entry"
)


def drop_implied_prices(entries, options_map, config=None):
    try:
        match = eval(config, {}) if isinstance(config, str) else dict(config or {})
        if not isinstance(match, dict) or not match:
            raise ValueError("expected a non-empty dict of metadata")
    except Exception as exc:                                   # noqa: BLE001
        return entries, [DropImpliedPricesError(
            data.new_metadata("<drop_implied_prices>", 0),
            "bad config %r: %s" % (config, exc), None)]

    # implicit_prices copies the transaction's filename and lineno onto each
    # price it makes, so that pair identifies the source transaction.
    sources = {
        (e.meta["filename"], e.meta["lineno"])
        for e in entries
        if isinstance(e, data.Transaction)
        and all(e.meta.get(k) == v for k, v in match.items())
    }
    kept = [
        e for e in entries
        if not (isinstance(e, data.Price) and METADATA_FIELD in e.meta
                and (e.meta["filename"], e.meta["lineno"]) in sources)
    ]
    return kept, []
