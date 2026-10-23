# fava + bean-check for one ledger, cloned from GitHub at start (see entrypoint.sh).
# Build context is the repository root; deploy/cloudflare and deploy/gcp both build it.
FROM python:3.13-slim

RUN apt-get update \
	&& apt-get install --yes --no-install-recommends ca-certificates git \
	&& rm -rf /var/lib/apt/lists/*

WORKDIR /opt/ledger-tools
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY deploy/container/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY deploy/container/ledger-pull.sh /usr/local/bin/ledger-pull
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/ledger-pull

# The image has no .git: the commit it was built from, for fava's Status page.
ARG LEDGER_TOOLS_COMMIT=""
ENV LEDGER_TOOLS_COMMIT=$LEDGER_TOOLS_COMMIT

EXPOSE 5000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
