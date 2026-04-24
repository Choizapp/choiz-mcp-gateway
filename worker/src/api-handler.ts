import type { UserProps, WorkerEnv } from "./types.js";

/**
 * API handler: invoked by the OAuth provider library ONLY after a valid
 * Bearer token has been presented on a request to an /mcp/* path.
 *
 * The library:
 *   1. Reads `Authorization: Bearer <token>` from the request.
 *   2. Looks the token up in the KV-backed store.
 *   3. If valid, calls us with `ctx.props` set to the props we attached at
 *      authorization time (see default-handler.ts).
 *   4. If invalid/expired, returns 401 before we ever run.
 *
 * So by the time this function is called, the caller has PROVEN they are a
 * specific @choiz.com.mx user (because Google signed off) and we can safely
 * stamp that identity into the upstream request.
 *
 * Transport: the upstream pipeline (gateway -> adapter -> MCP server) speaks
 * MCP Streamable HTTP end-to-end, which is what claude.ai uses. The adapter
 * layer (supergateway) handles translation from upstream SSE servers; from
 * the Worker's perspective this is a straight reverse proxy.
 */
export default {
  async fetch(
    request: Request,
    env: WorkerEnv,
    ctx: ExecutionContext & { props?: UserProps },
  ): Promise<Response> {
    const props = (ctx.props ?? {}) as UserProps;
    const email = props.email;
    if (!email) {
      // Defensive: should not happen if the library is configured correctly.
      return new Response(JSON.stringify({ error: "no_identity_in_token" }), {
        status: 401,
        headers: { "content-type": "application/json" },
      });
    }

    const reqUrl = new URL(request.url);
    const upstream = new URL(reqUrl.pathname + reqUrl.search, env.UPSTREAM_BASE);

    // Clone headers; strip anything that would break the proxy or leak state.
    const fwdHeaders = new Headers(request.headers);
    fwdHeaders.delete("host");
    fwdHeaders.delete("cf-connecting-ip");
    fwdHeaders.delete("cf-ray");
    fwdHeaders.delete("cf-visitor");
    fwdHeaders.delete("x-forwarded-for");
    fwdHeaders.delete("x-forwarded-proto");

    // Gateway trusts these two headers (set only by us, behind the shared secret).
    fwdHeaders.set("x-worker-shared-secret", env.WORKER_SHARED_SECRET);
    fwdHeaders.set("x-choiz-user-email", email);

    return fetch(upstream.toString(), {
      method: request.method,
      headers: fwdHeaders,
      body: request.body,
      redirect: "manual",
    });
  },
};
