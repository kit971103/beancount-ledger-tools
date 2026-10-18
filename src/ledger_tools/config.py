"""Where the ledger is, and what it says about itself.

This package is public; the ledger it serves is not.  Everything that names the
owner's accounts, documents or account numbers lives in the ledger repo's
`ledger_tools.toml`, never in the code:

    ledger/
      main.beancount
      ledger_tools.toml   <- read here
      documents/          <- default documents directory

The ledger root is `$LEDGER_ROOT` if set, else the nearest directory at or above
the current one that holds `ledger_tools.toml`.  Plugins and fava extensions do
not use this: they take their configuration from `main.beancount`.
"""

import os
import tomllib
from functools import cache

CONFIG_NAME = "ledger_tools.toml"


@cache
def ledger_root():
    """-> absolute path of the ledger repository."""
    env = os.environ.get("LEDGER_ROOT")
    if env:
        if not os.path.exists(os.path.join(env, CONFIG_NAME)):
            raise SystemExit("LEDGER_ROOT=%s holds no %s" % (env, CONFIG_NAME))
        return os.path.abspath(env)
    d = os.getcwd()
    while True:
        if os.path.exists(os.path.join(d, CONFIG_NAME)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            raise SystemExit(
                "no %s in %s or any parent directory.\n"
                "Run from inside the ledger repository, or set LEDGER_ROOT."
                % (CONFIG_NAME, os.getcwd()))
        d = parent


@cache
def load():
    """-> the parsed `ledger_tools.toml`."""
    with open(os.path.join(ledger_root(), CONFIG_NAME), "rb") as fh:
        return tomllib.load(fh)


def get(*keys, default=KeyError):
    """`get("extract", "docs", "amex")` -> that value, or exit naming the missing key."""
    node = load()
    for i, k in enumerate(keys):
        if not isinstance(node, dict) or k not in node:
            if default is not KeyError:
                return default
            raise SystemExit("%s: missing [%s] %s" % (
                CONFIG_NAME, ".".join(keys[:i]) or "top level", k))
        node = node[k]
    return node


def path(*keys):
    """A config value that is a path relative to the ledger root -> absolute path."""
    return os.path.join(ledger_root(), get(*keys).replace("/", os.sep))
