# Shopify MCP — Admin GraphQL API (products, orders, customers,
# inventory, collections, metafields, ...).
#
# Source: https://github.com/GeLi2001/shopify-mcp — public package, no
# Choiz fork. Cloned at build time at a pinned commit. We need the
# package's compiled `dist/tools/registry.js` (the `tools` array of
# initialize/execute objects) so our entrypoint can register a curated
# read-only subset on its own McpServer behind StreamableHTTPServerTransport.
#
# The upstream `dist/index.js` is a stdio-bound launcher that registers
# ALL 31 tools (including writes: create/update/delete/cancel/refund/
# inventory-set/...). We bypass it entirely; see entrypoint.mjs for the
# allowlist policy. Architectural shape is identical to mcp/gsc — single
# in-process Node server, no supergateway, no per-request child churn.
#
# Multi-tenant pattern (same as facebook / instagram / ga4 / gsc):
# TWO services in compose.yml, one image. Per-tenant differences:
#   MYSHOPIFY_DOMAIN  — choiz-2.myshopify.com vs gotimeless-ai.myshopify.com
#   SHOPIFY_ACCESS_TOKEN — admin API access token per store
# The slug `/mcp/shopify-<tenant>/` is the only thing claude.ai sees.

FROM node:20-slim

ARG SHOPIFY_MCP_SHA=c90faaf434023bd22c1beb6bd59ff735bca18fac

# git: clone source at build time. ca-certificates: HTTPS to
# <shop>.myshopify.com/admin/api/.../graphql.json.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone + build. `npm install` (without --production) fetches devDeps
# required by tsc; `npm run build` compiles to dist/. Pruning afterwards
# trims devDeps so the runtime image stays small.
RUN git clone https://github.com/GeLi2001/shopify-mcp.git . \
 && git checkout "${SHOPIFY_MCP_SHA}" \
 && npm install \
 && npm run build \
 && npm prune --production

# Our streamable-http launcher. Lives in /app so its
# `import "./dist/tools/registry.js"` and
# `import "@modelcontextprotocol/sdk/..."` both resolve against
# /app/node_modules.
COPY entrypoint.mjs /app/entrypoint.mjs

EXPOSE 8080

ENTRYPOINT ["node", "/app/entrypoint.mjs"]
