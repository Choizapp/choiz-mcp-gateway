// GSC MCP entrypoint — serves ahonn/mcp-server-gsc over MCP revision
// 2026-07-28, with the 2025-era protocol kept as a stateless fallback.
//
// Why this exists: claude.ai switched to revision 2026-07-28 on 2026-09-11.
// That revision removed the `initialize` handshake and `Mcp-Session-Id`,
// made `server/discover` mandatory and moved the protocol version into a
// `params._meta` envelope. `@modelcontextprotocol/sdk` 1.x (the SDK the
// upstream package is written against) tops out at 2025-11-25 and answers
// a modern request with a bare HTTP 400 — claude.ai shows the connector as
// Connected but every tool call fails with "Connection closed". Verified
// live on 2026-10-19: `server/discover` against gsc_timeless_mcp returned
// 400 "No valid session ID provided".
//
// Shape: a BRIDGE, not a fork.
//   * Inside: the upstream `createServer(credentials)` — a v1 lowlevel
//     Server holding the 8 GSC tools and their analytics logic, untouched —
//     connected ONCE at startup to a v1 Client over an in-memory transport.
//     The 2025-era handshake happens in-process and never touches the wire.
//   * Outside: a v2 `Server` from `@modelcontextprotocol/server`, served by
//     `createMcpHandler`. It speaks 2026-07-28 (discover, `_meta` envelope,
//     resultType, cache hints) and also answers 2025-era requests
//     statelessly from the same factory, so both eras stay in sync.
//     `tools/list` and `tools/call` forward to the v1 Client.
//
// Replaces the previous per-session StreamableHTTPServerTransport map
// (PR #32). Stateless also removes the stale-Mcp-Session-Id-after-redeploy
// failure mode: there is no session to go stale.
//
// Auth: Choiz `ga4-mcp-reader` service-account JSON arrives via
// `GSC_SERVICE_ACCOUNT_JSON_B64` (compose-level alias of
// `GA4_SERVICE_ACCOUNT_JSON_B64`); decoded once at startup to
// /tmp/gsc-sa.json (chmod 600) and the path is passed into createServer().
//
// Tenant scope: that service account can read BOTH properties
// (choiz.com.mx and gotimeless.ai), so until 2026-10-19 either connector
// could query either brand. Each container is now pinned to one property via
// `GSC_SITE_URL` (set per service in compose.yml), the same "the route IS
// the brand" rule as powerbi: `siteUrl` is dropped from every tool schema and
// filled in server-side, `list_sites` returns only this property, and a call
// naming any other property is refused rather than silently redirected — a
// model holding a stale schema should learn it asked the wrong connector, not
// get the other brand's numbers under the wrong name. The startup fails if
// `GSC_SITE_URL` is unset: an unscoped container must never come up.
// The scope lives in code, not in the credential; separate service accounts
// per property would be the stronger guarantee.

import { createServer as createUpstreamServer } from "./dist/server.js";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { Server, createMcpHandler } from "@modelcontextprotocol/server";
import { toNodeHandler } from "@modelcontextprotocol/node";
import { writeFileSync, chmodSync } from "node:fs";
import http from "node:http";

// enhanced_search_analytics can pull up to 25K rows; the v1 Client's 60s
// default request timeout is too tight for that.
const TOOL_CALL_TIMEOUT_MS = 180_000;

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

const SITE_URL = process.env.GSC_SITE_URL;
if (!SITE_URL) {
  throw new Error("GSC_SITE_URL env var is required (e.g. sc-domain:gotimeless.ai)");
}
console.error(`GSC MCP: scoped to ${SITE_URL}`);

// Every tool except list_sites takes a `siteUrl`; remove it from the schema
// the model sees and say in the description which property the tool hits.
function scopeTool(tool) {
  const inputSchema = structuredClone(tool.inputSchema);
  delete inputSchema.properties?.siteUrl;
  if (Array.isArray(inputSchema.required)) {
    inputSchema.required = inputSchema.required.filter((k) => k !== "siteUrl");
    if (inputSchema.required.length === 0) delete inputSchema.required;
  }
  const description =
    tool.name === "list_sites"
      ? `Returns the Search Console property this connector serves (${SITE_URL}).`
      : `${tool.description}. Always runs against the Search Console property ${SITE_URL}; ` +
        `this connector cannot query any other property.`;
  return { ...tool, description, inputSchema };
}

function toolError(text) {
  return { content: [{ type: "text", text }], isError: true };
}

// list_sites returns every property the credential can see; keep only ours.
function scopeListSites(result) {
  const text = result.content?.[0]?.text;
  const data = JSON.parse(text);
  data.siteEntry = (data.siteEntry ?? []).filter((s) => s.siteUrl === SITE_URL);
  return { ...result, content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
}

// --- Inside: the upstream v1 server, reached through an in-process client.
// The upstream handlers build a fresh SearchConsoleService per call and keep
// no state between calls, so one shared pair serves concurrent requests.
const upstream = createUpstreamServer(credentialsPath);
const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
await upstream.connect(serverSide);
const bridge = new Client({ name: "choiz-gsc-bridge", version: "1.0.0" });
await bridge.connect(clientSide);

// --- Outside: one v2 Server per request (createMcpHandler calls the factory
// per exchange). Only `tools` is advertised: upstream also declares
// resources/prompts but registers no handlers for them.
function buildServer() {
  const server = new Server(
    { name: "mcp-server-gsc", version: "0.3.0" },
    { capabilities: { tools: {} } },
  );

  server.setRequestHandler("tools/list", async () => {
    const { tools } = await bridge.listTools();
    return { tools: tools.map(scopeTool) };
  });

  server.setRequestHandler("tools/call", async (request) => {
    const { name, arguments: args } = request.params;
    const requested = args?.siteUrl;
    if (requested !== undefined && requested !== SITE_URL) {
      return toolError(
        `This connector only serves the Search Console property ${SITE_URL}; ` +
          `'${requested}' belongs to a different connector.`,
      );
    }
    try {
      const result = await bridge.callTool(
        { name, arguments: { ...(args ?? {}), siteUrl: SITE_URL } },
        undefined,
        { timeout: TOOL_CALL_TIMEOUT_MS },
      );
      return name === "list_sites" && !result.isError ? scopeListSites(result) : result;
    } catch (err) {
      // Upstream reports bad arguments, unknown tools and Google API
      // failures by throwing. Hand them back as a tool error so the model
      // reads the message, instead of a JSON-RPC error claude.ai shows as a
      // generic "Error occurred during tool execution".
      return toolError(String(err?.message ?? err));
    }
  });

  return server;
}

const handler = createMcpHandler(buildServer, {
  onerror: (err) => console.error("mcp handler error:", err),
});

const httpServer = http.createServer(
  toNodeHandler(handler, {
    onerror: (err) => console.error("node adapter error:", err),
  }),
);

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
