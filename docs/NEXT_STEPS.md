# Current state + backlog

Snapshot of where this project is and what's left to do. Update this file whenever you close or open a work item, so a future reader (human or AI) can pick it up without re-reading commit history.

## What works today

- Full OAuth flow: Claude.ai ↔ Worker ↔ Google Workspace ↔ `@choiz.com.mx` users.
- Seven MCPs behind the gateway (google-ads disabled and DROPPED from queue — see below):
  - **warehouse** (read-only SQL access to the RDS warehouse via `postgres-mcp` wrapped by `supergateway`).
  - **meta-ads** (Choiz fork of `pipeboard-co/meta-ads-mcp`, Node stdio wrapped with `supergateway`). End-to-end verified from claude.ai. Running SHA `6300075` since 2026-04-26 via CI/CD.
  - **facebook** (Choiz fork of `HagaiHen/facebook-mcp-server`, Python stdio wrapped with `supergateway`). First multi-tenant MCP: one image, two containers (`facebook_choiz_mcp` + `facebook_timeless_mcp`) differing only in `FACEBOOK_ACCESS_TOKEN` + `FACEBOOK_PAGE_ID`. End-to-end verified from claude.ai. Running SHA `6418c71` since 2026-04-27 via CI/CD.
  - **instagram** (Choiz fork of `jlbadano/ig-mcp`, Python stdio wrapped with `supergateway`). Multi-tenant: `instagram_choiz_mcp` + `instagram_timeless_mcp`, sharing `FACEBOOK_APP_ID/SECRET`. End-to-end verified from claude.ai. Running SHA `1c01c4d` since 2026-04-27 via CI/CD.
  - **ga4** (Choiz fork at `Choizapp/choiz-ga4-mcp@a0278ff`, FastMCP wrapper around `google-analytics-data v1beta`). Multi-tenant: `ga4_choiz_mcp` (property 337268679) + `ga4_timeless_mcp` (property 507460155), sharing `GA4_SERVICE_ACCOUNT_JSON_B64` (b64 of the reader service-account, decoded by container entrypoint). First MCP with service-account-JSON injection via env. End-to-end verified from claude.ai 2026-04-29. Stateless supergateway after `--stateful` was tried and tripped issue #126. See `project_ga4_deployed_state` and `project_supergateway_stateful_bug126`.
  - **kapso** (remote-proxy) — first MCP wrapped without a container. Gateway proxies `mcp.choiz.com.mx/mcp/kapso/` → `https://app.kapso.ai/mcp` and injects `x-api-key` per-request. claude.ai never sees the key. End-to-end smoke from EC2 (initialize + tools/list + project_info call) passed 2026-04-29 late. Pattern documented in `project_remote_proxy_pattern` memory + the `remoteUpstreams` table in `gateway/src/index.ts`.
  - **google-ads** — DISABLED 2026-04-28 + DROPPED FROM QUEUE 2026-04-29. The container backend works (smoke-tested via curl on EC2; fork pinned at `6fefe68` returns valid responses under 2 KB). Two compounding blockers: (a) under `--stateless`, gRPC connection storm destabilizes the cloudflared tunnel; (b) under `--stateful`, supergateway issue #126 SIGTERMs the child after the first call. Re-enabling requires a deeper fix (native FastMCP HTTP transport, separate host, or a different transport entirely). Image, env vars, and Dockerfile retained for if/when that work happens.

### Operational hardening shipped 2026-04-29

- **EBS root grown 8 GB → 30 GB** (online, no downtime). New `resize-volume.yml` workflow + `EbsResizeGatewayRoot` inline IAM policy. Disk now ~28% used. See `project_ebs_resize_2026-04-29`.
- **monitor-tunnel auto-reboot escalation**. When the SSM-based cloudflared restart returns anything other than `Success` (host fully hung), the workflow now calls `aws ec2 reboot-instances` via the hypervisor. New `EC2InstancePowerManagement` inline IAM policy. The "ops" GitHub label was created so the ops-issue creation actually fires. See `project_tunnel_zombie_continuation_2026-04-29`.
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
- [~] **google-ads** — DROPPED from the queue 2026-04-29. Backend kept (fork `6fefe68`, image in GHCR, env vars in `.env`) but service stays commented in `compose.yml`. Re-enable requires a transport rethink (native FastMCP HTTP, separate host, etc.) — out of scope for this migration pass.
- [x] **ga4** (choiz + timeless) — done 2026-04-29. Choizapp/choiz-ga4-mcp pinned at `a0278ff`. First MCP with service-account JSON injection via env (b64 + custom entrypoint). Surfaced two new failure modes: supergateway `--stateful` trips bug #126 under claude.ai, AND default FastMCP indent=2 + permissive limits bust the payload ceiling. Both fixed in fork (compact JSON, lower default limit, slim schema tools) and Dockerfile (drop `--stateful`).
- [x] **gsc** (choiz + timeless) — done 2026-04-29. Public package `mcp-server-gsc`, no Choiz fork. Reuses GA4 service-account JSON via compose alias. Two identical containers; tenant separation purely slug-based.
- [x] **kapso** — done 2026-04-29 late. First **remote-proxy** MCP: gateway-only proxy to `https://app.kapso.ai/mcp` with `X-API-Key` injection. NO container, NO Dockerfile, NO CI build job; only `gateway/src/index.ts` + `compose.yml` env + `.env`. See `project_remote_proxy_pattern` and `project_kapso_deployed_state` memories. **Split per-project 2026-05-22**: Kapso API keys are project-scoped, so `/mcp/kapso` (OTP) was retired and replaced with one slug per project — `kapso-choiz-sales`, `kapso-choiz-support`, `kapso-timeless-sales`, `kapso-timeless-support`.
- [~] **posthog** — DROPPED. Anthropic shipped a native PostHog connector in claude.ai; no need to wrap.
- [~] **tiktok-ads** — DEFERRED. Choiz is not currently running paid TikTok ads, so the migration is parked until that activity resumes.
- [ ] **tiktok-organic** — possible candidate. Status of the local config unknown (Santi unsure if it currently works). Verify before queueing.
- [ ] **power-bi** — Santi flagged this as a priority on 2026-04-29 EOD: he wants Power BI usable from a Claude Code routine without Power BI Desktop being open. The `powerbi-modeling-mcp` we use today is local-bound (talks to localhost XMLA on Desktop). The right path is service-principal + Power BI Service XMLA endpoint (headless, server-side refresh token). Not a 1-hour task; needs an MCP that can target the Service endpoint, plus an Azure AD app registration. Research pending.
- [ ] **google-ads** — last and conditional. Two compounding blockers (stateless tunnel storm + stateful supergateway #126). If revisited, **evaluate swapping the fork for a different google-ads MCP** rather than fighting the same one.

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

### 2.5. Pre-GA4 structural hardening — STATUS as of 2026-04-29 EOD

The three items in the original plan resolved like this:

- **(C) EC2 disk 8 GB → 30 GB** ✅ done 2026-04-29 (commit `14055a8`).
  Online resize via the new `resize-volume.yml` workflow + scoped IAM
  policy. Disk now ~28% used. See `project_ebs_resize_2026-04-29` memory.
- **(A) Response shrinker** — NOT built as a generic gateway layer.
  Instead each fork ships compact JSON output (the ga4 fork added a
  `_pack()` helper that does `json.dumps(..., separators=(',', ':'))`).
  Acceptable for the queue. If patching the same shape into N more
  forks gets tedious, revisit the generic layer.
- **(B) supergateway stateful migration** — ATTEMPTED, FAILED, ABANDONED.
  Stateful trips supergateway issue #126 under claude.ai's two-stream
  pattern (SSE conflict → SIGTERM child). See
  `project_supergateway_stateful_bug126` memory. The next-level fix
  is migrating each MCP server to native FastMCP HTTP transport (drop
  supergateway entirely). Not done; deferred to a calmer day.

Additionally, NOT in the original plan but shipped today as durable
hardening:

- **monitor-tunnel auto-reboot** — when SSM-based cloudflared restart
  fails (host hung), escalate to `ec2:RebootInstances` via the
  hypervisor. New `EC2InstancePowerManagement` inline policy. The
  manual-reboot fire-drill is now automated. See
  `project_tunnel_zombie_continuation_2026-04-29`.

Pre-deploy checklist for new MCPs (folded into `ADDING_AN_MCP.md` —
update if anything below is missing):
- Stay on default `--stateless` until issue #126 is closed upstream.
- Compact JSON output (`json.dumps(..., separators=(',', ':'))`) on
  every tool return.
- Default tool `limit` parameters small (≤10 for query-style tools).
- `mem_limit` + `pids_limit` per service from day one.
- Smoke test under load FROM the EC2 (curl) before connecting from
  claude.ai. Watch tunnel `/healthz` externally for ~10 min after.

### 3. Nice-to-have hardening

Not blocking, but worth doing eventually:

- **Dedicated Postgres role**: create `mcp_gateway_ro` (instead of reusing `warehousereadonly`), tagged with `application_name = 'mcp_gateway'` so audit logs can separate MCP traffic from everything else.
- **Per-user query attribution**: have the gateway inject `SET application_name = '<user_email>'` before each connection. Requires switching from `postgres-mcp` to a fork or a small shim; not trivial.
- **Structured logs**: the gateway currently `console.log`s; pipe to Cloudwatch via the SSM agent or a sidecar for searchable history.
- **Rate limiting**: a user hammering the warehouse via Claude.ai could DoS it. Add a per-user token bucket in the Worker (Cloudflare has durable objects for this).
- **Slim down the EC2 repo clone**: now that CI/CD pushes `compose.yml` via SSM, the rest of the repo on EC2 (`gateway/`, `mcp/`, `docs/`, etc.) is unused at runtime. Could be pruned to just `compose.yml` + `.env` for clarity.
- **Image rollback ergonomics**: `:latest` is convenient but rollbacks require knowing the old SHA. Could add a small script that lists recent SHAs in GHCR and pins one in `.env` via `IMAGE_TAG`.
- **Secondary maintainer**: document who the backup maintainer is so this doesn't single-thread on one person.
- **Cloudflare blast-radius hardening** (opened 2026-06-26 after the worker-wipe outage): the `CLOUDFLARE_API_TOKEN` used by CI has `Workers Scripts:Edit`, which can also **delete** the Worker — a single leaked/misused token (or dashboard access) can take down every connector, as happened on 2026-06-26 (Worker deleted manually, no GH/CI trace; cause never attributed). To do: (a) review who has Cloudflare account access and which API tokens exist + their scopes; (b) consider splitting a tighter deploy token (or Workers "deploy-only" where possible); (c) document in `OPERATIONS.md` that the master copy of every Worker secret lives in **GitHub Actions Secrets + Vaultwarden** (Cloudflare and Vercel "Sensitive" vars are NOT readable back). See `project_worker_route_detach_2026-06-26` memory.

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
