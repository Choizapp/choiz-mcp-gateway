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
 *     -> we verify the email is @choiz.com.mx and email_verified=true
 *     -> we call completeAuthorization(...) with the user's email as props
 *         -> this mints an opaque token, stores it in OAUTH_KV, and gives us
 *            a redirect URL back to the original claude.ai redirect_uri
 *     -> we redirect the user there
 *   claude.ai then calls POST /token with the code and gets the bearer token.
 */

type Bindings = WorkerEnv & { OAUTH_PROVIDER: OAuthHelpers };

const app = new Hono<{ Bindings: Bindings }>();

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
  // `hd` hints the Workspace domain so the Google account picker pre-filters.
  googleUrl.searchParams.set("hd", c.env.ALLOWED_EMAIL_DOMAIN);
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
  const domain = c.env.ALLOWED_EMAIL_DOMAIN;
  if (!user.email.endsWith(`@${domain}`)) {
    return c.text(`Only @${domain} accounts are allowed. You signed in as ${user.email}.`, 403);
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
