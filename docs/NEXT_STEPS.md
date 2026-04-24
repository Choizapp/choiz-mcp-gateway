# Current state + backlog

Snapshot of where this project is and what's left to do. Update this file whenever you close or open a work item, so a future reader (human or AI) can pick it up without re-reading commit history.

## What works today

- Full OAuth flow: Claude.ai ↔ Worker ↔ Google Workspace ↔ `@choiz.com.mx` users.
- Two MCPs behind the gateway:
  - **warehouse** (read-only SQL access to the RDS warehouse via `postgres-mcp` wrapped by `supergateway`).
  - **meta-ads** (Choiz fork of `pipeboard-co/meta-ads-mcp`, Node stdio wrapped with `supergateway`). End-to-end verified from claude.ai. See [meta-ads deployed-state memory] for SHA pinning details — `Dockerfile` pins `META_ADS_SHA=6300075…` but EC2 currently runs the previous build (`fe38c9e…`) because the rebuild on 2026-04-24 OOM-killed during `npm ci`.
- End-to-end verified from Claude.ai: connector shows Connected, tool calls return results.
- Cloudflare Tunnel + Worker + gateway + MCP container stack is production-shaped (systemd, restart policies, no inbound ports, SSM-only admin).
- Pilot user: `sabruzzini@choiz.com.mx`.

## Not started / next up

Ordered by impact vs. effort. **Item 2 (CI/CD) is now a prerequisite for item 1** — see resource note below.

### 1. Migrate more MCPs off of local `claude_desktop_config.json`

Use [ADDING_AN_MCP.md](ADDING_AN_MCP.md) as the recipe.

Agreed queue (see migration-order memory):

- [x] **meta-ads** — done, except trailing tool-description deploy (image `6300075` pushed, not built on EC2).
- [ ] **facebook** (choiz + timeless tenants) — two containers per the multi-tenant pattern.
- [ ] **instagram** (choiz + timeless).
- [ ] **ga4** + **gsc** (choiz + timeless each).
- [ ] **tiktok-ads** (choiz only; ignore tiktok-organic for now).
- [ ] **google-ads**.
- [ ] **kapso** + **posthog** — already remote, but wrap through gateway to hide keys / brand as official.
- [ ] **power-bi** — special case, deferred (token expiry issue, see memory).

For each one, decide whether it belongs on the gateway:

- **Yes**: talks to a network service (APIs, databases, SaaS) and could be useful to more than one person.
- **No**: needs local filesystem access or per-user credentials that can't be centralized. Leave those in the user's desktop config.

> ⚠️ **Do CI/CD (item 2) before migrating MCP #3.** The t3.micro can't reliably build heavy Node MCPs in-place — meta-ads' rebuild on 2026-04-24 OOM-killed and took the SSM agent offline (containers stayed up on the old image via `restart: unless-stopped`, so prod didn't break, but the new image never landed). Either add a swapfile + prune the builder cache to unblock individual rebuilds, or — better — move builds off the EC2 entirely (item 2). See the EC2-resource-problem memory for the exact commands.

### 2. CI/CD with GitHub Actions

Manual `docker compose up -d --build` on the EC2 is fine now, but it won't scale once multiple people are editing this repo. Plan:

- **`deploy-gateway.yml`**: on push to `main`, if `gateway/**`, `compose.yml`, or `mcp/**` changed → GitHub Actions uses OIDC to assume an IAM role on AWS → `aws ssm send-command` to the EC2 → runs `cd ~/choiz-mcp-gateway && git pull && docker compose up -d --build`.
- **`deploy-worker.yml`**: on push to `main`, if `worker/**` changed → `wrangler deploy` using a `CLOUDFLARE_API_TOKEN` repo secret.

Both run on GitHub-hosted runners (no self-hosted runner to maintain).

Prerequisites:

- Create an IAM OIDC provider for GitHub in the AWS account.
- Create an IAM role `choiz-mcp-gateway-ci` with a trust policy scoped to the repo + branch, and permissions limited to `ssm:SendCommand` on the specific instance.
- Mint a Cloudflare API token with `Workers Scripts: Edit` + `Workers KV Storage: Edit` on the zone `choiz.com.mx`. Store as repo secret `CLOUDFLARE_API_TOKEN`.

### 3. Team rollout

Once at least 3-4 useful MCPs are on the gateway:

- Write a short internal doc (Notion page) with screenshots of:
  - Claude.ai Settings → Connectors → Add custom connector.
  - The URL format.
  - The Google sign-in step.
  - What each connector does and what data it can see.
- Pilot with 2-3 early adopters from the data team for a week.
- Open rollout to the 15 people on Team plan.

### 4. Nice-to-have hardening

Not blocking, but worth doing eventually:

- **Dedicated Postgres role**: create `mcp_gateway_ro` (instead of reusing `warehousereadonly`), tagged with `application_name = 'mcp_gateway'` so audit logs can separate MCP traffic from everything else.
- **Per-user query attribution**: have the gateway inject `SET application_name = '<user_email>'` before each connection. Requires switching from `postgres-mcp` to a fork or a small shim; not trivial.
- **Structured logs**: the gateway currently `console.log`s; pipe to Cloudwatch via the SSM agent or a sidecar for searchable history.
- **Rate limiting**: a user hammering the warehouse via Claude.ai could DoS it. Add a per-user token bucket in the Worker (Cloudflare has durable objects for this).
- **Secondary maintainer**: document who the backup maintainer is so this doesn't single-thread on one person.

### 5. Known pending decisions

- Which Postgres role does `warehouse_mcp` use? (currently: whatever `WAREHOUSE_DATABASE_URL` in the EC2 `.env` points to — check it's read-only).
- Do we want to retain MCP call audit logs beyond the Worker's 100-event in-memory buffer?
- Do we enable Cloudflare Access on top of OAuth for extra defense in depth? (adds friction, but gets us SSO-aware network ACLs).

## Glossary

- **MCP** — Model Context Protocol. The protocol Claude uses to call external tools and fetch data.
- **DCR** — Dynamic Client Registration. OAuth extension that lets Claude.ai register itself with our Worker without us creating a client by hand.
- **Streamable HTTP transport** — The MCP transport Claude.ai uses. Single HTTP endpoint, POST for messages, optional GET for server-initiated streams. Introduced in the 2025-03-26 MCP spec.
- **SSE transport** — Older MCP transport. Two endpoints: GET `/sse` (long-lived stream) + POST `/messages/?session_id=X`. We only use it inside the container; `supergateway` translates it.
- **supergateway** — A Node CLI by supercorp that wraps an MCP server in a chosen transport. We use it to expose stdio MCPs as Streamable HTTP.
- **Worker** — A Cloudflare serverless function. Runs JS at the edge in every Cloudflare datacenter. Our Worker handles OAuth and proxies `/mcp/*` to the tunnel.
- **Tunnel** — Cloudflare's outbound-only reverse proxy. The `cloudflared` daemon on the EC2 opens an outbound connection to Cloudflare; incoming traffic is routed over that connection. No inbound ports needed.
- **SSM** — AWS Systems Manager Session Manager. IAM-authenticated shell into EC2, no SSH keys, no open port 22.
