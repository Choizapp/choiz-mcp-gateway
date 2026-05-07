// GSC MCP entrypoint — serves ahonn/mcp-server-gsc via streamable-http
// without supergateway.
//
// The upstream package's `src/index.ts` ships only a stdio launcher; its
// `src/remote.ts` is a Cloudflare Workers / Hono variant that doesn't
// bind to a Node port. Both reuse `createServer(credentials): Server`
// from `src/server.ts`. We import that builder and wire it through the
// MCP TS SDK's `StreamableHTTPServerTransport`, then bind a plain Node
// HTTP server on 0.0.0.0:8080. Same architectural shape as
// mcp/instagram/entrypoint.py and mcp/ga4/entrypoint.py — single
// in-process server, no per-request child churn.
//
// Auth: same Choiz `ga4-mcp-reader` service-account JSON as GA4 + GSC
// historically. The base64 form arrives via `GSC_SERVICE_ACCOUNT_JSON_B64`
// (compose-level alias of `GA4_SERVICE_ACCOUNT_JSON_B64`); we decode it
// once at startup to /tmp/gsc-sa.json and pass the path to createServer.

import { createServer } from "./dist/server.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { randomUUID } from "node:crypto";
import { writeFileSync, chmodSync } from "node:fs";
import http from "node:http";

function materializeServiceAccount() {
  const b64 = process.env.GSC_SERVICE_ACCOUNT_JSON_B64;
  if (!b64) {
    throw new Error("GSC_SERVICE_ACCOUNT_JSON_B64 env var is required");
  }
  const path = "/tmp/gsc-sa.json";
  writeFileSync(path, Buffer.from(b64, "base64"));
  chmodSync(path, 0o600);
  return path;
}

async function main() {
  const credentialsPath = materializeServiceAccount();

  // createServer registers the 8 tools (list_sites, search_analytics,
  // enhanced_search_analytics, detect_quick_wins, index_inspect,
  // list_sitemaps, get_sitemap, submit_sitemap) and returns a fully
  // wired @modelcontextprotocol/sdk Server.
  const server = createServer(credentialsPath);

  // Stateful streamable-http: claude.ai opens parallel POST + GET SSE
  // streams; the SDK's transport correctly multiplexes them by session.
  // (Different from the supergateway --stateful flag tripped by bug #126
  // — that bug was specific to supergateway's bridging logic.)
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: () => randomUUID(),
  });

  await server.connect(transport);

  const httpServer = http.createServer(async (req, res) => {
    try {
      await transport.handleRequest(req, res);
    } catch (err) {
      console.error("handleRequest error:", err);
      if (!res.headersSent) {
        res.statusCode = 500;
        res.setHeader("content-type", "application/json");
        res.end(JSON.stringify({ error: String(err) }));
      } else {
        res.end();
      }
    }
  });

  httpServer.listen(8080, "0.0.0.0", () => {
    console.error("GSC MCP listening on http://0.0.0.0:8080/");
  });

  // Graceful shutdown so docker compose stop sends SIGTERM and we exit
  // cleanly (otherwise compose waits the 10s default timeout).
  for (const sig of ["SIGINT", "SIGTERM"]) {
    process.on(sig, () => {
      console.error(`Received ${sig}, shutting down`);
      httpServer.close(() => process.exit(0));
    });
  }
}

main().catch((err) => {
  console.error("Fatal:", err);
  process.exit(1);
});
