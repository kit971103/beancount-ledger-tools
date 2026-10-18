"""Expand receivable postings into one sub-account per person, named by metadata.

The ledger books money owed by or to other people on a single account, with the
person on the posting:

    2025-05-06 * "Sports Club" "Session"
      Assets:Prepaid:Card              -60.00 CAD
      Expenses:Entertainment:Sport      20.00 CAD
      Assets:Receivable                       20.00 CAD
        person: "Jane Doe"
      Assets:Receivable                       20.00 CAD

The entries the reports see post to `Assets:Receivable:JaneDoe` and, for the posting
with no person, `Assets:Receivable:Misc`.  Each person's account balance is what they
owe (negative: what I owe them), so nothing needs matching, and the source files
never list the people: the plugin opens every sub-account itself, on the parent's
`open` date and with its currencies.  A `balance` directive may name a
sub-account directly (`balance Assets:Receivable:JaneDoe 0 CAD`).

The person is the posting's metadata value, else the transaction's (identical
metadata is promoted to the transaction), else the default.  A name becomes an
account component by keeping its ASCII letters and digits, capitalising the
first letter of each word and joining the words: "Jane Doe" and "jane doe" are
both `JaneDoe`.  A name with no ASCII letter at all falls to the default.  Two
names that differ only in the case of later letters ("McKay", "Mckay") are an
error, not two people -- fix the spelling in the source.

Usage, in main.beancount:

    plugin "ledger_tools.plugin.ar_by_person" "{
      'account': 'Assets:Receivable',
      'key': 'person',
      'default': 'Misc',
    }"

Put the directive after `exclude_flagged`, so a dropped scheduled entry opens no
account.
"""

import ast
import collections
import re

from beancount.core import data

__plugins__ = ("ar_by_person",)

DEFAULTS = {"account": "Assets:Receivable", "key": "person", "default": "Misc"}

ArByPersonError = collections.namedtuple("ArByPersonError", "source message entry")


def component(name, default):
    """Turn a person's name into an account component, e.g. "Jane doe" -> "JaneDoe"."""
    words = re.findall(r"[A-Za-z0-9]+", str(name))
    comp = "".join(w[0].upper() + w[1:] for w in words)
    if not comp or not re.match(r"[A-Z]", comp):
        return default
    return comp


def ar_by_person(entries, options_map, config=None):
    """Rewrite postings on `account` to `account:<Person>` and open each one."""
    cfg = dict(DEFAULTS)
    if config:
        cfg.update(ast.literal_eval(config))
    parent, key, default = cfg["account"], cfg["key"], cfg["default"]

    errors = []
    parent_open = None
    opened = set()
    for entry in entries:
        if isinstance(entry, data.Open):
            opened.add(entry.account)
            if entry.account == parent:
                parent_open = entry

    spellings = collections.defaultdict(dict)  # lower -> {component: first entry}
    first_use = {}
    new_entries = []
    for entry in entries:
        if isinstance(entry, data.Transaction) and any(
            p.account == parent for p in entry.postings
        ):
            postings = []
            for posting in entry.postings:
                if posting.account == parent:
                    name = (posting.meta or {}).get(key) or entry.meta.get(key) or default
                    comp = component(name, default)
                    spellings[comp.lower()].setdefault(comp, entry)
                    account = "{}:{}".format(parent, comp)
                    first_use.setdefault(account, entry.date)
                    posting = posting._replace(account=account)
                postings.append(posting)
            entry = entry._replace(postings=postings)
        new_entries.append(entry)

    for forms in spellings.values():
        if len(forms) > 1:
            (first, a), (second, b) = list(forms.items())[:2]
            errors.append(
                ArByPersonError(
                    b.meta,
                    "{} '{}' and '{}' differ only in case; use one spelling".format(
                        key, first, second
                    ),
                    b,
                )
            )

    if first_use and parent_open is None:
        errors.append(
            ArByPersonError(
                data.new_metadata("<ar_by_person>", 0),
                "{} has postings but no open directive".format(parent),
                None,
            )
        )
        return new_entries, errors

    opens = []
    for account in sorted(first_use):
        if account in opened:
            continue
        meta = data.new_metadata("<ar_by_person>", 0)
        opens.append(
            data.Open(meta, parent_open.date, account, parent_open.currencies, None)
        )
    new_entries.extend(opens)
    new_entries.sort(key=data.entry_sortkey)
    return new_entries, errors
