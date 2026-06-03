import type { Request } from "express";

const SHARED_SECRET = process.env.WORKER_SHARED_SECRET;
if (!SHARED_SECRET) {
  throw new Error("WORKER_SHARED_SECRET env var is required");
}

// Comma-separated list of Google Workspace domains we accept. Mirrors the
// Worker-side ALLOWED_EMAIL_DOMAINS — defense-in-depth: the Worker is
// already enforcing this after Google OAuth, but the gateway re-checks in
// case someone bypasses the Worker by leaking the shared secret. Defaults
// keep the gateway functional even if the env var is missing.
const ALLOWED_DOMAINS = (
  process.env.ALLOWED_EMAIL_DOMAINS ?? "choiz.com.mx,choiz.com.ar,gotimeless.ai"
)
  .split(",")
  .map((d) => d.trim())
  .filter(Boolean);

export interface AuthenticatedUser {
  email: string;
}

/**
 * Validates that the request came from the Cloudflare Worker in front of
 * the tunnel. The Worker is the only component that should know the shared
 * secret. Any request lacking it is rejected.
 *
 * The authenticated email comes from the Worker after it completes the
 * Google Workspace OAuth flow, so we can trust it IFF the shared secret
 * matches. We still re-validate the email's domain against the allowlist
 * so a leaked shared secret cannot be used to inject arbitrary identities.
 */
export function authenticate(req: Request): AuthenticatedUser | null {
  if (!workerSecretOk(req)) return null;

  const email = req.header("x-choiz-user-email");
  if (!email) return null;
  const domain = email.split("@")[1] ?? "";
  if (!ALLOWED_DOMAINS.includes(domain)) return null;

  return { email };
}

/**
 * Lighter check for non-/mcp paths (e.g. the public label download proxy):
 * confirms the request came from the Worker via the shared secret, WITHOUT
 * requiring a user email. Used where the capability token in the URL is the
 * actual authorization (the Worker forwards browser GETs that carry no bearer).
 */
export function workerSecretOk(req: Request): boolean {
  return req.header("x-worker-shared-secret") === SHARED_SECRET;
}
