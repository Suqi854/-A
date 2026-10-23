// A量化 固定地址中转：https://aq5-relay.<你的子域>.workers.dev → 当前的 trycloudflare 隧道地址
// 电脑引擎每次隧道地址变化都会带密钥 POST /__aq5/target 更新；其他请求原样转发给电脑。
const page = (msg) => `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>A量化</title><body style="background:#050608;color:#d4dbe2;font:16px system-ui;padding:40px;text-align:center">
<h2 style="color:#ffb000">A量化</h2><p>${msg}</p><p style="color:#6c7a89">电脑开机、A量化运行并开启「手机远程」后，刷新本页即可。<br>也可以先用 GitHub 网页版只读查看。</p></body>`;

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname === "/__aq5/target") {
      if (req.method === "POST") {
        if (!env.SECRET || req.headers.get("x-aq5-secret") !== env.SECRET) return new Response("forbidden", { status: 403 });
        const { target } = await req.json();
        if (!/^https:\/\/[a-z0-9-]+\.trycloudflare\.com$/.test(target || "")) return new Response("bad target", { status: 400 });
        await env.KV.put("target", target);
        return Response.json({ ok: true });
      }
      return Response.json({ ok: true, online: !!(await env.KV.get("target")) });
    }
    const target = await env.KV.get("target", { cacheTtl: 30 });
    const html = (m, s) => new Response(page(m), { status: s, headers: { "content-type": "text/html; charset=utf-8" } });
    if (!target) return html("电脑引擎还没有上线。", 503);
    const h = new Headers(req.headers);
    h.delete("host");
    try {
      const r = await fetch(new URL(url.pathname + url.search, target), {
        method: req.method, headers: h, redirect: "manual",
        body: ["GET", "HEAD"].includes(req.method) ? undefined : req.body,
      });
      if (r.status === 530 || r.status === 1033) return html("暂时连不上电脑（可能关机、休眠或隧道正在重启）。", 502);
      return new Response(r.body, { status: r.status, headers: new Headers(r.headers) });
    } catch (e) {
      return html("暂时连不上电脑（可能关机、休眠或隧道正在重启）。", 502);
    }
  },
};
