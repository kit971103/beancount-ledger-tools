# fava + bean-check for one ledger, cloned from GitHub at start (see entrypoint.sh).
# Build context is the repository root (wrangler.jsonc: image_build_context "../..").
FROM python:3.13-slim

RUN apt-get update \
	&& apt-get install --yes --no-install-recommends ca-certificates git \
	&& rm -rf /var/lib/apt/lists/*

WORKDIR /opt/ledger-tools
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY deploy/cloudflare/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 5000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
