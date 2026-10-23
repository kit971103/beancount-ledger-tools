#!/bin/sh
# Let GitHub Actions deploy new images to the service deploy.sh made, without a
# key: the workflow's OIDC token is exchanged (Workload Identity Federation) for
# a deployer service account that may push images and roll out revisions, and
# only for pushes to main of one repository.  Safe to re-run.
#
#   PROJECT_ID=my-project CI_REPO=<owner>/<repo> deploy/gcp/ci-setup.sh
#
# Prints the three repository variables the workflow reads.
set -eu

: "${PROJECT_ID:?set PROJECT_ID}"
: "${CI_REPO:?set CI_REPO=<owner>/<repo> (this repository)}"
REGION="${REGION:-us-west1}"
SERVICE=ledger-fava
REPO=ledger
POOL=github
PROVIDER=github
DEPLOYER=ledger-fava-deployer
SA="$SERVICE@$PROJECT_ID.iam.gserviceaccount.com"
DEPLOYER_SA="$DEPLOYER@$PROJECT_ID.iam.gserviceaccount.com"
# The numeric id, not the name: a deleted repository's name can be taken again.
CI_REPO_ID="${CI_REPO_ID:-$(gh api "repos/$CI_REPO" --jq .id)}"

# A new service account takes a while to be visible to other services' IAM.
retry() {
	for _ in 1 2 3 4 5 6; do
		"$@" && return 0
		echo "retrying in 10s" >&2
		sleep 10
	done
	"$@"
}

gcloud config set project "$PROJECT_ID"
gcloud services enable iamcredentials.googleapis.com sts.googleapis.com
NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

if ! gcloud iam workload-identity-pools describe "$POOL" --location=global >/dev/null 2>&1; then
	gcloud iam workload-identity-pools create "$POOL" --location=global \
		--display-name="GitHub Actions"
fi
CONDITION="assertion.repository_id == '$CI_REPO_ID' && assertion.ref == 'refs/heads/main'"
if ! gcloud iam workload-identity-pools providers describe "$PROVIDER" \
	--location=global --workload-identity-pool="$POOL" >/dev/null 2>&1; then
	gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
		--location=global --workload-identity-pool="$POOL" \
		--issuer-uri=https://token.actions.githubusercontent.com \
		--attribute-mapping=google.subject=assertion.sub,attribute.repository_id=assertion.repository_id \
		--attribute-condition="$CONDITION"
else
	gcloud iam workload-identity-pools providers update-oidc "$PROVIDER" \
		--location=global --workload-identity-pool="$POOL" \
		--attribute-condition="$CONDITION"
fi

# The deployer may push to the image repository, roll out Cloud Run revisions
# running as the service's account, and nothing else: no IAM, no secrets, no IAP
# settings.  run.developer is project-wide (the project holds only this service):
# a deploy also reads the region's operations, outside the service's own policy.
if ! gcloud iam service-accounts describe "$DEPLOYER_SA" >/dev/null 2>&1; then
	gcloud iam service-accounts create "$DEPLOYER" --display-name="GitHub Actions deployer for $SERVICE"
fi
retry gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_SA" \
	--role=roles/iam.workloadIdentityUser \
	--member="principalSet://iam.googleapis.com/projects/$NUMBER/locations/global/workloadIdentityPools/$POOL/attribute.repository_id/$CI_REPO_ID" >/dev/null
retry gcloud artifacts repositories add-iam-policy-binding "$REPO" --location="$REGION" \
	--member="serviceAccount:$DEPLOYER_SA" --role=roles/artifactregistry.writer >/dev/null
retry gcloud projects add-iam-policy-binding "$PROJECT_ID" --condition=None \
	--member="serviceAccount:$DEPLOYER_SA" --role=roles/run.developer >/dev/null
retry gcloud iam service-accounts add-iam-policy-binding "$SA" \
	--member="serviceAccount:$DEPLOYER_SA" --role=roles/iam.serviceAccountUser >/dev/null

echo "Repository variables (Settings -> Secrets and variables -> Actions -> Variables):"
echo "  GCP_PROJECT_ID=$PROJECT_ID"
echo "  GCP_WIF_PROVIDER=projects/$NUMBER/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER"
echo "  GCP_DEPLOY_SA=$DEPLOYER_SA"
[ "$REGION" = us-west1 ] || echo "  GCP_REGION=$REGION"
