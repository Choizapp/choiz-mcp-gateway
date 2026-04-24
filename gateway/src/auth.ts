import type { Request } from "express";

const SHARED_SECRET = process.env.WORKER_SHARED_SECRET;
if (!SHARED_SECRET) {
  throw new Error("WORKER_SHARED_SECRET env var is required");
}

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
 * matches.
 */
export function authenticate(req: Request): AuthenticatedUser | null {
  const secret = req.header("x-worker-shared-secret");
  if (secret !== SHARED_SECRET) return null;

  const email = req.header("x-choiz-user-email");
  if (!email) return null;
  if (!email.endsWith("@choiz.com.mx")) return null;

  return { email };
}
