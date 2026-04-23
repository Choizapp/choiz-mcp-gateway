import express from "express";
import { randomUUID } from "node:crypto";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { authenticate } from "./auth.js";
import { createWarehouseServer } from "./mcps/warehouse.js";

type ServerFactory = (userEmail: string) => McpServer;

const app = express();
app.use(express.json({ limit: "2mb" }));

const transports = new Map<string, StreamableHTTPServerTransport>();

function mountMcp(path: string, factory: ServerFactory): void {
  app.post(path, async (req, res) => {
    const user = authenticate(req);
    if (!user) {
      res.status(401).json({ error: "unauthorized" });
      return;
    }

    const sessionId = req.header("mcp-session-id");
    let transport = sessionId ? transports.get(sessionId) : undefined;

    if (!transport) {
      transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: () => randomUUID(),
        onsessioninitialized: (id) => {
          transports.set(id, transport!);
        },
      });
      transport.onclose = () => {
        if (transport!.sessionId) transports.delete(transport!.sessionId);
      };

      const server = factory(user.email);
      await server.connect(transport);
    }

    await transport.handleRequest(req, res, req.body);
  });

  const passthrough = async (req: express.Request, res: express.Response) => {
    const user = authenticate(req);
    if (!user) {
      res.status(401).json({ error: "unauthorized" });
      return;
    }
    const sessionId = req.header("mcp-session-id");
    const transport = sessionId ? transports.get(sessionId) : undefined;
    if (!transport) {
      res.status(400).end();
      return;
    }
    await transport.handleRequest(req, res);
  };

  app.get(path, passthrough);
  app.delete(path, passthrough);
}

mountMcp("/warehouse", createWarehouseServer);

app.get("/healthz", (_req, res) => {
  res.json({ ok: true });
});

const port = Number(process.env.PORT ?? 8080);
app.listen(port, "0.0.0.0", () => {
  console.log(`[mcp-gateway] listening on :${port}`);
});
