import express from "express";
import { createProxyMiddleware } from "http-proxy-middleware";
import { authenticate, workerSecretOk } from "./auth.js";

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
  apiKeyEnv: string;    // env var holding the upstream API key
  apiKeyHeader: string; // header name to set on the outgoing request
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
};

// --- Health check (no auth) ---
app.get("/healthz", (_req, res) => {
  res.json({
    ok: true,
    routes: [
      ...Object.keys(upstreams).filter((k) => upstreams[k]),
      ...Object.keys(remoteUpstreams).filter(
        (k) => process.env[remoteUpstreams[k].apiKeyEnv],
      ),
    ],
  });
});

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
  const apiKey = process.env[cfg.apiKeyEnv];
  if (!apiKey) {
    console.warn(`[mcp-gateway] skipping remote ${prefix}: ${cfg.apiKeyEnv} not set`);
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

const port = Number(process.env.PORT ?? 8080);
app.listen(port, "0.0.0.0", () => {
  console.log(`[mcp-gateway] listening on :${port}`);
  console.log(`[mcp-gateway] container routes:`, Object.keys(upstreams).filter((k) => upstreams[k]));
  console.log(
    `[mcp-gateway] remote routes:`,
    Object.keys(remoteUpstreams).filter((k) => process.env[remoteUpstreams[k].apiKeyEnv]),
  );
});
