import { Hono } from "hono";
import type { AuthRequest, OAuthHelpers } from "@cloudflare/workers-oauth-provider";
import type { UserProps, WorkerEnv } from "./types.js";

/**
 * Default handler: everything that's NOT under /mcp/*. This is where the
 * OAuth UX lives — /authorize, the Google callback, consent UI if we add one.
 *
 * Flow:
 *   claude.ai -> GET /authorize?client_id=...&redirect_uri=...&code_challenge=...
 *     -> we parse the OAuth request, stash it in KV keyed by a random state
 *     -> redirect the user to Google with that state
 *   Google -> GET /callback?code=...&state=...
 *     -> we exchange the code for Google's access_token
 *     -> we fetch the user's email from Google
 *     -> we verify email_verified=true and the email's domain is in
 *        ALLOWED_EMAIL_DOMAINS (comma-separated list of Workspace domains)
 *     -> we call completeAuthorization(...) with the user's email as props
 *         -> this mints an opaque token, stores it in OAUTH_KV, and gives us
 *            a redirect URL back to the original claude.ai redirect_uri
 *     -> we redirect the user there
 *   claude.ai then calls POST /token with the code and gets the bearer token.
 */

type Bindings = WorkerEnv & { OAUTH_PROVIDER: OAuthHelpers };

const app = new Hono<{ Bindings: Bindings }>();

/**
 * Constant-time string comparison. The length check leaks the key length, which
 * is acceptable for a long random API key; the loop avoids leaking which byte
 * differs. Workers have no Buffer.timingSafeEqual, so we roll our own.
 */
function timingSafeEqual(a: string, b: string): boolean {
  const enc = new TextEncoder();
  const ab = enc.encode(a);
  const bb = enc.encode(b);
  if (ab.length !== bb.length) return false;
  let diff = 0;
  for (let i = 0; i < ab.length; i++) diff |= ab[i] ^ bb[i];
  return diff === 0;
}

/**
 * Step 1: claude.ai redirects the user here to start the flow.
 */
app.get("/authorize", async (c) => {
  const oauthReqInfo = await c.env.OAUTH_PROVIDER.parseAuthRequest(c.req.raw);

  // We stash the parsed OAuth request under a random state so we can pick it
  // back up after Google bounces the user to /callback.
  const state = crypto.randomUUID();
  await c.env.OAUTH_KV.put(`authreq:${state}`, JSON.stringify(oauthReqInfo), {
    expirationTtl: 600, // 10 min is plenty for a login flow
  });

  const origin = new URL(c.req.url).origin;
  const googleUrl = new URL("https://accounts.google.com/o/oauth2/v2/auth");
  googleUrl.searchParams.set("client_id", c.env.GOOGLE_CLIENT_ID);
  googleUrl.searchParams.set("redirect_uri", `${origin}/callback`);
  googleUrl.searchParams.set("response_type", "code");
  googleUrl.searchParams.set("scope", "openid email profile");
  googleUrl.searchParams.set("state", state);
  // `hd=*` hints the Google account picker to show ANY Workspace account
  // (rather than a single domain). We accept multiple domains
  // (ALLOWED_EMAIL_DOMAINS), so a single-domain hd hint would hide accounts
  // from the other allowed Workspaces. The post-callback check below still
  // enforces the actual domain allowlist.
  googleUrl.searchParams.set("hd", "*");
  // Force consent the first time, then skip it on subsequent logins.
  googleUrl.searchParams.set("prompt", "select_account");

  return c.redirect(googleUrl.toString());
});

/**
 * Step 2: Google bounces back here after the user logs in.
 */
app.get("/callback", async (c) => {
  const code = c.req.query("code");
  const state = c.req.query("state");
  if (!code || !state) {
    return c.text("Missing code or state", 400);
  }

  const authReqJson = await c.env.OAUTH_KV.get(`authreq:${state}`);
  if (!authReqJson) {
    return c.text("Invalid or expired state", 400);
  }
  await c.env.OAUTH_KV.delete(`authreq:${state}`);
  const oauthReqInfo = JSON.parse(authReqJson) as AuthRequest;

  const origin = new URL(c.req.url).origin;

  // Exchange Google's auth code for an access token.
  const tokenRes = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      code,
      client_id: c.env.GOOGLE_CLIENT_ID,
      client_secret: c.env.GOOGLE_CLIENT_SECRET,
      redirect_uri: `${origin}/callback`,
      grant_type: "authorization_code",
    }),
  });
  if (!tokenRes.ok) {
    return c.text(`Google token exchange failed: ${await tokenRes.text()}`, 502);
  }
  const tokenBody = (await tokenRes.json()) as { access_token?: string };
  if (!tokenBody.access_token) {
    return c.text("Google did not return an access token", 502);
  }

  // Fetch the user's profile (we only need email + name).
  const userRes = await fetch("https://www.googleapis.com/oauth2/v3/userinfo", {
    headers: { authorization: `Bearer ${tokenBody.access_token}` },
  });
  if (!userRes.ok) {
    return c.text(`Google userinfo failed: ${await userRes.text()}`, 502);
  }
  const user = (await userRes.json()) as {
    email?: string;
    email_verified?: boolean;
    name?: string;
    hd?: string;
  };

  if (!user.email || !user.email_verified) {
    return c.text("Google account has no verified email", 403);
  }
  const allowedDomains = c.env.ALLOWED_EMAIL_DOMAINS
    .split(",")
    .map((d) => d.trim())
    .filter(Boolean);
  const userDomain = user.email.split("@")[1] ?? "";
  if (!allowedDomains.includes(userDomain)) {
    return c.text(
      `Your account (${user.email}) is not in the allowed domain list. ` +
        `Contact ops if your domain should be added. Allowed: ${allowedDomains
          .map((d) => `@${d}`)
          .join(", ")}.`,
      403,
    );
  }

  const props: UserProps = { email: user.email, name: user.name };

  // This is the step that actually mints the token claude.ai will use.
  const { redirectTo } = await c.env.OAUTH_PROVIDER.completeAuthorization({
    request: oauthReqInfo,
    userId: user.email,
    metadata: { label: user.name ?? user.email },
    scope: oauthReqInfo.scope,
    props,
  });

  return Response.redirect(redirectTo);
});

/**
 * Public DHL label download proxy.
 *
 * Lives here (NOT under /mcp/) so the OAuthProvider hands it to us with NO
 * bearer required — a browser opening the link has no MCP token. The
 * unguessable token in the path (issued by the dhl MCP, ~192 bits, short TTL)
 * IS the capability. We validate its shape, forward the GET to the gateway
 * with the shared secret, and stream the PDF straight back to the browser.
 */
app.get("/dl/dhl/:token", async (c) => {
  const token = c.req.param("token");
  if (!/^[A-Za-z0-9_-]{16,64}$/.test(token)) {
    return c.text("Bad token", 400);
  }
  const upstream = await fetch(`${c.env.UPSTREAM_BASE}/dl/dhl/${token}`, {
    headers: { "x-worker-shared-secret": c.env.WORKER_SHARED_SECRET },
  });
  // Pass through status + the headers a browser download needs.
  const headers = new Headers();
  for (const h of ["content-type", "content-disposition", "content-length"]) {
    const v = upstream.headers.get(h);
    if (v) headers.set(h, v);
  }
  return new Response(upstream.body, { status: upstream.status, headers });
});

/**
 * Machine-credentialed warehouse query endpoint for the Vercel dashboard
 * (kapso-ops-dashboard). Lives here (NOT under /mcp/) so the OAuthProvider hands
 * it to us with no bearer-token machinery — we do our own API-key check instead
 * of the interactive Google OAuth flow that human MCP clients go through.
 *
 * Flow: dashboard -> POST /api/warehouse/query  (Authorization: Bearer <key>)
 *   -> constant-time compare the key against DASHBOARD_API_KEY
 *   -> forward the JSON body to the gateway with the shared secret
 *   -> gateway runs it read-only against the RDS and returns { rows }.
 */
app.post("/api/warehouse/query", async (c) => {
  const expected = c.env.DASHBOARD_API_KEY;
  if (!expected) {
    return c.json({ error: "endpoint_not_configured" }, 503);
  }
  const auth = c.req.header("authorization") ?? "";
  const presented = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (!presented || !timingSafeEqual(presented, expected)) {
    return c.json({ error: "unauthorized" }, 401);
  }

  // Cap the body so a bad/abusive caller can't stream an unbounded payload.
  const payload = await c.req.text();
  if (payload.length > 64 * 1024) {
    return c.json({ error: "payload_too_large" }, 413);
  }

  const upstream = await fetch(`${c.env.UPSTREAM_BASE}/api/warehouse/query`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-worker-shared-secret": c.env.WORKER_SHARED_SECRET,
    },
    body: payload,
  });
  // The gateway only ever returns JSON here; pass status + body straight back.
  const text = await upstream.text();
  return new Response(text, {
    status: upstream.status,
    headers: { "content-type": "application/json" },
  });
});

/**
 * Health + sanity page. Not required by the OAuth flow, just handy.
 */
app.get("/", (c) => {
  return c.json({
    service: "choiz-mcp-worker",
    message: "MCP gateway for the Choiz internal team. Configure as a Custom Connector in Claude.",
    authorize: "/authorize",
    token: "/token",
    register: "/register",
    api: "/mcp/*",
  });
});

export default app;
