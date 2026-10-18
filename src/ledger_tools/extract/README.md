# `ledger_tools.extract` — turning a bank's own export into checked rows

These are the extractors a reconciliation is built on. None of them writes to
the ledger: an extractor reads a document, proves the result against figures
the document itself states, and hands back rows. Which figures each one
reproduced on a given ledger is recorded in that ledger, not here.

## Running one

From inside the ledger repository (the directory holding `ledger_tools.toml`,
or anywhere below it; `LEDGER_ROOT` overrides the search):

```sh
ledger-extract rbc_banking               # all RBC chequing + savings statements
ledger-extract bank_csv --source amex    # one CSV export
ledger-extract bank_csv                  # every CSV export
ledger-extract scotia_visa --since 2026-01-01 --json scotia.json
```

Each takes `--docs DIR`, `--since` / `--until` (ISO dates, filtered on the
document's date prefix), `--json FILE` and `--refresh`. The exit status is
non-zero when any document failed a check, so these are usable in a script.
They are importable modules too: `from ledger_tools.extract import bank_csv`.

### Where the documents are

The documents directory is `<ledger>/documents` unless `--docs` or
`LEDGER_DOCS` says otherwise. Inside it, each source's files sit in a folder
named in the ledger's config, because the folder names are the ledger's own
account names:

```toml
[extract.docs]
amex = "Liabilities/Card/Amex"
rbc-daily = "Assets/Bank/RBC/Chequing"

[extract.labels]          # optional: how reports name a source
rbc-daily = "chequing 1234"
```

Files are named `YYYY-MM-DD.<description>.<ext>`, dated the last day they
cover. A documents directory that is git-ignored does **not exist inside a git
worktree**, so a run from a worktree needs `--docs` (or `LEDGER_DOCS`). Do not
solve that by pointing a junction at it: `git worktree remove` follows the
junction and deletes the real files.

Extracted text is cached under `~/.cache/beancount-extract` and re-extracted
when the PDF is newer, so a re-run costs nothing.

## Which extraction mode, and why it is not a free choice

| source key | module | mode | why not the others |
|---|---|---|---|
| `rbc-daily`, `rbc-savings` | `rbc_banking` | `pdftotext -table` | `-layout` splits the table into date / description / amount streams that no longer line up |
| `rbc-mastercard` | `rbc_mastercard` | `pdftotext -table` | two-column page; the left column is cut at the `AMOUNT ($)` header |
| `bmo-main` | `bmo` | `pdftotext -table` | `-layout` works until a continuation page re-lays the columns |
| `bmo-loc` | `bmo` | `pdftotext -table` | two-column page, cut at the `Amount ($)` header |
| `scotia` | `scotia_visa` | **pdfplumber** | `-layout` offsets the amount column from its rows *silently*; Xpdf 4.00's pdftotext has neither `-table` nor `-bbox-layout` |
| `simplii` | `simplii_visa` | `pdftotext -table` | `-layout` splits the busier tables |
| `rogers` | `rogers_statement` | `pdftotext -raw` | `-layout` puts the summary figures one line off their labels and splits dates from descriptions |
| `cra` | `cra_noa` | `pdftotext -raw` | `-layout` interleaves the wrapped summary labels with the amount column, so amounts land beside the wrong line codes |
| `eqbank-main`, `eqbank-card`, `amex`, `rogers`, `scotia`, `simplii` | `bank_csv` | `csv` | — |
| `ibkr` | `ibkr_flex` | `csv` | — the header is checked instead: a file matching neither Flex query is refused |

`pdftotext` is an external binary, not a Python package: it is found at
`%ProgramFiles%\Git\mingw64\bin\pdftotext.exe` (Xpdf 4.00) or on `PATH`, and
`PDFTOTEXT` overrides both. `pdfplumber` is a dependency of this package.

## Sign conventions

Rows carry the amount as the **source** states it, and each source says what
positive means:

* bank accounts (EQ Bank, RBC chequing/savings, BMO chequing): `+` money in;
* cards (AMEX, Rogers, Scotia, Simplii, RBC Mastercard, BMO line of credit):
  `+` a charge, `-` a payment or refund.

`ledger_tools.extract.ledger` flips the card sources when it compares them with
the ledger, where a card charge is a negative posting to a liability.

## Traps these parsers already know about

* **RBC chequing**: do not map tokens onto the column headers — a header sits
  5–10 characters right of the numbers under it, by a different amount per
  column, so "nearest header" reads deposits as withdrawals. Cluster the token
  *end* positions instead. A negative balance prints as `- 4.00`.
* **RBC Mastercard**: `POST DATE`, not `TRANS DATE`, is the ledger date. One row
  legitimately carries no reference number — the `CASH BACK REWARD` redemption.
* **BMO chequing**: parse by the running balance, not by column position. An
  overdraft is what a money regex without a leading `-` gets silently wrong.
* **BMO line of credit**: the extraction sometimes loses the row number, so a
  row is recognised by its two dates. The amount is the *last* money token on
  the line — `INTEREST ADVANCES @ 10.00000%` carries its own.
* **Scotia**: footnote daggers appear as raw `\x87` bytes inside words, and
  `Previous balance,Feb 3/25 $0.00` has no space after the comma — a lazy
  pattern captures `3` as the balance. Summary figures are matched against a
  whitespace-stripped copy of the page for that reason. The cash back block
  changed format in early 2026.
* **Simplii**: the payments table is absent on a statement with no payment, so
  the section is taken from the table header. A credit is marked with a trailing
  `-` (refunds) *or* a leading one (`CASH BACK CREDIT`). The cycle crossing New
  Year prints a year on both period dates.
* **Rogers**: the reference number is not unique — a cash advance and its fee
  share one — so the row key is reference **plus** amount. Bank-generated rows
  may carry no reference at all.
* **Scotia CSV**: `Description` is truncated at 25 characters and the rest
  overflows into `Sub-description`. Both are source text; they are joined with
  two spaces and nothing else.

## Parse → Merge → Validate (`ibkr_flex`)

`ibkr_flex` is the one extractor that turns an export into **transactions**, not
just rows, because an IBKR row is not a bank-account line: a day's `Net cash activity`
may be a deposit, one leg of an internal transfer or unexplained interest, and an FX
order needs three postings. It still writes nothing; the three steps are sub-commands:

1. **Parse** — `parse` reads every `*.flex-*.csv` in the `ibkr` documents folder,
   checks each header against the two known Flex queries (fund activity, trades),
   keeps the rows that matter (non-`CASH:` cash rows; `AssetClass == CASH` trades)
   and prints beancount text: amounts at cents, FX with `@@`, commission to
   `Expenses:Investment:Commission`, sub-cent sweeps dropped.
2. **Merge** — `merge` reads the IBKR sub-ledger back with `ledger.parse` and prints
   three lists: export transactions already booked (on `bank-ref`, else date +
   account + amount), new ones to add (as beancount text), and IBKR postings in the
   window no export row explains.
3. **Validate** — `validate` must exit 0: (a) export vs ledger postings as multisets
   in both directions, (b) CAD/HKD back to ~0 at every USD assertion in
   `balance.beancount` (dates covered by `temp.beancount` excepted), (c) the fund
   query's `CASH:` rows equal the trade query per account/day/currency.

The ledger describes its IBKR accounts in `[extract.ibkr]`:

```toml
[extract.ibkr]
ledger = "sub-ledgers/ibkr"          # the sub-ledger folder, from the ledger root
fund_query = "1000001"               # Flex query ids, as in the export file names
trade_query = "1000002"
main = "Assets:Broker:IBKR:Taxable"  # the account internal transfers leave from
plug_before = "2024-01-01"           # optional: earlier transfers go to `plug`
plug = "Assets:Clearing:IBKR-Plug"

[extract.ibkr.accounts]              # ClientAccountID = [IBKR alias, ledger account]
U0000001 = ["Main Account", "Assets:Broker:IBKR:Taxable"]
U0000002 = ["TFSA", "Assets:Broker:IBKR:TFSA"]
```

## Comparing an extract with the ledger

`ledger_tools.extract.ledger` reads a sub-ledger back as rows and compares the two
as multisets **in both directions** — the claim a reconciliation makes, which is
not the same as a spot check:

```sh
ledger-extract ledger --file sub-ledgers/rbc \
    --account Assets:Bank:RBC:Chequing --statements rbc-daily --since 2025-01-01
ledger-extract ledger --file sub-ledgers/amex \
    --account Liabilities:Card:Amex --source amex
```

`--file` takes a sub-ledger folder (every `*.beancount` in it is read) or a
single file. It parses textually and without plugins, so `&` scheduled entries —
which `ledger_tools.plugin.exclude_flagged` hides from every report — are
counted here.

## Suggesting a category for new export rows

`ledger_tools.extract.categorise` lists the export rows the sub-ledger lacks (on
date + amount) and, for each, the counter-account its earlier rows used — matched
on `bank-desc`, else the narration, per person on the `[extract] receivable`
account — with a verdict: `*` when ≥3 prior rows agree ≥60%, else `!`. It writes
nothing. `--csv` reads one file instead of the source's glob (a partial export):

```sh
ledger-extract categorise --source rogers \
    --csv <documents>/<rogers folder>/2025-01-31.transactions-partial.csv \
    --file sub-ledgers/rogers --account Liabilities:Card:Rogers --since 2025-01-01
```

An empty listing is also the check that a partial export is fully booked.
