/**
 * Shape of the Worker's runtime environment.
 *
 * Bindings declared in wrangler.jsonc (vars + KV) are typed automatically in
 * worker-configuration.d.ts, but secrets set via `wrangler secret put` are NOT
 * reflected there, so we extend the generated type here.
 */
export interface WorkerEnv extends Env {
  // Secrets (set via `wrangler secret put <NAME>`)
  GOOGLE_CLIENT_ID: string;
  GOOGLE_CLIENT_SECRET: string;
  WORKER_SHARED_SECRET: string;
}

/**
 * Props attached to an issued token. After a user completes the Google OAuth
 * flow, we call `completeAuthorization({ props: ... })`. Those props become
 * available as `ctx.props` in the API handler on every subsequent MCP request,
 * and THEY are the source of truth for the authenticated identity — we never
 * trust client-supplied headers.
 */
export interface UserProps {
  email: string;
  name?: string;
  [key: string]: unknown;
}
