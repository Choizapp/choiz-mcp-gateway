# Instagram MCP (Graph API for Instagram Business accounts).
#
# Source: https://github.com/Choizapp/choiz-instagram-mcp (Choiz fork of
# jlbadano/ig-mcp with Graph API v23 metric updates: 'impressions' is
# deprecated in v22+, replaced by 'views'; 'follows' and 'total_interactions'
# added).
# Stack: Python; src/instagram_mcp_server.py uses the low-level
# mcp.server.lowlevel.Server (NOT FastMCP). Tools return Sequence[TextContent],
# so the str-return primitive bug we hit on facebook does NOT apply here.
#
# History: previously bundled Node + supergateway to translate the MCP's
# stdio transport into MCP Streamable HTTP. Same supergateway --stateless
# leak that hit warehouse (2026-05-06) and facebook (2026-05-07): each
# request spawned a fresh Python child that was never reaped, so RAM grew
# indefinitely. Pre-fix observation on 2026-05-07: instagram_choiz_mcp at
# 2.1 GB resident with no memlimit.
#
# Fix (2026-05-07): drop Node + supergateway, serve the lowlevel Server
# directly through MCP's StreamableHTTPSessionManager. /opt/entrypoint.py
# instantiates InstagramMCPServer (which registers all tools on its inner
# .server), mounts that Server behind the session manager, wraps it in a
# Starlette app and runs it under uvicorn. Same end result as warehouse and
# facebook (single Python process, no per-request child churn) via a
# different code path because the fork doesn't use FastMCP.
#
# A SINGLE image is built from this Dockerfile and reused by both
# instagram_choiz_mcp and instagram_timeless_mcp services. The two tenants
# differ only in env vars (INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_BUSINESS_ACCOUNT_ID).
# FACEBOOK_APP_ID / FACEBOOK_APP_SECRET are shared (one Choiz Facebook app).
#
# Bump the SHA below to pick up upstream fork changes; CI/CD rebuilds and
# redeploys.

FROM python:3.12-slim

ARG INSTAGRAM_MCP_SHA=1c01c4d3b001b9d3b147f58ef948a2bba11cbdf9

# git: fetch source at build time. ca-certificates: HTTPS to graph.facebook.com.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN git clone https://github.com/Choizapp/choiz-instagram-mcp.git . \
 && git checkout "${INSTAGRAM_MCP_SHA}"

# Install the fork's deps as-pinned (httpx, pydantic, structlog, ...). The
# fork's requirements.txt has bare `mcp>=1.0.0`, so we re-pin afterwards to
# 1.25.0 (matches warehouse + facebook) for reproducible behaviour of
# StreamableHTTPSessionManager. starlette + uvicorn are needed by our
# entrypoint to host the MCP session manager as an ASGI app.
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir 'mcp==1.25.0' 'uvicorn[standard]' starlette

# Flush stdout immediately so init logs land in `docker logs` without delay.
ENV PYTHONUNBUFFERED=1

COPY entrypoint.py /opt/entrypoint.py

EXPOSE 8080

# entrypoint.py instantiates InstagramMCPServer (registers tools on its
# inner lowlevel Server), mounts it behind StreamableHTTPSessionManager and
# serves at "/" via uvicorn. No supergateway, no Node, no per-request
# child spawning.
ENTRYPOINT ["python", "/opt/entrypoint.py"]
