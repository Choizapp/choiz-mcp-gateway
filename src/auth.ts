import type { Request } from "express";

const SHARED_SECRET = process.env.WORKER_SHARED_SECRET;
if (!SHARED_SECRET) {
  throw new Error("WORKER_SHARED_SECRET env var is required");
}

export interface AuthenticatedUser {
  email: string;
}

export function authenticate(req: Request): AuthenticatedUser | null {
  const secret = req.header("x-worker-shared-secret");
  if (secret !== SHARED_SECRET) return null;

  const email = req.header("x-choiz-user-email");
  if (!email) return null;
  if (!email.endsWith("@choiz.com.mx")) return null;

  return { email };
}
