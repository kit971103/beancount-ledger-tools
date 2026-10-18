"""Draw RRSP / TFSA / FHSA contribution room down as each contribution happens.

The ledger grants room -- RRSP and FHSA from each notice of assessment, TFSA as
the annual dollar limit -- and asserts the balance each NOA prints.  This plugin is
the other half: every contribution to a registered account uses room, on the day
it is made, so the room accounts always show what is left, and the NOA's
assertion checks the two halves against each other.  If an assertion fails,
either a contribution is missing or mis-booked, or the CRA counted something
differently; that is the point of asserting.

A contribution is a transaction that moves CAD INTO an account whose `open` has
`type:` RRSP, TFSA or FHSA from ANOTHER Assets or Liabilities account: the net CAD
on the registered account, over the transaction's postings, is positive, and some
posting sits on a different balance-sheet account.  That covers a payroll RRSP
deposit (via Assets:98Transfer) and an internal transfer from a taxable
broker account.  It excludes an FX conversion inside the account, in either direction
(every other posting is on the same account or an expense), cash that arrives from
Income or Equity (interest, or unexplained rows parked in Equity:Suspense),
and a `pad` (not a transaction when plugins run).  To each one it appends two
postings, in memory, so the room used sits in the same transaction as the money:

    2025-01-15 * "Broker" "Internal transfer"
      Assets:Broker:Taxable       -10000.00 CAD
      Assets:Broker:RRSP            6000.00 CAD
      Assets:Broker:TFSA            4000.00 CAD
      Assets:CRA:RRSP-Room         -6000.00 CRA_ROOM   ; appended
      Equity:Contribution-Room      6000.00 CRA_ROOM   ; appended
      Assets:CRA:TFSA-Room         -4000.00 CRA_ROOM   ; appended
      Equity:Contribution-Room      4000.00 CRA_ROOM   ; appended

The appended postings carry `generated-by: "contribution_room"` in their own
metadata.  The pair balances in CRA_ROOM, so the transaction still balances.

Usage, in main.beancount (before deferred_tax, which only needs to come last):

    plugin "ledger_tools.plugin.contribution_room" "{
     'types': ('RRSP', 'TFSA', 'FHSA'),
     'room': 'Assets:CRA:{type}-Room',
     'equity': 'Equity:Contribution-Room',
     'commodity': 'CRA_ROOM',
     'currency': 'CAD',
    }"

What it does NOT know, on purpose -- each is stated so an assertion miss can be
read against it:

  * Only CAD counts.  A contribution booked in another currency (say, a TFSA
    transfer in USD) uses no room here.
  * Withdrawals are ignored.  A TFSA withdrawal restores room only on the next
    1 January; book that as its own grant.
  * The RRSP 60-day rule is not modelled: a January contribution deducted for the
    previous year still uses room on the day it is made.
  * A transfer between two registered accounts would count as a contribution to
    the receiving one.
"""

import collections
from ast import literal_eval
from decimal import Decimal

from beancount.core import amount, data

__plugins__ = ("contribution_room",)

ContributionRoomError = collections.namedtuple(
    "ContributionRoomError", "source message entry"
)

DEFAULTS = {
    "types": ("RRSP", "TFSA", "FHSA"),
    "room": "Assets:CRA:{type}-Room",
    "equity": "Equity:Contribution-Room",
    "commodity": "CRA_ROOM",
    "currency": "CAD",
}


def contribution_room(entries, options_map, config=None):
    try:
        cfg = dict(DEFAULTS)
        cfg.update(literal_eval(config) if isinstance(config, str) else (config or {}))
    except Exception as exc:  # noqa: BLE001
        return entries, [
            ContributionRoomError(
                data.new_metadata("<contribution_room>", 0),
                "bad config %r: %s" % (config, exc),
                None,
            )
        ]

    kind = {
        e.account: e.meta.get("type")
        for e in entries
        if isinstance(e, data.Open) and e.meta.get("type") in cfg["types"]
    }
    opened = {e.account for e in entries if isinstance(e, data.Open)}

    out, errors = [], []
    for e in entries:
        if not isinstance(e, data.Transaction):
            out.append(e)
            continue
        added = []
        net = collections.defaultdict(Decimal)
        for p in e.postings:
            if (
                p.account in kind
                and p.units is not None
                and p.units.currency == cfg["currency"]
            ):
                net[p.account] += p.units.number
        for acct, n in sorted(net.items()):
            funded = any(
                p.account != acct
                and p.account.split(":", 1)[0] in ("Assets", "Liabilities")
                for p in e.postings
            )
            if n <= 0 or not funded:
                continue
            room = cfg["room"].format(type=kind[acct])
            if room not in opened:
                errors.append(
                    ContributionRoomError(
                        e.meta, "no open for %s (room for %s)" % (room, acct), e
                    )
                )
                continue
            meta = {
                "filename": e.meta["filename"],
                "lineno": e.meta["lineno"],
                "generated-by": "contribution_room",
            }
            units = lambda x: amount.Amount(x, cfg["commodity"])  # noqa: E731
            added += [
                data.Posting(room, units(-n), None, None, None, dict(meta)),
                data.Posting(cfg["equity"], units(n), None, None, None, dict(meta)),
            ]
        out.append(e._replace(postings=e.postings + added) if added else e)
    return out, errors
