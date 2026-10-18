"""IBKR Flex exports -> fund-activity and FX transactions: Parse, Merge, Validate.

Two Flex queries feed the IBKR sub-ledger (`[extract.ibkr] ledger` in the config).
Each is recognised by its exact header, and a file whose header matches neither is
refused -- that is the check against being handed the wrong export:

  * **The fund query -- deposits, withdrawals and FX translation.**  One block per account,
    header repeated.  Only rows whose ActivityDescription does NOT start with
    `CASH:` are used: `Net cash activity` (a day's net cash movement in one
    currency -- a deposit, a withdrawal, one side of an internal transfer, or
    interest), `DISBURSEMENT`, `CASH RECEIPTS / ELECTRONIC FUND TRANSFERS`.  The
    `CASH:` rows are the same FX trades as the other query, per execution rather
    than per order and without the order id, so they serve only as a cross-check.
    The account's functional currency is USD, so this query never shows USD cash
    moving -- a USD deposit would be invisible to it.
  * **The trade query.**  Only `AssetClass == "CASH"` rows (FX conversions) are
    used; stock trades are someone else's job.  `Symbol` is `BASE.QUOTE`,
    `Quantity` is in BASE, `Proceeds` in QUOTE, `IBOrderID` becomes `bank-ref`.

The flow is three steps, one sub-command each:

  parse     export -> beancount text, printed.  Nothing is written.
  merge     compare that with what the ledger already holds: rows already booked
            (matched on `bank-ref`, else date + account + amount), rows to add, and
            ledger entries in the window that no export row explains.  Printed;
            the ledger is edited by hand (or by the agent following the
            `ibkr-flex-reconcile` skill) -- nothing in this package writes to it.
  validate  prove the ledger and the exports agree: (a) every IBKR posting in the
            window matches an export row and vice versa, as multisets; (b) CAD and
            HKD run back to ~0 at every `balance` date in balance.beancount;
            (c) the fund query's `CASH:` rows sum, per account/day/currency, to the
            trade query's FX trades.

Booking decisions:
  * amounts are rounded to cents -- the ledger is kept at `.00`;
  * an FX trade with a leg that rounds to 0.00 is one of IBKR's nightly residual
    sweeps that cents cannot express, and is NOT booked (the only case where the
    ledger holds fewer transactions than the export).  Sweeps with both legs
    visible at cents are booked: dropping a 0.0065 USD / 0.05 HKD sweep would
    leave the 0.05 HKD behind, and those add up;
  * commission is USD (proved: CAD/HKD return to zero only if it is) and goes to
    Expenses:Investment:Commission;
  * a `Net cash activity` row under SMALL in absolute value is not a transfer but
    interest or similar the export does not name: flag `?`, against
    Equity:Suspense;
  * rows on one date, one currency, where a MAIN outflow equals the inflows to the
    registered accounts, are one internal transfer, posted account to account.

The ledger names its IBKR accounts in `[extract.ibkr]`: the Flex query ids, each
ClientAccountID's alias and ledger account, which one is MAIN (the taxable
account), and optionally a plug account for transfers dated before `plug_before`.

Usage:
    ledger-extract ibkr_flex parse    [--since 2025-01-01] [--until ...]
    ledger-extract ibkr_flex merge    [--since 2025-01-01]
    ledger-extract ibkr_flex validate [--since 2025-01-01]
"""

import argparse
import csv
import datetime
import functools
import glob
import os
import re
import sys
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP

from ledger_tools import config
from ledger_tools.extract import common, ledger

SOURCE = "ibkr"
TEMP_FILE = "temp.beancount"

HEADERS = {
    "fund": ["ClientAccountID", "AccountAlias", "DateTime", "FunctionalCurrency",
                 "FXCurrency", "ActivityDescription", "Quantity", "Proceeds", "Cost"],
    "trade": ["ClientAccountID", "AccountAlias", "AssetClass", "DateTime", "Buy/Sell",
                  "Symbol", "Quantity", "TradePrice", "Proceeds", "IBCommission",
                  "IBOrderID", "OrderType", "CurrencyPrimary"],
}

CLEARING = "Assets:98Transfer"
SUSPENSE = "Equity:Suspense"
COMMISSION = "Expenses:Investment:Commission"
COMMISSION_CCY = "USD"
SMALL = Decimal("50")          # |Net cash activity| below this is not a transfer
CENT = Decimal("0.01")


@functools.cache
def cfg():
    """`[extract.ibkr]` -> dict(accounts, main, prefix, ledger, queries, plug_before, plug).

    Before `plug_before` no bank is in the ledger, so a deposit/withdrawal can never
    pair: its clearing leg goes to `plug` instead, keeping CLEARING's residual
    meaningful.
    """
    c = config.get("extract", "ibkr")
    return dict(
        accounts={k: tuple(v) for k, v in c["accounts"].items()},
        main=c["main"], prefix=c["main"].rsplit(":", 1)[0] + ":",
        ledger=config.path("extract", "ibkr", "ledger"),
        queries=dict(fund=c["fund_query"], trade=c["trade_query"]),
        plug_before=c.get("plug_before", ""), plug=c.get("plug"))


def q2(x):
    return x.quantize(CENT, ROUND_HALF_UP)


def _when(s):
    """'20250122;005231' / '20250121' -> (ISO date, 'HH:MM:SS' or None)."""
    d = datetime.datetime.strptime(s[:8], "%Y%m%d").date().isoformat()
    t = None
    if ";" in s:
        hms = s.split(";")[1]
        t = "%s:%s:%s" % (hms[0:2], hms[2:4], hms[4:6])
    return d, t


# --------------------------------------------------------------------------- #
# Parse

def _read(path):
    """-> ("fund" or "trade", list of dict rows).  Refuses a header it does not recognise."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        raise SystemExit("%s: empty" % path)
    query = next((q for q, h in HEADERS.items() if rows[0] == h), None)
    if query is None:
        raise SystemExit(
            "%s: header matches neither Flex query %s (deposits) nor %s (trades):\n  %s\n"
            "Re-run the right query in IBKR Client Portal." %
            (os.path.basename(path), cfg()["queries"]["fund"], cfg()["queries"]["trade"],
             ",".join(rows[0])))
    m = re.search(r"flex-(\d+)", os.path.basename(path))
    if m and m.group(1) != cfg()["queries"][query]:
        raise SystemExit("%s: named for query %s but its header is query %s's"
                         % (os.path.basename(path), m.group(1), cfg()["queries"][query]))
    head = HEADERS[query]
    out = []
    for n, r in enumerate(rows[1:], 2):
        if r == head or not any(r):
            continue                                        # repeated per account
        if len(r) != len(head):
            raise SystemExit("%s:%d: %d columns, expected %d"
                             % (os.path.basename(path), n, len(r), len(head)))
        row = dict(zip(head, r))
        if row["ClientAccountID"] not in cfg()["accounts"]:
            raise SystemExit("%s:%d: unknown IBKR account %s (%s) -- add it to "
                             "[extract.ibkr.accounts]"
                             % (os.path.basename(path), n, row["ClientAccountID"],
                                row["AccountAlias"]))
        row["_src"] = "%s:%d" % (os.path.basename(path), n)
        out.append(row)
    return query, out


def load(docs=None, since=None, until=None):
    """Every export in the IBKR documents folder -> (cash rows, fx rows, CASH: rows)."""
    root = os.path.join(common.docs_dir(docs), common.doc_folder(SOURCE).replace("/", os.sep))
    files = sorted(glob.glob(os.path.join(root, "*.flex-*.csv")))
    if not files:
        raise SystemExit("no *.flex-*.csv under %s" % root)
    cash, fx, fx_check = [], [], []
    for f in files:
        query, rows = _read(f)
        for r in rows:
            acct = cfg()["accounts"][r["ClientAccountID"]][1]
            if query == "fund":
                d, t = _when(r["DateTime"])
                rec = dict(date=d, time=t, account=acct, currency=r["FXCurrency"],
                           raw=Decimal(r["Quantity"]), desc=r["ActivityDescription"],
                           src=r["_src"])
                if r["FunctionalCurrency"] != "USD":
                    raise SystemExit("%s: functional currency %s, expected USD"
                                     % (r["_src"], r["FunctionalCurrency"]))
                (fx_check if rec["desc"].startswith("CASH:") else cash).append(rec)
            else:
                if r["AssetClass"] != "CASH":
                    continue
                d, t = _when(r["DateTime"])
                base, quote = r["Symbol"].split(".")
                if r["CurrencyPrimary"] != quote:
                    raise SystemExit("%s: CurrencyPrimary %s is not the quote of %s"
                                     % (r["_src"], r["CurrencyPrimary"], r["Symbol"]))
                fx.append(dict(date=d, time=t, account=acct, side=r["Buy/Sell"],
                               symbol=r["Symbol"], base=base, quote=quote,
                               qty=Decimal(r["Quantity"]), proceeds=Decimal(r["Proceeds"]),
                               commission=Decimal(r["IBCommission"]),
                               ref=r["IBOrderID"], order_type=r["OrderType"], src=r["_src"]))

    def keep(r):
        return (not since or r["date"] >= since) and (not until or r["date"] <= until)
    cash, fx, fx_check = ([r for r in xs if keep(r)] for xs in (cash, fx, fx_check))
    cash.sort(key=lambda r: (r["date"], r["time"] or "", r["account"]))
    fx.sort(key=lambda r: (r["date"], r["time"] or "", r["ref"]))
    return cash, fx, fx_check


def is_sweep(t):
    """IBKR's residual sweep: a leg too small to exist at cent precision."""
    return q2(t["qty"]) == 0 or q2(t["proceeds"]) == 0


def transactions(cash, fx):
    """Rows -> transactions: dict(date, time, kind, flag, narration, meta, postings, refs).

    `postings` are (account, Decimal, currency, price or None, comment or None);
    `price` is the @@ total as (Decimal, currency).
    """
    txns = []
    # internal transfers: one date + currency, a MAIN outflow equal to the
    # registered accounts' inflows
    main = cfg()["main"]
    used = set()
    by_day = defaultdict(list)
    for i, r in enumerate(cash):
        by_day[(r["date"], r["currency"])].append(i)
    for (d, ccy), idx in sorted(by_day.items()):
        outs = [i for i in idx if cash[i]["account"] == main and cash[i]["raw"] < 0]
        ins = [i for i in idx if cash[i]["account"] != main and cash[i]["raw"] >= SMALL]
        if len(outs) == 1 and ins and \
                sum(cash[i]["raw"] for i in ins) == -cash[outs[0]]["raw"]:
            rows = [cash[outs[0]]] + [cash[i] for i in ins]
            used.update(outs + ins)
            txns.append(dict(
                date=d, time=rows[0]["time"], kind="transfer", flag="*",
                narration="Internal transfer", meta={"bank-desc": rows[0]["desc"]},
                postings=[(r["account"], q2(r["raw"]), ccy, None, None) for r in rows],
                refs=[r["src"] for r in rows]))
    for i, r in enumerate(cash):
        if i in used:
            continue
        amt = q2(r["raw"])
        if abs(r["raw"]) < SMALL:
            txns.append(dict(
                date=r["date"], time=r["time"], kind="unknown", flag="?",
                narration="Unidentified cash activity", meta={"bank-desc": r["desc"]},
                postings=[(r["account"], amt, r["currency"], None, None),
                          (SUSPENSE, -amt, r["currency"], None, None)],
                refs=[r["src"]]))
            continue
        txns.append(dict(
            date=r["date"], time=r["time"], kind="deposit" if amt > 0 else "withdrawal",
            flag="*", narration="Deposit" if amt > 0 else "Withdrawal",
            meta={"bank-desc": r["desc"]},
            postings=[(r["account"], amt, r["currency"], None, None),
                      (cfg()["plug"] if str(r["date"]) < cfg()["plug_before"] else CLEARING,
                       -amt, r["currency"], None, None)],
            refs=[r["src"]]))
    for t in fx:
        if is_sweep(t):
            continue
        qty, proceeds = q2(t["qty"]), q2(t["proceeds"])
        if qty > 0:          # BUY base: pay quote
            legs = [(t["account"], qty, t["base"], None, None),
                    (t["account"], proceeds, t["quote"], (qty, t["base"]), None)]
        else:                # SELL base: receive quote
            legs = [(t["account"], proceeds, t["quote"], None, None),
                    (t["account"], qty, t["base"], (proceeds, t["quote"]), None)]
        comm = q2(t["commission"])
        if comm:
            legs += [(t["account"], comm, COMMISSION_CCY, None, None),
                     (COMMISSION, -comm, COMMISSION_CCY, None, None)]
        meta = {"bank-ref": t["ref"]}
        if t["time"]:
            meta["bank-time"] = t["time"]
        if not t["order_type"]:
            meta["bank-desc"] = "IBKR residual sweep"
        txns.append(dict(date=t["date"], time=t["time"], kind="fx", flag="*",
                         narration="FX %s %s" % (t["side"], t["symbol"]), meta=meta,
                         postings=legs, refs=[t["src"]]))
    txns.sort(key=lambda x: (x["date"], x["time"] or "", x["narration"]))
    return txns


def fmt_amount(a, ccy):
    return "%s %s" % (format(a, ",.2f").replace(",", ""), ccy)


def render(t):
    lines = ['%s %s "IBKR" "%s"' % (t["date"], t["flag"], t["narration"])]
    for k, v in t["meta"].items():
        lines.append('  %s: "%s"' % (k, v))
    for acct, amt, ccy, price, comment in t["postings"]:
        s = "  %-38s %14s" % (acct, fmt_amount(amt, ccy))
        if price:
            s += " @@ %s" % fmt_amount(*price)
        if comment:
            s += "  ; " + comment
        lines.append(s)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Merge / Validate

def ibkr_postings(txns):
    """Export transactions -> rows (date, account, currency, amount) on IBKR accounts."""
    out = []
    for t in txns:
        for acct, amt, ccy, _, _ in t["postings"]:
            if acct.startswith(cfg()["prefix"]):
                out.append((t["date"], acct, ccy, amt))
    return out


def ledger_txns(since=None, until=None, skip_files=(TEMP_FILE,)):
    txns = []
    root = cfg()["ledger"]
    for f in sorted(os.listdir(root)):
        if f.endswith(".beancount") and f not in skip_files:
            txns.extend(ledger.parse(os.path.join(root, f)))
    return [t for t in txns
            if (not since or t["date"].isoformat() >= since)
            and (not until or t["date"].isoformat() <= until)]


def ledger_postings(txns):
    out = []
    for t in txns:
        for p in t["postings"]:
            if p["account"].startswith(cfg()["prefix"]):
                out.append((t["date"].isoformat(), p["account"], p["currency"], p["amount"]))
    return out


def cmd_parse(args):
    cash, fx, _ = load(args.docs, args.since, args.until)
    txns = transactions(cash, fx)
    year = None
    for t in txns:
        if t["date"][:4] != year:
            year = t["date"][:4]
            print("\n** %s\n" % year)
        print(render(t))
        print()
    kinds = Counter(t["kind"] for t in txns)
    sweeps = sum(1 for t in fx if is_sweep(t))
    print("; %d transactions: %s; %d sub-cent sweeps not booked"
          % (len(txns), ", ".join("%s %d" % kv for kv in sorted(kinds.items())), sweeps),
          file=sys.stderr)
    return 0


def cmd_merge(args):
    cash, fx, _ = load(args.docs, args.since, args.until)
    txns = transactions(cash, fx)
    led = ledger_txns(args.since, args.until)
    refs = {t["meta"].get("bank-ref", "").strip('"') for t in led}
    have = Counter(ledger_postings(led))
    new, booked = [], 0
    for t in txns:
        rows = Counter(ibkr_postings([t]))
        if t["meta"].get("bank-ref") in refs or not rows - have:
            have -= rows
            booked += 1
            continue
        new.append(t)
    print("; export: %d transactions, %d already booked, %d new" % (len(txns), booked, len(new)))
    for t in new:
        print(render(t))
        print()
    # ledger entries the export does not explain (after taking out what matched)
    left = have
    if left:
        print("; ledger postings on IBKR accounts that no export row explains: %d"
              % sum(left.values()))
        for (d, a, c, amt), n in sorted(left.items()):
            print(";   %s %-22s %14s %s%s" % (d, a, amt, c, "  x%d" % n if n > 1 else ""))
    return 0


def _assertion_dates(since):
    out = []
    path = os.path.join(cfg()["ledger"], "balance.beancount")
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"(\d{4}-\d{2}-\d{2}) balance (\S+)\s+\S+\s+USD", line)
            if m and (not since or m.group(1) >= since):
                out.append((m.group(1), m.group(2)))
    return out


def _temp_dates():
    """Dates whose non-USD balance temp.beancount deliberately converts away."""
    path = os.path.join(cfg()["ledger"], TEMP_FILE)
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as fh:
        return set(re.findall(r"assert-date: \"(\d{4}-\d{2}-\d{2})\"", fh.read()))


def cmd_validate(args):
    cash, fx, fx_check = load(args.docs, args.since, args.until)
    txns = transactions(cash, fx)
    bad = 0

    # (a) multiset, both directions
    exp = ibkr_postings(txns)
    led = ledger_postings(ledger_txns(args.since, args.until))
    only_e, only_l = ledger.compare(
        [dict(k=r) for r in exp], [dict(k=r) for r in led], key=lambda r: r["k"])
    sweeps = [t for t in fx if is_sweep(t)]
    print("(a) export: %d transactions, %d IBKR postings (%d sub-cent sweeps not booked); "
          "ledger: %d IBKR postings" % (len(txns), len(exp), len(sweeps), len(led)))
    for label, rows in (("in the export, not the ledger", only_e),
                        ("in the ledger, not the export", only_l)):
        print("    %s: %d" % (label, len(rows)))
        for r in rows[:40]:
            print("        %s %-22s %14s %s" % r)
        bad += len(rows)

    # (b) non-USD back to ~0 at every USD assertion
    tol = Decimal(args.tolerance)
    temp = _temp_dates()
    flows = sorted(ibkr_postings(txns))
    fails = 0
    dates = _assertion_dates(args.since)
    for d, acct in dates:
        bal = defaultdict(Decimal)
        for fd, a, c, amt in flows:
            if a == acct and c != "USD" and fd < d:
                bal[c] += amt
        off = {c: v for c, v in bal.items() if abs(v) > tol}
        if off:
            mark = "temp.beancount" if d in temp else "FAIL"
            if d not in temp:
                fails += 1
            print("(b) %s %s %s -- %s" % (d, acct, dict((c, str(v)) for c, v in off.items()),
                                          mark))
    print("(b) %d USD assertion dates checked, %d with CAD/HKD beyond %s not covered by %s"
          % (len(dates), fails, tol, TEMP_FILE))
    bad += fails

    # (c) the fund query's CASH: rows vs the trade query, per account/day/currency
    a, b = defaultdict(Decimal), defaultdict(Decimal)
    for r in fx_check:
        a[(r["date"], r["account"], r["currency"])] += r["raw"]
    for t in fx:
        for ccy, v in ((t["base"], t["qty"]), (t["quote"], t["proceeds"])):
            if ccy != "USD":
                b[(t["date"], t["account"], ccy)] += v
    diff = {k: (a[k], b[k]) for k in set(a) | set(b) if abs(a[k] - b[k]) > Decimal("0.001")}
    print("(c) %d account/day/currency groups, %d disagree" % (len(set(a) | set(b)), len(diff)))
    for k in sorted(diff):
        print("        %s %-22s %s  CASH: rows %s  trades %s" % (k + diff[k]))
    bad += len(diff)
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=("parse", "merge", "validate"))
    ap.add_argument("--docs", help="path to documents/ (default: <ledger>/documents)")
    ap.add_argument("--since", help="first date (default: everything filed)")
    ap.add_argument("--until", help="last date")
    ap.add_argument("--tolerance", default="0.10",
                    help="CAD/HKD residual allowed at an assertion (default 0.10)")
    args = ap.parse_args(argv)
    return {"parse": cmd_parse, "merge": cmd_merge, "validate": cmd_validate}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
