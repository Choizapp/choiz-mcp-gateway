# Meta Ads MCP.
#
# Source: https://github.com/Choizapp/choiz-meta-ads-mcp (public fork).
# Transport: stdio (uses @modelcontextprotocol/sdk's StdioServerTransport).
# We wrap it with supergateway so the container exposes MCP Streamable HTTP
# on :8080, which is what claude.ai speaks.
#
# The SHA below pins the exact build. Bump it intentionally when you want to
# pick up upstream changes, then docker compose up -d --build meta_ads_mcp.

FROM node:20-slim

ARG META_ADS_SHA=01f32d94ae7972d01276051428ffe93ca1a8030c

# git is only needed at build time to fetch the source.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Split into separate layers so a failure in npm ci (often network-flaky on
# small EC2 instances) doesn't force re-cloning. Each layer is cached
# independently by Docker.
RUN git clone https://github.com/Choizapp/choiz-meta-ads-mcp.git . \
 && git checkout "${META_ADS_SHA}"

# npm retry flags: the default (2 retries, 10s max timeout) is not enough on
# constrained networks. Extend both.
RUN npm config set fetch-retries 5 \
 && npm config set fetch-retry-mintimeout 20000 \
 && npm config set fetch-retry-maxtimeout 120000 \
 && npm ci --no-audit --no-fund

RUN npm run build \
 && npm prune --omit=dev

# supergateway bridges stdio <-> Streamable HTTP.
RUN npm install -g supergateway

EXPOSE 8080

# --streamableHttpPath / matters: the gateway strips the /mcp/<name> prefix
# before forwarding, so the upstream must serve at /.
ENTRYPOINT ["supergateway"]
CMD [ \
  "--stdio", "node /app/build/index.js", \
  "--outputTransport", "streamableHttp", \
  "--port", "8080", \
  "--streamableHttpPath", "/" \
]
