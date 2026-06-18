import express from "express";
import { createProxyMiddleware } from "http-proxy-middleware";
import { authenticate, workerSecretOk } from "./auth.js";
import { runReadOnlyQuery } from "./warehouse.js";

const app = express();

// --- Routing table: path prefix -> upstream MCP URL (internal containers) ---
// Add a new entry here when you add a new MCP container to compose.yml.
const upstreams: Record<string, string | undefined> = {
  "/mcp/warehouse":         process.env.UPSTREAM_WAREHOUSE,
  "/mcp/meta-ads":          process.env.UPSTREAM_META_ADS,
  "/mcp/facebook-choiz":    process.env.UPSTREAM_FACEBOOK_CHOIZ,
  "/mcp/facebook-timeless": process.env.UPSTREAM_FACEBOOK_TIMELESS,
  "/mcp/instagram-choiz":    process.env.UPSTREAM_INSTAGRAM_CHOIZ,
  "/mcp/instagram-timeless": process.env.UPSTREAM_INSTAGRAM_TIMELESS,
  "/mcp/ga4-choiz":          process.env.UPSTREAM_GA4_CHOIZ,
  "/mcp/ga4-timeless":       process.env.UPSTREAM_GA4_TIMELESS,
  "/mcp/gsc-choiz":          process.env.UPSTREAM_GSC_CHOIZ,
  "/mcp/gsc-timeless":       process.env.UPSTREAM_GSC_TIMELESS,
  "/mcp/shopify-choiz":      process.env.UPSTREAM_SHOPIFY_CHOIZ,
  "/mcp/shopify-timeless":   process.env.UPSTREAM_SHOPIFY_TIMELESS,
  // google-ads re-enabled 2026-05-07 with official googleads/google-ads-mcp
  // (PR #15). The original 2026-04-28 disable was due to supergateway
  // --stateless gRPC respawn-storms; the official MCP runs in-process under
  // FastMCP so the storm pattern is structurally gone.
  "/mcp/google-ads":         process.env.UPSTREAM_GOOGLE_ADS,
  // Power BI MCP — per-brand slug, matches ga4 / gsc / facebook / instagram /
  // shopify multi-tenant pattern. One container per brand pinned to a single
  // dataset; tools have no `dataset` argument because the route IS the brand.
  // The previous single `/mcp/powerbi` route was retired on 2026-05-19.
  "/mcp/powerbi-choiz":      process.env.UPSTREAM_POWERBI_CHOIZ,
  "/mcp/powerbi-timeless":   process.env.UPSTREAM_POWERBI_TIMELESS,
  // TikTok Organic MCP — read-only access to a brand's TikTok account
  // (Display API v2 owned-content). Server-side OAuth via long-lived
  // refresh_token; end users only see Google Workspace login. Slug-by-brand
  // for the same reason as powerbi (avoids LLM picking the wrong tenant).
  // Choiz only at launch; Timeless slot pending Dev Portal app + tokens.
  "/mcp/tiktok-organic-choiz": process.env.UPSTREAM_TIKTOK_ORGANIC_CHOIZ,
  // DHL Express MCP — single tenant. Tracking (read) + label/return generation
  // (WRITE) via the MyDHL API. The only write-capable MCP in the gateway; the
  // /mcp auth guard above is its sole access barrier. Server-side HTTP Basic
  // auth to DHL; end users only see Google Workspace login.
  "/mcp/dhl":                  process.env.UPSTREAM_DHL,
  // Google Sheets MCP — read + WRITE. Single slug, single container:
  // Sheets are not brand-scoped the way GA4 properties / Power BI datasets
  // are, so there is no choiz/timeless split. Access is bounded by which
  // spreadsheets are shared with the sheets-editor service account, plus
  // the /mcp auth guard above. Write-capable like dhl (see note above);
  // unlike dhl the blast radius is "any Sheet shared with the SA", so keep
  // the SA's sharing surface tight.
  "/mcp/sheets":               process.env.UPSTREAM_SHEETS,
  // Viral Loops MCP — single tenant, READ-ONLY. Custom FastMCP server
  // (mcp/viral-loops/entrypoint.py) wrapping the Viral Loops Web API v3 with a
  // per-campaign apiToken. The campaign IS the token, so there is no campaignId
  // argument on any tool. Only GET endpoints are exposed.
  "/mcp/viral-loops":          process.env.UPSTREAM_VIRAL_LOOPS,
};

// --- Remote MCPs proxied through the gateway (no internal container) ---
// These vendors already serve Streamable HTTP. We proxy through the gateway
// to (a) hide the upstream API key from claude.ai and (b) brand the URL under
// mcp.choiz.com.mx. See memory project_wrap_remote_mcps.
//
// To add one: register here, set <APIKEY_ENV> in the EC2 .env. No Dockerfile,
// no compose service, no CI build job.
interface RemoteUpstream {
  target: string;       // https URL the proxy forwards to (origin + base path)
  apiKeyHeader: string; // header name to set on the outgoing request
  // Credential source — provide exactly ONE of:
  apiKeyEnv?: string;   // (a) env var whose value is used as the header verbatim
  // (b) build a Basic-style credential from a username + secret pair:
  basicUserEnv?: string;   //     env var holding the username
  basicSecretEnv?: string; //     env var holding the secret
  valuePrefix?: string;    //     header value = `${valuePrefix}${base64(user:secret)}`
}

// Resolve the outgoing auth header VALUE for a remote upstream, or undefined if
// its credential env var(s) are not set (route is then skipped). Supports both
// the verbatim single-env case (kapso) and the username+secret Basic build
// (Mixpanel) without baking a pre-formatted blob into .env.
function resolveRemoteAuthValue(cfg: RemoteUpstream): string | undefined {
  if (cfg.apiKeyEnv) return process.env[cfg.apiKeyEnv] || undefined;
  if (cfg.basicUserEnv && cfg.basicSecretEnv) {
    const user = process.env[cfg.basicUserEnv];
    const secret = process.env[cfg.basicSecretEnv];
    if (!user || !secret) return undefined;
    return (cfg.valuePrefix ?? "") + Buffer.from(`${user}:${secret}`).toString("base64");
  }
  return undefined;
}
const remoteUpstreams: Record<string, RemoteUpstream> = {
  // Kapso is project-scoped: one API key = one project. We expose one slug
  // per project (choiz/timeless × sales/support). The OTP project that
  // originally backed `/mcp/kapso` was retired 2026-05-22.
  "/mcp/kapso-choiz-sales": {
    target: "https://app.kapso.ai/mcp",
    apiKeyEnv: "KAPSO_API_KEY_CHOIZ_SALES",
    apiKeyHeader: "x-api-key",
  },
  "/mcp/kapso-choiz-support": {
    target: "https://app.kapso.ai/mcp",
    apiKeyEnv: "KAPSO_API_KEY_CHOIZ_SUPPORT",
    apiKeyHeader: "x-api-key",
  },
  "/mcp/kapso-timeless-sales": {
    target: "https://app.kapso.ai/mcp",
    apiKeyEnv: "KAPSO_API_KEY_TIMELESS_SALES",
    apiKeyHeader: "x-api-key",
  },
  "/mcp/kapso-timeless-support": {
    target: "https://app.kapso.ai/mcp",
    apiKeyEnv: "KAPSO_API_KEY_TIMELESS_SUPPORT",
    apiKeyHeader: "x-api-key",
  },
  // Mixpanel hosts an official Streamable HTTP MCP. Headless auth is a service
  // account sent as the (unusual) literal header
  // `Authorization: Bearer Basic <base64(username:secret)>` — note the
  // "Bearer Basic " prefix (verified against the live API 2026-06-18). We keep
  // the SA username + secret as two readable env vars and build that value here
  // (see resolveRemoteAuthValue), so rotating the secret is a one-value change.
  // MIXPANEL_API_KEY = SA username (…mp-service-account); MIXPANEL_API_SECRET =
  // its secret. Single project/tenant; no per-brand split.
  "/mcp/mixpanel": {
    target: "https://mcp.mixpanel.com/mcp",
    apiKeyHeader: "authorization",
    basicUserEnv: "MIXPANEL_API_KEY",
    basicSecretEnv: "MIXPANEL_API_SECRET",
    valuePrefix: "Bearer Basic ",
  },
};

// --- Health check (no auth) ---
app.get("/healthz", (_req, res) => {
  res.json({
    ok: true,
    routes: [
      ...Object.keys(upstreams).filter((k) => upstreams[k]),
      ...Object.keys(remoteUpstreams).filter(
        (k) => resolveRemoteAuthValue(remoteUpstreams[k]),
      ),
    ],
  });
});

// --- Dashboard JSON query endpoint (machine-credentialed, NOT under /mcp) ---
// The kapso-ops-dashboard (Vercel) reads the analytic `a.*` views over HTTPS
// here instead of opening a socket to the private RDS. The Cloudflare Worker
// validates the dashboard's API key and only then forwards the request with the
// shared secret; we re-check the secret (same trust model as /dl/dhl) and run
// the SQL read-only. Returns clean JSON: { rows: [...] }. Registered OUTSIDE the
// /mcp guard because this caller is a machine, not a Google-authenticated user.
const READ_ONLY_PREFIX = /^\s*(select|with|explain|table|values)\b/i;
app.post(
  "/api/warehouse/query",
  express.json({ limit: "64kb" }),
  async (req, res) => {
    if (!workerSecretOk(req)) {
      res.status(401).json({ error: "unauthorized" });
      return;
    }
    const body = (req.body ?? {}) as { sql?: unknown; params?: unknown };
    const sql = body.sql;
    const params = body.params ?? [];
    if (typeof sql !== "string" || !sql.trim()) {
      res.status(400).json({ error: "missing_sql" });
      return;
    }
    if (!Array.isArray(params)) {
      res.status(400).json({ error: "params_must_be_array" });
      return;
    }
    if (!READ_ONLY_PREFIX.test(sql)) {
      // Defense-in-depth: the READ ONLY transaction already blocks writes, but
      // we refuse anything that isn't obviously a read before it hits the DB.
      res.status(400).json({ error: "only_read_queries_allowed" });
      return;
    }
    try {
      const rows = await runReadOnlyQuery(sql, params);
      res.json({ rows });
    } catch (err) {
      // Never log SQL params or row contents (PII). Message + tag only.
      console.error("[api/warehouse/query] failed:", (err as Error).message);
      res.status(500).json({ error: "query_failed" });
    }
  },
);

// --- Auth guard for everything under /mcp ---
app.use("/mcp", (req, res, next) => {
  const user = authenticate(req);
  if (!user) {
    res.status(401).json({ error: "unauthorized" });
    return;
  }
  // Forward identity to upstream so MCPs can audit per-user if they care.
  req.headers["x-mcp-user"] = user.email;
  next();
});

// --- Wire each prefix to its upstream (internal containers) ---
for (const [prefix, target] of Object.entries(upstreams)) {
  if (!target) continue;
  app.use(
    prefix,
    createProxyMiddleware({
      target,
      changeOrigin: true,
      // Strip the prefix so the upstream sees the path it expects (e.g. /sse).
      pathRewrite: { [`^${prefix}`]: "" },
      // Preserve streaming (SSE, chunked responses).
      selfHandleResponse: false,
      on: {
        error: (err, _req, res) => {
          console.error(`[proxy:${prefix}] upstream error`, err);
          if (res && "writeHead" in res && !res.headersSent) {
            res.writeHead(502, { "Content-Type": "application/json" });
            res.end(JSON.stringify({ error: "upstream_unavailable" }));
          }
        },
      },
    }),
  );
}

// --- Wire remote-proxied MCPs (HTTP→HTTPS with API-key injection) ---
for (const [prefix, cfg] of Object.entries(remoteUpstreams)) {
  const apiKey = resolveRemoteAuthValue(cfg);
  if (!apiKey) {
    const envNames = cfg.apiKeyEnv ?? `${cfg.basicUserEnv}+${cfg.basicSecretEnv}`;
    console.warn(`[mcp-gateway] skipping remote ${prefix}: ${envNames} not set`);
    continue;
  }
  app.use(
    prefix,
    createProxyMiddleware({
      target: cfg.target,
      changeOrigin: true,
      selfHandleResponse: false,
      on: {
        proxyReq: (proxyReq) => {
          // Inject the upstream auth — claude.ai never sees this header.
          proxyReq.setHeader(cfg.apiKeyHeader, apiKey);
          // Strip our internal forwarding headers so they never leak upstream.
          proxyReq.removeHeader("x-worker-shared-secret");
          proxyReq.removeHeader("x-choiz-user-email");
          proxyReq.removeHeader("x-mcp-user");
        },
        error: (err, _req, res) => {
          console.error(`[proxy:${prefix}] upstream error`, err);
          if (res && "writeHead" in res && !res.headersSent) {
            res.writeHead(502, { "Content-Type": "application/json" });
            res.end(JSON.stringify({ error: "upstream_unavailable" }));
          }
        },
      },
    }),
  );
}

// --- DHL label download (public capability URL, NOT under /mcp auth) ---
// Browser GET mcp.choiz.com.mx/dl/dhl/<token> -> Worker (DefaultHandler, no
// bearer) -> here. The unguessable token in the path is the authorization; we
// only verify the request came from the Worker (shared secret), then proxy to
// the dhl MCP container's GET /download/<token>. Registered OUTSIDE the /mcp
// guard above because browsers carry no user email / bearer.
const dhlDownloadTarget = process.env.UPSTREAM_DHL;
if (dhlDownloadTarget) {
  app.use(
    "/dl/dhl",
    (req, res, next) => {
      if (!workerSecretOk(req)) {
        res.status(401).json({ error: "unauthorized" });
        return;
      }
      next();
    },
    createProxyMiddleware({
      target: dhlDownloadTarget,
      changeOrigin: true,
      // Express strips the "/dl/dhl" mount prefix before the proxy runs, so
      // pathRewrite sees the already-stripped "/<token>". Rewrite the leading
      // slash to "/download/" so the MCP receives /download/<token>.
      // (A "^/dl/dhl" rewrite would be a no-op — the prefix is already gone.)
      pathRewrite: { "^/": "/download/" },
      selfHandleResponse: false,
      on: {
        proxyReq: (proxyReq) => {
          // Never leak our internal headers to the container.
          proxyReq.removeHeader("x-worker-shared-secret");
          proxyReq.removeHeader("x-choiz-user-email");
          proxyReq.removeHeader("x-mcp-user");
        },
        error: (err, _req, res) => {
          console.error("[proxy:/dl/dhl] upstream error", err);
          if (res && "writeHead" in res && !res.headersSent) {
            res.writeHead(502, { "Content-Type": "application/json" });
            res.end(JSON.stringify({ error: "upstream_unavailable" }));
          }
        },
      },
    }),
  );
}

// --- Viral Loops export download (public capability URL, NOT under /mcp) ---
// Same trust model as /dl/dhl: browser GET mcp.choiz.com.mx/dl/viral-loops/<token>
// -> Worker (no bearer) -> here. The unguessable token is the authorization; we
// only verify the request came from the Worker (shared secret), then proxy to
// the viral-loops MCP container's GET /download/<token>. export_participants
// stashes the CSV/JSON in-container and hands back this link instead of inlining
// tens of thousands of rows.
const viralLoopsDownloadTarget = process.env.UPSTREAM_VIRAL_LOOPS;
if (viralLoopsDownloadTarget) {
  app.use(
    "/dl/viral-loops",
    (req, res, next) => {
      if (!workerSecretOk(req)) {
        res.status(401).json({ error: "unauthorized" });
        return;
      }
      next();
    },
    createProxyMiddleware({
      target: viralLoopsDownloadTarget,
      changeOrigin: true,
      // Express strips the "/dl/viral-loops" mount prefix before the proxy runs,
      // so pathRewrite sees the already-stripped "/<token>". Rewrite the leading
      // slash to "/download/" so the MCP receives /download/<token>.
      pathRewrite: { "^/": "/download/" },
      selfHandleResponse: false,
      on: {
        proxyReq: (proxyReq) => {
          // Never leak our internal headers to the container.
          proxyReq.removeHeader("x-worker-shared-secret");
          proxyReq.removeHeader("x-choiz-user-email");
          proxyReq.removeHeader("x-mcp-user");
        },
        error: (err, _req, res) => {
          console.error("[proxy:/dl/viral-loops] upstream error", err);
          if (res && "writeHead" in res && !res.headersSent) {
            res.writeHead(502, { "Content-Type": "application/json" });
            res.end(JSON.stringify({ error: "upstream_unavailable" }));
          }
        },
      },
    }),
  );
}

const port = Number(process.env.PORT ?? 8080);
app.listen(port, "0.0.0.0", () => {
  console.log(`[mcp-gateway] listening on :${port}`);
  console.log(`[mcp-gateway] container routes:`, Object.keys(upstreams).filter((k) => upstreams[k]));
  console.log(
    `[mcp-gateway] remote routes:`,
    Object.keys(remoteUpstreams).filter((k) => resolveRemoteAuthValue(remoteUpstreams[k])),
  );
});
