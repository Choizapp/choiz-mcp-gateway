import express from "express";
import { createProxyMiddleware } from "http-proxy-middleware";
import { authenticate } from "./auth.js";

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
  "/mcp/kapso": {
    target: "https://app.kapso.ai/mcp",
    apiKeyEnv: "KAPSO_API_KEY",
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

const port = Number(process.env.PORT ?? 8080);
app.listen(port, "0.0.0.0", () => {
  console.log(`[mcp-gateway] listening on :${port}`);
  console.log(`[mcp-gateway] container routes:`, Object.keys(upstreams).filter((k) => upstreams[k]));
  console.log(
    `[mcp-gateway] remote routes:`,
    Object.keys(remoteUpstreams).filter((k) => process.env[remoteUpstreams[k].apiKeyEnv]),
  );
});
