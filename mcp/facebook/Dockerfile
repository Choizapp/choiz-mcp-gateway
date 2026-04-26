# Facebook MCP (Pages API).
#
# Source: https://github.com/Choizapp/choiz-facebook-mcp (Choiz fork of
# HagaiHen/facebook-mcp-server with Graph API v23 + deprecated-metric fixes).
# Stack: Python (server.py uses mcp[cli] with stdio transport).
# We wrap it with supergateway so the container exposes MCP Streamable HTTP
# on :8080, which is what claude.ai speaks.
#
# A SINGLE image is built from this Dockerfile and reused by both
# facebook_choiz_mcp and facebook_timeless_mcp services. The two tenants
# differ only in env vars (FACEBOOK_ACCESS_TOKEN, FACEBOOK_PAGE_ID).
#
# The SHA below pins the exact build. Bump it intentionally when you want to
# pick up upstream changes, then push to master — CI/CD rebuilds and deploys.

FROM python:3.12-slim

ARG FACEBOOK_MCP_SHA=affa2b9c05b5e53a8327cc1ee5d6cc22437d8471

# git: fetch source at build time.
# nodejs/npm: install supergateway (the stdio <-> Streamable HTTP bridge).
# ca-certificates: HTTPS to graph.facebook.com.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates nodejs npm \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN git clone https://github.com/Choizapp/choiz-facebook-mcp.git . \
 && git checkout "${FACEBOOK_MCP_SHA}"

# Upstream requirements.txt has bare `mcp`; we explicitly install mcp[cli]
# because server.py does NOT call mcp.run() — it only defines the FastMCP
# instance and registers tools. The `mcp` CLI (provided by the [cli] extra)
# loads server.py and starts the stdio transport itself, equivalent to the
# user's local `uv run mcp run server.py` setup.
RUN pip install --no-cache-dir "mcp[cli]" requests python-dotenv

# supergateway bridges stdio <-> Streamable HTTP (same wrapper used by meta-ads).
RUN npm install -g supergateway

# Disable Python stdio buffering globally so supergateway sees the MCP's
# initial handshake immediately. The `mcp` CLI subprocess inherits this.
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

# `mcp run server.py` is the launcher: it imports server.py, finds the
# FastMCP instance, and serves it over stdio. supergateway captures that
# stdio and exposes it as Streamable HTTP on :8080.
# --streamableHttpPath / matters: the gateway strips /mcp/<name> before
# forwarding, so the upstream must serve at /.
ENTRYPOINT ["supergateway"]
CMD [ \
  "--stdio", "mcp run /app/server.py", \
  "--outputTransport", "streamableHttp", \
  "--port", "8080", \
  "--streamableHttpPath", "/" \
]
