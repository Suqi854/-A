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

// —— 云端值守的可靠触发：GitHub 自己的定时任务经常延迟或不触发，改由这里（Cloudflare 定时任务，按分钟准时）
// 交易日 08:15-15:50 每 5 分钟检查：GitHub 上没有云端值守在跑/排队，就启动一个（workflow_dispatch mode=run）。
// 需要 Worker 密钥 GH_TOKEN：只授权仓库 Suqi854/-A 的 Actions 读写（fine-grained token），由 设置云端自动启动.cmd 写入。
const REPO = "Suqi854/-A", WF = "aq5-cloud.yml";

async function gh(env, path, init = {}) {
  return fetch("https://api.github.com/repos/" + REPO + path, { ...init, headers: { "User-Agent": "aq5-relay",
    "Accept": "application/vnd.github+json", "Authorization": "Bearer " + String(env.GH_TOKEN || "").trim(), ...(init.headers || {}) } });
}

async function kickCloud(env, force = false) {
  const bj = new Date(Date.now() + 8 * 3600e3), wd = bj.getUTCDay(), hm = bj.getUTCHours() * 100 + bj.getUTCMinutes();
  let out;
  if (!env.GH_TOKEN) out = { ok: false, msg: "还没设置 GH_TOKEN（运行 设置云端自动启动.cmd）" };
  else if (!force && (wd === 0 || wd === 6 || hm < 815 || hm >= 1550)) out = { ok: true, msg: "不在值守时段" };
  else {
    const r = await gh(env, `/actions/workflows/${WF}/runs?per_page=10`);
    if (!r.ok) out = { ok: false, msg: "查询运行记录失败 " + r.status + (r.status === 401 ? "：GitHub 令牌无效或已过期，请重新运行 设置云端自动启动.cmd" : "") };
    else {
      const runs = (await r.json()).workflow_runs || [];
      const live = runs.find(x => x.status !== "completed");
      const today = bj.toISOString().slice(0, 10);
      // 今天已经有一次 8:30 以后启动、几分钟就结束的运行：多半是休市或没有状态，1 小时内不再重复启动
      const quick = runs.find(x => x.status === "completed" && x.created_at.slice(0, 10) === new Date(Date.now()).toISOString().slice(0, 10)
        && (new Date(x.updated_at) - new Date(x.run_started_at)) < 180e3 && Date.now() - new Date(x.updated_at) < 3600e3
        && new Date(new Date(x.run_started_at).getTime() + 8 * 3600e3).getUTCHours() * 100 + new Date(new Date(x.run_started_at).getTime() + 8 * 3600e3).getUTCMinutes() >= 830);
      if (live) out = { ok: true, msg: `云端值守在运行（#${live.run_number} ${live.status}）` };
      else if (quick && !force) out = { ok: true, msg: `最近一次运行很快就结束了（#${quick.run_number}），1 小时内不重复启动` };
      else {
        const d = await gh(env, `/actions/workflows/${WF}/dispatches`, { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ref: "main", inputs: { mode: "run" } }) });
        out = d.status === 204 ? { ok: true, msg: "已启动云端值守", started: true } : { ok: false, msg: "启动失败 " + d.status + " " + (await d.text()).slice(0, 120) };
      }
    }
  }
  out.time = bj.toISOString().slice(0, 19).replace("T", " ");
  await env.KV.put("kick", JSON.stringify(out));
  return out;
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(kickCloud(env));
  },
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname === "/__aq5/cloud") {  // 看云端触发的最近结果（不含任何密钥）；带 ?kick=1 并附中转密钥可手动触发
      if (url.searchParams.get("kick") === "1") {
        const want = (env.SECRET || "").trim(), got = (req.headers.get("x-aq5-secret") || "").trim();
        if (!want || got !== want) return new Response("forbidden", { status: 403 });
        return Response.json(await kickCloud(env, true));
      }
      return Response.json({ token: !!env.GH_TOKEN, last: JSON.parse((await env.KV.get("kick")) || "null") }, { headers: { "cache-control": "no-store" } });
    }
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
