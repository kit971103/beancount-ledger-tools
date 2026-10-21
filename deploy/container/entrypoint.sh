#!/bin/sh
# Start fava on a ledger.  /ledger is used as-is when it already holds
# main.beancount (a local `docker run -v ...`); otherwise the ledger repository
# is cloned from GitHub: LEDGER_REPO=<owner>/<repo>, GITHUB_TOKEN a read-only token.
set -eu

if [ ! -f /ledger/main.beancount ]; then
	: "${LEDGER_REPO:?set LEDGER_REPO=<owner>/<repo>}"
	: "${GITHUB_TOKEN:?set GITHUB_TOKEN to a read-only token for $LEDGER_REPO}"
	git clone --depth 1 --branch "${LEDGER_BRANCH:-master}" \
		"https://x-access-token:${GITHUB_TOKEN}@github.com/${LEDGER_REPO}.git" /ledger
	git -C /ledger remote set-url origin "https://github.com/${LEDGER_REPO}.git"  # drop the token
fi

# Report errors but serve anyway: fava shows them on its Errors page.
bean-check /ledger/main.beancount || echo "bean-check reported errors; fava lists them"

exec fava --host 0.0.0.0 --port 5000 --read-only /ledger/main.beancount
