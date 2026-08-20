# Facebook MCP (Pages API).
#
# Source: https://github.com/Choizapp/choiz-facebook-mcp (Choiz fork of
# HagaiHen/facebook-mcp-server with Graph API v23 + deprecated-metric fixes).
# Stack: Python (server.py registers tools on a FastMCP instance — it does NOT
# call mcp.run()).
#
# History: previously bundled Node + supergateway to translate the MCP's stdio
# transport (via `mcp run /app/server.py`) into MCP Streamable HTTP. The same
# child-process leak that hit warehouse_mcp on 2026-05-06 affects this image
# too: supergateway --stateless spawns a Python child per request and never
# reaps them, so RAM grows indefinitely under sustained load and eventually
# starves the host (recurring "tunnel zombie" pattern).
#
# Fix (2026-05-07): drop Node + supergateway, run FastMCP's native
# streamable-http transport directly. /opt/entrypoint.py monkey-patches
# FastMCP.__init__ to default streamable_http_path="/", host="0.0.0.0",
# port=8080, then imports server.py to build the FastMCP instance and calls
# mcp.run(transport="streamable-http"). Same shape as mcp/warehouse/.
#
# A SINGLE image is built from this Dockerfile and reused by both
# facebook_choiz_mcp and facebook_timeless_mcp services. The two tenants differ
# only in env vars (FACEBOOK_ACCESS_TOKEN, FACEBOOK_PAGE_ID).
#
# Bump (2026-08-20): picks up two fixes in the fork. (1) get_page_posts
# passed no pagination parameters, so Graph applied its 25-per-page default
# and the tool could never see further back than ~7 weeks; it now takes
# limit/since/until and walks the cursors itself. (2) _request returned
# Graph's paging.next verbatim, which embeds the Page access token in a URL
# -- it was leaking to the MCP client on every paginated call. Both tenants
# get this at once, since they share this image.
#
# The SHA below pins the exact build of the user-facing fork. Bump it when you
# want to pick up upstream changes; CI/CD rebuilds and redeploys.

FROM python:3.12-slim

ARG FACEBOOK_MCP_SHA=8cf0d8c98a6e23b7ec040afb5b783ce78482bd8b

# git: fetch source at build time. ca-certificates: HTTPS to graph.facebook.com.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN git clone https://github.com/Choizapp/choiz-facebook-mcp.git . \
 && git checkout "${FACEBOOK_MCP_SHA}"

# mcp pinned to 1.25.0 (same minor as warehouse) so the monkey-patched
# FastMCP.__init__ signature stays predictable. requests + python-dotenv are
# the fork's runtime deps (its requirements.txt lists them as bare names).
# We no longer need the [cli] extra: the previous image used `mcp run` to load
# server.py over stdio; entrypoint.py imports it directly now.
RUN pip install --no-cache-dir 'mcp==1.25.0' requests python-dotenv

# Flush stdout immediately so init logs land in `docker logs` without delay.
ENV PYTHONUNBUFFERED=1

COPY entrypoint.py /opt/entrypoint.py

EXPOSE 8080

# entrypoint.py applies the FastMCP monkey-patch BEFORE importing server.py,
# then runs the streamable-http transport in-process. No supergateway hop, no
# Node, no child-process churn.
ENTRYPOINT ["python", "/opt/entrypoint.py"]
