"""Book the tax that is owed but not yet due: on RRSPs, and on unrealized gains.

Two liabilities, one rate, one expense account:

RRSPs.  An RRSP is pre-tax money: every dollar in it is taxed on the way out.
Treating the whole balance as net worth overstates it, and treating the refund an
RRSP deduction produces as income overstates the year's earnings -- the refund is
the CRA lending back tax it will collect later.  So:

  * a refund goes to the RRSP liability, not to Income or to Expenses:Taxes;
  * at every month-end this plugin sets that liability to `rate` x the market
    value of all accounts opened with `type: "RRSP"`, and posts the difference
    to the expense account.

The expense account therefore carries only what is genuinely a gain or a cost:
tax on the RRSP's growth, and the gap between the refund received and the tax
provisioned at `rate`.  A payroll RRSP contribution, whose tax saving arrives as
lower withholding rather than a refund, is provisioned the same way.

Unrealized gains.  Every posting to the `gains` account (Income:Investment:
UnrealizedGain) in a transaction touching a *taxable* account -- one whose `open`
`type` is not in `exempt_types` -- gets a companion entry on the same date, in the
same currency: `rate` x the gain to the expense account, against the gains
liability.  A loss reverses it.  RRSP gains are excluded because the RRSP
provision above already values them; TFSA and FHSA gains are not taxed.

Usage, in main.beancount (after every plugin that adds or drops postings):

    plugin "ledger_tools.plugin.deferred_tax" "{
     'rate': '0.20',
     'expense': 'Expenses:Investment:DeferredTax',
     'rrsp_liability': 'Liabilities:CRA:RRSP-Deferred-Tax',
     'gains': 'Income:Investment:UnrealizedGain',
     'gains_liability': 'Liabilities:CRA:CapitalGains-Deferred-Tax',
     'exempt_types': ('RRSP', 'TFSA', 'FHSA'),
    }"

`rrsp_type` (default "RRSP") picks the RRSP accounts by their `open` metadata.

Market value and gains are taken per currency -- a position is valued in its cost
or price currency with the price map as of the month-end, and not converted
further -- so a USD account gets a USD liability and needs no FX rate.  RRSP
month-ends run from the first RRSP posting to the last month-end on or before the
latest transaction; the current, unfinished month gets no entry.

RRSPs and taxable brokerage accounts typically book market value through `pad`, and
beancount expands pads only after every user plugin (loader.PLUGINS_POST).  So the
plugin expands them itself, on a copy used only to read values and gains; what it
returns is the original entries plus its own.

The entries are generated in memory only, flagged `*`, and carry
`generated-by: "deferred_tax"`.  `rate` is an assumption, not a fact from any
document; change it here and every entry is re-derived.
"""

import collections
import datetime
from decimal import Decimal

from beancount.core import amount
from beancount.core import convert
from beancount.core import data
from beancount.core import inventory
from beancount.core import prices
from beancount.ops import pad

__plugins__ = ("deferred_tax",)

DeferredTaxError = collections.namedtuple(
    "DeferredTaxError", "source message entry")

CENT = Decimal("0.01")
REQUIRED = ("rate", "expense", "rrsp_liability", "gains", "gains_liability")


def deferred_tax(entries, options_map, config=None):
    cfg, errors = _parse_config(config)
    if errors:
        return entries, errors

    types = {e.account: e.meta.get("type") for e in entries
             if isinstance(e, data.Open)}
    padded, _ = pad.pad(entries, options_map)
    txns = sorted((e for e in padded if isinstance(e, data.Transaction)),
                  key=data.entry_sortkey)
    if not txns:
        return entries, []

    new = _rrsp_true_ups(txns, types, prices.build_price_map(entries), cfg)
    new += _gain_provisions(txns, types, cfg)
    return sorted(entries + new, key=data.entry_sortkey), []


def _rrsp_true_ups(txns, types, price_map, cfg):
    """Month-end entries holding the RRSP liability at -rate x RRSP value."""
    rrsp = {a for a, t in types.items() if t == cfg["rrsp_type"]}
    first = next((t.date for t in txns
                  if any(p.account in rrsp for p in t.postings)), None)
    if first is None:
        return []
    last = txns[-1].date

    held = inventory.Inventory()
    owed = inventory.Inventory()
    new = []
    month_end = _month_end(first)
    it = iter(txns)
    txn = next(it, None)
    while month_end <= last:
        while txn is not None and txn.date <= month_end:
            for p in txn.postings:
                if p.account in rrsp:
                    held.add_position(p)
                elif p.account == cfg["rrsp_liability"]:
                    owed.add_position(p)
            txn = next(it, None)
        entry = _true_up(month_end, held, owed, price_map, cfg)
        if entry is not None:
            new.append(entry)
            for p in entry.postings:
                if p.account == cfg["rrsp_liability"]:
                    owed.add_position(p)
        month_end = _month_end(month_end + datetime.timedelta(days=1))
    return new


def _true_up(date, held, owed, price_map, cfg):
    """The entry that brings `owed` to -rate x value(held), or None."""
    value = collections.defaultdict(Decimal)
    for pos in held:
        v = convert.get_value(pos, price_map, date)
        value[v.currency] += v.number
    current = collections.defaultdict(Decimal)
    for pos in owed:
        current[pos.units.currency] += pos.units.number

    postings = []
    for cur in sorted(set(value) | set(current)):
        target = -(value[cur] * cfg["rate"]).quantize(CENT)
        diff = target - current[cur]
        if diff:
            postings += _pair(cfg["rrsp_liability"], cfg["expense"], diff, cur)
    if not postings:
        return None
    return _entry(date, "RRSP deferred tax at {:.0%} of market value".format(
        cfg["rate"]), postings)


def _gain_provisions(txns, types, cfg):
    """One entry per unrealized-gain posting on a taxable account."""
    new = []
    for txn in txns:
        others = [p.account for p in txn.postings if p.account != cfg["gains"]]
        if not others or any(types.get(a) in cfg["exempt_types"] for a in others):
            continue
        for p in txn.postings:
            if p.account != cfg["gains"]:
                continue
            # Income is negative: a gain of 100 posts -100 to the gains account.
            tax = (-p.units.number * cfg["rate"]).quantize(CENT)
            if tax:
                new.append(_entry(
                    txn.date,
                    "Deferred tax at {:.0%} on unrealized gain, {}".format(
                        cfg["rate"], ", ".join(sorted(set(others)))),
                    _pair(cfg["gains_liability"], cfg["expense"], -tax,
                          p.units.currency)))
    return new


def _pair(liability, expense, liability_amount, currency):
    return [data.Posting(liability, amount.Amount(liability_amount, currency),
                         None, None, None, None),
            data.Posting(expense, amount.Amount(-liability_amount, currency),
                         None, None, None, None)]


def _entry(date, narration, postings):
    meta = data.new_metadata("<deferred_tax>", 0, {"generated-by": "deferred_tax"})
    return data.Transaction(meta, date, "*", None, narration,
                            data.EMPTY_SET, data.EMPTY_SET, postings)


def _month_end(d):
    nxt = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    return nxt - datetime.timedelta(days=1)


def _parse_config(config):
    """Turn the directive's config string into a dict, or errors."""
    def error(message):
        return None, [DeferredTaxError(
            data.new_metadata("<deferred_tax>", 0), message, None)]

    if not isinstance(config, str):
        return error("Config must be a dict literal string, not {!r}".format(config))
    try:
        parsed = eval(config, {"__builtins__": {}})  # noqa: S307 -- same as beancount's own plugins
    except Exception as exc:  # noqa: BLE001
        return error("Cannot parse config {!r}: {}".format(config, exc))
    if not isinstance(parsed, dict):
        return error("Config must be a dict, not {!r}".format(parsed))
    for key in REQUIRED:
        if key not in parsed:
            return error("Config needs {!r}".format(key))
    try:
        rate = Decimal(str(parsed["rate"]))
    except Exception:  # noqa: BLE001
        return error("'rate' must be a number, not {!r}".format(parsed["rate"]))
    if not Decimal(0) <= rate < Decimal(1):
        return error("'rate' must be in [0, 1), not {}".format(rate))
    cfg = {k: parsed[k] for k in REQUIRED}
    cfg["rate"] = rate
    cfg["rrsp_type"] = parsed.get("rrsp_type", "RRSP")
    cfg["exempt_types"] = tuple(parsed.get("exempt_types", ("RRSP", "TFSA", "FHSA")))
    return cfg, []
