# fava on Cloudflare Containers

A Worker that forwards every request to one container running `fava --read-only`
on your ledger. The container clones the ledger repository from GitHub when it
starts, re-fetches it every 5 minutes (`LEDGER_PULL_INTERVAL`), and sleeps after
30 minutes idle. Documents (`documents/`, usually git-ignored) are not included.

**fava has no login. Do not deploy without Cloudflare Access in front of it.**

## Local test

```sh
docker build -f deploy/container/Dockerfile -t ledger-fava:local .   # from the repo root
docker run --rm -p 5000:5000 -v /path/to/ledger:/ledger:ro ledger-fava:local
# or let it clone:
docker run --rm -p 5000:5000 -e LEDGER_REPO=<owner>/<repo> -e GITHUB_TOKEN=... ledger-fava:local
```

## Deploy

Needs a Workers Paid plan and Docker running locally (wrangler builds the image).

1. Create a fine-grained GitHub token: only the ledger repository, *Contents:
   read-only*.
2. In `wrangler.jsonc`, set `vars.LEDGER_REPO` (and `LEDGER_BRANCH`).
3. `npm install`, `npx wrangler login`, `npx wrangler secret put GITHUB_TOKEN`.
4. In Cloudflare Zero Trust, add a self-hosted **Access application** for the
   Worker's hostname (workers.dev or a custom domain) with a policy allowing
   only your email. Check that an unauthenticated request is refused.
5. `npx wrangler deploy`. The first deploy can take several minutes.
