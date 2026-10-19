# GSC MCP — Google Search Console (search_analytics, list_sites,
# get_sitemap, index_inspect, ...).
#
# Source: https://github.com/ahonn/mcp-server-gsc — public package, no
# Choiz fork. Cloned at build time at a pinned commit and built locally
# (the published npm tarball ships only the stdio launcher; we want
# `dist/server.js`'s `createServer` builder so entrypoint.mjs can bridge
# it to the v2 SDK in-process).
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
#
# Two SDK generations live side by side (2026-10-19). The v1 SDK stays at
# 1.26.0 because the upstream server is written against it, and it only ever
# talks to our bridge client in-process. The v2 packages are what face the
# wire: `@modelcontextprotocol/server` speaks revision 2026-07-28, which no
# 1.x release does (1.32.1 still caps at 2025-11-25), and
# `@modelcontextprotocol/node` adapts its fetch handler to node:http. See
# entrypoint.mjs. The v2 server brings its own zod 4 nested under it; the
# upstream's zod 3 stays top-level.
RUN git clone https://github.com/ahonn/mcp-server-gsc.git . \
 && git checkout "${GSC_MCP_SHA}" \
 && npm install \
 && npm install --save-exact \
      "@modelcontextprotocol/sdk@1.26.0" \
      "@modelcontextprotocol/server@2.3.1" \
      "@modelcontextprotocol/node@2.1.1" \
 && npm run build \
 && npm prune --production \
 && node -e "for (const p of ['sdk','server','node']) console.log(p, 'pinned at', require('/app/node_modules/@modelcontextprotocol/' + p + '/package.json').version)"

# Fail the BUILD, not production, if any import entrypoint.mjs uses ever moves.
# These are the exact specifiers and names it imports.
RUN node --input-type=module -e "import { Client } from '@modelcontextprotocol/sdk/client/index.js'; import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'; import { Server, createMcpHandler } from '@modelcontextprotocol/server'; import { toNodeHandler } from '@modelcontextprotocol/node'; for (const [n, v] of Object.entries({ Client, InMemoryTransport, Server, createMcpHandler, toNodeHandler })) if (typeof v !== 'function') throw new Error(n + ' missing'); console.log('import check OK')"

# Our HTTP launcher. Bundled in /app so its `import "./dist/server.js"`
# resolves correctly and so both SDK generations resolve from
# /app/node_modules.
COPY entrypoint.mjs /app/entrypoint.mjs

EXPOSE 8080

# `node /app/entrypoint.mjs` decodes the SA JSON, builds the upstream GSC
# Server behind an in-process client, and serves it through the v2 SDK's
# createMcpHandler on a Node http server on 0.0.0.0:8080. No supergateway,
# no per-request child spawning.
ENTRYPOINT ["node", "/app/entrypoint.mjs"]
