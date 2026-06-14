import { Pool, types, type QueryResultRow } from "pg";

// ─────────────────────────────────────────────────────────────────────
// Warehouse client for the dashboard JSON query endpoint (/api/warehouse/query).
//
// This is the ONLY place in the gateway that talks SQL to the RDS directly (the
// warehouse MCP container has its own postgres-mcp connection). It exists so the
// Vercel dashboard (kapso-ops-dashboard) can read the analytic views `a.*` over
// HTTPS without a direct line to the private RDS: Vercel functions have dynamic
// IPs and cannot reach the RDS, but they can reach this gateway (Cloudflare
// Tunnel), which lives in the RDS's VPC.
//
// Safety: we connect with a read-only role AND run every statement inside a
// READ ONLY transaction with a statement timeout, so even an over-privileged
// role cannot write and a runaway query is bounded.
//
// Type parsers mirror the dashboard's old pg config so the JSON we return keeps
// the exact contract the app expects:
//   - bigint (INT8) / numeric → JS number (JSON has no bigint anyway)
//   - timestamp / timestamptz / date → RAW string. The `a.*` views are already
//     materialised in America/Mexico_City; reparsing to Date would shift them to
//     the runtime's UTC and misalign the day buckets.
// ─────────────────────────────────────────────────────────────────────

const asNumber = (v: string | null): number | null =>
  v === null ? null : Number(v);
types.setTypeParser(types.builtins.INT8, asNumber); // bigint
types.setTypeParser(types.builtins.NUMERIC, asNumber); // numeric / decimal

const asString = (v: string | null): string | null => v;
types.setTypeParser(types.builtins.TIMESTAMP, asString);
types.setTypeParser(types.builtins.TIMESTAMPTZ, asString);
types.setTypeParser(types.builtins.DATE, asString);

// Prefer a dedicated read-only role; fall back to the same URL the warehouse
// MCP uses (already read-only per .env convention). Use `||` (not `??`): compose
// sets WAREHOUSE_READONLY_DATABASE_URL to an EMPTY STRING via `:-` when unset, and
// `??` would keep that empty string instead of falling back to WAREHOUSE_DATABASE_URL.
const connectionString =
  process.env.WAREHOUSE_READONLY_DATABASE_URL ||
  process.env.WAREHOUSE_DATABASE_URL;

// El dashboard dispara ~10 queries en paralelo sobre la vista
// a.fct_kapso_conversations (cara de materializar). Bajo esa concurrencia el RDS
// se ralentiza, así que 15s quedaba corto (timeouts). 60s da margen; igual la
// carga fría pasa 1 vez al día (después queda cacheada en el dashboard).
const STATEMENT_TIMEOUT_MS = (() => {
  const n = Number(process.env.WAREHOUSE_STATEMENT_TIMEOUT_MS);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 60_000;
})();

let pool: Pool | null = null;

function getPool(): Pool {
  if (pool) return pool;
  if (!connectionString) {
    throw new Error(
      "WAREHOUSE_DATABASE_URL (or WAREHOUSE_READONLY_DATABASE_URL) is required for /api/warehouse/query",
    );
  }
  // RDS requires TLS but its cert is signed by the AWS CA (not in the system
  // trust store). rejectUnauthorized=false encrypts without verifying the chain
  // — same trade-off the dashboard made. Strip any sslmode= from the string so
  // pg doesn't read it as verify-full and override our ssl object.
  const cleaned = connectionString
    .replace(/([?&])sslmode=[^&]*(&|$)/i, (_m, pre, post) =>
      post === "&" ? pre : pre === "?" ? "" : "",
    )
    .replace(/[?&]$/, "");

  pool = new Pool({
    connectionString: cleaned,
    ssl: { rejectUnauthorized: process.env.WAREHOUSE_SSL_STRICT === "true" },
    // El dashboard dispara ~10 queries en paralelo sobre la misma vista cara.
    // Con 5 concurrentes el RDS se ahogaba (cada una >15s); con 3 hay menos
    // contención → cada query más rápida. Las que esperan turno se encolan
    // (connectionTimeout abajo las cubre). Override: WAREHOUSE_POOL_MAX.
    max: (() => {
      const n = Number(process.env.WAREHOUSE_POOL_MAX);
      return Number.isFinite(n) && n > 0 ? Math.floor(n) : 3;
    })(),
    idleTimeoutMillis: 30_000,
    // Generoso: las queries encoladas no deben fallar esperando turno mientras
    // las otras corren.
    connectionTimeoutMillis: 60_000,
  });
  pool.on("error", (err) => {
    // A dropped idle client must not crash the gateway process.
    console.error("[warehouse] pool error:", err.message);
  });
  return pool;
}

/**
 * Runs a single read-only SELECT against the warehouse and returns its rows.
 * Wrapped in a READ ONLY transaction with a statement timeout: Postgres rejects
 * any write inside it (defense in depth over the read-only role), and bounds a
 * runaway query. Always called with parameters ($1, $2, ...) — never with
 * values interpolated into the SQL.
 */
export async function runReadOnlyQuery<
  T extends QueryResultRow = QueryResultRow,
>(sql: string, params: ReadonlyArray<unknown>): Promise<T[]> {
  const client = await getPool().connect();
  try {
    await client.query("BEGIN TRANSACTION READ ONLY");
    await client.query(`SET LOCAL statement_timeout = ${STATEMENT_TIMEOUT_MS}`);
    const res = await client.query<T>(sql, params as unknown[]);
    await client.query("COMMIT");
    return res.rows;
  } catch (err) {
    await client.query("ROLLBACK").catch(() => {});
    throw err;
  } finally {
    client.release();
  }
}
