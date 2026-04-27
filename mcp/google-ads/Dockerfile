# Google Ads MCP (campaigns, ad groups, GAQL queries, image-asset analytics).
#
# Source: https://github.com/Choizapp/choiz-google-ads-mcp (Choiz fork of
# cohnen/mcp-google-ads). Carries two patches on top of upstream:
#   1. 9d27ca9 — migrate from REST (which returns 500s) to the gRPC google-ads
#      client library.
#   2. a61ba93 — gateway-prep: pin gRPC deps in requirements.txt + pyproject.toml,
#      drop indent=2 + lower hardcoded LIMITs to fit the claude.ai 2-3 KB
#      streamable-http payload ceiling, and read OAuth credentials from env
#      vars (so containers don't need a credentials.json file mount).
#
# Stack: Python; google_ads_server.py uses FastMCP and calls mcp.run(transport=
# "stdio") in its __main__ block. So we launch via `python google_ads_server.py`
# directly (instagram-style), no `mcp run` CLI needed.
#
# Single tenant: only Choiz today (no Timeless equivalent active). MCC login
# customer id 3916621621 is shared across all sub-accounts the bot user
# (hola@choiz.com.mx) has access to.
#
# We wrap with supergateway so the container exposes MCP Streamable HTTP on
# :8080, which is what claude.ai speaks.
#
# The SHA below pins the exact build. Bump it intentionally when you want to
# pick up upstream changes, then push to master — CI/CD rebuilds and deploys.

FROM python:3.12-slim

ARG GOOGLE_ADS_MCP_SHA=a61ba938d3a8110879202693bb312a248a3e763a

# git: fetch source at build time.
# nodejs/npm: install supergateway (the stdio <-> Streamable HTTP bridge).
# ca-certificates: HTTPS to googleads.googleapis.com (gRPC over HTTPS).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates nodejs npm \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN git clone https://github.com/Choizapp/choiz-google-ads-mcp.git . \
 && git checkout "${GOOGLE_ADS_MCP_SHA}"

# requirements.txt was fixed in a61ba93 to declare google-ads (gRPC) explicitly
# and prune the unused REST stack. mcp[cli] gives us the launcher CLI even
# though we don't strictly need it (server has its own __main__).
RUN pip install --no-cache-dir -r requirements.txt

# supergateway bridges stdio <-> Streamable HTTP (same wrapper used by
# meta-ads/facebook/instagram).
RUN npm install -g supergateway

# Disable Python stdio buffering so supergateway sees the MCP's initial
# handshake immediately. Critical for the Streamable HTTP wrapper.
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

# `python google_ads_server.py` is the launcher: the script's __main__ calls
# mcp.run(transport="stdio"). supergateway captures that stdio and exposes
# it as Streamable HTTP on :8080.
# --streamableHttpPath / matters: the gateway strips /mcp/<name> before
# forwarding, so the upstream must serve at /.
ENTRYPOINT ["supergateway"]
CMD [ \
  "--stdio", "python /app/google_ads_server.py", \
  "--outputTransport", "streamableHttp", \
  "--port", "8080", \
  "--streamableHttpPath", "/" \
]
