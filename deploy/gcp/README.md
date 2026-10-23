# fava on Cloud Run

One Cloud Run service running `fava --read-only` on your ledger, behind
Identity-Aware Proxy (IAP), sized for Google Cloud's free tier. The image is
`deploy/container/` (shared with `deploy/cloudflare/`): it clones the ledger
repository from GitHub when an instance starts and re-fetches it every 5 minutes
(`LEDGER_PULL_INTERVAL`); fava reloads on change. Cloud Run scales to zero when
idle, and between requests it throttles the CPU, so the re-fetch runs while the
service is in use; a cold start always serves the latest push. Documents (`documents/`, usually
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
3. An OAuth client for IAP. A project outside a Google Cloud organization (any
   personal account) cannot use IAP's Google-managed client
   ([custom OAuth configuration](https://docs.cloud.google.com/iap/docs/custom-oauth-configuration)),
   and the console is the only way to make one:
   - Google Auth Platform → Branding → *Get started*: an app name, your email,
     audience *External*. Under Audience leave it in *Testing* and add your
     account as a test user.
   - Clients → *Create client* → *Web application*. Copy the client ID and
     secret, then add the authorized redirect URI
     `https://iap.googleapis.com/v1/oauth/clientIds/CLIENT_ID:handleRedirect`.
4. From the repository root, at the tag you want to serve:

   ```sh
   PROJECT_ID=my-project LEDGER_REPO=<owner>/<repo> IAP_USER=me@example.com \
     GITHUB_TOKEN=github_pat_... \
     OAUTH_CLIENT_ID=....apps.googleusercontent.com OAUTH_CLIENT_SECRET=GOCSPX-... \
     deploy/gcp/deploy.sh
   ```

   `GITHUB_TOKEN` and the OAuth client are read on the first run only: the
   token is stored in Secret Manager as `ledger-github-token`, the client in the
   service's IAP settings. `REGION` defaults to `us-west1`, `LEDGER_BRANCH` to
   `master`. The script prints the service URL.
5. Open the URL in a private window: Google sign-in first, then fava. Another
   Google account must be refused.

Re-run `deploy.sh` after changing this repository, or let GitHub Actions do it
(below). A wrapper holding your variables can live beside it as `*.local.ps1`,
which git ignores. A new push to the ledger
needs nothing: it shows within 5 minutes of use, or at the next cold start. To
serve it at once, use *Pull ledger now* on the Status page (the
`ledger_tools.fava_ext.status` extension).

## Deploy from GitHub Actions

`.github/workflows/deploy-gcp.yml` builds the image on every push to `main` that
touches the image (`src/`, `pyproject.toml`, `deploy/container/`) and rolls it
out; *Run workflow* does the same by hand. It changes only the image: the
service's account, environment, secret and IAP settings stay as `deploy.sh` left
them, so run `deploy.sh` once first and again whenever those change.

The workflow holds no key. Once, after `deploy.sh`:

```sh
PROJECT_ID=my-project CI_REPO=<owner>/<this-repo> deploy/gcp/ci-setup.sh
```

It needs `gh` (logged in) to look up the repository's numeric id, or
`CI_REPO_ID`. It makes a Workload Identity pool `github` whose provider accepts
only this repository's pushes to `main` (forks, pull requests and other branches
get nothing), and a service account `ledger-fava-deployer` the workflow becomes.
Put the variables it prints under the repository's *Settings → Secrets and
variables → Actions → Variables* (variables, not secrets: none is a credential,
but they belong out of a public workflow file). Until `GCP_PROJECT_ID` is set the
workflow is skipped.

## What it creates

| resource | name | why |
|---|---|---|
| Artifact Registry repository | `ledger` | holds the image; cleanup keeps two versions |
| service account | `ledger-fava` | runs the service; may read the token secret only |
| secret | `ledger-github-token` | read-only GitHub token for the clone |
| Cloud Run service | `ledger-fava` | fava, port 5000, 512 MiB, one instance at most |
| IAP settings | the service's OAuth client | Google sign-in for a project without an organization |
| IAM bindings | IAP service agent: `run.invoker`; `IAP_USER`: `iap.httpsResourceAccessor` | IAP may call the service; only `IAP_USER` passes IAP |
| Workload Identity pool and provider (`ci-setup.sh`) | `github` | GitHub's OIDC tokens, from this repository's `main` only |
| service account (`ci-setup.sh`) | `ledger-fava-deployer` | the workflow: `artifactregistry.writer` on `ledger`, `run.developer`, `iam.serviceAccountUser` on `ledger-fava` |

Local test of the image: see [deploy/cloudflare/README.md](../cloudflare/README.md#local-test)
(same image).
