# Current state + backlog

Snapshot of where this project is and what's left to do. Update this file whenever you close or open a work item, so a future reader (human or AI) can pick it up without re-reading commit history.

## What works today

- Full OAuth flow: Claude.ai ↔ Worker ↔ Google Workspace ↔ `@choiz.com.mx` users.
- Four MCPs behind the gateway (google-ads disabled — see below):
  - **warehouse** (read-only SQL access to the RDS warehouse via `postgres-mcp` wrapped by `supergateway`).
  - **meta-ads** (Choiz fork of `pipeboard-co/meta-ads-mcp`, Node stdio wrapped with `supergateway`). End-to-end verified from claude.ai. Running SHA `6300075` since 2026-04-26 via CI/CD.
  - **facebook** (Choiz fork of `HagaiHen/facebook-mcp-server`, Python stdio wrapped with `supergateway`). First multi-tenant MCP: one image, two containers (`facebook_choiz_mcp` + `facebook_timeless_mcp`) differing only in `FACEBOOK_ACCESS_TOKEN` + `FACEBOOK_PAGE_ID`. End-to-end verified from claude.ai. Running SHA `6418c71` since 2026-04-27 via CI/CD.
  - **instagram** (Choiz fork of `jlbadano/ig-mcp`, Python stdio wrapped with `supergateway`). Multi-tenant: `instagram_choiz_mcp` + `instagram_timeless_mcp`, sharing `FACEBOOK_APP_ID/SECRET`. End-to-end verified from claude.ai. Running SHA `1c01c4d` since 2026-04-27 via CI/CD.
  - **google-ads** — DISABLED 2026-04-28. The container backend works (smoke-tested via curl on EC2; fork pinned at `6fefe68` returns valid responses under 2 KB). The blocker is at the network layer: any claude.ai interaction with this MCP triggers a cloudflared tunnel zombie within seconds, which takes down all the other MCPs. Empirically reproduced on 2026-04-28: stack stable as long as google-ads is not invoked, breaks within ~10 s of the first claude.ai call. Image, env vars, and Dockerfile retained; service block in compose.yml is commented out pending a stateful supergateway migration or relocation to a separate host. See `project_google_ads_destabilizes_host_CONFIRMED` memory and the comment block in compose.yml.
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
- [x] **facebook** (choiz + timeless tenants) — done 2026-04-27. First multi-tenant MCP. Choizapp/choiz-facebook-mcp pinned at `6418c71`.
- [x] **instagram** (choiz + timeless) — done 2026-04-27. Choizapp/choiz-instagram-mcp pinned at `1c01c4d`. Surfaced a new failure mode: claude.ai rejects streamable-http tool results above ~2-3 KB ("Error occurred during tool execution" with no server-side trace). Fix shipped in the fork: strip CDN URL query strings + compact JSON.
- [~] **google-ads** — backend ready (fork `6fefe68`), DISABLED in production 2026-04-28. Triggers a cloudflared tunnel zombie within seconds of any claude.ai call, taking down the rest of the stack. Backend smoke-test via curl on EC2 returns clean <2 KB responses with real data; the failure is at the network layer between this container and the gateway tunnel. Re-enable requires either switching supergateway to stateful mode or moving the container to a separate host. See `project_google_ads_destabilizes_host_CONFIRMED` memory.
- [ ] **ga4** + **gsc** (choiz + timeless each).
- [ ] **kapso** + **posthog** — already remote, but wrap through gateway to hide keys / brand as official.
- [ ] **tiktok-ads** (choiz only; ignore tiktok-organic for now).
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

### 2.5. Pre-GA4 structural hardening (decide before continuing the queue)

The google-ads migration on 2026-04-28 surfaced three patterns that will
recur on GA4, posthog, kapso, and tiktok-ads. Patching each fork ad-hoc
doesn't scale. Open before resuming the queue:

- **(A) Response shrinker in the gateway / Worker**. A generic layer that
  intercepts MCP tool results and truncates / paginates anything over
  ~2 KB before it reaches claude.ai. Removes the per-fork "compact format"
  patch loop entirely. ~1 day of work. Highest-leverage of the three.
- **(B) Replace `supergateway --stateless` with stateful or with native
  FastMCP HTTP transport**. The stateless mode respawns the python child
  per request, which negates module-level caches (e.g. `GoogleAdsClient`)
  and drives ~50-90 MB of RSS growth per call. Affects every Python MCP
  with non-trivial imports. ~half a day per MCP, validated case by case.
- **(C) EC2 disk 8 GB → 30 GB + auto-prune dangling images in CI**. The
  google-ads deploy hit 87% disk and an ipc-timeout on `compose pull`
  because the pre-existing `:latest` images held by old containers don't
  prune until `compose up -d` recreates them. Brittle. Cheap operational
  fix; should be done before GA4 + Kapso land.

Recommended order: **(C) → (A) → (B)**. (C) is operational/cheap; (A) is
the highest-leverage technical change; (B) is the deepest but only worth
it after (A) has reduced the urgency of payload bloat in individual forks.

A pre-deploy checklist also belongs in `ADDING_AN_MCP.md`: measure each
tool's worst-case payload in CI and assert <2 KB; set `mem_limit` +
`pids_limit` per service from day one.

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
