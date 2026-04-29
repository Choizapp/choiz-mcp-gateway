import express from "express";
import { createProxyMiddleware } from "http-proxy-middleware";
import { authenticate } from "./auth.js";

const app = express();

// --- Routing table: path prefix -> upstream MCP URL ---
// Add a new entry here when you add a new MCP container to compose.yml.
const upstreams: Record<string, string | undefined> = {
  "/mcp/warehouse":         process.env.UPSTREAM_WAREHOUSE,
  "/mcp/meta-ads":          process.env.UPSTREAM_META_ADS,
  "/mcp/facebook-choiz":    process.env.UPSTREAM_FACEBOOK_CHOIZ,
  "/mcp/facebook-timeless": process.env.UPSTREAM_FACEBOOK_TIMELESS,
  "/mcp/instagram-choiz":    process.env.UPSTREAM_INSTAGRAM_CHOIZ,
  "/mcp/instagram-timeless": process.env.UPSTREAM_INSTAGRAM_TIMELESS,
  "/mcp/google-ads":         process.env.UPSTREAM_GOOGLE_ADS,
};

// --- Health check (no auth) ---
app.get("/healthz", (_req, res) => {
  res.json({ ok: true, routes: Object.keys(upstreams).filter((k) => upstreams[k]) });
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

// --- Wire each prefix to its upstream ---
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

const port = Number(process.env.PORT ?? 8080);
app.listen(port, "0.0.0.0", () => {
  console.log(`[mcp-gateway] listening on :${port}`);
  console.log(`[mcp-gateway] routes:`, Object.keys(upstreams).filter((k) => upstreams[k]));
});
