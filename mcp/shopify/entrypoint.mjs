// Shopify MCP entrypoint — serves a READ-ONLY subset of GeLi2001/shopify-mcp
// via streamable-http with per-session McpServer + StreamableHTTPServerTransport.
//
// Why we don't reuse the package's launcher (dist/index.js):
//   1. It binds StdioServerTransport, not StreamableHTTPServerTransport.
//   2. It registers ALL tools, including writes (create-/update-/delete-/
//      cancel-/refund-/inventory-set-...). The user asked for read-only.
//
// Architecture (canonical MCP TS SDK session pattern):
//   * Module-level: one GraphQLClient (Admin GraphQL) + initialized
//     read-only tools registry. Both are stateless w.r.t. MCP sessions
//     and safely shared across all client sessions.
//   * Per HTTP session: a fresh McpServer + StreamableHTTPServerTransport
//     pair, stored in `transports` keyed by Mcp-Session-Id. Sharing a
//     single McpServer across sessions causes "Server already initialized"
//     on every initialize after the first — surfaces in claude.ai as a
//     generic "Authorization failed + ofid_..." error.
//
// Allowlist policy: names starting with "get-" (21 read tools in the
// upstream registry at SHA c90faaf4). Every write tool starts with
// create-/update-/delete-/order-/customer-/refund-/set-/manage-/
// inventory-set-/complete-. The container logs allowed + denied lists
// on boot so drift against upstream is easy to spot.

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";
import { GraphQLClient } from "graphql-request";
import { randomUUID } from "node:crypto";
import http from "node:http";

import { tools } from "./dist/tools/registry.js";

function readEnv() {
  const domain = process.env.MYSHOPIFY_DOMAIN;
  const token = process.env.SHOPIFY_ACCESS_TOKEN;
  const apiVersion = process.env.SHOPIFY_API_VERSION || "2026-01";

  if (!domain) throw new Error("MYSHOPIFY_DOMAIN env var is required");
  if (!token) throw new Error("SHOPIFY_ACCESS_TOKEN env var is required");

  process.env.MYSHOPIFY_DOMAIN = domain;
  process.env.SHOPIFY_ACCESS_TOKEN = token;

  return { domain, token, apiVersion };
}

const { domain, token, apiVersion } = readEnv();

const shopifyClient = new GraphQLClient(
  `https://${domain}/admin/api/${apiVersion}/graphql.json`,
  {
    headers: {
      "X-Shopify-Access-Token": token,
      "Content-Type": "application/json",
    },
  },
);

// Filter the upstream registry to read-only tools.
const readOnly = tools.filter((t) => t.name.startsWith("get-"));
const denied = tools.filter((t) => !t.name.startsWith("get-")).map((t) => t.name);

// Each tool's .initialize(client) stores the GraphQL client on a
// module-level singleton inside the tool's file. Safe to call once at
// startup — the client itself is concurrency-safe (a graphql-request
// GraphQLClient is just an HTTP wrapper with no per-request state).
for (const tool of readOnly) {
  tool.initialize(shopifyClient);
}

console.error(
  `Shopify MCP: domain=${domain} apiVersion=${apiVersion} ` +
    `allowed=${readOnly.length} denied=${denied.length}`,
);
console.error("allowed tools:", readOnly.map((t) => t.name).join(", "));
console.error("denied (writes):", denied.join(", "));

// --- Response trimming -----------------------------------------------------
//
// claude.ai silently rejects MCP tool results above ~2-3 KB. A raw Shopify
// Admin GraphQL response for `get-orders limit:10` is ~6-9 KB once you
// include lineItems + shippingAddress + billingAddress + taxLines per
// order. The model "gets stuck thinking" with no surfaced error.
//
// Policy: for LIST tools, drop the heavy nested fields so the response is
// a usable summary (id, name, totals, top-level customer info, status).
// DETAIL tools (`*-by-id`) and known-small tools (shop-info, locations,
// markets, metafield-definitions, inventory-*, price-lists,
// product-variants-detailed) pass through unmodified — single records
// fit the ceiling, and detail is what the by-id calls exist for.
//
// Add/remove fields per tool here. The trim is recursive: any object in
// the result tree that has a matching key gets that key deleted. This
// covers wrappers like `{orders: [...], pageInfo: {...}}` correctly —
// pageInfo's properties are not in TRIM_RULES so they stay.
const TRIM_RULES = {
  // get-orders v1 stripped ~10 fields per order; the remaining
  // top-line money objects (subtotalPrice + totalShippingPrice +
  // totalTax) plus full customer detail still leave ~500 B per order,
  // so limit:10 = ~5 KB — above the ceiling. v2 also drops the
  // redundant money objects (totalPrice is enough for a list view)
  // and customer subfields the model rarely needs in a list.
  "get-orders": [
    "lineItems",
    "shippingAddress",
    "billingAddress",
    "taxLines",
    "discountApplications",
    "note",
    "subtotalPrice",
    "totalShippingPrice",
    "totalTax",
    "phone",
    "verifiedEmail",
    "acceptsMarketing",
    "tags",
    "createdAt",
  ],
  "get-customer-orders": [
    "lineItems",
    "shippingAddress",
    "billingAddress",
    "taxLines",
    "discountApplications",
    "note",
    "subtotalPrice",
    "totalShippingPrice",
    "totalTax",
  ],
  // get-products v1 still returned 7-12 KB for limit:5 because
  // `description` (plaintext, sometimes paragraphs) and
  // `priceRangeV2` + tags + vendor + handle + productType pile up.
  // For a list view the model only needs id + title + status.
  "get-products": [
    "variants",
    "images",
    "media",
    "descriptionHtml",
    "description",
    "options",
    "metafields",
    "seo",
    "tags",
    "vendor",
    "productType",
    "handle",
    "totalInventory",
    "priceRangeV2",
    "compareAtPriceRange",
    "onlineStoreUrl",
    "tracksInventory",
    "publishedAt",
    "templateSuffix",
    "giftCard",
  ],
  "get-customers": [
    "addresses",
    "defaultAddress",
    "note",
    "metafields",
    "phone",
    "verifiedEmail",
    "acceptsMarketing",
    "amountSpent",
    "lastOrder",
    "tags",
  ],
  "get-collections": [
    "products",
    "descriptionHtml",
    "description",
    "image",
    "seo",
    "ruleSet",
  ],
  "get-fulfillment-orders": ["lineItems", "merchantRequests"],
};

// Runtime telemetry: how many top-level objects had at least one field
// stripped, per tool. Logged on every tool call. Cheap signal for
// tuning the trim list later.
const trimCounts = {};
function trim(toolName, obj) {
  const fields = TRIM_RULES[toolName];
  if (!fields) return obj;
  let hits = 0;
  function walk(v) {
    if (Array.isArray(v)) return v.map(walk);
    if (v && typeof v === "object") {
      const out = {};
      let touched = false;
      for (const [k, val] of Object.entries(v)) {
        if (fields.includes(k)) {
          touched = true;
          continue;
        }
        out[k] = walk(val);
      }
      if (touched) hits++;
      return out;
    }
    return v;
  }
  const trimmed = walk(obj);
  if (hits > 0) {
    trimCounts[toolName] = (trimCounts[toolName] || 0) + hits;
    console.error(
      `trim ${toolName}: stripped from ${hits} object(s); cumulative=${trimCounts[toolName]}`,
    );
  }
  return trimmed;
}

// Build a fresh McpServer with the read-only tools registered. Called
// once per new MCP session (i.e. per claude.ai connector connection).
function buildMcpServer() {
  const server = new McpServer({
    name: "shopify",
    version: "1.0.0",
    description:
      "Shopify Admin GraphQL — read-only subset (products, orders, customers, inventory, collections, metafields). Lists are trimmed of heavy nested fields (lineItems, addresses, variants, images) to fit claude.ai's ~2-3 KB tool-result ceiling. For full details on a single record, use the matching `*-by-id` tool.",
  });
  for (const tool of readOnly) {
    server.tool(tool.name, tool.schema.shape, async (args) => {
      const raw = await tool.execute(args);
      const trimmed = trim(tool.name, raw);
      return {
        // Compact JSON: every byte counts against the ceiling.
        content: [{ type: "text", text: JSON.stringify(trimmed) }],
      };
    });
  }
  return server;
}

// Session-id -> transport. Each transport is bound to its own
// McpServer at creation time. Cleared via the transport's onclose
// callback when claude.ai terminates the session (or it times out).
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
      const server = buildMcpServer();
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
  console.error("Shopify MCP listening on http://0.0.0.0:8080/");
});

for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => {
    console.error(`Received ${sig}, shutting down`);
    httpServer.close(() => process.exit(0));
  });
}
