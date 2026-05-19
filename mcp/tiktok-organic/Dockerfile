# TikTok Organic MCP — custom server, no upstream package to wrap.
#
# Talks to TikTok Display API v2 (open.tiktokapis.com) using a long-lived
# refresh_token kept in env. End users authenticate to claude.ai with
# Google Workspace; they never see a TikTok login.
#
# Multi-tenant pattern (one image, one container per brand): TIKTOK_BRAND
# + TIKTOK_CLIENT_KEY/SECRET/REFRESH_TOKEN per-container. Matches ga4 /
# gsc / facebook / instagram / shopify / powerbi slug-by-brand convention.

FROM python:3.12-slim

# ca-certificates: HTTPS to open.tiktokapis.com.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pin mcp to 1.25.0 to match the rest of the stack (warehouse / facebook /
# instagram / ga4 / powerbi). requests for the TikTok HTTP calls — no SDK
# needed, the surface we use is six REST endpoints and one OAuth refresh.
RUN pip install --no-cache-dir \
    'mcp==1.25.0' \
    'requests>=2.31,<3'

ENV PYTHONUNBUFFERED=1

COPY entrypoint.py /opt/entrypoint.py

EXPOSE 8080

ENTRYPOINT ["python", "/opt/entrypoint.py"]
