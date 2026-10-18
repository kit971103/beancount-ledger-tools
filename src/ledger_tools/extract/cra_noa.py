"""Parse CRA notices of assessment and reassessment.

Extraction is `pdftotext -raw`. The summary is a two-column table (label, amount)
whose labels wrap onto several lines, and `-layout` interleaves the wrapped
labels with the amount column so that amounts land beside the wrong line codes
(it can print the RRSP deduction under "Equals: unused room"). `-raw` keeps
each label's text and its amount in reading order; a wrapped label simply
arrives as label lines followed by an amount-only line.

Every `*notice-of-*.pdf` anywhere under the CRA documents folder
(`[extract.docs] cra`) is read, so the notices can live beside whichever account
each is booked to (e.g. one sub-folder per tax year).

Checks on every notice:

 1. line 42000 + line 42800 = line 43500 (federal + provincial tax = total payable);
 2. line 48200 - line 43500 = "Total payable minus Total credits" (CR = owed to
    you, DR = owed by you); on a reassessment, the "Final amount" column;
 3. page 1's sentence ("You have a refund of $X" / "You need to pay $X" /
    "You have no amount to pay") agrees with "Balance from this (re)assessment";
 4. RRSP: next limit = limit - PRPP - deducted + 18% earned - PA - prescribed
    - PSPA + PAR, and available room = next limit - unused contributions;
 5. chain: each notice's opening RRSP limit equals the previous notice's
    closing one (reported as a chain break).

A reassessment's RRSP statement is the *current* position, not the tax year's
(a reassessment of an earlier year issued alongside this year's assessment shows
this year's statement), so the chain is ordered by the statement's own year, and
when two notices print the same statement year, the one issued later wins.

Usage:
    ledger-extract cra_noa [--docs DIR] [--json out.json]
"""

import argparse
import glob
import os
import re
import sys
from datetime import date
from decimal import Decimal

from ledger_tools.extract import common

D = common.D
SOURCE = "cra"

AMT = r'\d{1,3}(?:,\d{3})*(?:\.\d{2})?(?: (?:CR|DR))?'
TRAILING = re.compile(r'^(?P<label>.*?)\s*(?P<amts>(?:%s\s*)+)$' % AMT)
ONE = re.compile(AMT)
ISSUED = re.compile(r'Date issued (\w{3}) (\d{1,2}), (\d{4})')
TAX_YEAR = re.compile(r'Tax year (\d{4})')


def signed(tok):
    """'437.35 CR' -> +437.35 (owed to you); '2,621.56 DR' -> -2621.56."""
    tok = tok.strip()
    if tok.endswith(" DR"):
        return -D(tok[:-3])
    if tok.endswith(" CR"):
        return D(tok[:-3])
    return D(tok)


def summary_rows(text):
    """The Summary table as [(label, [signed amounts])], labels unwrapped."""
    start = text.index("\nSummary\n")
    end = text.index("Explanation of changes", start)
    rows, pending = [], []
    for line in text[start:end].splitlines()[2:]:
        line = line.strip()
        if not line or line.startswith(("Line Description", "assessment",
                                        "CR/DR")):
            continue
        if line in ("Final balance Nil",):
            rows.append(("Final balance", [Decimal(0)]))
            continue
        m = TRAILING.match(line)
        if m:
            label = " ".join(pending + [m.group("label")]).strip()
            rows.append((label, [signed(t) for t in ONE.findall(m.group("amts"))]))
            pending = []
        else:
            pending.append(line)
    return rows


def pick(rows, key):
    """Final-column amount of the first row whose label starts with `key`."""
    for label, amts in rows:
        if label.startswith(key):
            return amts[-1]
    raise KeyError(key)


def last_int(text, pattern):
    """The amount at the end of the first line matching `pattern`."""
    m = re.search(r'^.*%s.*?(\(?[\d,]+\)?)\s*$' % pattern, text, re.M)
    if not m:
        raise KeyError(pattern)
    v = m.group(1)
    return -D(v.strip("()")) if v.startswith("(") else D(v)


def parse(path):
    text = common.pdftotext(path, "-raw")
    errs = []
    m = ISSUED.search(text)
    issued = date(int(m.group(3)), common.MON3[m.group(1)], int(m.group(2)))
    ty = int(TAX_YEAR.search(text).group(1))
    kind = "reassessment" if "Notice of reassessment" in text else "assessment"
    st = dict(path=path, kind=kind, tax_year=ty, issued=issued.isoformat(),
              errs=errs, txns=[])

    rows = summary_rows(text)
    fed, bc = pick(rows, "42000"), pick(rows, "42800")
    payable, credits = pick(rows, "43500"), pick(rows, "48200")
    deducted = pick(rows, "43700")
    net = next(a[-1] for l, a in rows
               if re.match(r'(Subtotal \()?Total payable minus', l))
    this = next(a[-1] for l, a in rows if l.startswith("Balance from this"))
    st.update(net_federal_tax=fed, net_bc_tax=bc, total_payable=payable,
              tax_deducted=deducted, total_credits=credits, balance=net,
              balance_this=this)
    if fed + bc != payable:
        errs.append("42000 %s + 42800 %s != 43500 %s" % (fed, bc, payable))
    if credits - payable != net:
        errs.append("48200 %s - 43500 %s != balance %s" % (credits, payable, net))

    p1 = re.search(r'You have a refund of \$([\d,]+\.\d{2})', text)
    p2 = re.search(r'You need to pay \$([\d,]+\.\d{2})', text)
    if p1:
        said = D(p1.group(1))
    elif p2:
        said = -D(p2.group(1))
    elif "You have no amount to pay" in text:
        said = Decimal(0)
    else:
        said = None
        errs.append("page 1 states no balance")
    if said is not None and said != this:
        errs.append("page 1 says %s, summary says %s" % (said, this))

    # RRSP statement: two layouts (to TY2022, and from TY2023 with "Equals:"
    # sub-totals); the labels below are common to both.  The old layout wraps
    # "Unused RRSP contributions ... deduct for 2023" over two lines with the
    # amount on a third, so the section is unwrapped before it is searched.
    sec = text[text.index("RRSP deduction limit"):]
    sec = sec[:sec.index("Note: If your available")]
    lines, pending = [], []
    for line in sec.splitlines():
        pending.append(line.strip())
        # "... deduct for 2022" ends in digits too; a year is not an amount
        if re.search(r'\(?[\d,]+\)?$', line.strip()) \
                and not re.search(r'for \d{4}$', line.strip()):
            lines.append(" ".join(pending))
            pending = []
    text = "\n".join(lines) + "\n" + text[text.index("Note: If your available"):]
    yr = int(re.search(r'RRSP deduction limit for (\d{4})', text).group(1))
    r = dict(
        year=yr,
        limit=last_int(text, r'RRSP deduction limit for %d\b' % yr),
        prpp=last_int(text, r"Employer's PRPP contributions for %d" % yr),
        deducted=last_int(text, r'Allowable RRSP contributions deducted for %d' % yr),
        earned=last_int(text, r'18% of \d{4} earned income'),
        pa=last_int(text, r'Minus: %d pension adjustment' % yr),
        prescribed=(last_int(text, r'prescribed amount for connected persons')
                    if 'prescribed amount' in text else Decimal(0)),
        pspa=last_int(text, r'net past service pension adjustment'),
        par=last_int(text, r'Plus: %d pension adjustment reversal' % (yr + 1)),
        next_limit=last_int(text, r'RRSP deduction limit for %d\b' % (yr + 1)),
        unused=last_int(text, r'Unused RRSP contributions previously reported'),
        available=last_int(text, r'[Aa]vailable (?:RRSP )?contribution room for %d'
                           % (yr + 1)),
    )
    st["rrsp"] = r
    want = (r["limit"] - r["prpp"] - r["deducted"] + r["earned"] - r["pa"]
            - r["prescribed"] - r["pspa"] + r["par"])
    if want != r["next_limit"]:
        errs.append("RRSP: computed next limit %s != stated %s"
                    % (want, r["next_limit"]))
    if r["next_limit"] - r["unused"] != r["available"]:
        errs.append("RRSP: %s - unused %s != available %s"
                    % (r["next_limit"], r["unused"], r["available"]))

    st["fhsa_room"] = {int(y): D(v) for y, v in re.findall(
        r'Your FHSA participation room for (\d{4}) ([\d,]+)', text)}
    return st


def main(argv=None):
    ap = common.add_common_args(argparse.ArgumentParser(description=__doc__.split("\n")[0]))
    a = ap.parse_args(argv)
    docs = common.docs_dir(a.docs)
    files = sorted(glob.glob(os.path.join(docs, common.doc_folder(SOURCE).replace("/", os.sep), "**",
                                          "*notice-of-*.pdf"), recursive=True),
                   key=os.path.basename)
    files = [f for f in files
             if (not a.since or os.path.basename(f)[:10] >= a.since)
             and (not a.until or os.path.basename(f)[:10] <= a.until)]
    parsed = [parse(f) for f in files]

    # Chain on the RRSP statement year; a later notice for the same year wins.
    by_year = {}
    for st in sorted(parsed, key=lambda s: s["issued"]):
        by_year[st["rrsp"]["year"]] = st
    chain = [by_year[y] for y in sorted(by_year)]
    problems = common.report("CRA notices (RRSP chain)", chain,
                             ("RRSP limit", lambda s: s["rrsp"]["next_limit"],
                              lambda s: s["rrsp"]["limit"]))
    superseded = [s for s in parsed if s not in chain]
    if superseded:
        problems += common.report("CRA notices (superseded RRSP statement)",
                                  superseded)

    print("\n%-10s %-12s %4s %12s %12s %12s  %s" % (
        "issued", "kind", "TY", "deducted", "balance", "RRSP room", "FHSA room"))
    for st in parsed:
        r = st["rrsp"]
        print("%-10s %-12s %4d %12s %12s %7s %d  %s" % (
            st["issued"], st["kind"], st["tax_year"], st["tax_deducted"],
            st["balance_this"], r["available"], r["year"] + 1,
            ", ".join("%d: %s" % kv for kv in st["fhsa_room"].items())))
    common.write_json(a.json, parsed)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
