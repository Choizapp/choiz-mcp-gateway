// GSC MCP entrypoint — serves ahonn/mcp-server-gsc via streamable-http
// without supergateway, using the canonical MCP TS SDK per-session pattern.
//
// The upstream package's `src/index.ts` ships only a stdio launcher; its
// `src/remote.ts` is a Cloudflare Workers / Hono variant that doesn't
// bind to a Node port. Both reuse `createServer(credentials): Server`
// from `src/server.ts`. That builder instantiates a fresh `Server` per
// call (no shared state across calls — verified upstream at SHA
// e671b155), so it's safe to invoke once per MCP session.
//
// Architecture (canonical MCP TS SDK session pattern — same shape as
// mcp/shopify/entrypoint.mjs post-PR #21):
//   * Module-level: the service-account JSON is decoded once to disk.
//     The path is stateless w.r.t. MCP sessions and shared across all.
//   * Per HTTP session: a fresh `Server` from `createServer(path)` +
//     fresh `StreamableHTTPServerTransport`, stored in `transports`
//     keyed by Mcp-Session-Id. Sharing a single Server across sessions
//     causes "Server already initialized" on the second `initialize`
//     request — surfaces in claude.ai as a generic "Authorization
//     failed + ofid_..." error.
//
// Why the rewrite: smoke test on 2026-05-19 (post tiktok-organic deploy)
// hit `gsc-choiz` with HTTP 400 "Invalid Request: Server already
// initialized" on a fresh initialize call. The pre-existing single-
// shared-Server pattern was already documented as a latent bug
// (project_gsc_latent_session_bug). This change applies the fix.
//
// Auth: Choiz `ga4-mcp-reader` service-account JSON arrives via
// `GSC_SERVICE_ACCOUNT_JSON_B64` (compose-level alias of
// `GA4_SERVICE_ACCOUNT_JSON_B64`); decoded once at startup to
// /tmp/gsc-sa.json (chmod 600) and the path is passed into
// createServer() per session.

import { createServer } from "./dist/server.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";
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

const credentialsPath = materializeServiceAccount();
console.error(`GSC MCP: credentials path=${credentialsPath}`);

// Session-id -> transport. Each transport is bound to its own Server at
// creation time. Cleared via transport.onclose when claude.ai
// terminates the session (or it times out).
const transports = Object.create(null);

async function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      if (chunks.length === 0) return resolve(undefined);
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));
      } catch (err) {
        reject(err);
      }
    });
    req.on("error", reject);
  });
}

function send400(res, message) {
  if (res.headersSent) return;
  res.statusCode = 400;
  res.setHeader("content-type", "application/json");
  res.end(
    JSON.stringify({
      jsonrpc: "2.0",
      error: { code: -32000, message },
      id: null,
    }),
  );
}

async function handle(req, res) {
  const sessionId = req.headers["mcp-session-id"];
  const method = req.method || "GET";

  if (method === "POST") {
    let body;
    try {
      body = await readJsonBody(req);
    } catch (err) {
      send400(res, `Invalid JSON body: ${String(err)}`);
      return;
    }

    let transport;
    if (sessionId && transports[sessionId]) {
      transport = transports[sessionId];
    } else if (!sessionId && body && isInitializeRequest(body)) {
      transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: () => randomUUID(),
        onsessioninitialized: (id) => {
          transports[id] = transport;
          console.error(`session ${id} initialized`);
        },
      });
      transport.onclose = () => {
        const sid = transport.sessionId;
        if (sid && transports[sid]) {
          delete transports[sid];
          console.error(`session ${sid} closed`);
        }
      };
      // Fresh Server per session — see file header.
      const server = createServer(credentialsPath);
      await server.connect(transport);
      await transport.handleRequest(req, res, body);
      return;
    } else {
      send400(res, "Bad Request: No valid session ID provided");
      return;
    }
    await transport.handleRequest(req, res, body);
    return;
  }

  if (method === "GET" || method === "DELETE") {
    if (!sessionId || !transports[sessionId]) {
      send400(res, "Invalid or missing session ID");
      return;
    }
    await transports[sessionId].handleRequest(req, res);
    return;
  }

  res.statusCode = 405;
  res.setHeader("allow", "GET, POST, DELETE");
  res.end();
}

const httpServer = http.createServer(async (req, res) => {
  try {
    await handle(req, res);
  } catch (err) {
    console.error("handleRequest error:", err);
    if (!res.headersSent) {
      res.statusCode = 500;
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ error: String(err) }));
    } else {
      try {
        res.end();
      } catch {}
    }
  }
});

httpServer.listen(8080, "0.0.0.0", () => {
  console.error("GSC MCP listening on http://0.0.0.0:8080/");
});

// Graceful shutdown so `docker compose stop` sends SIGTERM and we exit
// cleanly (otherwise compose waits the 10s default timeout).
for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => {
    console.error(`Received ${sig}, shutting down`);
    httpServer.close(() => process.exit(0));
  });
}
