#!/bin/sh
# Startup script of the fava VM (see create.sh); Compute Engine runs it as root on
# every boot, so each step skips what is already in place.
#
#   fava       127.0.0.1:5000, read-only, on /srv/ledger (no port is open to the internet)
#   cloudflared  publishes fava at the tunnel's hostname, behind Cloudflare Access
#   tailscale  SSH and admin over the tailnet
#   ledger-pull.timer  fast-forwards /srv/ledger every 10 minutes; fava reloads on change
#
# Settings come from instance metadata (ledger-repo, ledger-branch, tools-ref),
# secrets from Secret Manager through the VM's service account.
set -eu

meta() {
	curl -fsS -H Metadata-Flavor:Google \
		"http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"
}
LEDGER_REPO=$(meta ledger-repo)
LEDGER_BRANCH=$(meta ledger-branch)
TOOLS_REF=$(meta tools-ref)

# 1 GB of RAM: swap keeps pip and bean-check clear of the OOM killer.
if [ ! -f /swapfile ]; then
	fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile
fi
swapon --show | grep -q /swapfile || swapon /swapfile

apt-get update -q
apt-get install -y -q --no-install-recommends ca-certificates curl git python3-venv

# Reads one secret's latest version; no gcloud needed.
cat >/usr/local/bin/gcp-secret <<'EOF'
#!/bin/sh
set -eu
md=http://metadata.google.internal/computeMetadata/v1
token=$(curl -fsS -H Metadata-Flavor:Google "$md/instance/service-accounts/default/token" |
	python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])')
project=$(curl -fsS -H Metadata-Flavor:Google "$md/project/project-id")
curl -fsS -H "Authorization: Bearer $token" \
	"https://secretmanager.googleapis.com/v1/projects/$project/secrets/$1/versions/latest:access" |
	python3 -c 'import base64, json, sys; sys.stdout.write(base64.b64decode(json.load(sys.stdin)["payload"]["data"]).decode())'
EOF
chmod 755 /usr/local/bin/gcp-secret

id fava >/dev/null 2>&1 || useradd --system --create-home --home-dir /srv/fava fava
install -d -o fava -g fava /srv/ledger

# fava and the plugins, at the tag in tools-ref.
if [ "$(cat /opt/fava/ref 2>/dev/null)" != "$TOOLS_REF" ]; then
	python3 -m venv /opt/fava
	/opt/fava/bin/pip install -q \
		"beancount-ledger-tools @ git+https://github.com/kit971103/beancount-ledger-tools@$TOOLS_REF"
	echo "$TOOLS_REF" >/opt/fava/ref
fi

# Clone or fast-forward the ledger; the token is read per run and never stored.
cat >/etc/default/ledger <<EOF
LEDGER_REPO=$LEDGER_REPO
LEDGER_BRANCH=$LEDGER_BRANCH
EOF
cat >/usr/local/bin/ledger-pull <<'EOF'
#!/bin/sh
set -eu
. /etc/default/ledger
auth="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$(gcp-secret ledger-github-token)" | base64 -w0)"
if [ -d /srv/ledger/.git ]; then
	git -C /srv/ledger -c http.extraHeader="$auth" pull -q --ff-only --depth 1
else
	git -c http.extraHeader="$auth" clone -q --depth 1 --branch "$LEDGER_BRANCH" \
		"https://github.com/$LEDGER_REPO.git" /srv/ledger
fi
EOF
chmod 755 /usr/local/bin/ledger-pull

cat >/etc/systemd/system/ledger-pull.service <<'EOF'
[Unit]
Description=Fast-forward the ledger checkout
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=fava
ExecStart=/usr/local/bin/ledger-pull
EOF
cat >/etc/systemd/system/ledger-pull.timer <<'EOF'
[Unit]
Description=Fast-forward the ledger checkout every 10 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
EOF
cat >/etc/systemd/system/fava.service <<'EOF'
[Unit]
Description=fava, read-only, on localhost
Wants=network-online.target
After=network-online.target

[Service]
User=fava
ExecStartPre=/usr/local/bin/ledger-pull
ExecStart=/opt/fava/bin/fava --read-only --host 127.0.0.1 --port 5000 /srv/ledger/main.beancount
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now ledger-pull.timer
systemctl enable fava.service
systemctl restart fava.service    # picks up a new tools-ref

# Cloudflare Tunnel: hostname and Access policy are set in the Cloudflare dashboard.
if ! command -v cloudflared >/dev/null; then
	curl -fsSL -o /tmp/cloudflared.deb \
		https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
	dpkg -i /tmp/cloudflared.deb && rm /tmp/cloudflared.deb
fi
if ! systemctl is-enabled -q cloudflared 2>/dev/null; then
	cloudflared service install "$(gcp-secret ledger-cloudflared-token)"
fi

# Tailscale, with Tailscale SSH: the VM has no SSH port open.
if ! command -v tailscale >/dev/null; then
	curl -fsSL https://tailscale.com/install.sh | sh
fi
if ! tailscale status >/dev/null 2>&1; then
	tailscale up --auth-key="$(gcp-secret ledger-tailscale-authkey)" --ssh --hostname=ledger-fava
fi
