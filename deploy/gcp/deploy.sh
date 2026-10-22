#!/bin/sh
# Build the fava image and deploy it to Cloud Run behind IAP.  Safe to re-run:
# each step skips what already exists, and every run pushes a new image.
#
#   PROJECT_ID=my-project LEDGER_REPO=<owner>/<repo> IAP_USER=me@example.com \
#     GITHUB_TOKEN=... OAUTH_CLIENT_ID=... OAUTH_CLIENT_SECRET=... \
#     deploy/gcp/deploy.sh                         # from the repository root
#
# GITHUB_TOKEN (into Secret Manager) and the OAuth client (into the service's IAP
# settings) are needed on the first run only; see README.md for the client.
set -eu

: "${PROJECT_ID:?set PROJECT_ID}"
: "${LEDGER_REPO:?set LEDGER_REPO=<owner>/<repo>}"
: "${IAP_USER:?set IAP_USER to the Google account allowed in}"
REGION="${REGION:-us-west1}"            # a free-tier region
LEDGER_BRANCH="${LEDGER_BRANCH:-master}"
SERVICE=ledger-fava
SECRET=ledger-github-token
REPO=ledger
IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/fava:$(git describe --tags --always --dirty)"
SA="$SERVICE@$PROJECT_ID.iam.gserviceaccount.com"
HERE=$(dirname "$0")

gcloud config set project "$PROJECT_ID"
gcloud services enable cloudresourcemanager.googleapis.com run.googleapis.com \
	artifactregistry.googleapis.com secretmanager.googleapis.com iap.googleapis.com
NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

# Image: free storage is 0.5 GB, so keep only the two newest.
if ! gcloud artifacts repositories describe "$REPO" --location="$REGION" >/dev/null 2>&1; then
	gcloud artifacts repositories create "$REPO" --repository-format=docker --location="$REGION"
fi
gcloud artifacts repositories set-cleanup-policies "$REPO" --location="$REGION" \
	--policy="$HERE/cleanup-policy.json" --no-dry-run
gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet
docker build -f deploy/container/Dockerfile -t "$IMAGE" .
docker push "$IMAGE"

# The service runs as its own account, which may read the token and nothing else.
if ! gcloud iam service-accounts describe "$SA" >/dev/null 2>&1; then
	gcloud iam service-accounts create "$SERVICE" --display-name="fava (read-only ledger)"
fi
if ! gcloud secrets describe "$SECRET" >/dev/null 2>&1; then
	: "${GITHUB_TOKEN:?set GITHUB_TOKEN (read-only, $LEDGER_REPO only) for the first run}"
	printf '%s' "$GITHUB_TOKEN" | gcloud secrets create "$SECRET" --data-file=-
fi
gcloud secrets add-iam-policy-binding "$SECRET" --member="serviceAccount:$SA" \
	--role=roles/secretmanager.secretAccessor >/dev/null

# Cloud Run's filesystem is memory, but the clone is a few MB and fava idles near 70 MiB.
gcloud run deploy "$SERVICE" --region="$REGION" --image="$IMAGE" \
	--service-account="$SA" --port=5000 --memory=512Mi --max-instances=1 \
	--set-env-vars="LEDGER_REPO=$LEDGER_REPO,LEDGER_BRANCH=$LEDGER_BRANCH" \
	--set-secrets="GITHUB_TOKEN=$SECRET:latest" \
	--no-allow-unauthenticated --iap

# fava has no login: IAP lets in IAP_USER only.
gcloud run services add-iam-policy-binding "$SERVICE" --region="$REGION" \
	--member="serviceAccount:service-$NUMBER@gcp-sa-iap.iam.gserviceaccount.com" \
	--role=roles/run.invoker >/dev/null
gcloud iap web add-iam-policy-binding --member="user:$IAP_USER" \
	--role=roles/iap.httpsResourceAccessor \
	--region="$REGION" --resource-type=cloud-run --service="$SERVICE" >/dev/null

# A project outside a Google Cloud organization cannot use IAP's Google-managed
# OAuth client (IAP answers 502 "Empty Google Account OAuth client ID(s)/secret(s)"),
# so the service gets the custom client made in the console.
# (gcloud iap settings ignores the configured project: --project must be given.)
IAP="--project=$PROJECT_ID --region=$REGION --resource-type=cloud-run --service=$SERVICE"
if [ -n "${OAUTH_CLIENT_ID:-}" ]; then
	: "${OAUTH_CLIENT_SECRET:?set OAUTH_CLIENT_SECRET with OAUTH_CLIENT_ID}"
	settings=$(mktemp) && trap 'rm -f "$settings"' EXIT
	printf '{"accessSettings": {"oauthSettings": {"clientId": "%s", "clientSecret": "%s"}}}\n' \
		"$OAUTH_CLIENT_ID" "$OAUTH_CLIENT_SECRET" >"$settings"
	gcloud iap settings set "$settings" $IAP >/dev/null
elif [ -z "$(gcloud iap settings get $IAP --format='value(accessSettings.oauthSettings.clientId)')" ]; then
	echo "IAP has no OAuth client yet: set OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET (see README.md)" >&2
	exit 1
fi

gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)'
