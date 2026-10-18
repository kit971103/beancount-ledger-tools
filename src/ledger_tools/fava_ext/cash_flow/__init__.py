"""Fava extension: a cash flow statement (direct method), with a CSV export.

The method -- which accounts are cash, how transfers are looked through, how
currencies convert -- is in statement.py's docstring.  This module is the fava
glue: it takes the period and filter from fava's filter bar, and the report
currency from its conversion selector when that names a currency (else the
configured one).  The opening and closing cash, and the exchange-rate effect
that reconciles them with the flows, are shown only when no filter other than
time is active: under a tag or account filter the flows are a selection, and
would not add up to the pool's balance change.

Every Assets/Liabilities account needs `cf:` metadata on its `open`, or an
ancestor's (Assets:Receivable:<Person> takes Assets:Receivable's); one without either that
meets cash is listed under "Other" as `(no cf:) <account>`.

    2000-01-01 custom "fava-extension" "ledger_tools.fava_ext.cash_flow" "{
      'currency': 'CAD',
      'transfer_accounts': ('Assets:98Transfer', 'Assets:99ZeroSum:Match-Transfer'),
      'split_account': 'Assets:99ZeroSum:Split-Tag',
    }"

Look-through needs zerosum's `'match_metadata': True`, which tags both halves
of each matched transfer with the same `match_id`.

The CSV (the link on the page, or `extension/CashFlow/csv` with the same query
string) has one row per account and per section total, then the
reconciliation.
"""

from __future__ import annotations

import csv
import io

from beancount.core import prices
from flask import Response
from flask import request

from fava.context import g
from fava.ext import FavaExtensionBase
from fava.ext import extension_endpoint

from . import statement as st


class CashFlow(FavaExtensionBase):
    """Cash flow statement over the cash pool (`cf: "cash"` accounts)."""

    report_title = "Cash Flow"

    def after_load_file(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self._config = st.Config(**config)
        self._index = st.Index(self.ledger.all_entries, self._config)
        self._price_map = prices.build_price_map(self.ledger.all_entries)

    def _ensure_loaded(self) -> None:
        if not hasattr(self, "_index"):
            self.after_load_file()

    def currency(self) -> str:
        """The conversion selector's currency, else the configured one."""
        conversion = g.conversion
        if conversion not in ("at_cost", "at_value", "units") and "," not in conversion:
            return conversion
        return self._config.currency

    def is_filtered(self) -> bool:
        return bool(request.args.get("filter", "").strip()
                    or request.args.get("account", "").strip())

    def statement(self) -> st.Statement:
        self._ensure_loaded()
        date_range = g.filtered.date_range
        return st.build(
            self._index, self._price_map, self.currency(),
            g.filtered.entries, self.ledger.all_entries,
            begin=date_range.begin if date_range else None,
            end=date_range.end if date_range else None,
            reconcile=not self.is_filtered(),
        )

    @extension_endpoint
    def csv(self) -> Response:
        """The statement as CSV, for the same filters as the page."""
        statement = self.statement()
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["section", "account", "depth", "leaf", statement.currency])
        for section in statement.sections:
            for node in section.tree.rows():
                writer.writerow([section.key, node.account, node.depth,
                                 int(not node.children), _cents(node.total)])
            writer.writerow([section.key, "TOTAL", 0, 0, _cents(section.total)])
        writer.writerow(["summary", "Net cash flow", 0, 0, _cents(statement.net_flow)])
        if statement.opening is not None:
            writer.writerow(["summary", "Opening cash", 0, 0, _cents(statement.opening)])
            writer.writerow(["summary", "Effect of exchange rates", 0, 0,
                             _cents(statement.fx_effect)])
            writer.writerow(["summary", "Closing cash", 0, 0, _cents(statement.closing)])
        begin = statement.begin.isoformat() if statement.begin else "all"
        name = f"cash-flow-{begin}-{statement.currency}.csv"
        return Response(out.getvalue(), mimetype="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{name}"'})


def _cents(number):
    return f"{number:.2f}"
