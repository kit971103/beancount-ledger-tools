"""Console entry points.

`ledger-extract <name> [args...]` runs `ledger_tools.extract.<name>.main(args)`;
run it from inside the ledger repository (see `ledger_tools.config`).
"""

import importlib
import pkgutil
import sys

from ledger_tools import extract as _extract


def _extractors():
    return sorted(m.name for m in pkgutil.iter_modules(_extract.__path__)
                  if m.name != "common")


def extract(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    names = _extractors()
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in names:
        print("usage: ledger-extract {%s} [args...]" % ",".join(names), file=sys.stderr)
        raise SystemExit(0 if argv and argv[0] in ("-h", "--help") else 2)
    mod = importlib.import_module("ledger_tools.extract." + argv[0])
    sys.argv = ["ledger-extract " + argv[0]] + argv[1:]
    raise SystemExit(mod.main(argv[1:]))
