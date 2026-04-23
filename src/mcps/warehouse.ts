import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Pool } from "pg";
import { z } from "zod";

const pool = new Pool({
  connectionString: process.env.WAREHOUSE_DATABASE_URL,
  max: 5,
  ssl: { rejectUnauthorized: false },
});

export function createWarehouseServer(userEmail: string): McpServer {
  const server = new McpServer({
    name: "choiz-warehouse",
    version: "0.1.0",
  });

  server.tool(
    "query",
    "Run a read-only SQL query against the Choiz data warehouse (Postgres). Only SELECT / WITH / EXPLAIN statements are accepted.",
    { sql: z.string().describe("A read-only SQL statement") },
    async ({ sql }) => {
      const first = sql.trim().split(/\s+/)[0]?.toLowerCase() ?? "";
      if (!["select", "with", "explain", "show"].includes(first)) {
        throw new Error("Only SELECT / WITH / EXPLAIN / SHOW queries are allowed");
      }

      const client = await pool.connect();
      try {
        await client.query(`SET application_name = 'mcp:${userEmail.replace(/'/g, "")}'`);
        await client.query("SET TRANSACTION READ ONLY");
        const result = await client.query(sql);
        return {
          content: [
            {
              type: "text",
              text: JSON.stringify(
                {
                  rowCount: result.rowCount,
                  rows: result.rows,
                },
                null,
                2,
              ),
            },
          ],
        };
      } finally {
        client.release();
      }
    },
  );

  return server;
}
