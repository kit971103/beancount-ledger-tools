# fava on a free-tier Compute Engine VM

One `e2-micro` VM running `fava --read-only` on your ledger, published through
Cloudflare Tunnel behind Cloudflare Access, and reachable for administration
over Tailscale. No port is open to the internet: the VM's network has no
firewall rules, fava listens on `127.0.0.1`, and `cloudflared` and `tailscaled`
connect outward. The ledger is cloned from GitHub and fast-forwarded every
10 minutes; fava reloads on change. Documents (`documents/`, usually
git-ignored) are not included.

| file | what |
|---|---|
| `create.sh` | run locally: service account, secrets, network, VM |
| `startup.sh` | runs on the VM at every boot: swap, fava, the pull timer, cloudflared, Tailscale |

## Cost

Compute Engine's free tier, as of 2026-10
([Free Tier](https://docs.cloud.google.com/free/docs/free-cloud-features)):
one non-preemptible `e2-micro` in `us-west1`, `us-central1` or `us-east1`, 30 GB
of standard persistent disk, 1 GB a month of outbound data from North America.
The external IPv4 address the VM needs for outbound traffic is not in the free
tier and is billed by the hour. Traffic through the tunnel counts as outbound
data. A billing account is required: set a budget alert.

## Deploy

Needs `gcloud`, logged in (`gcloud auth login`), and a domain on Cloudflare.

1. Create a project, link a billing account, add a budget alert.
2. GitHub: a fine-grained token for the ledger repository only, *Contents:
   read-only*.
3. Cloudflare Zero Trust → Networks → Tunnels: create a `cloudflared` tunnel and
   copy its token. Add a public hostname, e.g. `fava.example.com`, with service
   `http://localhost:5000`.
4. Cloudflare Zero Trust → Access → Applications: add a self-hosted application
   for that hostname with a policy allowing only your email. **Do this before
   the VM starts: the hostname is public as soon as the tunnel connects.**
5. Tailscale admin → Settings → Keys: generate a one-off, pre-approved auth key.
6. From the repository root:

   ```sh
   PROJECT_ID=my-project LEDGER_REPO=<owner>/<repo> \
     GITHUB_TOKEN=github_pat_... CLOUDFLARED_TOKEN=eyJ... TS_AUTHKEY=tskey-auth-... \
     deploy/gcp/create.sh
   ```

   The tokens are read on the first run only and stored in Secret Manager.
   `ZONE` defaults to `us-west1-b`, `LEDGER_BRANCH` to `master`, `TOOLS_REF`
   (the tag of this repository installed on the VM) to `v0.1.0`.
7. The first boot takes a few minutes. Watch it with
   `gcloud compute instances get-serial-port-output ledger-fava --zone=us-west1-b`.
8. Check: `https://fava.example.com` asks for Cloudflare Access login, then
   serves fava; `tailscale ssh root@ledger-fava` opens a shell.

Re-run `create.sh` (with a new `TOOLS_REF`) to upgrade: it updates the metadata
and startup script and reboots the VM. A push to the ledger needs nothing.

## On the VM

| what | how |
|---|---|
| fava | `systemctl status fava`, `journalctl -u fava` |
| pull now | `systemctl start ledger-pull` |
| startup log | `journalctl -u google-startup-scripts` |
| fava over the tailnet only | `tailscale serve --bg 5000` |

## What it creates

| resource | name | why |
|---|---|---|
| service account | `ledger-fava` | the VM's identity; may read its three secrets only |
| secrets | `ledger-github-token`, `ledger-cloudflared-token`, `ledger-tailscale-authkey` | read from Secret Manager on the VM; the GitHub token per pull, never stored there; the tunnel token ends up in the `cloudflared` systemd unit |
| VPC network | `ledger`, subnet `ledger-<region>` | no firewall rules: no inbound traffic |
| VM | `ledger-fava` | `e2-micro`, Debian 13, 30 GB standard disk, Shielded VM secure boot |
