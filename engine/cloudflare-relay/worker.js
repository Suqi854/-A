// A量化 固定地址中转：https://aq5-relay.<你的子域>.workers.dev
// 电脑在线：原样转发到电脑的 trycloudflare 隧道（完整版）；
// 电脑离线：自动改为提供 GitHub 网页版（只读 + 手机本地分析 + 云端代跑的数据），不再报错。
// 电脑引擎每次隧道地址变化都会带密钥 POST /__aq5/target 更新。
const PAGES = "https://suqi854.github.io/-A";

async function upstream(target, req, url, ms) {
  const h = new Headers(req.headers);
  h.delete("host");
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), ms);
  try {
    const r = await fetch(new URL(url.pathname + url.search, target), {
      method: req.method, headers: h, redirect: "manual", signal: ac.signal,
      body: ["GET", "HEAD"].includes(req.method) ? undefined : req.body,
    });
    if ([502, 504, 530, 1033].includes(r.status)) return null;
    return r;
  } catch (e) {
    return null;
  } finally {
    clearTimeout(t);
  }
}

async function pages(url) {
  const r = await fetch(PAGES + url.pathname + url.search, { cf: { cacheTtl: 60, cacheEverything: true } });
  const out = new Response(r.body, r);
  out.headers.set("x-aq5-mode", "static");
  out.headers.set("cache-control", "no-cache");
  return out;
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname === "/__aq5/target") {
      if (req.method === "POST") {
        const want = (env.SECRET || "").trim(), got = (req.headers.get("x-aq5-secret") || "").trim();
        if (!want || got !== want) return new Response("forbidden", { status: 403 });
        const { target } = await req.json();
        if (!/^https:\/\/[a-z0-9-]+\.trycloudflare\.com$/.test(target || "")) return new Response("bad target", { status: 400 });
        await env.KV.put("target", target);
        return Response.json({ ok: true });
      }
      return Response.json({ ok: true, online: !!(await env.KV.get("target")) });
    }
    const target = await env.KV.get("target", { cacheTtl: 30 });
    // 电脑是否真的在线（云端代跑据此决定要不要接手）
    if (url.pathname === "/__aq5/alive") {
      if (!target) return Response.json({ alive: false });
      const r = await upstream(target, new Request(url.origin + "/api/auth/me"), new URL(url.origin + "/api/auth/me"), 6000);
      return Response.json({ alive: !!(r && r.status === 200) }, { headers: { "cache-control": "no-store" } });
    }
    const isApi = url.pathname.startsWith("/api/");
    if (target) {
      const r = await upstream(target, req, url, isApi ? 25000 : 6000);
      if (r) return new Response(r.body, { status: r.status, headers: new Headers(r.headers) });
    }
    if (isApi) return Response.json({ error: "电脑离线" }, { status: 503 });
    return pages(url);
  },
};
