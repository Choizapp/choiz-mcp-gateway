# Current state + backlog

Snapshot of where this project is and what's left to do. Update this file whenever you close or open a work item, so a future reader (human or AI) can pick it up without re-reading commit history.

## What works today

- Full OAuth flow: Claude.ai ↔ Worker ↔ Google Workspace ↔ `@choiz.com.mx` users.
- Three MCPs behind the gateway:
  - **warehouse** (read-only SQL access to the RDS warehouse via `postgres-mcp` wrapped by `supergateway`).
  - **meta-ads** (Choiz fork of `pipeboard-co/meta-ads-mcp`, Node stdio wrapped with `supergateway`). End-to-end verified from claude.ai. Running SHA `6300075` since 2026-04-26 via CI/CD.
  - **facebook** (Choiz fork of `HagaiHen/facebook-mcp-server`, Python stdio wrapped with `supergateway`). First multi-tenant MCP: one image, two containers (`facebook_choiz_mcp` + `facebook_timeless_mcp`) differing only in `FACEBOOK_ACCESS_TOKEN` + `FACEBOOK_PAGE_ID`. End-to-end verified from claude.ai. Running SHA `affa2b9` since 2026-04-27 via CI/CD.
- End-to-end verified from Claude.ai: connector shows Connected, tool calls return results.
- Cloudflare Tunnel + Worker + gateway + MCP container stack is production-shaped (systemd, restart policies, no inbound ports, SSM-only admin).
- **CI/CD live** (deployed 2026-04-26). GitHub Actions builds ARM64 images → pushes to GHCR → SSM pushes `compose.yml` to EC2 + `docker compose pull && up -d`. EC2 no longer builds anything. See [CICD.md](CICD.md).
- Pilot user: `sabruzzini@choiz.com.mx`.

## Not started / next up

Ordered by impact vs. effort.

### 1. Migrate more MCPs off of local `claude_desktop_config.json`

Use [ADDING_AN_MCP.md](ADDING_AN_MCP.md) as the recipe. With CI/CD live, adding a new MCP is now: write a Dockerfile, add a service to `compose.yml`, add a `build-<name>` job to `.github/workflows/deploy-gateway.yml`, push. No on-EC2 work.

Agreed queue (see migration-order memory):

- [x] **meta-ads** — done end-to-end, including the trailing tool-description deploy.
- [x] **facebook** (choiz + timeless tenants) — done 2026-04-27. First multi-tenant MCP. Choizapp/choiz-facebook-mcp pinned at `affa2b9`.
- [ ] **instagram** (choiz + timeless) — repeats the facebook recipe (same multi-tenant pattern, Python stdio, supergateway wrapper).
- [ ] **ga4** + **gsc** (choiz + timeless each).
- [ ] **tiktok-ads** (choiz only; ignore tiktok-organic for now).
- [ ] **google-ads**.
- [ ] **kapso** + **posthog** — already remote, but wrap through gateway to hide keys / brand as official.
- [ ] **power-bi** — special case, deferred (token expiry issue, see memory).

For each one, decide whether it belongs on the gateway:

- **Yes**: talks to a network service (APIs, databases, SaaS) and could be useful to more than one person.
- **No**: needs local filesystem access or per-user credentials that can't be centralized. Leave those in the user's desktop config.

### 2. Team rollout

Once at least 3-4 useful MCPs are on the gateway:

- Write a short internal doc (Notion page) with screenshots of:
  - Claude.ai Settings → Connectors → Add custom connector.
  - The URL format.
  - The Google sign-in step.
  - What each connector does and what data it can see.
- Pilot with 2-3 early adopters from the data team for a week.
- Open rollout to the 15 people on Team plan.

### 3. Nice-to-have hardening

Not blocking, but worth doing eventually:

- **Dedicated Postgres role**: create `mcp_gateway_ro` (instead of reusing `warehousereadonly`), tagged with `application_name = 'mcp_gateway'` so audit logs can separate MCP traffic from everything else.
- **Per-user query attribution**: have the gateway inject `SET application_name = '<user_email>'` before each connection. Requires switching from `postgres-mcp` to a fork or a small shim; not trivial.
- **Structured logs**: the gateway currently `console.log`s; pipe to Cloudwatch via the SSM agent or a sidecar for searchable history.
- **Rate limiting**: a user hammering the warehouse via Claude.ai could DoS it. Add a per-user token bucket in the Worker (Cloudflare has durable objects for this).
- **Slim down the EC2 repo clone**: now that CI/CD pushes `compose.yml` via SSM, the rest of the repo on EC2 (`gateway/`, `mcp/`, `docs/`, etc.) is unused at runtime. Could be pruned to just `compose.yml` + `.env` for clarity.
- **Image rollback ergonomics**: `:latest` is convenient but rollbacks require knowing the old SHA. Could add a small script that lists recent SHAs in GHCR and pins one in `.env` via `IMAGE_TAG`.
- **Secondary maintainer**: document who the backup maintainer is so this doesn't single-thread on one person.

### 4. Known pending decisions

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
- **GHCR** — GitHub Container Registry (`ghcr.io`). Where CI/CD pushes built images, scoped to the `Choizapp` org.
- **OIDC** (in CI context) — OpenID Connect. Lets GitHub Actions assume an AWS IAM role without long-lived AWS keys.
