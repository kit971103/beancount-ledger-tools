"""The cash flow statement itself: pure functions over beancount entries, no fava.

Direct method.  The *cash pool* is every account whose `open` carries
`cf: "cash"` -- bank deposits, cash, stored-value cards, and credit cards, so a
card purchase is an outflow the day it posts, under its expense category, and
paying the card off is a movement inside the pool.  Every other Assets or
Liabilities account says where it belongs with `cf: "operating"`,
`"investing"` or `"financing"`; Income and Expenses are operating, Equity is
"other" (a `pad` into a cash account shows up there, as unexplained).

For each transaction with a cash leg, the flows are its *other* legs, negated:

    2025-01-15 * "Grocer"
      Liabilities:Card:Visa        -80.00 CAD   <- cash
      Expenses:Food:Groceries       80.00 CAD   -> operating, -80.00

Three kinds of leg are looked through rather than reported:

  * a transfer-clearing leg matched by zerosum (`match_id` on both halves) is
    replaced by the partner transaction's other legs.  Net pay into a bank thus
    becomes gross salary, taxes and the payroll RRSP deduction; a deposit to
    a broker becomes an investing outflow.  A partner with a cash leg of its own is
    not looked into: the transfer is between cash accounts and cancels.
    Look-through recurses (a partner's own transfer legs are followed in turn)
    and scales, so a payslip that paid into two cash accounts is shared out in
    proportion.  An unmatched leg is reported as such.
  * a split-tag leg (ledger_tools.plugin.split_tagged_legs) is replaced by its
    split-off twin's legs, when the twin passed the filter; otherwise (under
    `#partner`, say) those legs are outside the selection and left out.
  * a leg on another cash account is internal and never reported.

Amounts are the legs' weights (price or cost included) converted into the
report currency at the date of the cash transaction, so every transaction's
flows sum to minus its cash legs, exactly.  The difference between that and
the pool's balance change -- the pool valued at the opening and closing
rates -- is the effect of exchange rates, as in IAS 7.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal

from beancount.core import convert
from beancount.core import data
from beancount.core import flags
from beancount.core.amount import Amount

CF_KEY = "cf"
CASH = "cash"
SECTIONS = ("operating", "investing", "financing", "other")
SECTION_TITLES = {
    "operating": "Operating activities",
    "investing": "Investing activities",
    "financing": "Financing activities",
    "other": "Other and unexplained",
}
# Pseudo-accounts for legs that could not be followed to a real account.
UNMATCHED = "(unmatched transfer)"
FILTERED_OUT = "(split off by the tag filter)"
INTERNAL = "(between cash accounts)"  # never reported, but counted in the check
UNCLASSIFIED_PREFIX = "(no cf:) "

# Entries fava's time filter synthesizes (opening balances, earnings transfer,
# conversions) are bookkeeping, not cash moving.
SYNTHETIC_FLAGS = {flags.FLAG_SUMMARIZE, flags.FLAG_TRANSFER, flags.FLAG_CONVERSIONS}
MAX_DEPTH = 4
TOLERANCE = Decimal("0.005")
ZERO = Decimal(0)


@dataclass
class Config:
    """Where the ledger keeps its clearing accounts; see the extension docstring."""

    currency: str = "CAD"
    transfer_accounts: tuple[str, ...] = ("Assets:98Transfer",
                                          "Assets:99ZeroSum:Match-Transfer")
    split_account: str = "Assets:99ZeroSum:Split-Tag"
    match_key: str = "match_id"


@dataclass
class Node:
    """One account in a section's tree; `total` includes the whole subtree."""

    account: str
    label: str
    depth: int
    total: Decimal = ZERO
    own: Decimal = ZERO
    children: dict[str, Node] = field(default_factory=dict)

    def rows(self):
        """Yield this node's descendants depth-first, largest first."""
        for child in sorted(self.children.values(),
                            key=lambda n: (-abs(n.total), n.account)):
            yield child
            yield from child.rows()


@dataclass
class Section:
    key: str
    title: str
    tree: Node
    total: Decimal = ZERO


@dataclass
class Problem:
    """A transaction whose flows do not add up, or an amount not converted."""

    entry: data.Transaction
    message: str


@dataclass
class Statement:
    currency: str
    begin: datetime.date | None
    end: datetime.date | None  # exclusive
    sections: list[Section]
    net_flow: Decimal
    opening: Decimal | None = None
    closing: Decimal | None = None
    fx_effect: Decimal | None = None
    problems: list[Problem] = field(default_factory=list)


class Index:
    """What look-through needs from the whole ledger, built once per load."""

    def __init__(self, entries, config: Config):
        self.config = config
        self.cf: dict[str, str] = {}
        self.matches: dict[str, list[tuple[data.Transaction, data.Posting]]] = defaultdict(list)
        for entry in entries:
            if isinstance(entry, data.Open) and CF_KEY in entry.meta:
                self.cf[entry.account] = str(entry.meta[CF_KEY])
            elif isinstance(entry, data.Transaction):
                for posting in entry.postings:
                    match_id = (posting.meta or {}).get(config.match_key)
                    if match_id is not None and posting.account in config.transfer_accounts:
                        self.matches[match_id].append((entry, posting))

    def cf_of(self, account: str) -> str | None:
        """The account's `cf:`, else its nearest ancestor's -- accounts a plugin
        opens (Assets:Receivable:<Person>) carry none of their own."""
        parts = account.split(":")
        for i in range(len(parts), 0, -1):
            cf = self.cf.get(":".join(parts[:i]))
            if cf is not None:
                return cf
        return None

    def is_cash(self, account: str) -> bool:
        return self.cf_of(account) == CASH

    def partner(self, entry, posting):
        """The other half of a zerosum match, or None."""
        match_id = (posting.meta or {}).get(self.config.match_key)
        for other_entry, other_posting in self.matches.get(match_id, ()):
            if other_posting is not posting:
                return other_entry, other_posting
        return None


def _twins(entries, config: Config):
    """Map each tagged half of a split transaction to its split-off twin."""
    by_source = defaultdict(list)
    for entry in entries:
        if isinstance(entry, data.Transaction) and any(
                p.account == config.split_account for p in entry.postings):
            by_source[(entry.meta.get("filename"), entry.meta.get("lineno"))].append(entry)
    twins = {}
    for group in by_source.values():
        tagged = [e for e in group if "split-tag" not in e.meta]
        split_off = [e for e in group if "split-tag" in e.meta]
        # A transaction split on several tags leaves a chain; pair in order.
        for a, b in zip(tagged + split_off[:-1], split_off):
            twins[id(a)] = b
    return twins


class Builder:
    """Turns filtered entries into a Statement."""

    def __init__(self, index: Index, price_map, currency: str, filtered_entries):
        self.index = index
        self.config = index.config
        self.price_map = price_map
        self.currency = currency
        self.twins = _twins(filtered_entries, self.config)
        self.problems: list[Problem] = []

    # -- conversion ---------------------------------------------------------

    def value(self, amount: Amount, date, entry) -> Decimal:
        if amount.currency == self.currency:
            return amount.number
        converted = convert.convert_amount(amount, self.currency, self.price_map, date)
        if converted.currency != self.currency:
            self.problems.append(Problem(entry, f"no {amount.currency}->{self.currency} "
                                                f"price on {date}; {amount} counted as 0"))
            return ZERO
        return converted.number

    def weight(self, posting, date, entry) -> Decimal:
        return self.value(convert.get_weight(posting), date, entry)

    # -- look-through -------------------------------------------------------

    def expand(self, entry, skip, date, depth, seen):
        """(account, weight) for every leg of `entry` not in `skip`, clearing
        legs followed to where the money came from or went.  Cash legs are
        returned under the account name, for the caller to recognise."""
        out = []
        for posting in entry.postings:
            if any(posting is s for s in skip):
                continue
            account = posting.account
            weight = self.weight(posting, date, entry)
            if account in self.config.transfer_accounts:
                out.extend(self.follow_transfer(entry, posting, weight, date, depth, seen))
            elif account == self.config.split_account:
                out.extend(self.follow_split(entry, weight, date, depth, seen))
            else:
                out.append((account, weight))
        return out

    def scaled(self, legs, weight):
        """Scale the non-cash `legs` to sum to `weight`; cash legs drop out as
        internal.  With no non-cash leg at all, the whole weight is internal."""
        sources = [(a, w) for a, w in legs
                   if a != INTERNAL and not self.index.is_cash(a)]
        total = sum((w for _, w in sources), ZERO)
        if not sources:
            return [(INTERNAL, weight)]
        if not total:
            return [(UNMATCHED, weight)]
        ratio = weight / total
        return [(a, w * ratio) for a, w in sources]

    def follow_transfer(self, entry, posting, weight, date, depth, seen):
        found = self.index.partner(entry, posting)
        if found is None or depth >= MAX_DEPTH:
            return [(UNMATCHED, weight)]
        other, other_posting = found
        if any(self.index.is_cash(p.account) for p in other.postings):
            # The partner moves cash itself, so it is reported on its own; this
            # transfer is cash to cash.  (Sharing it out over the partner's
            # other legs would book an ATM withdrawal as its 3.00 fee.)
            return [(INTERNAL, weight)]
        if id(other) in seen:
            # Back into a transaction already being expanded (two payroll legs
            # matched to one pension-plan deposit): that leg is followed on its
            # own, so here it only absorbs its share, like a cash leg.
            return [(INTERNAL, weight)]
        legs = self.expand(other, [other_posting], date, depth + 1, seen | {id(entry)})
        return self.scaled(legs, weight)

    def follow_split(self, entry, weight, date, depth, seen):
        twin = self.twins.get(id(entry))
        if twin is None:
            return [(FILTERED_OUT, weight)]
        skip = [p for p in twin.postings if p.account == self.config.split_account]
        legs = self.expand(twin, skip, date, depth + 1, seen | {id(entry)})
        return self.scaled(legs, weight)

    # -- classification -----------------------------------------------------

    def section_of(self, account: str) -> tuple[str, str]:
        """(section, account name to report under)."""
        if account.startswith("("):
            return "other", account
        root = account.split(":", 1)[0]
        if root in ("Income", "Expenses"):
            return "operating", account
        if root == "Equity":
            return "other", account
        cf = self.index.cf_of(account)
        if cf in SECTIONS:
            return cf, account
        return "other", UNCLASSIFIED_PREFIX + account

    # -- the statement ------------------------------------------------------

    def flows(self, entries):
        """Yield (entry, account, flow) for every transaction touching cash."""
        for entry in entries:
            if not isinstance(entry, data.Transaction) or entry.flag in SYNTHETIC_FLAGS:
                continue
            cash = [p for p in entry.postings if self.index.is_cash(p.account)]
            if not cash:
                continue
            cash_total = sum((self.weight(p, entry.date, entry) for p in cash), ZERO)
            legs = [(a, w) for a, w in self.expand(entry, cash, entry.date, 0, frozenset())
                    if not self.index.is_cash(a)]
            leg_total = sum((w for _, w in legs), ZERO)
            if abs(cash_total + leg_total) > TOLERANCE:
                self.problems.append(Problem(
                    entry, f"flows {-leg_total:.2f} but cash moved {cash_total:.2f}"))
            for account, weight in legs:
                if account != INTERNAL:
                    yield entry, account, -weight

    def pool_balance(self, all_entries, before: datetime.date, rate_date) -> Decimal:
        """The cash pool's units at the start of `before`, valued at `rate_date`."""
        units = defaultdict(Decimal)
        for entry in all_entries:
            if entry.date >= before:
                break
            if isinstance(entry, data.Transaction):
                for p in entry.postings:
                    if self.index.is_cash(p.account):
                        units[p.units.currency] += p.units.number
        return sum((self.value(Amount(n, c), rate_date, None) for c, n in units.items() if n),
                   ZERO)


def build(index: Index, price_map, currency: str, filtered_entries, all_entries,
          begin=None, end=None, reconcile=True) -> Statement:
    """The statement for `filtered_entries`, over [begin, end).

    With `reconcile`, also the pool's opening and closing values and the
    exchange-rate effect; it only means something when no filter other than
    time is active, since the balances come from `all_entries`.
    """
    builder = Builder(index, price_map, currency, filtered_entries)
    roots = {key: Node("", SECTION_TITLES[key], 0) for key in SECTIONS}
    for _, account, flow in builder.flows(filtered_entries):
        # CRA_ROOM legs (the contribution_room plugin's, priced at 0) are not
        # cash; legs a tag filter split off are outside the selection.
        if not flow or account == FILTERED_OUT:
            continue
        section, name = builder.section_of(account)
        _add(roots[section], name, flow)

    sections = [Section(k, SECTION_TITLES[k], roots[k], roots[k].total)
                for k in SECTIONS if roots[k].children]
    statement = Statement(currency, begin, end, sections,
                          sum((s.total for s in sections), ZERO))
    if reconcile and all_entries:
        first = begin or all_entries[0].date
        last = end or (all_entries[-1].date + datetime.timedelta(days=1))
        statement.begin, statement.end = first, last
        statement.opening = builder.pool_balance(all_entries, first,
                                                 first - datetime.timedelta(days=1))
        statement.closing = builder.pool_balance(all_entries, last,
                                                 last - datetime.timedelta(days=1))
        statement.fx_effect = statement.closing - statement.opening - statement.net_flow
    statement.problems = builder.problems
    return statement


def _add(root: Node, account: str, amount: Decimal):
    """Add `amount` to `account`'s node and every ancestor under `root`."""
    root.total += amount
    if account.startswith("("):
        parts = [account]
    else:
        parts = account.split(":")
    node = root
    for depth, part in enumerate(parts, start=1):
        name = ":".join(parts[:depth])
        node = node.children.setdefault(name, Node(name, part, depth))
        node.total += amount
    node.own += amount
