"""Split the opted-out legs of a tagged transaction into a transaction of their own.

A beancount tag belongs to the whole transaction, and fava's filter bar keeps or
drops whole transactions.  Where only part of a tagged transaction belongs to
the tag, the ledger marks the other legs with posting metadata set to FALSE
(here `partner: FALSE`):

    2025-01-15 * "Grocer" #partner
      Liabilities:Card:Visa        -80.00 CAD
      Expenses:Food:Groceries       50.00 CAD
      Expenses:Household            30.00 CAD
        partner: FALSE

With this plugin, the entry the reports see is two, joined through a zero-sum
account, so that `#partner` in the filter bar -- on the income statement or any
other page -- counts exactly the legs that are the tag's:

    2025-01-15 * "Grocer" #partner
      Liabilities:Card:Visa        -80.00 CAD
      Expenses:Food:Groceries       50.00 CAD
      Assets:99ZeroSum:Split-Tag    30.00 CAD
    2025-01-15 * "Grocer"
      split-tag: "partner"
      Assets:99ZeroSum:Split-Tag   -30.00 CAD
      Expenses:Household            30.00 CAD
        partner: FALSE

The source file is untouched.  The bank or card leg stays whole in the tagged
half, so "one posting per export row" still holds for every reconciled
account; the zero-sum account nets to zero on every date.  Unfiltered reports
are unchanged.

Usage, in main.beancount:

    plugin "ledger_tools.plugin.split_tagged_legs" "{
      'account': 'Assets:99ZeroSum:Split-Tag',
      'tags': {'partner': 'partner'},
    }"

`tags` maps a tag (without `#`) to the posting metadata key that opts a leg out
of it; only the boolean FALSE opts out, so `partner: TRUE` or a string is kept.
`account` must be opened like any other.  Each opted-out leg's weight (cost or
price included) is carried through the zero-sum account, one posting per
currency.  A transaction whose every leg opts out is an error, not a split:
the tag should come off the transaction instead.

Put the directive after `exclude_flagged`, so scheduled entries are dropped
rather than split.
"""

import collections

from beancount.core import convert
from beancount.core import data
from beancount.core import inventory

__plugins__ = ("split_tagged_legs",)

SplitTaggedLegsError = collections.namedtuple(
    "SplitTaggedLegsError", "source message entry"
)


def split_tagged_legs(entries, options_map, config=None):
    """Split each tagged transaction's opted-out legs into a second transaction.

    Args:
      entries: a list of directives.
      options_map: the parser's options map (unused).
      config: a string holding a dict literal, see the module docstring.
    Returns:
      A (entries, errors) pair.
    """
    account, tags, errors = _parse_config(config)
    if errors:
        return entries, errors

    out = []
    for entry in entries:
        if not isinstance(entry, data.Transaction):
            out.append(entry)
            continue
        # A split-off half may still carry another configured tag, so keep
        # splitting until nothing is left to split.
        pending = [entry]
        while pending:
            txn = pending.pop()
            split = _split_once(txn, account, tags, errors)
            if split is None:
                out.append(txn)
            else:
                pending.extend(split)
    out.sort(key=data.entry_sortkey)
    return out, errors


def _split_once(txn, account, tags, errors):
    """Split `txn` on the first configured tag that has opted-out legs.

    Returns None when there is nothing to split, else the (tagged, split-off)
    pair.
    """
    for tag in sorted(tags.keys() & txn.tags):
        key = tags[tag]
        out_legs = [p for p in txn.postings if (p.meta or {}).get(key) is False]
        if not out_legs:
            continue
        in_legs = [p for p in txn.postings if (p.meta or {}).get(key) is not False]
        if not in_legs:
            errors.append(SplitTaggedLegsError(
                txn.meta,
                "Every leg carries `{}: FALSE`; remove #{} from the transaction "
                "instead".format(key, tag),
                txn,
            ))
            return None

        weight = inventory.Inventory()
        for posting in out_legs:
            weight.add_amount(convert.get_weight(posting))

        def bridge(sign):
            return [data.Posting(account, pos.units if sign > 0 else -pos.units,
                                 None, None, None, None)
                    for pos in weight]

        tagged = txn._replace(postings=in_legs + bridge(+1))
        meta = dict(txn.meta)
        meta["split-tag"] = tag
        split_off = txn._replace(meta=meta, tags=txn.tags - {tag},
                                 postings=bridge(-1) + out_legs)
        return [tagged, split_off]
    return None


def _parse_config(config):
    """Turn the directive's config string into (account, {tag: key}, errors)."""
    def error(message):
        return None, {}, [SplitTaggedLegsError(
            data.new_metadata("<split_tagged_legs>", 0), message, None)]

    if not isinstance(config, str):
        return error("Config must be a dict literal string, not {!r}".format(config))
    try:
        parsed = eval(config, {"__builtins__": {}})  # noqa: S307 -- same as beancount's own plugins
    except Exception as exc:  # noqa: BLE001
        return error("Cannot parse config {!r}: {}".format(config, exc))
    if not isinstance(parsed, dict):
        return error("Config must be a dict, not {!r}".format(parsed))
    account = parsed.get("account")
    tags = parsed.get("tags")
    if not isinstance(account, str) or not account:
        return error("Config needs 'account', the zero-sum account to split through")
    if not isinstance(tags, dict) or not tags or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in tags.items()):
        return error("Config needs 'tags', a non-empty {tag: metadata key} dict")
    return account, tags, []
