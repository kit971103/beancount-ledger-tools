#!/bin/sh
# Start fava on a ledger.  /ledger is used as-is when it already holds
# main.beancount (a local `docker run -v ...`); otherwise the ledger repository
# is cloned from GitHub: LEDGER_REPO=<owner>/<repo>, GITHUB_TOKEN a read-only token,
# and re-fetched every LEDGER_PULL_INTERVAL seconds (default 300); fava reloads
# when the files change.
set -eu

if [ ! -f /ledger/main.beancount ]; then
	: "${LEDGER_REPO:?set LEDGER_REPO=<owner>/<repo>}"
	: "${GITHUB_TOKEN:?set GITHUB_TOKEN to a read-only token for $LEDGER_REPO}"
	BRANCH="${LEDGER_BRANCH:-master}"
	git clone --depth 1 --branch "$BRANCH" \
		"https://x-access-token:${GITHUB_TOKEN}@github.com/${LEDGER_REPO}.git" /ledger
	git -C /ledger remote set-url origin "https://github.com/${LEDGER_REPO}.git"  # drop the token

	# The checkout is a read-only mirror: move it to the remote tip, whatever that is.
	pull() {
		git -C /ledger fetch -q --depth 1 \
			"https://x-access-token:${GITHUB_TOKEN}@github.com/${LEDGER_REPO}.git" "$BRANCH" || return 1
		if [ "$(git -C /ledger rev-parse HEAD)" != "$(git -C /ledger rev-parse FETCH_HEAD)" ]; then
			git -C /ledger reset -q --hard FETCH_HEAD
			echo "ledger updated to $(git -C /ledger rev-parse --short HEAD)"
		fi
	}
	while sleep "${LEDGER_PULL_INTERVAL:-300}"; do
		pull || echo "ledger pull failed; retrying in ${LEDGER_PULL_INTERVAL:-300}s"
	done &
fi

# Report errors but serve anyway: fava shows them on its Errors page.
bean-check /ledger/main.beancount || echo "bean-check reported errors; fava lists them"

exec fava --host 0.0.0.0 --port 5000 --read-only /ledger/main.beancount
