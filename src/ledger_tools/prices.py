"""Regenerate a ledger's price file from the Bank of Canada's monthly FX averages.

    ledger-prices            # from inside the ledger repository

Rewrites the price file (`[prices] out`, default price.beancount at the ledger
root) in full, every time.  The file is
generated output: nothing in it is hand-written, and hand-editing a single
line desynchronises it from the rest of its month (see below).  To change
what the file says, change this script and re-run it.

Why the Bank of Canada
----------------------
The BoC's Valet API is open, needs no key, is an official central-bank
series, and its FX_RATES_MONTHLY group goes back decades without a gap.  It
also publishes TWD, which the ECB does not publish at all.

The BoC quotes one unit of the foreign currency in CAD (FXMHKDCAD is CAD per
HKD), which makes CAD the natural pivot: every rate this script writes is

    price of X in Q  =  (CAD per X) / (CAD per Q)

taken from the SAME monthly observation.  That is why a block must be
regenerated as a unit: every line in it is a ratio against that month's other
figures, so editing one line alone leaves it inconsistent with its neighbours
in a way nothing will flag.

Every pair, because beancount does not triangulate
--------------------------------------------------
Beancount's price map holds each declared pair and its inverse, and nothing
else.  It will NOT infer CAD/HKD from HKD/USD and CAD/USD.  Verified rather
than assumed -- a ledger holding only

    2026-08-01 price CAD 0.719528 USD
    2026-08-01 price HKD 0.127500 USD

converts 1000 HKD to 127.500000 USD and returns the same 1000 HKD untouched
when asked for CAD.

Every commodity is quoted in each report currency (`[prices] report`), and
each report currency against the others.  Inverses are left to beancount:
writing both `price CAD x USD` and `price USD y CAD` would only invite the two
to drift apart.

Emitting the full set also defuses a trap.  implicit_prices turns any posting
that carried an @ price into a price directive, and a single old transfer can
leave a stray price as the only link between two currencies -- valuing one at
that day's rate for months.  A monthly line always outranks a months-old
implied one, so a complete set makes those strays irrelevant.

Monthly averages, not month-end spot
------------------------------------
FX_RATES_MONTHLY is the average of the month's daily rates, not its closing
rate.  That suits a ledger which is mostly flows through bank accounts, and
it means a balance sheet drawn at a month end is valued at that month's
average.  It is a choice, and price.beancount says so where a reader will
see it.

Each block is dated the first day of its month because that is the BoC's own
observation date, kept verbatim.  Beancount resolves a price as the latest
one on or before the report date, so every day in a month is valued at that
month's average.

Fixed-value commodities
-----------------------
`[prices.fixed]` gives a commodity a constant CAD value, e.g. a loyalty-points
redemption rate (`POINTS = "0.01"`: 100 points = 1.00 CAD).  Not a market price
-- nobody trades points -- so its CAD leg is constant and only the other report
currencies move month to month.  Change the config if the basis changes; do not
edit the output.

Cross-check
-----------
Every run also pulls the ECB's daily reference rates from frankfurter.dev,
averages them per month, and compares.  This is verification only -- not one
number in the output comes from it.  The two series are not expected to match
exactly: different fixing times (16:30 ET vs 16:00 CET), different
business-day calendars, and for the non-USD pairs an average of a ratio
compared against a ratio of averages.  Divergence has run to a median of
0.03% and a worst case of 0.23%.  The script FAILS if the median exceeds
MAX_MEDIAN_DIVERGENCE or any single month exceeds MAX_DIVERGENCE, so a feed
that silently changes meaning stops the run instead of quietly rewriting the
ledger's valuations.  A commodity with no ECB symbol (TWD) rests on a single
source.

Configuration, in the ledger's `ledger_tools.toml`:

    [prices]
    start = "2022-11-01"            # the ledger's earliest posting
    report = ["USD", "CAD", "HKD"]  # the currencies reports are read in
    notes = '''...'''               # optional: appended verbatim to the output

    [prices.boc]                    # commodity -> BoC series (CAD per unit)
    USD = "FXMUSDCAD"

    [prices.ecb]                    # commodity -> ECB symbol, for the cross-check
    CAD = "CAD"

    [prices.fixed]                  # commodity -> constant CAD per unit
    POINTS = "0.01"

Dependencies: none beyond the standard library.
"""

import argparse
import io
import json
import os
import sys
import urllib.request
from collections import defaultdict
from decimal import Decimal as D, getcontext

getcontext().prec = 28

from ledger_tools import config

# The ledger's configuration, read by main() before anything else runs.
REPORT = []     # the currencies reports are read in; every commodity gets a line in each
BOC = {}        # commodity -> BoC series, CAD per one unit (CAD is the pivot: no series)
ECB_SYMBOL = {}  # commodity -> the ECB feed's symbol; USD-based, so USD has nothing to check
FIXED = {}      # commodity -> constant CAD per one unit
NOTES = ""


def configure():
    global REPORT, BOC, ECB_SYMBOL, FIXED, NOTES
    REPORT = list(config.get("prices", "report"))
    BOC = dict(config.get("prices", "boc"))
    ECB_SYMBOL = dict(config.get("prices", "ecb", default={}))
    FIXED = {k: D(v) for k, v in config.get("prices", "fixed", default={}).items()}
    NOTES = config.get("prices", "notes", default="")

# Cross-check tolerances.  See the "Cross-check" section of the module docstring.
MAX_MEDIAN_DIVERGENCE = D("0.15")   # percent
MAX_DIVERGENCE = D("1.00")          # percent, any single month

BOC_URL = "https://www.bankofcanada.ca/valet/observations/%s/json?start_date=%s"
ECB_URL = "https://api.frankfurter.dev/v1/%s..%s?base=USD&symbols=%s"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "beancount-ledger-tools/0.1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_boc(start):
    series = ",".join(BOC.values())
    obs = get(BOC_URL % (series, start))["observations"]
    if not obs:
        sys.exit("BoC returned no observations from %s" % start)
    return obs


def cad_per_unit(obs):
    """CAD per one unit of every commodity, from one BoC monthly observation."""
    out = {"CAD": D(1), **FIXED}
    for com, series in BOC.items():
        out[com] = D(obs[series]["v"])
    return out


def pairs():
    """(commodity, quote) lines to emit -- every pair once, inverses left to beancount."""
    others = [c for c in list(BOC) + ["CAD"] + list(FIXED) if c not in REPORT]
    out = [(o, q) for o in others for q in REPORT]
    # ...and the report currencies against each other, one direction each.
    for i, a in enumerate(REPORT):
        for b in REPORT[i + 1:]:
            out.append((b, a))
    return out


def ecb_monthly(start, end):
    """ECB daily rates (units per USD) averaged into USD-per-unit, by month."""
    daily = get(ECB_URL % (start, end, ",".join(ECB_SYMBOL.values())))["rates"]
    buckets = defaultdict(list)
    for day, row in daily.items():
        buckets[day[:7]].append(row)
    out = {}
    for month, rows in buckets.items():
        q = {}
        for sym in ECB_SYMBOL.values():
            vals = [D(1) / D(str(r[sym])) for r in rows if sym in r]
            if vals:
                q[sym] = sum(vals) / len(vals)
        out[month] = q
    return out


def cross_check(obs, ecb):
    """Compare the BoC's USD quotes against the ECB's, month by month."""
    div = []
    for o in obs:
        cad = cad_per_unit(o)
        e = ecb.get(o["d"][:7])
        if not e:
            continue
        for com, sym in ECB_SYMBOL.items():
            if sym not in e:
                continue
            boc_in_usd = cad[com] / cad["USD"]
            div.append((abs(boc_in_usd - e[sym]) / boc_in_usd * 100, o["d"], com))
    if not div:
        sys.exit("cross-check produced no comparisons -- is the ECB feed reachable?")
    div.sort(reverse=True)
    worst, median = div[0], div[len(div) // 2]
    print("cross-check: %d comparisons, median %.3f%%, worst %.3f%% (%s %s)"
          % (len(div), median[0], worst[0], worst[1], worst[2]))
    if median[0] > MAX_MEDIAN_DIVERGENCE:
        sys.exit("ABORT: median divergence %.3f%% exceeds %s%% -- check both feeds "
                 "before trusting either." % (median[0], MAX_MEDIAN_DIVERGENCE))
    if worst[0] > MAX_DIVERGENCE:
        sys.exit("ABORT: %s %s diverges %.3f%%, over the %s%% limit."
                 % (worst[1], worst[2], worst[0], MAX_DIVERGENCE))
    return worst, median, len(div)


def fmt(val):
    n = 6 if val > D("0.01") else 8
    return str(val.quantize(D(1).scaleb(-n)))


def render(obs, worst, median, n_div):
    L = []
    w = L.append
    last = obs[-1]["d"][:7]
    plan = pairs()

    w(";; -*- mode: org; mode: beancount; -*-")
    w(";; GENERATED by `ledger-prices` (beancount-ledger-tools) -- do not hand-edit;")
    w(";; change ledger_tools.toml's [prices] and re-run it.")
    w("")
    w("* What this file is")
    w("; Every `price` directive in the ledger that is NOT derived from a transaction.")
    w("; The `beancount.plugins.implicit_prices` plugin in main.beancount already turns")
    w("; any posting carrying a cost or an @ price into a price directive; this file is")
    w("; for the ones nothing in the ledger implies -- plain FX rates, so that fava and")
    w("; bean-query can convert a multi-currency balance into a reporting currency.")
    w(";")
    w("; Include it after the file that declares every commodity priced here.")
    w("")
    w("** Every pair, because beancount does not triangulate")
    w("; Beancount's price map holds each declared pair and its inverse, and nothing")
    w("; else.  It will NOT infer CAD/HKD from HKD/USD and CAD/USD.  Verified rather")
    w("; than assumed -- a ledger holding only")
    w(";")
    w(";     2026-08-01 price CAD 0.719528 USD")
    w(";     2026-08-01 price HKD 0.127500 USD")
    w(";")
    w("; converts 1000 HKD to 127.500000 USD and returns the same 1000 HKD untouched")
    w("; when asked for CAD.")
    w(";")
    w("; Reports here are read in %s, so every commodity is quoted in each"
      % ", ".join(REPORT))
    w("; of them and each is quoted against the others -- %d lines a month."
      % len(plan))
    w("; Inverses are deliberately NOT written: beancount supplies them, and writing")
    w("; both directions of a pair only invites the two to drift apart on a refresh.")
    w(";")
    w("; What this cannot fix: beancount takes the latest price on or before the report")
    w("; date, so an implied price (from a transaction's @) dated AFTER the newest")
    w("; monthly block still wins over the %s average.  It is observed market data"
      % last)
    w("; and more recent than the block, so it is not wrong -- but it does mean that")
    w("; in the window after such a date the columns are not all on one rate basis.")
    w("; The window closes as soon as the next month's block lands.  To put every")
    w("; column on the monthly average and nothing else, drop the implicit_prices")
    w("; plugin; the cost is losing the actual rate the bank gave you.")
    w("")
    w("** These are MONTHLY AVERAGES, not month-end spot rates")
    w("; The Bank of Canada publishes, for each month, the average of that month's daily")
    w("; rates.  That is the right rate for translating a month of income and spending,")
    w("; and it is deliberately NOT a month-end snapshot: a balance sheet drawn at a")
    w("; month end is valued here at the month's average, not at its closing rate.  For")
    w("; this ledger -- mostly flows through bank accounts -- that is the more useful of")
    w("; the two, but it is a choice, and reports should not be read as closing-rate")
    w("; valuations.")
    w(";")
    w("; Each block is dated the FIRST day of its month, which is the BoC's own")
    w("; observation date, kept verbatim rather than shifted to month end.  Beancount")
    w("; uses the latest price on or before the report date, so every day in a month is")
    w("; valued at that month's average.")
    w("")
    w("** Sources")
    w("; Primary, and the only source these numbers are built from:")
    w(";   Bank of Canada Valet API, group FX_RATES_MONTHLY.")
    w(";   The BoC quotes one unit of the foreign currency in CAD, which makes CAD the")
    w(";   pivot: every line below is (CAD per X) / (CAD per Q) from the SAME monthly")
    w(";   observation, so all %d lines in a block are mutually consistent.  The BoC"
      % len(plan))
    w(";   publishes 4 to 5 significant figures; the decimals below are the exact")
    w(";   quotient of those, not precision the source does not have.")
    w(";")
    w("; Cross-check, used for verification and nothing else:")
    w(";   ECB daily reference rates via frankfurter.dev, averaged per month.")
    w(";   %d comparisons across %d months: median divergence %.3f%%, worst %.3f%%"
      % (n_div, len(obs), median[0], worst[0]))
    w(";   (%s, %s).  The two should not match exactly -- different fixing times"
      % (worst[1], worst[2]))
    w(";   (16:30 ET vs 16:00 CET), different business-day calendars, and for the")
    w(";   non-USD pairs an average of a ratio against a ratio of averages.  The ECB")
    w(";   has no TWD, so a TWD-based commodity rests on a single source.")
    w(";")
    w("; Regenerate with:  ledger-prices")
    w("; Its docstring (ledger_tools/prices.py) documents all of the above.")
    w("")
    w("")
    w("* Monthly average rates")

    n_lines = 0
    for o in obs:
        cad = cad_per_unit(o)
        w("")
        w("** %s" % o["d"][:7])
        for com, quote in plan:
            w("%s price %-12s %s %s"
              % (o["d"], com, fmt(cad[com] / cad[quote]), quote))
            n_lines += 1

    w("")
    w("")
    w("* The series stops at %s" % last)
    w("; The BoC publishes a month's average on the last business day of that month, so")
    w("; the current month has no figure yet and the newest block above is %s.  A report"
      % last)
    w("; dated later converts at the %s average -- beancount's normal 'latest price on" % last)
    w("; or before this date' behaviour, and the honest answer for an unfinished month.")
    w(";")
    w("; If a spot valuation for today is ever wanted instead, take it from the BoC's")
    w("; DAILY group (FXUSDCAD, FXHKDCAD, ...) and add it as its own dated block -- but")
    w("; label it, because mixing a spot rate into a file of monthly averages without")
    w("; saying so is exactly the kind of silent inconsistency this file avoids.")
    for com, cad in FIXED.items():
        w("")
        w("")
        w("* %s is a fixed value, not a quote" % com)
        w("; 1 %s = %s CAD, from [prices.fixed], so its CAD line is the same" % (com, cad))
        w("; number in every block -- correct, not a bug in the generator.")
    if NOTES:
        w("")
        w("")
        L.extend(NOTES.rstrip("\n").split("\n"))
    w("")
    return "\n".join(L), n_lines


def main():
    configure()
    start = config.get("prices", "start")
    out = os.path.join(config.ledger_root(), config.get("prices", "out", default="price.beancount"))
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default=start,
                    help="first month to fetch (default %s)" % start)
    ap.add_argument("--out", default=out,
                    help="output file (default %s)" % out)
    args = ap.parse_args()

    obs = fetch_boc(args.start)
    print("BoC FX_RATES_MONTHLY: %d observations, %s .. %s"
          % (len(obs), obs[0]["d"], obs[-1]["d"]))

    ecb = ecb_monthly(args.start, obs[-1]["d"])
    worst, median, n_div = cross_check(obs, ecb)

    text, n_lines = render(obs, worst, median, n_div)
    io.open(args.out, "w", encoding="utf-8", newline="\n").write(text)
    print("wrote %s: %d price directives across %d monthly blocks (%d pairs each)"
          % (args.out, n_lines, len(obs), len(pairs())))
    print("Now run: bean-check on the ledger")


if __name__ == "__main__":
    main()
