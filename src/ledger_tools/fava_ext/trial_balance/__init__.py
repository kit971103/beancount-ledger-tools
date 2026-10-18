"""Fava extension: a trial balance of hand-picked accounts, in groups of your own.

Fava's own reports group by account root (Assets, Liabilities, ...) and label rows
with the beancount account name.  This page does neither: an account appears only
if its `open` directive carries a `tb-group` metadata, it is listed under that
group, and it is labelled by its `name` metadata, else the account name.

    2000-01-01 open Assets:Bank:Checking CAD
      name: "Everyday Chequing"
      tb-group: "Cash"

A tagged account carries its whole subtree, so tagging a parent brings its
children along -- except a child tagged itself, which is counted under its own
group and not again under the parent's.

Balances come from fava's filtered, UNCAPPED tree: with a time filter, balance
sheet accounts show their closing balance (fava clamps earlier activity into an
opening balance), Income/Expenses show the period's activity, and prior earnings
sit in Equity.  No subtotals or grand total are shown: over a selection of
accounts they would not sum to anything meaningful.  Amounts are converted with fava's conversion selector (the "CAD" in
the URL) and split into Debit (positive) and Credit (negative) columns, in
beancount's sign -- `invert-income-liabilities-equity` is not applied here.

Group order comes from the extension config; groups it does not name follow in
alphabetical order:

    2000-01-01 custom "fava-extension" "ledger_tools.fava_ext.trial_balance" "{
      'groups': ['Cash', 'Credit cards', 'Loans'],
    }"
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal

from fava.context import g
from fava.core.inventory import CounterInventory
from fava.ext import FavaExtensionBase

GROUP_KEY = "tb-group"
NAME_KEY = "name"


@dataclass
class Row:
    """One tagged account and its balance, one amount per currency."""

    account: str
    label: str
    amounts: dict[str, Decimal]


@dataclass
class Group:
    """A named group of rows."""

    name: str
    rows: list[Row] = field(default_factory=list)


def _nearest_tagged(account: str, tagged: dict[str, str]) -> str | None:
    """The account itself or its closest ancestor that carries `tb-group`."""
    parts = account.split(":")
    for i in range(len(parts), 0, -1):
        candidate = ":".join(parts[:i])
        if candidate in tagged:
            return candidate
    return None


class TrialBalance(FavaExtensionBase):
    """Trial balance of `tb-group`-tagged accounts, regrouped and relabelled."""

    report_title = "Trial Balance"

    def _group_order(self) -> list[str]:
        config = self.config if isinstance(self.config, dict) else {}
        return list(config.get("groups", []))

    def groups(self, *, show_zero: bool = False) -> list[Group]:
        """Return the groups in display order."""
        opens = g.ledger.all_entries_by_type.Open
        tagged = {o.account: str(o.meta[GROUP_KEY]) for o in opens
                  if GROUP_KEY in o.meta}
        labels = {}
        for o in opens:
            if o.account in tagged:
                labels[o.account] = str(o.meta.get(NAME_KEY) or o.account)

        # Sum each tree node into its nearest tagged ancestor-or-self, so a
        # tagged parent carries its untagged children exactly once.
        inventories = {account: CounterInventory() for account in tagged}
        for account, node in g.filtered.root_tree.items():
            owner = _nearest_tagged(account, tagged)
            if owner is not None:
                inventories[owner].add_inventory(node.balance)

        by_name: dict[str, Group] = {}
        for account in sorted(tagged):
            converted = g.conv.apply(inventories[account], g.ledger.prices,
                                     g.filtered.end_date)
            amounts = {c: n for c, n in sorted(converted.items()) if n}
            if not amounts and not show_zero:
                continue
            group = by_name.setdefault(tagged[account], Group(tagged[account]))
            group.rows.append(Row(account, labels[account], amounts))

        order = self._group_order()
        rank = {name: i for i, name in enumerate(order)}
        ordered = sorted(by_name.values(),
                         key=lambda grp: (rank.get(grp.name, len(order)), grp.name))
        for group in ordered:
            group.rows.sort(key=lambda row: row.label.lower())
        return ordered
