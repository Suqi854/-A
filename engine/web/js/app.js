/* A-QUANT TERMINAL 前端 */
(function () {
  'use strict';
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (v, d = 2) => v == null || isNaN(v) ? '--' : Number(v).toFixed(d);
  const sgn = (v, d = 2) => v == null || isNaN(v) ? '--' : (v > 0 ? '+' : '') + Number(v).toFixed(d);
  const pct = (v, d = 2) => v == null || isNaN(v) ? '--' : sgn(v, d) + '%';
  const cls = v => v > 0 ? 'up' : v < 0 ? 'down' : 'flat';
  const money = v => { if (v == null || isNaN(v)) return '--'; const a = Math.abs(v); return a >= 1e8 ? (v / 1e8).toFixed(2) + '亿' : a >= 1e4 ? (v / 1e4).toFixed(a >= 1e6 ? 0 : 1) + '万' : v.toFixed(0); };
  const yuan = v => v == null ? '--' : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const short = c => (c || '').replace(/^(sh|sz|bj)/, '');
  const PHASE = { closed: ['休市', 'muted'], before: ['盘前', 'muted'], auction: ['集合竞价', 'amber'], pre: ['竞价结束', 'amber'],
    trading: ['交易中', 'up'], noon: ['午间休市', 'muted'], close_auction: ['收盘竞价', 'amber'], after: ['已收盘', 'muted'] };
  const SESSION = { auction: '竞价选股', close: '尾盘选股', manual: '手动选股' };
  const S = { page: null, ov: null, settings: null, anly: null, kchart: null, ichart: null, acct: 'me', run: null, timers: {}, seen: new Set() };

  const STATIC = !!window.AQ_STATIC;
  if (STATIC) document.body.classList.add('static');
  const normCode = c => { c = String(c || '').trim().toLowerCase(); return /^\d{6}$/.test(c) ? (/^[65]/.test(c) ? 'sh' : 'sz') + c : c; };
  async function sj(file) {
    const r = await fetch('data/' + file + '?t=' + Math.floor(Date.now() / 60000));
    if (!r.ok) throw new Error('公开数据里没有这项内容');
    return r.json();
  }
  async function staticApi(path, body) {
    if (body !== undefined) throw new Error('这是公开只读版：下单、选股、设置等操作请在电脑端进行');
    const [p, qs] = path.split('?'); const q = new URLSearchParams(qs || ''); const code = normCode(q.get('code'));
    switch (p) {
      case 'overview': return sj('latest.json');
      case 'picks/runs': return sj('runs.json');
      case 'picks/candidates': { const r = (await sj('runs.json')).find(x => String(x.id) === q.get('run')); return r ? r.cand_list || [] : []; }
      case 'picks/state': return { running: false };
      case 'analyze': try { return await sj('analysis/' + code + '.json'); } catch { throw new Error('公开版只包含近7天入选股和AI持仓的分析，这只股票不在其中'); }
      case 'kline': { const x = await sj('analysis/' + code + '.json'); return { code, rows: x.kline || [] }; }
      case 'intraday': return sj('intraday/' + code + '.json');
      case 'sim': return sj('ai.json');
      case 'learn': return sj('learn.json');
      case 'events': return sj('events.json');
      case 'settings': return sj('settings.json');
      case 'strategies': return sj('strategies.json');
      case 'quotes': return {};
      case 'search': { const v = (q.get('q') || '').toLowerCase(); const idx = await sj('analysis/index.json').catch(() => []);
        return idx.filter(x => x.code.includes(v) || (x.name || '').toLowerCase().includes(v)).slice(0, 12); }
      default: throw new Error('公开只读版不支持此功能，请在电脑端使用');
    }
  }
  async function api(path, body) {
    if (STATIC) return staticApi(path, body);
    const opt = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    const r = await fetch('/api/' + path, opt);
    const j = await r.json().catch(() => ({ error: '服务无响应' }));
    if (!r.ok || (j && j.error)) throw new Error(j.error || r.statusText);
    return j;
  }
  function toast(msg, err) {
    const t = document.createElement('div'); t.className = 'toast' + (err ? ' err' : ''); t.textContent = msg;
    document.body.appendChild(t); setTimeout(() => t.remove(), err ? 5000 : 2600);
  }
  const panel = (title, body, opts = {}) => `<div class="panel ${opts.cls || ''}" ${opts.id ? `id="${opts.id}"` : ''}>
    <div class="ph"><h3>${title}</h3><div class="r">${opts.right || ''}</div></div><div class="pb ${opts.np ? 'np' : ''}">${body}</div></div>`;
  const nameCell = (name, code) => `<span class="nm">${esc(name || '')}</span><span class="cd">${short(code)}</span>`;

  // ------------------------------------------------------------------ 路由与快捷键
  const PAGES = ['mon', 'pick', 'anly', 'sim', 'learn', 'bt', 'log', 'set'];
  function go(p, arg) {
    if (!PAGES.includes(p)) p = 'mon';
    if (location.hash.slice(1).split('/')[0] !== p || arg) location.hash = p + (arg ? '/' + arg : '');
    else show(p);
  }
  function show(p, arg) {
    S.page = p;
    $$('#nav a').forEach(a => a.classList.toggle('on', a.dataset.p === p));
    $$('.page').forEach(s => s.classList.toggle('on', s.id === 'p-' + p));
    $('#main').scrollTop = 0;
    ({ mon: renderMon, pick: renderPick, anly: () => renderAnly(arg), sim: renderSim, learn: renderLearn, bt: renderBt, log: renderLog, set: renderSet })[p]();
  }
  window.addEventListener('hashchange', () => { const [p, a] = location.hash.slice(1).split('/'); show(PAGES.includes(p) ? p : 'mon', a); });
  document.addEventListener('keydown', e => {
    const m = /^F([1-8])$/.exec(e.key);
    if (m) { e.preventDefault(); go(PAGES[+m[1] - 1]); }
    else if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') { e.preventDefault(); $('#cmd').focus(); }
    else if (e.key === 'Escape') { $('.modal')?.remove(); $('#suggest').style.display = 'none'; }
  });

  // ------------------------------------------------------------------ 命令栏
  let sugIdx = 0, sugList = [], sugTimer;
  $('#cmd').addEventListener('input', e => {
    clearTimeout(sugTimer);
    const v = e.target.value.trim();
    if (!v) { $('#suggest').style.display = 'none'; return; }
    sugTimer = setTimeout(async () => {
      try { sugList = await api('search?q=' + encodeURIComponent(v)); } catch { sugList = []; }
      sugIdx = 0; drawSug();
    }, 150);
  });
  function drawSug() {
    const el = $('#suggest');
    if (!sugList.length) { el.style.display = 'none'; return; }
    el.innerHTML = sugList.map((s, i) => `<div class="${i === sugIdx ? 'on' : ''}" data-c="${s.code}"><span>${esc(s.name)}</span><span>${short(s.code)}</span></div>`).join('');
    el.style.display = 'block';
  }
  $('#suggest').addEventListener('mousedown', e => { const d = e.target.closest('[data-c]'); if (d) pickCode(d.dataset.c); });
  $('#cmd').addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') { sugIdx = Math.min(sugList.length - 1, sugIdx + 1); drawSug(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { sugIdx = Math.max(0, sugIdx - 1); drawSug(); e.preventDefault(); }
    else if (e.key === 'Enter') {
      const v = e.target.value.trim().toUpperCase();
      const m = /^F([1-8])$/.exec(v);
      if (m) { go(PAGES[+m[1] - 1]); e.target.value = ''; return; }
      if (sugList[sugIdx]) pickCode(sugList[sugIdx].code);
      else if (/^(SH|SZ)?\d{6}$/.test(v)) pickCode(v.toLowerCase());
    }
  });
  $('#cmd').addEventListener('blur', () => setTimeout(() => $('#suggest').style.display = 'none', 150));
  function pickCode(c) { $('#cmd').value = ''; $('#suggest').style.display = 'none'; $('#cmd').blur(); go('anly', c); }

  // ------------------------------------------------------------------ 顶栏、跑马灯
  setInterval(() => { const d = new Date(); $('#clock').textContent = d.toTimeString().slice(0, 8); }, 500);
  function header(ov) {
    const [t, c] = PHASE[ov.phase] || ['--', 'muted'];
    const ph = $('#phase'); ph.className = 'phase ' + c + (ov.phase === 'trading' || ov.phase === 'auction' ? ' live' : ''); ph.innerHTML = `<i></i>${t}`;
    $('#mockTag').hidden = !ov.mock && !STATIC; if (STATIC) $('#mockTag').textContent = '公开只读';
    $('#lastUpd').textContent = (STATIC ? '数据时间 ' + ov.time.slice(5, 16) : '更新 ' + ov.time.slice(11));
    const items = [...ov.indices, ...ov.overseas, ...ov.watchlist.filter(w => w.price)];
    const html = items.map(q => `<span class="it" data-c="${q.code}"><b>${esc(q.name)}</b><span class="${cls(q.pct)}">${num(q.price)} ${pct(q.pct)}</span></span>`).join('');
    const inner = $('#tapeInner'); if (inner.dataset.h !== html) { inner.innerHTML = html + html; inner.dataset.h = html; }
    const lr = ov.last_run;
    const badge = $('#pickBadge');
    if (lr && lr.date === ov.time.slice(0, 10) && lr.picks > 0) { badge.hidden = false; badge.textContent = lr.picks; } else badge.hidden = true;
    // 新事件提醒
    const fresh = S.seen.size > 0;
    (ov.events || []).slice().reverse().forEach(ev => {
      if (S.seen.has(ev.id)) return;
      S.seen.add(ev.id);
      if (fresh && (ev.kind === 'pick' || ev.kind === 'trade' || ev.kind === 'alert')) toast(ev.title, ev.level === 'error');
    });
  }
  $('#tape').addEventListener('click', e => { const it = e.target.closest('[data-c]'); if (it && /^(sh|sz)\d{6}$/.test(it.dataset.c) && !/^(sh000|sz399)/.test(it.dataset.c)) go('anly', it.dataset.c); });

  async function poll() {
    try {
      const ov = await api('overview');
      S.ov = ov; header(ov);
      if (S.page === 'mon') updateMon(ov);
    } catch (e) { $('#lastUpd').textContent = '连接中断'; }
  }
  setInterval(() => { if (!document.hidden) poll(); }, STATIC ? 60000 : 5000);

  // ================================================================== F1 监控
  function renderMon() {
    $('#p-mon').innerHTML = `<div class="grid">
      ${panel('INDEX <small>主要指数 · 分时</small>', '<div class="idx" id="idxCards"></div>', { cls: 'c12', np: true })}
      ${panel('BREADTH <small>市场温度 · 沪深主板</small>', '<div id="breadth" class="loading">载入</div>', { cls: 'c4' })}
      ${panel('SIGNAL <small>最新选股</small>', '<div id="monPick" class="loading">载入</div>', { cls: 'c4', right: '<button class="btn sm" onclick="location.hash=\'pick\'">全部 ›</button>' })}
      ${panel('ACCOUNT <small>模拟账户</small>', '<div id="monAcct"></div>', { cls: 'c4', right: '<button class="btn sm" onclick="location.hash=\'sim\'">模拟盘 ›</button>' })}
      ${STATIC ? '' : panel('WATCHLIST <small>自选监控 · 3秒刷新</small>', '<div class="scroll" style="max-height:420px"><table class="t" id="wlT"></table></div>',
        { cls: 'c8', np: true, right: `<input class="in" id="wlAdd" placeholder="代码 如600519" style="width:130px;height:22px"><button class="btn sm pri" id="wlAddB">+ 自选</button>` })}
      ${panel('OVERSEAS <small>外围市场</small>', '<table class="t" id="ovsT"></table>', { cls: 'c4', np: true })}
      ${panel('TOP <small>涨幅榜</small>', '<div class="scroll" style="max-height:330px"><table class="t" id="topT"></table></div>', { cls: 'c4', np: true })}
      ${panel('INFLOW <small>主力净流入榜</small>', '<div class="scroll" style="max-height:330px"><table class="t" id="inT"></table></div>', { cls: 'c4', np: true })}
      ${panel('FEED <small>实时动态</small>', '<div class="scroll" style="max-height:330px" id="feed"></div>', { cls: 'c4', np: true })}
    </div>`;
    if (!STATIC) $('#wlAddB').onclick = addWatch; if (!STATIC) $('#wlAdd').onkeydown = e => { if (e.key === 'Enter') addWatch(); };
    if (S.ov) updateMon(S.ov);
    loadIdxSpark(); clearInterval(S.timers.spark); S.timers.spark = setInterval(() => S.page === 'mon' && loadIdxSpark(), 60000);
  }
  async function addWatch() {
    const v = $('#wlAdd').value.trim(); if (!v) return;
    try { const r = await api('search?q=' + encodeURIComponent(v)); if (!r.length) throw new Error('找不到该股票');
      await api('watchlist', { code: r[0].code, op: 'add' }); $('#wlAdd').value = ''; toast('已加入自选：' + r[0].name); poll();
    } catch (e) { toast(e.message, 1); }
  }
  async function loadIdxSpark() {
    if (!S.ov) return;
    for (const q of S.ov.indices) {
      try { const d = await api('intraday?code=' + q.code); const cv = $(`#sp-${q.code}`); if (cv) Charts.spark(cv, d.points.map(p => p.p), d.prev); } catch { }
    }
  }
  function updateMon(ov) {
    if (!$('#idxCards')) return;
    // 指数
    const ic = $('#idxCards');
    if (ic.children.length !== ov.indices.length) {
      ic.innerHTML = ov.indices.map(q => `<div class="card" data-c="${q.code}"><div class="n"><span>${esc(q.name)}</span><span class="mono">${short(q.code)}</span></div>
        <div class="p mono"></div><div class="c"></div><canvas id="sp-${q.code}"></canvas></div>`).join('');
      loadIdxSpark();
    }
    ov.indices.forEach(q => { const c = ic.querySelector(`[data-c="${q.code}"]`); if (!c) return;
      c.querySelector('.p').className = 'p mono ' + cls(q.pct); c.querySelector('.p').textContent = num(q.price);
      c.querySelector('.c').innerHTML = `<span class="${cls(q.pct)}">${sgn(q.chg)}  ${pct(q.pct)}</span> <span class="muted">额 ${money(q.amount)}</span>`; });
    // 宽度
    const b = ov.breadth;
    if (b && b.total) {
      const tot = b.total, w = x => (x / tot * 100).toFixed(1) + '%';
      const mx = Math.max(...b.dist, 1), labels = ['<-7', '-5', '-3', '-1', '<0', '0', '>0', '1', '3', '5', '>7'];
      const rg = ov.regime || {}, rc = rg.code === 'bull' ? 'up' : rg.code === 'bear' ? 'down' : 'amber';
      $('#breadth').className = '';
      $('#breadth').innerHTML = `<div class="breadth-bar"><div style="width:${w(b.up)};background:var(--up)">${b.up}</div><div style="width:${w(b.flat)};background:#3d4955">${b.flat}</div><div style="width:${w(b.down)};background:var(--down)">${b.down}</div></div>
        <div class="stat4"><div><div class="l">涨停</div><div class="v up">${b.limit_up}</div></div><div><div class="l">跌停</div><div class="v down">${b.limit_down}</div></div>
        <div><div class="l">成交额</div><div class="v">${b.amount_yi >= 10000 ? (b.amount_yi / 10000).toFixed(2) + '万亿' : b.amount_yi + '亿'}</div></div><div><div class="l">主力净额</div><div class="v ${cls(b.main_net_yi)}">${sgn(b.main_net_yi, 1)}亿</div></div></div>
        <div class="dist">${b.dist.map((v, i) => `<div style="height:${(v / mx * 100).toFixed(0)}%;background:${i < 5 ? 'var(--down)' : i > 5 ? 'var(--up)' : '#3d4955'}"><span>${v}</span></div>`).join('')}</div>
        <div class="distl">${labels.map(l => `<span>${l}</span>`).join('')}</div>
        <div class="regime"><span class="s ${rc}">${esc(rg.state || '--')}</span><span class="hint">${esc(rg.desc || '')}<br>中位涨幅 <b class="${cls(b.median_pct)}">${pct(b.median_pct)}</b></span></div>`;
    } else if (b && b.error) $('#breadth').innerHTML = `<div class="empty">全市场快照获取失败<br><small>${esc(b.error)}</small></div>`;
    // 最新选股
    const lr = ov.last_run, mp = $('#monPick'); mp.className = '';
    if (!lr) mp.innerHTML = `<div class="empty">还没有选股记录<br><br>交易日 09:26 / 14:45 自动运行<br><br><button class="btn pri" onclick="location.hash='pick'">去手动选股</button></div>`;
    else mp.innerHTML = `<div class="row"><span class="chip a">${SESSION[lr.session] || lr.session}</span><span class="mono muted">${lr.date} ${lr.finished || ''}</span><span class="sp"></span><span class="mono muted">扫描${lr.scanned}·精评${lr.candidates}</span></div>
        ${lr.items.length ? `<table class="t" style="margin-top:6px"><tr><th>股票</th><th>评分</th><th>买入价</th><th>止损</th><th>目标</th></tr>${lr.items.map(p => `<tr class="click" data-c="${p.code}"><td>${nameCell(p.name, p.code)}</td><td class="amber">${num(p.score, 1)}</td><td>${num(p.price)}</td><td class="down">${num(p.stop)}</td><td class="up">${num(p.target1)}</td></tr>`).join('')}</table>`
        : `<div class="nopick" style="margin-top:8px"><b>宁缺毋滥</b><span class="hint">本次没有达到门槛的机会，今天先观望</span></div>`}`;
    mp.querySelectorAll('[data-c]').forEach(tr => tr.onclick = () => go('anly', tr.dataset.c));
    // 账户
    $('#monAcct').innerHTML = Object.values(ov.accounts).map(a => `<div style="margin-bottom:10px"><div class="row"><b>${esc(a.name)}</b><span class="sp"></span><span class="mono">${yuan(a.equity)}</span></div>
      <div class="row hint mono"><span>今日 <b class="${cls(a.today_pnl)}">${sgn(a.today_pnl)}</b></span><span>累计 <b class="${cls(a.total_pnl)}">${sgn(a.total_pnl)} (${pct(a.total_pct)})</b></span><span class="sp"></span><span>持仓 ${a.positions.length}</span></div>
      ${a.positions.slice(0, 4).map(p => `<div class="row mono" style="font-size:12px;cursor:pointer" data-c="${p.code}"><span>${esc(p.name)}</span><span class="sp"></span><span class="${cls(p.pnl_pct)}">${pct(p.pnl_pct)}</span><span class="muted">距止损 ${pct(p.to_stop, 1)}</span></div>`).join('')}</div>`).join('<div class="sep"></div>');
    $('#monAcct').querySelectorAll('[data-c]').forEach(r => r.onclick = () => go('anly', r.dataset.c));
    // 自选
    const wl = $('#wlT') || document.createElement('table'); const old = {}; wl.querySelectorAll('tr[data-c]').forEach(tr => old[tr.dataset.c] = +tr.dataset.p);
    wl.innerHTML = `<thead><tr><th>股票</th><th>现价</th><th>涨跌幅</th><th>涨跌</th><th class="hide-m">量比</th><th class="hide-m">换手</th><th class="hide-m">振幅</th><th class="hide-m">成交额</th><th class="hide-m">市盈</th><th></th></tr></thead><tbody>` +
      ov.watchlist.map(q => `<tr class="click" data-c="${q.code}" data-p="${q.price || 0}"><td>${nameCell(q.name || '--', q.code)}</td><td class="${cls(q.pct)} ${old[q.code] && q.price !== old[q.code] ? (q.price > old[q.code] ? 'flash-up' : 'flash-down') : ''}">${num(q.price)}</td>
        <td class="${cls(q.pct)}">${pct(q.pct)}</td><td class="${cls(q.chg)}">${sgn(q.chg)}</td><td class="hide-m ${q.vr > 2 ? 'amber' : ''}">${num(q.vr)}</td><td class="hide-m">${num(q.turnover)}%</td><td class="hide-m">${num(q.amp)}%</td>
        <td class="hide-m">${money(q.amount)}</td><td class="hide-m">${num(q.pe, 1)}</td><td><button class="btn sm" data-rm="${q.code}">×</button></td></tr>`).join('') + '</tbody>';
    wl.querySelectorAll('tr[data-c]').forEach(tr => tr.onclick = e => { if (e.target.dataset.rm) return; go('anly', tr.dataset.c); });
    wl.querySelectorAll('[data-rm]').forEach(b => b.onclick = async () => { await api('watchlist', { code: b.dataset.rm, op: 'remove' }); poll(); });
    $('#ovsT').innerHTML = `<tr><th>市场</th><th>点位</th><th>涨跌幅</th></tr>` + ov.overseas.map(q => `<tr><td class="nm">${esc(q.name)}</td><td>${num(q.price)}</td><td class="${cls(q.pct)}">${pct(q.pct)}</td></tr>`).join('');
    const rowT = r => `<tr class="click" data-c="${r.code}"><td>${nameCell(r.name, r.code)}</td><td class="${cls(r.pct)}">${num(r.price)}</td><td class="${cls(r.pct)}">${pct(r.pct)}</td><td class="${cls(r.main_net)}">${money(r.main_net)}</td></tr>`;
    $('#topT').innerHTML = `<tr><th>股票</th><th>现价</th><th>涨幅</th><th>主力</th></tr>` + ov.top.map(rowT).join('');
    $('#inT').innerHTML = `<tr><th>股票</th><th>现价</th><th>涨幅</th><th>主力</th></tr>` + ov.inflow.map(rowT).join('');
    [$('#topT'), $('#inT')].forEach(t => t.querySelectorAll('[data-c]').forEach(tr => tr.onclick = () => go('anly', tr.dataset.c)));
    $('#feed').innerHTML = ov.events.length ? ov.events.map(evHtml).join('') : '<div class="empty">暂无动态</div>';
  }
  const evHtml = e => `<div class="ev ${e.level}"><div class="tm">${e.time.slice(5, 16).replace(' ', '<br>')}</div><div><div class="tt">${esc(e.title)}</div>${e.body ? `<div class="bd">${esc(e.body)}</div>` : ''}</div></div>`;

  // ================================================================== F2 选股
  async function renderPick() {
    const st = S.settings || await api('settings').catch(() => null); S.settings = st;
    const times = st ? st.pick.times : ['09:26', '14:45'];
    $('#p-pick').innerHTML = `<div class="grid">
      ${panel('AUTO PICK <small>自动选股 · 宁缺毋滥</small>', `<div class="row"><span class="chip a">竞价选股 ${times[0]}</span><span class="chip a">尾盘选股 ${times[1]}</span>
          <span class="hint">交易日自动运行：全主板扫描 → 多因子 + 8 种策略精评 → 门槛 ${st ? st.pick.min_score : 72} 分（随大盘状态自动收紧）→ 最多 ${st ? st.pick.max_picks : 5} 只</span>
          <span class="sp"></span><button class="btn pri rw" id="runPick">▶ 立即选股</button></div><div id="pickProg"></div>`, { cls: 'c12' })}
      ${panel('RUNS <small>历史</small>', '<div id="runList" class="scroll" style="max-height:70vh"></div>', { cls: 'c3', np: true })}
      <div class="c9" id="runDetail"><div class="loading">载入</div></div>
    </div>`;
    $('#runPick').onclick = async () => { try { await api('picks/run', { session: 'manual' }); toast('开始选股…'); watchPick(); } catch (e) { toast(e.message, 1); } };
    await loadRuns();
    if (!STATIC) watchPick();
  }
  async function loadRuns(sel) {
    const runs = await api('picks/runs?limit=40');
    const el = $('#runList'); if (!el) return;
    if (!runs.length) { el.innerHTML = '<div class="empty">暂无记录</div>'; $('#runDetail').innerHTML = panel('RESULT', '<div class="empty">点「立即选股」先跑一次看看<br><small>收盘后跑会用当天收盘数据</small></div>'); return; }
    S.runs = runs; S.run = sel || (S.run && runs.find(r => r.id === S.run) ? S.run : runs[0].id);
    el.innerHTML = `<table class="t">${runs.map(r => `<tr class="click ${r.id === S.run ? 'bgup' : ''}" data-id="${r.id}"><td><div class="mono">${r.date.slice(5)} ${(r.started || '').slice(0, 5)}</div><div class="muted" style="font-size:11px">${SESSION[r.session] || r.session}</div></td>
      <td>${r.picks ? `<span class="chip a">${r.picks} 只</span>` : '<span class="chip">观望</span>'}</td></tr>`).join('')}</table>`;
    el.querySelectorAll('[data-id]').forEach(tr => tr.onclick = () => { S.run = +tr.dataset.id; loadRuns(S.run); });
    showRun(runs.find(r => r.id === S.run));
  }
  async function showRun(r) {
    const m = r.market || {}, br = m.breadth || {}, rg = m.regime || {};
    const cards = r.items.map(p => {
      const pl = { stop: p.stop, target1: p.target1, target2: p.target2 };
      return `<div class="pick"><div class="hd"><span class="nm">${esc(p.name)}</span><span class="cd">${short(p.code)}</span></div>
        <div class="score"><b>${num(p.score, 1)}</b><small>综合评分 · 估计胜率 ${num(p.prob * 100, 0)}%</small></div>
        <div class="row" style="margin-top:6px">${p.strategies.slice(0, 3).map(s => `<span class="chip a">${esc(s.name)}</span>`).join('')}</div>
        <div class="plan"><div><div class="l">选出价</div><div class="v">${num(p.price)}</div></div>
          <div><div class="l">止损</div><div class="v down">${num(pl.stop)}</div><div class="p down">${pct((pl.stop / p.price - 1) * 100, 1)}</div></div>
          <div><div class="l">止盈①</div><div class="v up">${num(pl.target1)}</div><div class="p up">${pct((pl.target1 / p.price - 1) * 100, 1)}</div></div>
          <div><div class="l">止盈②</div><div class="v up">${num(pl.target2)}</div><div class="p up">${pct((pl.target2 / p.price - 1) * 100, 1)}</div></div></div>
        <div class="hint">机会：</div><ul class="why">${p.reasons.map(x => `<li>${esc(x)}</li>`).join('')}</ul>
        ${p.risks.length ? `<div class="hint" style="margin-top:4px">风险：</div><ul class="why risk">${p.risks.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
        ${p.ret_1d != null ? `<div class="sep"></div><div class="row mono" style="font-size:12px"><span class="muted">事后验证</span><span>1日 <b class="${cls(p.ret_1d)}">${pct(p.ret_1d)}</b></span><span>3日 <b class="${cls(p.ret_3d)}">${pct(p.ret_3d)}</b></span><span>5日 <b class="${cls(p.ret_5d)}">${pct(p.ret_5d)}</b></span><span class="chip">${esc(p.outcome || '')}</span></div>` : ''}
        <div class="row" style="margin-top:8px"><button class="btn sm" data-a="${p.code}">详细分析</button><span class="sp"></span><button class="btn sm buy rw" data-b='${esc(JSON.stringify({ code: p.code, name: p.name, price: p.price, stop: p.stop, target1: p.target1, target2: p.target2, pick_id: p.id }))}'>模拟买入</button></div></div>`;
    }).join('');
    $('#runDetail').innerHTML = panel(`RESULT <small>${r.date} ${SESSION[r.session] || r.session} · ${r.started || ''}</small>`,
      `<div class="row hint mono" style="margin-bottom:10px"><span>扫描 <b class="amber">${r.scanned}</b></span><span>精评 <b class="amber">${r.candidates}</b></span><span>门槛 <b class="amber">${num(m.min_score, 0)}</b></span>
        <span>大盘 <b>${esc(rg.state || '--')}</b></span><span>上涨 <b class="up">${br.up ?? '--'}</b> / 下跌 <b class="down">${br.down ?? '--'}</b></span><span>涨停 <b class="up">${br.limit_up ?? '--'}</b></span></div>
       ${r.items.length ? `<div class="picks">${cards}</div>` : `<div class="nopick"><b>宁缺毋滥 · 本次无机会</b><span class="hint">扫描 ${r.scanned} 只、精评 ${r.candidates} 只，没有股票同时满足：综合分≥${num(m.min_score, 0)}、至少一个策略信号、盈亏比≥1.4、不过热。<br>空仓也是一种仓位。</span></div>`}`, {}) +
      `<div style="height:10px"></div>` + panel('CANDIDATES <small>候选前30（参考，不是推荐）</small>', '<div class="scroll" style="max-height:420px"><table class="t" id="candT"><tr><td class="loading">载入</td></tr></table></div>', { np: true });
    $$('#runDetail [data-a]').forEach(b => b.onclick = () => go('anly', b.dataset.a));
    $$('#runDetail [data-b]').forEach(b => b.onclick = () => openOrder(JSON.parse(b.dataset.b)));
    const c = await api('picks/candidates?run=' + r.id);
    const names = { breakout: '突破', pullback: '回踩', volume_start: '放量', oversold: '超跌', macd_cross: '金叉', accumulate: '吸筹', tail_strong: '尾强', auction_strong: '竞强' };
    $('#candT').innerHTML = `<tr><th>#</th><th class="l">股票</th><th>评分</th><th>价格</th><th class="l">信号</th></tr>` + c.map((x, i) => `<tr class="click" data-c="${x.code}"><td class="muted">${i + 1}</td><td class="l">${nameCell(x.name, x.code)}</td><td class="${x.score >= m.min_score ? 'amber' : ''}">${num(x.score, 1)}</td><td>${num(x.price)}</td><td class="l">${x.strategies.map(s => `<span class="chip">${names[s] || s}</span>`).join(' ')}</td></tr>`).join('');
    $$('#candT [data-c]').forEach(tr => tr.onclick = () => go('anly', tr.dataset.c));
  }
  async function watchPick() {
    clearInterval(S.timers.pick);
    const tick = async () => {
      const st = await api('picks/state').catch(() => null); const el = $('#pickProg'); if (!st || !el) return;
      if (st.running) { el.innerHTML = `<div class="hint mono" style="margin-top:8px">${esc(st.stage)} ${st.progress}/${st.total || '?'}</div><div class="prog"><i style="width:${st.total ? st.progress / st.total * 100 : 5}%"></i></div>`; $('#runPick').disabled = true; }
      else { if ($('#runPick').disabled) { $('#runPick').disabled = false; el.innerHTML = st.error ? `<div class="up">失败：${esc(st.error)}</div>` : ''; S.run = null; loadRuns(); clearInterval(S.timers.pick); } }
    };
    tick(); S.timers.pick = setInterval(tick, 1000);
  }

  // ================================================================== F3 个股
  async function renderAnly(code) {
    code = code || S.lastCode || (S.ov && S.ov.watchlist[0] && S.ov.watchlist[0].code) || 'sh600519';
    S.lastCode = code;
    $('#p-anly').innerHTML = `<div class="loading">分析 ${short(code)} 中</div>`;
    let a;
    try { a = await api('analyze?code=' + code); } catch (e) { $('#p-anly').innerHTML = `<div class="empty">${STATIC ? '' : '分析失败：'}${esc(e.message)}</div>` + (STATIC ? await staticIndex() : ''); return; }
    S.anly = a;
    const q = a.quote, pl = a.plan, mf = a.main_force;
    const ring = (v, col) => `<div class="ring"><svg width="96" height="96"><circle cx="48" cy="48" r="42" fill="none" stroke="#1c242d" stroke-width="7"/><circle cx="48" cy="48" r="42" fill="none" stroke="${col}" stroke-width="7" stroke-dasharray="${(v / 100 * 264).toFixed(1)} 264" style="filter:drop-shadow(0 0 6px ${col})"/></svg><b style="color:${col}">${num(v, 0)}</b></div>`;
    const tone = { buy: 'var(--up)', watch_buy: 'var(--amber)', hold: 'var(--muted)', reduce: 'var(--down)', avoid: 'var(--down)' }[a.tone];
    const mfc = mf.tone === 'up' ? 'up' : mf.tone === 'down' ? 'down' : 'amber';
    const maxF = Math.max(...a.flows.map(f => Math.abs(f.main)), 1);
    const inWl = S.ov && S.ov.watchlist.some(w => w.code === a.code);
    $('#p-anly').innerHTML = `<div class="grid">
      <div class="panel c12"><div class="qhead"><span class="nm">${esc(a.name)}</span><span class="mono muted">${a.code.toUpperCase()}</span>
        <span class="px ${cls(q.pct)}" id="aPx">${num(q.price)}</span><span class="ch ${cls(q.pct)}" id="aCh">${sgn(q.chg)} ${pct(q.pct)}</span><span class="sp"></span>
        <button class="btn rw" id="aWl">${inWl ? '已自选' : '+ 自选'}</button><button class="btn buy rw" id="aBuy">模拟买入</button><button class="btn sell rw" id="aSell">模拟卖出</button><button class="btn rw" id="aBt">回测</button></div>
        <div class="qgrid"><div><span>今开</span>${num(q.open)}</div><div><span>最高</span><b class="up">${num(q.high)}</b></div><div><span>最低</span><b class="down">${num(q.low)}</b></div><div><span>昨收</span>${num(q.prev)}</div>
          <div><span>量比</span>${num(q.vr)}</div><div><span>换手</span>${num(q.turnover)}%</div><div><span>成交额</span>${money(q.amount)}</div><div><span>主力净额</span><b class="${cls(q.main_net)}">${money(q.main_net)}</b></div>
          <div><span>市盈</span>${num(q.pe, 1)}</div><div><span>市净</span>${num(q.pb)}</div><div><span>总市值</span>${num(q.mcap, 0)}亿</div><div><span>流通</span>${num(q.fmcap, 0)}亿</div>
          <div><span>涨停</span>${num(q.up_limit)}</div><div><span>跌停</span>${num(q.dn_limit)}</div><div><span>大盘</span>${esc(a.regime.state)}</div><div><span>权重版本</span>v${a.weights_ver}</div></div></div>
      <div class="panel c8"><div class="chartbar"><div class="seg" id="chMode"><button data-m="k" class="on">日K</button><button data-m="i">分时</button></div>
          <div class="seg" id="chSub"><button data-s="macd" class="on">MACD</button><button data-s="rsi">RSI</button></div>
          <label class="hint"><input type="checkbox" id="chPlan" checked> 叠加交易计划</label><span class="sp"></span><span class="hint hide-m">拖动平移 · 滚轮/双指缩放 · ←→键</span></div>
        <div class="legend" id="chLg" style="padding:4px 10px;min-height:22px"></div><div class="chartbox"><canvas id="chK"></canvas></div></div>
      <div class="c4" style="display:flex;flex-direction:column;gap:10px">
        ${panel('VERDICT <small>综合结论</small>', `<div class="big-score">${ring(a.score, tone)}<div><div class="rating" style="color:${tone}">${esc(a.rating)}</div>
          <div class="hint mono" style="margin-top:6px">因子 ${num(a.fscore, 0)} · 策略 ${num(a.sscore, 0)}</div><div class="hint mono">${a.prob_src === 'model' ? '模型' : '经验'}估计3日上涨概率 <b class="amber">${num(a.prob * 100, 0)}%</b></div></div></div>
          <p style="margin:10px 0 0">${esc(a.advice)}</p>${a.prob_src !== 'model' ? '<p class="hint" style="margin:4px 0 0">※ 样本积累到200个后会自动训练概率模型，届时概率经过历史检验。</p>' : ''}`)}
        ${panel('PLAN <small>交易计划</small>', `<div class="plan" style="margin-top:0"><div><div class="l">买入区间</div><div class="v">${num(pl.buy_zone[0])}~${num(pl.buy_zone[1])}</div></div>
            <div><div class="l">止损</div><div class="v down">${num(pl.stop)}</div><div class="p down">${pct(pl.stop_pct, 1)}</div></div>
            <div><div class="l">止盈①</div><div class="v up">${num(pl.target1)}</div><div class="p up">${pct(pl.target1_pct, 1)}</div></div>
            <div><div class="l">止盈②</div><div class="v up">${num(pl.target2)}</div><div class="p up">${pct(pl.target2_pct, 1)}</div></div></div>
          <div class="row hint mono"><span>盈亏比 <b class="amber">${num(pl.rr, 1)}</b></span><span>ATR ${num(pl.atr, 2)}</span><span>最长持有 ${pl.hold_days} 天</span></div>
          <div class="row hint mono" style="margin-top:4px"><span>支撑 <b class="down">${pl.supports.map(x => num(x)).join(' / ') || '--'}</b></span><span>压力 <b class="up">${pl.resistances.map(x => num(x)).join(' / ') || '--'}</b></span></div>
          <ul class="why">${pl.rules.map(x => `<li>${esc(x)}</li>`).join('')}</ul>`)}
      </div>
      ${panel('SMART MONEY <small>主力行为</small>', `<div class="mf"><div class="row"><span class="lab ${mfc}">${esc(mf.label)}</span><span class="sp"></span><span class="mono hint">5日 <b class="${cls(mf.main5_yi)}">${sgn(mf.main5_yi)}亿</b> · 10日 <b class="${cls(mf.main10_yi)}">${sgn(mf.main10_yi)}亿</b></span></div>
          <p style="margin:6px 0">${esc(mf.desc)}</p><ul class="why">${mf.evidence.map(x => `<li>${esc(x)}</li>`).join('')}</ul></div>
          <div class="flowbars">${a.flows.map(f => `<div title="${f.date} ${money(f.main)}"><i style="${f.main >= 0 ? `bottom:50%;height:${(f.main / maxF * 50).toFixed(1)}%;background:var(--up)` : `top:50%;height:${(-f.main / maxF * 50).toFixed(1)}%;background:var(--down)`}"></i></div>`).join('')}</div>
          <div class="hint mono" style="display:flex;justify-content:space-between"><span>${a.flows[0] ? a.flows[0].date.slice(5) : ''}</span><span>近20日主力净额</span><span>${a.flows.length ? a.flows[a.flows.length - 1].date.slice(5) : ''}</span></div>`, { cls: 'c4' })}
      ${panel('FACTORS <small>多维度因子</small>', a.factors.map(f => `<div class="fbar" title="${esc(f.explain)}"><span>${f.label}</span><div class="bar"><em></em><i style="width:${f.score}%;background:${f.score >= 60 ? 'var(--up)' : f.score < 40 ? 'var(--down)' : 'var(--amber)'}"></i></div><span class="mono">${num(f.score, 0)}</span></div>`).join('') +
        '<div class="hint" style="margin-top:6px">每根条 = 0~100 分，中线 = 50。权重会随实战结果自动迭代（见「迭代」页）。悬停查看含义。</div>', { cls: 'c4' })}
      ${panel('SIGNALS <small>策略信号</small>', (a.signals.length ? a.signals.map(s => `<div style="margin-bottom:8px"><div class="row"><span class="chip a">${esc(s.name)}</span><span class="mono hint">置信度 ${num(s.conf * 100, 0)}% · 权重 ${num(s.weight, 2)}</span></div><div style="margin-top:3px">${esc(s.why)}</div><div class="hint">${esc(a.strategy_desc[s.id])}</div></div>`).join('')
        : '<div class="empty" style="padding:12px">当前没有触发任何买点策略</div>') +
        `<div class="sep"></div><div class="hint">看多理由</div><ul class="why">${a.reasons.map(x => `<li>${esc(x)}</li>`).join('') || '<li>暂无明显优势</li>'}</ul>
         <div class="hint">风险提示</div><ul class="why risk">${a.risks.map(x => `<li>${esc(x)}</li>`).join('') || '<li>暂无明显风险项</li>'}</ul>`, { cls: 'c4' })}
      ${panel('LEARN <small>学习要点 · 这只股票教会你什么</small>', a.lessons.map(x => `<div class="lesson">${esc(x)}</div>`).join(''), { cls: 'c12' })}
    </div>`;
    const cv = $('#chK');
    S.kchart = new Charts.KChart(cv, $('#chLg'));
    const k = await api(`kline?code=${a.code}&n=400`);
    S.kchart.setData(k.rows);
    const ov = () => $('#chPlan').checked ? [{ zone: pl.buy_zone, fill: 'rgba(255,176,0,.07)' }, { price: pl.stop, color: '#17d17a', label: '止损' },
      { price: pl.target1, color: '#ff4d4f', label: '止盈①' }, { price: pl.target2, color: '#ff8a8a', label: '止盈②' }] : [];
    S.kchart.setOverlays(ov());
    $('#chPlan').onchange = () => S.kchart.setOverlays(ov());
    $$('#chSub button').forEach(b => b.onclick = () => { $$('#chSub button').forEach(x => x.classList.toggle('on', x === b)); S.kchart.setSub(b.dataset.s); });
    $$('#chMode button').forEach(b => b.onclick = async () => {
      $$('#chMode button').forEach(x => x.classList.toggle('on', x === b));
      const box = $('.chartbox'); box.innerHTML = '<canvas id="chK"></canvas>';
      if (b.dataset.m === 'i') { S.ichart = new Charts.Intraday($('#chK'), $('#chLg')); S.ichart.setData(await api('intraday?code=' + a.code)); S.kchart = null; }
      else { S.kchart = new Charts.KChart($('#chK'), $('#chLg')); S.kchart.setData(k.rows); S.kchart.setOverlays(ov()); S.ichart = null; }
    });
    $('#aBuy').onclick = () => openOrder({ code: a.code, name: a.name, price: q.price, stop: pl.stop, target1: pl.target1, target2: pl.target2 });
    $('#aSell').onclick = () => openOrder({ code: a.code, name: a.name, price: q.price, side: 'sell' });
    $('#aBt').onclick = () => { S.btCode = a.code; go('bt'); };
    $('#aWl').onclick = async () => { await api('watchlist', { code: a.code, op: inWl ? 'remove' : 'add' }); toast(inWl ? '已移出自选' : '已加入自选'); await poll(); renderAnly(a.code); };
    clearInterval(S.timers.anly);
    S.timers.anly = setInterval(async () => {
      if (S.page !== 'anly' || S.anly !== a) return clearInterval(S.timers.anly);
      try { const r = await api('quotes?codes=' + a.code); const x = r[a.code]; if (!x) return;
        $('#aPx').textContent = num(x.price); $('#aPx').className = 'px ' + cls(x.pct); $('#aCh').textContent = `${sgn(x.chg)} ${pct(x.pct)}`; $('#aCh').className = 'ch ' + cls(x.pct);
        if (S.ichart && S.ov && S.ov.phase === 'trading') S.ichart.setData(await api('intraday?code=' + a.code));
      } catch { }
    }, 5000);
  }

  async function staticIndex() {
    const idx = await sj('analysis/index.json').catch(() => []);
    if (!idx.length) return '';
    return panel('PUBLIC <small>公开版可查看的个股</small>', `<table class="t"><tr><th>股票</th><th>评分</th><th>评级</th><th>分析时间</th></tr>${idx.map(x => `<tr class="click" data-c="${x.code}"><td>${nameCell(x.name, x.code)}</td><td class="amber">${num(x.score, 1)}</td><td>${esc(x.rating)}</td><td class="muted">${esc(x.time)}</td></tr>`).join('')}</table>`, { np: true })
      .replace('<table', '<table onclick="var t=event.target.closest(\'[data-c]\');if(t)location.hash=\'anly/\'+t.dataset.c"');
  }

  // ------------------------------------------------------------------ 下单弹窗
  function openOrder(o) {
    const emo = (S.settings && S.settings.emotions) || ['逻辑驱动', '计划内', '跟随系统', '追涨', '抄底', '恐慌', '贪心', '跟风'];
    const side = o.side || 'buy';
    const m = document.createElement('div'); m.className = 'modal';
    const cash = S.ov ? S.ov.accounts.me.cash : 0;
    const suggest = o.price ? Math.max(100, Math.floor(Math.min(cash * 0.2, cash) / o.price / 100) * 100) : 100;
    m.innerHTML = `<div class="box">${panel(`ORDER <small>${esc(o.name || '')} ${short(o.code)}</small>`, `
      <div class="ticket">
        <label>方向<div class="seg" id="oSide"><button data-v="buy" class="${side === 'buy' ? 'on' : ''}">买入</button><button data-v="sell" class="${side === 'sell' ? 'on' : ''}">卖出</button></div></label>
        <label>账户<select class="in" id="oAcct"><option value="me">我的模拟盘</option></select></label>
        <label>类型<select class="in" id="oKind"><option value="market">市价（现价+滑点）</option><option value="limit">限价</option></select></label>
        <label>价格<input class="in" id="oPrice" value="${o.price ?? ''}" inputmode="decimal"></label>
        <label>数量（股）<input class="in" id="oQty" value="${side === 'buy' ? suggest : (o.qty || 100)}" inputmode="numeric"></label>
        <label>可用资金<div class="mono" style="height:28px;line-height:28px">${yuan(cash)}</div></label>
        <label>止损价<input class="in" id="oStop" value="${o.stop ?? ''}"></label>
        <label>止盈① / ②<div class="row" style="flex-wrap:nowrap"><input class="in" id="oT1" value="${o.target1 ?? ''}" style="width:50%"><input class="in" id="oT2" value="${o.target2 ?? ''}" style="width:50%"></div></label>
        <label class="full"><span><input type="checkbox" id="oAuto" checked> 到价自动执行止损/止盈（推荐：帮你守纪律）</span></label>
        <label>下单心态<select class="in" id="oEmo">${emo.map(e => `<option>${e}</option>`).join('')}</select></label>
        <label>理由<input class="in" id="oWhy" placeholder="为什么买/卖？" value="${o.pick_id ? '系统选股' : ''}"></label>
      </div>
      <p class="hint">A股规则：100股一手、T+1（今天买明天才能卖）、涨停买不进/跌停卖不出、佣金万2.5最低5元、卖出印花税0.05%。非交易时间下单会挂起，开盘后自动成交。</p>
      <div class="row"><span class="sp"></span><button class="btn" id="oCancel">取消</button><button class="btn pri" id="oOk">确认下单</button></div>`)}</div>`;
    document.body.appendChild(m);
    let sd = side;
    $$('#oSide button', m).forEach(b => b.onclick = () => { sd = b.dataset.v; $$('#oSide button', m).forEach(x => x.classList.toggle('on', x === b)); });
    m.onclick = e => { if (e.target === m) m.remove(); };
    $('#oCancel', m).onclick = () => m.remove();
    $('#oOk', m).onclick = async () => {
      try {
        const r = await api('sim/order', { acct: $('#oAcct', m).value, code: o.code, side: sd, qty: +$('#oQty', m).value, kind: $('#oKind', m).value,
          price: $('#oPrice', m).value, stop: $('#oStop', m).value, target1: $('#oT1', m).value, target2: $('#oT2', m).value,
          emotion: $('#oEmo', m).value, reason: $('#oWhy', m).value, pick_id: o.pick_id, auto: $('#oAuto', m).checked });
        toast(r.message || '已提交'); m.remove(); poll(); if (S.page === 'sim') renderSim();
      } catch (e) { toast(e.message, 1); }
    };
  }

  // ================================================================== F4 模拟盘
  async function renderSim() {
    if (STATIC) S.acct = 'ai';
    const a = await api('sim?acct=' + S.acct).catch(e => ({ error: e.message }));
    if (a.error) { $('#p-sim').innerHTML = `<div class="empty">${esc(a.error)}</div>`; return; }
    const st = a.stats;
    $('#p-sim').innerHTML = `<div class="grid">
      <div class="panel c12"><div class="ph"><h3>SIM <small>模拟盘</small></h3><div class="r"><div class="seg rw" id="acctSeg"><button data-a="me" class="${S.acct === 'me' ? 'on' : ''}">我的模拟盘</button><button data-a="ai" class="${S.acct === 'ai' ? 'on' : ''}">AI自动盘</button></div>
        <span class="rw row"><input class="in" id="simCode" placeholder="代码" style="width:90px;height:22px"><button class="btn sm buy" id="simNew">下单</button><button class="btn sm" id="simReset">重置</button></span></div></div>
        <div class="summary"><div><div class="l">总资产</div><div class="v">${yuan(a.equity)}</div></div><div><div class="l">可用现金</div><div class="v">${yuan(a.cash)}</div></div>
          <div><div class="l">持仓市值</div><div class="v">${yuan(a.market_value)}</div></div><div><div class="l">今日盈亏</div><div class="v ${cls(a.today_pnl)}">${sgn(a.today_pnl)}</div></div>
          <div><div class="l">累计盈亏</div><div class="v ${cls(a.total_pnl)}">${sgn(a.total_pnl)}</div></div><div><div class="l">累计收益率</div><div class="v ${cls(a.total_pct)}">${pct(a.total_pct)}</div></div></div>
        ${S.acct === 'ai' ? '<div class="pb hint">AI自动盘：每次自动选股后按 20% 仓位模拟买入，并严格执行止损 / 止盈① 卖一半并保本 / 止盈② 清仓 / 10日时间止损。它的成绩就是这套系统的“实战成绩单”，也是自我迭代的数据来源。</div>' : ''}</div>
      ${panel('POSITIONS <small>持仓</small>', a.positions.length ? `<div class="scroll"><table class="t"><tr><th>股票</th><th>数量/可卖</th><th>成本</th><th>现价</th><th>盈亏</th><th>止损</th><th>止盈①</th><th class="hide-m">持有</th><th></th></tr>
        ${a.positions.map(p => `<tr><td class="click" data-c="${p.code}">${nameCell(p.name, p.code)}</td><td>${p.qty}/<span class="muted">${p.avail}</span></td><td>${num(p.cost, 3)}</td><td class="${cls(p.pct)}">${num(p.price)}</td>
          <td class="${cls(p.pnl)}">${sgn(p.pnl)}<br><small>${pct(p.pnl_pct)}</small></td><td class="down">${num(p.stop)}<br><small>${pct(p.to_stop, 1)}</small></td><td class="up">${num(p.target1)}<br><small>${pct(p.to_target, 1)}</small></td>
          <td class="hide-m">${p.hold_days}天${p.auto ? '<br><small class="amber">自动</small>' : ''}</td><td><button class="btn sm" data-ed="${p.code}">改</button> <button class="btn sm sell" data-sl='${esc(JSON.stringify({ code: p.code, name: p.name, price: p.price, side: 'sell', qty: p.avail }))}'>卖</button></td></tr>`).join('')}</table></div>` : '<div class="empty">空仓</div>', { cls: 'c8', np: !!a.positions.length })}
      ${panel('STATS <small>交易统计</small>', `<dl class="kv"><dt>平仓次数</dt><dd>${st.sells}</dd><dt>胜率</dt><dd class="amber">${st.win_rate == null ? '--' : num(st.win_rate * 100, 1) + '%'}</dd>
        <dt>平均盈利</dt><dd class="up">${num(st.avg_win)}</dd><dt>平均亏损</dt><dd class="down">${num(st.avg_loss)}</dd><dt>盈亏比(PF)</dt><dd>${num(st.profit_factor)}</dd>
        <dt>最大回撤</dt><dd class="down">${num(st.max_dd)}%</dd><dt>平均持有</dt><dd>${st.avg_hold ?? '--'} 天</dd><dt>累计手续费</dt><dd>${num(st.fees)}</dd></dl>
        <div class="sep"></div><div class="hint">按下单心态统计（找出让你亏钱的情绪）</div>
        <table class="t"><tr><th>心态</th><th>次数</th><th>胜率</th><th>盈亏</th></tr>${Object.entries(st.by_emotion).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v.n}</td><td>${num(v.win / v.n * 100, 0)}%</td><td class="${cls(v.pnl)}">${sgn(v.pnl)}</td></tr>`).join('') || '<tr><td colspan="4" class="muted">暂无平仓记录</td></tr>'}</table>`, { cls: 'c4' })}
      ${panel('EQUITY <small>资产曲线（每日收盘记录）</small>', '<div style="height:220px;position:relative"><canvas id="eqC" style="position:absolute;inset:0;width:100%;height:100%"></canvas></div>', { cls: 'c6' })}
      ${panel('ORDERS <small>委托</small>', `<div class="scroll" style="max-height:240px"><table class="t"><tr><th>时间</th><th class="l">股票</th><th>方向</th><th>数量</th><th>成交价</th><th>状态</th><th></th></tr>
        ${a.orders.map(o => `<tr><td class="muted">${o.created.slice(5, 16)}</td><td class="l">${esc(o.name)}</td><td class="${o.side === 'buy' ? 'up' : 'down'}">${o.side === 'buy' ? '买' : '卖'}${o.kind === 'limit' ? '(限' + num(o.price) + ')' : ''}</td><td>${o.qty}</td><td>${num(o.fill_price)}</td>
          <td>${{ pending: '<span class="amber">挂单</span>', filled: '成交', cancelled: '<span class="muted">已撤</span>', rejected: '<span class="up">废单</span>' }[o.status] || o.status}</td><td>${o.status === 'pending' ? `<button class="btn sm" data-cx="${o.id}">撤</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="7" class="muted">暂无</td></tr>'}</table></div>`, { cls: 'c6', np: true })}
      ${panel('TRADES <small>成交记录 · 复盘</small>', `<div class="scroll" style="max-height:360px"><table class="t"><tr><th>时间</th><th class="l">股票</th><th>方向</th><th>数量</th><th>价格</th><th>盈亏</th><th class="l hide-m">理由 / 心态</th></tr>
        ${a.trades.map(t => `<tr><td class="muted">${t.time.slice(5, 16)}</td><td class="l click" data-c="${t.code}">${esc(t.name)}</td><td class="${t.side === 'buy' ? 'up' : 'down'}">${t.side === 'buy' ? '买入' : '卖出'}</td><td>${t.qty}</td><td>${num(t.price)}</td>
          <td class="${cls(t.pnl)}">${t.pnl == null ? '' : sgn(t.pnl)}</td><td class="l hide-m nm">${esc(t.reason || '')} <span class="chip">${esc(t.emotion || '')}</span></td></tr>`).join('') || '<tr><td colspan="7" class="muted">暂无</td></tr>'}</table></div>`, { cls: 'c12', np: true })}
    </div>`;
    $$('#acctSeg button').forEach(b => b.onclick = () => { S.acct = b.dataset.a; renderSim(); });
    $$('#p-sim [data-c]').forEach(e => e.onclick = () => go('anly', e.dataset.c));
    $$('#p-sim [data-sl]').forEach(b => b.onclick = () => { const o = JSON.parse(b.dataset.sl); if (S.acct === 'ai') return toast('AI自动盘由系统按计划操作', 1); openOrder(o); });
    $$('#p-sim [data-cx]').forEach(b => b.onclick = async () => { await api('sim/cancel', { id: +b.dataset.cx }); renderSim(); });
    $$('#p-sim [data-ed]').forEach(b => b.onclick = () => editExit(a.positions.find(p => p.code === b.dataset.ed)));
    $('#simNew').onclick = async () => {
      if (S.acct === 'ai') return toast('AI自动盘由系统按计划操作，请切换到「我的模拟盘」', 1);
      const v = $('#simCode').value.trim(); if (!v) return toast('先输入股票代码', 1);
      try { const r = await api('search?q=' + encodeURIComponent(v)); if (!r.length) throw new Error('找不到该股票');
        const an = await api('analyze?code=' + r[0].code); const p = an.plan;
        openOrder({ code: an.code, name: an.name, price: an.quote.price, stop: p.stop, target1: p.target1, target2: p.target2 });
      } catch (e) { toast(e.message, 1); }
    };
    $('#simReset').onclick = async () => { if (!confirm(`确定把「${a.name}」重置为初始资金？所有持仓和记录会清空。`)) return; await api('sim/reset', { acct: S.acct }); toast('已重置'); renderSim(); };
    const eq = a.equity_curve;
    Charts.lines($('#eqC'), eq.map(e => e.date), [{ values: eq.map(e => e.equity), color: '#ffb000', width: 1.6 }], { base: a.init, fmt: v => (v / 1e4).toFixed(2) + '万' });
  }
  function editExit(p) {
    const m = document.createElement('div'); m.className = 'modal';
    m.innerHTML = `<div class="box">${panel(`EXIT <small>${esc(p.name)} 止损/止盈</small>`, `<div class="ticket">
      <label>止损价<input class="in" id="eS" value="${p.stop ?? ''}"></label><label>止盈①<input class="in" id="e1" value="${p.target1 ?? ''}"></label>
      <label>止盈②<input class="in" id="e2" value="${p.target2 ?? ''}"></label><label>自动执行<select class="in" id="eA"><option value="1" ${p.auto ? 'selected' : ''}>开</option><option value="0" ${p.auto ? '' : 'selected'}>关</option></select></label></div>
      <div class="row" style="margin-top:10px"><span class="sp"></span><button class="btn" id="eC">取消</button><button class="btn pri" id="eO">保存</button></div>`)}</div>`;
    document.body.appendChild(m);
    $('#eC', m).onclick = () => m.remove();
    $('#eO', m).onclick = async () => { await api('sim/exit', { acct: S.acct, code: p.code, stop: $('#eS', m).value, target1: $('#e1', m).value, target2: $('#e2', m).value, auto: $('#eA', m).value === '1' }); m.remove(); renderSim(); };
  }

  // ================================================================== F5 自我迭代
  async function renderLearn() {
    const L = await api('learn');
    const hist = L.history, pk = L.picks;
    const def = { momentum: 0.13, trend: 0.16, volume: 0.10, capital: 0.14, strength: 0.12, volatility: 0.06, value: 0.08, position: 0.11, liquidity: 0.10 };
    const last = hist[0];
    const ic = last ? last.ic.mean || {} : {};
    const strat = last ? last.strat : {};
    const md = L.model ? L.model.metrics : null;
    const st = x => x.n ? `<b class="amber">${num(x.win * 100, 0)}%</b> <small class="muted">均${pct(x.avg)} · ${x.n}次</small>` : '<span class="muted">--</span>';
    $('#p-learn').innerHTML = `<div class="grid">
      ${panel('HOW <small>系统怎么“自己变聪明”</small>', `<div class="row" style="gap:18px;align-items:flex-start">
        ${[['① 记录', `每次选股把前150只候选的因子值存为样本（已存 <b class="amber">${L.samples}</b> 个）`], ['② 回填', `收盘后自动回填这些股票之后 1/3/5 日的真实涨跌（已回填 <b class="amber">${L.evaluated}</b> 个）`],
           ['③ 评估', `计算每个因子的 Rank IC（因子高的股票后来是不是真的涨得多），统计每个策略的实战胜率`], ['④ 调整', `有效的因子加权、失效的降权（每次最多移动30%，有上下限，防止过拟合）；样本≥${L.min_model}后训练概率模型并做时间外检验`]]
          .map(([t, d]) => `<div style="flex:1;min-width:180px"><div class="amber mono" style="font-weight:700">${t}</div><div class="hint">${d}</div></div>`).join('')}
        <div class="rw" style="display:flex;flex-direction:column;gap:6px"><button class="btn pri" id="lrRun">▶ 立即学习</button><button class="btn" id="lrBoot">历史冷启动</button></div></div>
        <div class="hint mono" id="bootMsg" style="margin-top:6px">${L.boot.running ? `冷启动中：${esc(L.boot.msg)} ${L.boot.progress}/${L.boot.total}` : `历史回放样本 ${L.history_samples} 个（在历史K线上“回到过去”计算因子，再看之后的真实涨跌，让系统第一天就能学习）`}</div>`, { cls: 'c12' })}
      ${panel(`WEIGHTS <small>当前因子权重 v${L.version}</small>`, Object.keys(L.weights).map(k => { const w = L.weights[k], d = def[k];
          return `<div class="fbar" style="grid-template-columns:52px 1fr 100px"><span>${L.labels[k]}</span><div class="bar"><i style="width:${w / 0.3 * 100}%;background:var(--amber)"></i><em style="left:${d / 0.3 * 100}%;background:#fff"></em></div><span class="mono">${num(w * 100, 1)}% <small class="${cls(w - d)}">${sgn((w - d) * 100, 1)}</small></span></div>`; }).join('') +
        '<div class="hint">白线 = 初始权重。IC：因子与未来3日收益的相关性，>0.03 说明有用，<0 说明反向。</div>' +
        `<table class="t" style="margin-top:6px"><tr><th>因子</th><th>最近IC</th><th>IR</th></tr>${Object.keys(L.weights).map(k => `<tr><td>${L.labels[k]}</td><td class="${cls(ic[k])}">${num(ic[k], 3)}</td><td>${num(last ? (last.ic.ir || {})[k] : null, 2)}</td></tr>`).join('')}</table>`, { cls: 'c4' })}
      ${panel('STRATEGIES <small>策略实战表现（3日）</small>', `<table class="t"><tr><th class="l">策略</th><th>样本</th><th>胜率</th><th>均收益</th><th>权重</th></tr>${L.strategies.map(s => { const x = strat[s.id] || {};
          return `<tr title="${esc(s.desc)}"><td class="l">${esc(s.name)}</td><td>${x.n ?? 0}</td><td>${x.win == null ? '--' : num(x.win * 100, 0) + '%'}</td><td class="${cls(x.avg)}">${x.avg == null ? '--' : pct(x.avg)}</td><td class="amber">${num(x.weight ?? 1, 2)}</td></tr>`; }).join('')}</table>
          <div class="hint" style="margin-top:6px">某策略样本≥10后才调整权重：胜率高、收益好→权重升（最高1.5），反之降（最低0.5）。</div>`, { cls: 'c4' })}
      ${panel('SCORECARD <small>选股成绩单</small>', `<dl class="kv"><dt>次日胜率</dt><dd>${st(pk.d1)}</dd><dt>3日胜率</dt><dd>${st(pk.d3)}</dd><dt>5日胜率</dt><dd>${st(pk.d5)}</dd></dl><div class="sep"></div>
          <dl class="kv">${Object.entries(pk.outcomes).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl><div class="sep"></div>
          <div class="hint">概率模型</div>${md ? `<dl class="kv"><dt>训练时间</dt><dd>${L.model.created}</dd><dt>样本</dt><dd>${md.n}</dd><dt>基准成功率</dt><dd>${num(md.base_rate * 100, 1)}%</dd>
            <dt>检验AUC</dt><dd class="amber">${num(md.test_auc, 3)}</dd><dt>高分组命中</dt><dd>${md.top20_hit == null ? '--' : num(md.top20_hit * 100, 1) + '%'}</dd><dt>状态</dt><dd>${md.usable ? '<span class="up">已启用</span>' : '<span class="muted">效果不足，未启用</span>'}</dd></dl>`
            : `<div class="hint">样本不足 ${L.min_model} 个，暂未训练（当前 ${L.evaluated}）</div>`}`, { cls: 'c4' })}
      ${panel('HISTORY <small>迭代记录</small>', `<div class="scroll" style="max-height:320px"><table class="t"><tr><th>版本</th><th>时间</th><th class="l">主要变化</th></tr>${hist.map(h => `<tr><td class="amber">v${h.version}</td><td class="muted">${h.created}</td><td class="l nm">${esc(h.note)}</td></tr>`).join('') || '<tr><td colspan="3" class="muted">还没有迭代记录：有效样本≥' + L.min_ic + ' 个后，每天15:30自动迭代</td></tr>'}</table></div>`, { cls: 'c12', np: true })}
    </div>`;
    $('#lrBoot').onclick = async () => { try { const r = await api('learn/bootstrap', {}); toast(r.started ? '冷启动开始，约需1-3分钟' : r.message); setTimeout(() => S.page === 'learn' && renderLearn(), 4000); } catch (e) { toast(e.message, 1); } };
    if (L.boot.running) setTimeout(() => S.page === 'learn' && renderLearn(), 3000);
    $('#lrRun').onclick = async () => { try { const r = await api('learn/run', {}); toast(r.iterate.changed ? '权重已更新' : r.iterate.reason); renderLearn(); } catch (e) { toast(e.message, 1); } };
  }

  // ================================================================== F6 回测
  async function renderBt() {
    const strats = await api('strategies');
    $('#p-bt').innerHTML = `<div class="grid">${panel('BACKTEST <small>个股策略回测</small>', `<div class="row">
      <input class="in" id="btCode" placeholder="代码" value="${short(S.btCode || S.lastCode || '600519')}" style="width:110px">
      <select class="in" id="btStrat"><option value="score">综合评分 ≥ 阈值 + 有信号</option>${strats.map(s => `<option value="${s.id}">${s.name}</option>`).join('')}</select>
      <select class="in" id="btDays"><option value="250">近1年</option><option value="500" selected>近2年</option><option value="750">近3年</option></select>
      <label class="hint">阈值 <input class="in" id="btTh" value="68" style="width:54px"></label><button class="btn pri" id="btGo">▶ 回测</button>
      <span class="hint">规则：信号次日开盘买入；止损2ATR；到止盈①卖一半并保本；止盈②清仓；最多持有10天；扣除手续费。</span></div>`, { cls: 'c12' })}
      <div class="c12" id="btOut"></div></div>`;
    $('#btGo').onclick = runBt;
    if (S.btCode) runBt();
  }
  async function runBt() {
    const out = $('#btOut'); out.innerHTML = '<div class="loading">回测中</div>';
    try {
      const r = await api(`backtest?code=${$('#btCode').value.trim()}&strategy=${$('#btStrat').value}&days=${$('#btDays').value}&threshold=${$('#btTh').value}`);
      out.innerHTML = `<div class="grid">
        <div class="panel c12"><div class="summary"><div><div class="l">策略收益</div><div class="v ${cls(r.total)}">${pct(r.total)}</div></div><div><div class="l">同期持股不动</div><div class="v ${cls(r.bench)}">${pct(r.bench)}</div></div>
          <div><div class="l">年化</div><div class="v ${cls(r.annual)}">${pct(r.annual)}</div></div><div><div class="l">胜率</div><div class="v amber">${r.win_rate == null ? '--' : num(r.win_rate * 100, 1) + '%'}</div></div>
          <div><div class="l">交易次数</div><div class="v">${r.n}</div></div><div><div class="l">最大回撤</div><div class="v down">${num(r.max_dd)}%</div></div></div>
          <div class="pb"><b class="amber">结论：</b>${esc(r.verdict)} <span class="hint">（${r.start} ~ ${r.end}，${esc(r.strategy_name)}）</span></div></div>
        ${panel('CURVE <small>策略净值 vs 持股不动</small>', '<div style="height:280px;position:relative"><canvas id="btC" style="position:absolute;inset:0;width:100%;height:100%"></canvas></div><div class="legend"><span style="color:#ffb000">■ 策略</span><span style="color:#5f6d7b">■ 持股不动</span></div>', { cls: 'c7' })}
        ${panel('TRADES <small>逐笔</small>', `<div class="scroll" style="max-height:310px"><table class="t"><tr><th>买入</th><th>卖出</th><th>收益</th><th>天</th><th class="l">离场</th></tr>${r.trades.map(t => `<tr title="${esc(t.why)}"><td>${t.buy_date.slice(2)}<br><small class="muted">${num(t.buy)}</small></td><td>${t.sell_date.slice(2)}<br><small class="muted">${num(t.sell)}</small></td><td class="${cls(t.ret)}">${pct(t.ret)}</td><td>${t.days}</td><td class="l">${esc(t.exit)}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">期间没有触发信号</td></tr>'}</table></div>`, { cls: 'c5', np: true })}
      </div>`;
      Charts.lines($('#btC'), r.curve.map(c => c.date), [{ values: r.curve.map(c => c.bench), color: '#5f6d7b' }, { values: r.curve.map(c => c.eq), color: '#ffb000', width: 1.8 }], { base: 1 });
    } catch (e) { out.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  }

  // ================================================================== F7 日志
  async function renderLog() {
    const ev = await api('events?limit=200');
    $('#p-log').innerHTML = panel('LOG <small>系统日志：选股、成交、提醒、学习</small>', ev.map(evHtml).join('') || '<div class="empty">暂无</div>', { np: true });
  }

  // ================================================================== F8 设置
  async function renderSet() {
    const s = await api('settings'); S.settings = s;
    const f = (id, label, val, hint = '') => `<label>${label}<input class="in" id="${id}" value="${esc(val)}">${hint ? `<small class="hint">${hint}</small>` : ''}</label>`;
    $('#p-set').innerHTML = `<div class="grid">
      ${panel('MOBILE <small>手机访问</small>', `<p>手机和电脑连同一个 WiFi，用手机浏览器打开：</p><div class="mono amber" style="font-size:20px;word-break:break-all">${esc(S.ov ? S.ov.lan : '')}</div>
        <p class="hint">打开后点浏览器菜单「添加到主屏幕」，就像 App 一样使用。若打不开，请在 Windows 防火墙允许 Python 访问专用网络。</p>`, { cls: 'c4' })}
      ${panel('PICK <small>自动选股</small>', `<div class="ticket">${f('sT1', '竞价选股时间', s.pick.times[0])}${f('sT2', '尾盘选股时间', s.pick.times[1])}
        ${f('sMin', '入选最低分', s.pick.min_score, '越高越严格')}${f('sMax', '每次最多', s.pick.max_picks)}
        ${f('sAmt', '最低成交额(亿)', s.pick.min_amount_yi)}${f('sPos', 'AI盘单票仓位', s.pick.position_pct, '0.2=20%')}
        <label class="full"><span><input type="checkbox" id="sAuto" ${s.pick.auto_trade ? 'checked' : ''}> AI自动盘跟随选股模拟买入</span></label></div>`, { cls: 'c4' })}
      ${panel('RISK <small>止损止盈参数</small>', `<div class="ticket">${f('rAtr', '止损 = N × ATR', s.risk.stop_atr)}${f('rMax', '最大止损幅度', s.risk.max_stop_pct, '0.08=8%')}
        ${f('rR1', '止盈① 盈亏比', s.risk.rr_1)}${f('rR2', '止盈② 盈亏比', s.risk.rr_2)}${f('rDays', '最长持有(交易日)', s.risk.max_hold_days)}${f('sCash', '模拟初始资金', s.init_cash, '重置账户后生效')}</div>`, { cls: 'c4' })}
      ${panel('LISTS <small>监控列表</small>', `<div class="ticket">
        <label class="full">自选股（逗号分隔）<textarea class="in" id="lWl">${esc(s.watchlist.join(','))}</textarea></label>
        <label class="full">指数<input class="in" id="lIdx" value="${esc(s.indices.join(','))}"></label>
        <label class="full">外围<input class="in" id="lOv" value="${esc(s.overseas.join(','))}"></label></div>`, { cls: 'c6' })}
      ${panel('NOTIFY <small>微信推送（可选）</small>', `<div class="ticket">
        <label class="full">PushPlus token<input class="in" id="nPP" value="${esc(s.notify.pushplus_token)}" placeholder="pushplus.plus 获取"></label>
        <label class="full">Server酱 SendKey<input class="in" id="nSC" value="${esc(s.notify.serverchan_key)}" placeholder="sct.ftqq.com 获取"></label>
        <label class="full"><span><input type="checkbox" id="nOn" ${s.notify.enabled ? 'checked' : ''}> 开启推送（选股结果、成交）</span></label></div>
        <div class="row"><button class="btn" id="nTest">发送测试</button></div>`, { cls: 'c6' })}
      ${panel('PUBLISH <small>公开发布到 Git（手机 / 插件查看）</small>', `<div class="ticket">
        <label class="full">仓库地址<input class="in" id="pRemote" value="${esc(s.publish.remote)}"></label>
        <label>分支<input class="in" id="pBranch" value="${esc(s.publish.branch)}"></label><label>盘中间隔(分钟)<input class="in" id="pInt" value="${esc(s.publish.interval_min)}"></label>
        <label class="full"><span><input type="checkbox" id="pOn" ${s.publish.enabled ? 'checked' : ''}> 开启自动发布（选股后、盘中定时、15:45 收盘）</span></label>
        <label class="full"><span><input type="checkbox" id="pPush" ${s.publish.push ? 'checked' : ''}> 自动 git push（需要本机已安装 Git 并登录过 GitHub）</span></label></div>
        <p class="hint">只发布：大盘行情、选股结果、入选股与AI持仓的分析、AI自动盘、因子迭代。<b class="amber">不发布</b>：我的模拟盘、自选股、推送 token。</p>
        <div class="row"><button class="btn" id="pNow">立即发布一次</button><span class="hint mono" id="pState">${S.ov && S.ov.publish ? `上次 ${esc(S.ov.publish.last || '--')} · 推送 ${esc(S.ov.publish.last_push || '--')}${S.ov.publish.error ? ' · <span class="up">' + esc(S.ov.publish.error.slice(0, 120)) + '</span>' : ''}` : ''}</span></div>`, { cls: 'c12' })}
      <div class="c12 row"><span class="sp"></span><button class="btn pri" id="sSave">保存设置</button></div>
    </div>`;
    const v = id => $('#' + id).value.trim(), n = id => +v(id), list = id => v(id).split(/[,，\s]+/).filter(Boolean);
    $('#sSave').onclick = async () => {
      try {
        await api('settings', { pick: { times: [v('sT1'), v('sT2')], min_score: n('sMin'), max_picks: n('sMax'), min_amount_yi: n('sAmt'), position_pct: n('sPos'), auto_trade: $('#sAuto').checked },
          risk: { stop_atr: n('rAtr'), max_stop_pct: n('rMax'), rr_1: n('rR1'), rr_2: n('rR2'), max_hold_days: n('rDays') }, init_cash: n('sCash'),
          watchlist: list('lWl'), indices: list('lIdx'), overseas: list('lOv'), publish: { remote: v('pRemote'), branch: v('pBranch'), interval_min: n('pInt'), enabled: $('#pOn').checked, push: $('#pPush').checked }, notify: { pushplus_token: v('nPP'), serverchan_key: v('nSC'), enabled: $('#nOn').checked } });
        toast('已保存'); S.settings = null; poll();
      } catch (e) { toast(e.message, 1); }
    };
    $('#pNow').onclick = async () => { try { await api('publish', { full: true }); toast('开始发布，约需 1 分钟，结果见日志'); } catch (e) { toast(e.message, 1); } };
    $('#nTest').onclick = async () => { try { const r = await api('notify/test', {}); toast(r.sent ? '已发送，去微信看看' : r.reason, !r.sent); } catch (e) { toast(e.message, 1); } };
  }

  // ------------------------------------------------------------------ 启动
  api('settings').then(s => S.settings = s).catch(() => { });
  poll().then(() => { const [p, a] = location.hash.slice(1).split('/'); show(PAGES.includes(p) ? p : 'mon', a); });
})();
