# fava on Cloud Run

One Cloud Run service running `fava --read-only` on your ledger, behind
Identity-Aware Proxy (IAP), sized for Google Cloud's free tier. The image is
`deploy/container/` (shared with `deploy/cloudflare/`): it clones the ledger
repository from GitHub when an instance starts, so a cold start serves the latest
push. Cloud Run scales to zero when idle. Documents (`documents/`, usually
git-ignored) are not included.

**fava has no login. The service is deployed with `--no-allow-unauthenticated
--iap`; do not remove either.**

## Cost

Within the free tier for one person's use, as of 2026-10
([Free Tier](https://docs.cloud.google.com/free/docs/free-cloud-features)):
Cloud Run gives 2 million requests, 360,000 GB-seconds of memory and 180,000
vCPU-seconds a month; Artifact Registry 0.5 GB of storage. The image is about
135 MB compressed and the cleanup policy keeps the two newest. Google may change
these limits with 30 days' notice, and a billing account is still required: set
a budget alert.

## Deploy

Needs `gcloud` (logged in: `gcloud auth login`) and Docker running locally.

1. Create a project and link a billing account; add a budget alert.
2. Create a fine-grained GitHub token: only the ledger repository, *Contents:
   read-only*.
3. From the repository root, at the tag you want to serve:

   ```sh
   PROJECT_ID=my-project LEDGER_REPO=<owner>/<repo> IAP_USER=me@example.com \
     GITHUB_TOKEN=github_pat_... deploy/gcp/deploy.sh
   ```

   `GITHUB_TOKEN` is read on the first run only; it is stored in Secret Manager
   as `ledger-github-token`. `REGION` defaults to `us-west1`, `LEDGER_BRANCH`
   to `master`. The script prints the service URL.
4. Open the URL in a private window: Google sign-in first, then fava. Another
   Google account must be refused.

Re-run `deploy.sh` after changing this repository. A new push to the ledger
needs nothing: the next cold start clones it.

## What it creates

| resource | name | why |
|---|---|---|
| Artifact Registry repository | `ledger` | holds the image; cleanup keeps two versions |
| service account | `ledger-fava` | runs the service; may read the token secret only |
| secret | `ledger-github-token` | read-only GitHub token for the clone |
| Cloud Run service | `ledger-fava` | fava, port 5000, 512 MiB, one instance at most |
| IAM bindings | IAP service agent: `run.invoker`; `IAP_USER`: `iap.httpsResourceAccessor` | IAP may call the service; only `IAP_USER` passes IAP |

Local test of the image: see [deploy/cloudflare/README.md](../cloudflare/README.md#local-test)
(same image).
