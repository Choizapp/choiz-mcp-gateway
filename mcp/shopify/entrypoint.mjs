// Shopify MCP entrypoint — serves a READ-ONLY subset of GeLi2001/shopify-mcp
// via streamable-http, without supergateway.
//
// Why we don't reuse the package's launcher (dist/index.js):
//   1. It binds StdioServerTransport, not StreamableHTTPServerTransport.
//   2. It registers ALL tools, including writes (create-/update-/delete-/
//      cancel-/refund-/inventory-set-...). The user asked for read-only.
//
// Approach: import the package's `tools` registry (each entry exposes
// .name, .schema, .initialize(client), .execute(args)), build our own
// GraphQLClient with X-Shopify-Access-Token, filter the tools array to
// names starting with "get-" (read convention used uniformly across the
// package's 20 read tools), then register them on an McpServer mounted
// behind StreamableHTTPServerTransport.
//
// The `get-` prefix rule is intentional and structural:
//   * Every read tool in src/tools/registry.ts is named get-* (per the
//     package's getProducts, getOrders, getCustomers, getShopInfo, ...
//     convention as of SHA c90faaf4).
//   * Every write tool starts with create-/update-/delete-/order-/
//     customer-/refund-/set-/manage-/inventory-set-/complete-.
//   * If the upstream adds a non-get- read tool we miss it; if they add
//     a write tool prefixed get- we wrongly include it. Both are
//     unlikely given the consistency of the existing naming. The
//     container logs the final allowed list on boot so drift is easy
//     to spot in `docker compose logs`.

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
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

  // The upstream package reads MYSHOPIFY_DOMAIN from process.env at
  // various points (e.g. for error messages). Mirror what its
  // dist/index.js does for parity.
  process.env.MYSHOPIFY_DOMAIN = domain;
  process.env.SHOPIFY_ACCESS_TOKEN = token;

  return { domain, token, apiVersion };
}

async function main() {
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

  // Filter the upstream registry to read-only tools. See file header
  // for the rationale behind the get-* convention.
  const readOnly = tools.filter((t) => t.name.startsWith("get-"));
  const denied = tools.filter((t) => !t.name.startsWith("get-")).map((t) => t.name);

  // Initialize only the tools we'll actually expose. Each tool's
  // .initialize(client) stores the GraphQL client on a module-level
  // singleton inside the tool file — same pattern the upstream uses.
  for (const tool of readOnly) {
    tool.initialize(shopifyClient);
  }

  const server = new McpServer({
    name: "shopify",
    version: "1.0.0",
    description:
      "Shopify Admin GraphQL — read-only subset (products, orders, customers, inventory, collections, metafields). Pass `limit` low (≤5) on list calls to stay under claude.ai's ~2-3 KB tool-result ceiling.",
  });

  for (const tool of readOnly) {
    server.tool(tool.name, tool.schema.shape, async (args) => {
      const result = await tool.execute(args);
      return {
        // Compact JSON: claude.ai rejects tool-results over ~2-3 KB.
        // Pretty-printing alone can push a 5-product response past it.
        content: [{ type: "text", text: JSON.stringify(result) }],
      };
    });
  }

  console.error(
    `Shopify MCP: domain=${domain} apiVersion=${apiVersion} ` +
      `allowed=${readOnly.length} denied=${denied.length}`,
  );
  console.error("allowed tools:", readOnly.map((t) => t.name).join(", "));
  console.error("denied (writes):", denied.join(", "));

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
    console.error("Shopify MCP listening on http://0.0.0.0:8080/");
  });

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
