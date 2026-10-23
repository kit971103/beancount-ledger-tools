#!/bin/sh
# Move the /ledger checkout to the tip of LEDGER_BRANCH on GitHub, whatever that
# is: it is a read-only mirror.  Run every LEDGER_PULL_INTERVAL seconds by
# entrypoint.sh and on demand by fava's Status page; flock keeps the two apart.
set -eu
: "${LEDGER_REPO:?set LEDGER_REPO=<owner>/<repo>}"
: "${GITHUB_TOKEN:?set GITHUB_TOKEN to a read-only token for $LEDGER_REPO}"
BRANCH="${LEDGER_BRANCH:-master}"

exec 9>/tmp/ledger-pull.lock
flock 9
git -C /ledger fetch -q --depth 1 \
	"https://x-access-token:${GITHUB_TOKEN}@github.com/${LEDGER_REPO}.git" "$BRANCH"
if [ "$(git -C /ledger rev-parse HEAD)" != "$(git -C /ledger rev-parse FETCH_HEAD)" ]; then
	git -C /ledger reset -q --hard FETCH_HEAD
	echo "ledger updated to $(git -C /ledger rev-parse --short HEAD)"
fi
