# GSC MCP — Google Search Console (search_analytics, list_sites,
# get_sitemap, index_inspect, ...).
#
# Source: https://github.com/ahonn/mcp-server-gsc — public package, no
# Choiz fork. Cloned at build time at a pinned commit and built locally
# (the published npm tarball ships only the stdio launcher; we want
# `dist/server.js`'s `createServer` builder so we can wire it through
# the MCP TS SDK's StreamableHTTPServerTransport directly).
#
# History: previously bundled supergateway to translate the package's
# stdio MCP into Streamable HTTP. Same supergateway --stateless leak
# pattern that hit warehouse / facebook / instagram — under sustained
# load the Node child churn grows the container's RSS. Pre-fix on
# 2026-05-07: gsc_choiz_mcp at 489 MiB resident with mem_limit 1500m.
#
# Fix (2026-05-07): drop Node + supergateway. /opt/entrypoint.mjs
# imports `createServer` from the package's compiled output and
# mounts it behind StreamableHTTPServerTransport in-process. Same
# shape as mcp/instagram/ and mcp/ga4/ in spirit, just expressed in
# Node instead of Python.
#
# Multi-tenant pattern unchanged: TWO containers (gsc_choiz_mcp +
# gsc_timeless_mcp), 100% identical at the container level. The
# package has no site-filter env var, so tenant separation is
# slug-only. claude.ai picks the right `siteUrl` per-tool-call from
# the connector context.
#
# Auth: same Choiz `ga4-mcp-reader` service-account JSON as GA4
# (has siteFullUser on both choiz.com.mx and gotimeless.ai). Reused
# via the existing `GA4_SERVICE_ACCOUNT_JSON_B64` env on the EC2
# .env, surfaced into this container as `GSC_SERVICE_ACCOUNT_JSON_B64`
# (compose-level rename so GA4 and GSC entrypoints can evolve
# independently). The entrypoint decodes it once at start to
# /tmp/gsc-sa.json (chmod 600) and passes the path to createServer.

FROM node:20-slim

ARG GSC_MCP_SHA=e671b155271b2b412a302cff1e72f6e14424a7ac

# git: clone source at build time. ca-certificates: HTTPS to
# searchconsole.googleapis.com.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone the package and build it. We need the compiled dist/ so our
# entrypoint can import { createServer } from "./dist/server.js".
# `npm install` (without --production) fetches devDeps required by
# tsc; `npm run build` runs tsc + chmod on dist/*.js.
# The upstream repo ships no lockfile, so a bare `npm install` re-resolves
# @modelcontextprotocol/sdk from a semver RANGE on every rebuild — the image was
# reproducible only by accident. Outage 2026-08-24 on the warehouse image: an
# unrelated compose.yml change rebuilt all 16 images, warehouse picked up a newer
# SDK that had moved a module, and it crashlooped after months of working. Same
# exposure here, different ecosystem.
#
# 1.26.0 is the version measured running in production on 2026-08-24, and it
# satisfies upstream's range — this pins what already works rather than moving it.
# --save-exact so the write into package.json cannot drift either.
RUN git clone https://github.com/ahonn/mcp-server-gsc.git . \
 && git checkout "${GSC_MCP_SHA}" \
 && npm install \
 && npm install --save-exact "@modelcontextprotocol/sdk@1.26.0" \
 && npm run build \
 && npm prune --production \
 && node -e "console.log('sdk pinned at', require('/app/node_modules/@modelcontextprotocol/sdk/package.json').version)"

# Fail the BUILD, not production, if the SDK entrypoint.mjs imports ever move.
# These are the exact specifiers it uses.
RUN node --input-type=module -e "import '@modelcontextprotocol/sdk/server/streamableHttp.js'; import '@modelcontextprotocol/sdk/types.js'; console.log('import check OK')"

# Our streamable-http launcher. Bundled in /app so its
# `import "./dist/server.js"` resolves correctly and so its
# `import "@modelcontextprotocol/sdk/server/streamableHttp.js"` finds
# the SDK in /app/node_modules.
COPY entrypoint.mjs /app/entrypoint.mjs

EXPOSE 8080

# `node /app/entrypoint.mjs` decodes the SA JSON, builds the GSC
# Server, mounts it behind StreamableHTTPServerTransport and binds a
# Node http server on 0.0.0.0:8080. No supergateway, no per-request
# child spawning.
ENTRYPOINT ["node", "/app/entrypoint.mjs"]
