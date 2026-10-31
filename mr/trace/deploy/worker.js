// Optional Cloudflare Worker for a private, secret-link mr trace viewer (deploy it in your own account).
// Bindings: BUCKET (a private R2 bucket); secrets: UPLOAD_TOKEN, VIEW_TOKEN (wrangler secret put ...).
// PUT /upload with "Authorization: Bearer <UPLOAD_TOKEN>" stores the snapshot; GET /v/<VIEW_TOKEN> serves it.
export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const hdr = { "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow", "X-Content-Type-Options": "nosniff" };
    if (req.method === "PUT" && url.pathname === "/upload") {
      if (!(await same(req.headers.get("Authorization") || "", "Bearer " + env.UPLOAD_TOKEN))) return new Response("forbidden", { status: 403, headers: hdr });
      const body = await req.arrayBuffer();
      if (body.byteLength > 20e6) return new Response("too big", { status: 413, headers: hdr });
      await env.BUCKET.put("index.html", body, { httpMetadata: { contentType: "text/html; charset=utf-8" }, customMetadata: { uploaded: new Date().toISOString() } });
      return new Response("ok " + body.byteLength + "\n", { headers: hdr });
    }
    if ((req.method === "GET" || req.method === "HEAD") && (await same(url.pathname.replace(/\/$/, ""), "/v/" + env.VIEW_TOKEN))) {
      const o = await env.BUCKET.get("index.html");
      if (!o) return new Response("no snapshot uploaded yet", { status: 404, headers: hdr });
      return new Response(req.method === "HEAD" ? null : o.body, { headers: { ...hdr, "Content-Type": "text/html; charset=utf-8", "Last-Modified": o.uploaded.toUTCString() } });
    }
    return new Response("forbidden", { status: 403, headers: hdr });
  },
};
async function same(a, b) {
  const e = new TextEncoder();
  const [x, y] = await Promise.all([crypto.subtle.digest("SHA-256", e.encode(a)), crypto.subtle.digest("SHA-256", e.encode(b))]);
  return crypto.subtle.timingSafeEqual(x, y);
}
