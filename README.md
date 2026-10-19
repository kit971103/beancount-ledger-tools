# beancount-ledger-tools

Plugins, fava extensions, statement extractors and a download helper for a
personal [Beancount](https://beancount.github.io/) ledger. The ledger itself
lives in its own (private) repository; everything this code needs to know about
it is in that repository's `ledger_tools.toml` or in `main.beancount`.

| package | what | configured by |
|---|---|---|
| `ledger_tools.plugin` | beancount plugins: per-person receivables, split-tag legs, contribution room, deferred tax, sole-currency checks, dropping flagged entries or implied prices | `plugin "ledger_tools.plugin.<name>" "{...}"` in `main.beancount`; each module's docstring |
| `ledger_tools.fava_ext` | fava extensions: a grouped trial balance and a direct-method cash flow statement | `custom "fava-extension" "ledger_tools.fava_ext.<name>"` |
| `ledger_tools.extract` | read-only extractors for Canadian bank/card statements, IBKR Flex exports and CRA notices; each proves its parse against the document's own figures — see [its README](src/ledger_tools/extract/README.md) | `[extract]` in `ledger_tools.toml` |
| `ledger_tools.fetch` | `ledger-fetch`: you log in, Playwright replays recorded clicks to download exports | `[fetch.login]` |
| `ledger_tools.prices` | `ledger-prices`: monthly Bank of Canada FX averages as `price` directives | `[prices]` |
| `deploy/cloudflare` | fava in a Cloudflare Container, behind Cloudflare Access | [its README](deploy/cloudflare/README.md) |

## Use from a ledger

The ledger repository is a uv project depending on this package:

```toml
# ledger/pyproject.toml
[project]
name = "my-ledger"
version = "0"
requires-python = ">=3.13"
dependencies = ["beancount-ledger-tools[fetch]"]

[tool.uv]
package = false

[tool.uv.sources]
beancount-ledger-tools = { path = "../beancount-ledger-tools", editable = true }
```

Then, in the ledger: `uv sync`, `uv run bean-check main.beancount`,
`uv run fava main.beancount`, `uv run ledger-extract <name>`. The command-line
tools find the ledger by looking for `ledger_tools.toml` in the current
directory or above (or `$LEDGER_ROOT`). `pdftotext` (Xpdf or poppler) must be
installed separately for the PDF extractors.

## Keeping this repository free of personal data

This repository is public and the ledger is not. Enable the pre-commit check
once per clone:

```sh
git config core.hooksPath .githooks
```

`scripts/check_public.py` refuses a commit containing IBKR account ids,
country-numbered account names or long reference numbers, and anything on a
private denylist that lives in the ledger repository
(`$LEDGER_TOOLS_DENYLIST`, default `../beancount-ledgers/.public-denylist`).
Examples in docstrings use invented accounts, names and amounts.

## License

MIT
