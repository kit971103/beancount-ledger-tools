#!/bin/sh
# Create the free-tier e2-micro VM that serves fava through Cloudflare Tunnel and
# Tailscale (startup.sh sets it up).  Safe to re-run: each step skips what already
# exists; on an existing VM it updates the metadata and startup script, then
# reboots it to apply them.
#
#   PROJECT_ID=my-project LEDGER_REPO=<owner>/<repo> \
#     GITHUB_TOKEN=... CLOUDFLARED_TOKEN=... TS_AUTHKEY=... deploy/gcp/create.sh
#
# The three tokens are needed on the first run only (they go into Secret Manager).
set -eu

: "${PROJECT_ID:?set PROJECT_ID}"
: "${LEDGER_REPO:?set LEDGER_REPO=<owner>/<repo>}"
ZONE="${ZONE:-us-west1-b}"              # the free e2-micro: us-west1, us-central1 or us-east1
REGION="${ZONE%-*}"
LEDGER_BRANCH="${LEDGER_BRANCH:-master}"
TOOLS_REF="${TOOLS_REF:-v0.1.0}"        # tag of this repository installed on the VM
VM=ledger-fava
NETWORK=ledger
SA="$VM@$PROJECT_ID.iam.gserviceaccount.com"
HERE=$(dirname "$0")
METADATA="ledger-repo=$LEDGER_REPO,ledger-branch=$LEDGER_BRANCH,tools-ref=$TOOLS_REF"

gcloud config set project "$PROJECT_ID"
gcloud services enable cloudresourcemanager.googleapis.com compute.googleapis.com \
	secretmanager.googleapis.com

# The VM runs as its own account, which may read its three secrets and nothing else.
if ! gcloud iam service-accounts describe "$SA" >/dev/null 2>&1; then
	gcloud iam service-accounts create "$VM" --display-name="fava VM (read-only ledger)"
fi
put_secret() {  # name, value (needed only if the secret does not exist yet)
	if ! gcloud secrets describe "$1" >/dev/null 2>&1; then
		[ -n "$2" ] || { echo "set the token for $1 (first run)" >&2; exit 1; }
		printf '%s' "$2" | gcloud secrets create "$1" --data-file=-
	fi
	gcloud secrets add-iam-policy-binding "$1" --member="serviceAccount:$SA" \
		--role=roles/secretmanager.secretAccessor >/dev/null
}
put_secret ledger-github-token "${GITHUB_TOKEN:-}"          # read-only, the ledger repo only
put_secret ledger-cloudflared-token "${CLOUDFLARED_TOKEN:-}" # the tunnel's token
put_secret ledger-tailscale-authkey "${TS_AUTHKEY:-}"       # a one-off auth key

# A network of its own with no firewall rules: nothing inbound, everything outbound.
if ! gcloud compute networks describe "$NETWORK" >/dev/null 2>&1; then
	gcloud compute networks create "$NETWORK" --subnet-mode=custom
	gcloud compute networks subnets create "$NETWORK-$REGION" --network="$NETWORK" \
		--region="$REGION" --range=10.10.0.0/24
fi

# Free tier: one e2-micro in a US region, up to 30 GB of standard disk.  The
# external IPv4 address is only for outbound traffic (GitHub, Cloudflare, Tailscale).
if ! gcloud compute instances describe "$VM" --zone="$ZONE" >/dev/null 2>&1; then
	gcloud compute instances create "$VM" --zone="$ZONE" --machine-type=e2-micro \
		--image-family=debian-13 --image-project=debian-cloud \
		--boot-disk-type=pd-standard --boot-disk-size=30GB \
		--network="$NETWORK" --subnet="$NETWORK-$REGION" \
		--service-account="$SA" --scopes=cloud-platform --shielded-secure-boot \
		--metadata="$METADATA" --metadata-from-file=startup-script="$HERE/startup.sh"
else
	gcloud compute instances add-metadata "$VM" --zone="$ZONE" \
		--metadata="$METADATA" --metadata-from-file=startup-script="$HERE/startup.sh"
	gcloud compute instances reset "$VM" --zone="$ZONE"
fi
echo "Startup log: gcloud compute instances get-serial-port-output $VM --zone=$ZONE"
