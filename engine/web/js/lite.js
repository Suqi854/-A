/* 公开版（GitHub 网页）在电脑没开时也能分析任意个股：浏览器直接取腾讯行情，本地计算技术面。
   与电脑引擎相比：没有主力资金、行业强度、学习模型，这些项按中性处理并注明。 */
(function () {
  const loadVar = (url, name, ms = 10000) => new Promise((res, rej) => {
    const s = document.createElement('script'); let done = false;
    const fin = (ok, v) => { if (done) return; done = true; s.remove(); ok ? res(v) : rej(v); };
    s.src = url; s.onload = () => fin(window[name] != null, window[name] != null ? window[name] : new Error('行情源无数据'));
    s.onerror = () => fin(false, new Error('行情源无法访问'));
    document.head.appendChild(s); setTimeout(() => fin(false, new Error('行情源超时')), ms);
  });
  const vname = p => p + Math.random().toString(36).slice(2, 8);

  async function klines(code, period = 'day', n = 320) {
    const v = vname('kq_');
    const d = await loadVar(`https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=${v}&param=${code},${period},,,${n},qfq`, v);
    const node = (d.data || {})[code] || {};
    const arr = node['qfq' + period] || node[period] || [];
    const rows = arr.filter(a => a.length >= 6).map(a => ({ date: a[0], open: +a[1], close: +a[2], high: +a[3], low: +a[4], vol: +a[5] * 100 }));
    const q = ((node.qt || {})[code]) || null;
    return { rows, qt: q, source: '腾讯（浏览器直连）' };
  }
  async function minutes(code, m = 5, n = 320) {
    const v = vname('mk_');
    const d = await loadVar(`https://web.ifzq.gtimg.cn/appstock/app/kline/mkline?param=${code},m${m},,${n}&_var=${v}`, v);
    const arr = (((d.data || {})[code] || {})['m' + m]) || [];
    const rows = arr.map(a => { const t = String(a[0]); return { date: `${t.slice(0, 4)}-${t.slice(4, 6)}-${t.slice(6, 8)} ${t.slice(8, 10)}:${t.slice(10, 12)}`, open: +a[1], close: +a[2], high: +a[3], low: +a[4], vol: +a[5] * 100 }; });
    return { rows, source: '腾讯（浏览器直连）' };
  }
  async function intraday(code) {
    const v = vname('mq_');
    const d = await loadVar(`https://web.ifzq.gtimg.cn/appstock/app/minute/query?_var=${v}&code=${code}`, v);
    const node = (d.data || {})[code] || {}, arr = ((node.data || {}).data) || [];
    const qt = ((node.qt || {})[code]) || [];
    let lastV = 0; const pts = [];
    arr.forEach(s => { const p = s.split(' '); if (p.length < 3) return; const vv = +p[2] * 100, amt = +(p[3] || 0);
      pts.push({ t: p[0].slice(0, 2) + ':' + p[0].slice(2), p: +p[1], v: Math.max(vv - lastV, 0), avg: vv ? amt / vv : +p[1] }); lastV = vv; });
    return { code, prev: +qt[4] || null, points: pts, source: '腾讯（浏览器直连）' };
  }
  async function search(q) {
    const v = 'v_hint';
    window.v_hint = undefined;
    const s = await loadVar(`https://smartbox.gtimg.cn/s3/?v=2&q=${encodeURIComponent(q)}&t=all&c=1`, v);
    return String(s || '').split('^').map(x => x.split('~')).filter(p => p.length >= 5 && /GP-A/.test(p[4]) && /^(sh|sz)$/.test(p[0]))
      .map(p => ({ code: p[0] + p[1], name: p[2] })).filter(x => /^(sh60[0135]|sz00[0-3])/.test(x.code)).slice(0, 12);
  }

  // ---------------- 指标
  const sma = (a, n) => a.map((_, i) => i >= n - 1 ? a.slice(i - n + 1, i + 1).reduce((s, x) => s + x, 0) / n : null);
  const ema = (a, n) => { const k = 2 / (n + 1), o = []; a.forEach((v, i) => o.push(i ? o[i - 1] + k * (v - o[i - 1]) : v)); return o; };
  const sig = (x, c, s) => 100 / (1 + Math.exp(-Math.max(-30, Math.min(30, (x - c) / s))));
  const r2 = x => x == null ? null : Math.round(x * 100) / 100;

  function analyze(code, rows, qt) {
    if (rows.length < 60) throw new Error('K线数据不足');
    const c = rows.map(r => r.close), v = rows.map(r => r.vol), n = c.length, last = c[n - 1];
    const ma5 = sma(c, 5), ma10 = sma(c, 10), ma20 = sma(c, 20), ma60 = sma(c, 60), vma5 = sma(v, 5), vma20 = sma(v, 20);
    const tr = rows.map((r, i) => i ? Math.max(r.high - r.low, Math.abs(r.high - c[i - 1]), Math.abs(r.low - c[i - 1])) : r.high - r.low);
    const atr = sma(tr, 14)[n - 1] || last * 0.03;
    let up = 0, dn = 0; const rsi6 = [];
    for (let i = 1; i < n; i++) { const d = c[i] - c[i - 1]; if (i <= 6) { up += Math.max(d, 0) / 6; dn += Math.max(-d, 0) / 6; } else { up = (up * 5 + Math.max(d, 0)) / 6; dn = (dn * 5 + Math.max(-d, 0)) / 6; } rsi6.push(dn ? 100 - 100 / (1 + up / dn) : 100); }
    const rsi = rsi6[rsi6.length - 1];
    const dif = ema(c, 12).map((x, i) => x - ema(c, 26)[i]), dea = ema(dif, 9);
    const ret = k => (last / c[n - 1 - k] - 1) * 100;
    const r5 = ret(5), r20 = ret(20);
    const q = qt ? { name: qt[1], price: +qt[3], prev: +qt[4], open: +qt[5], chg: +qt[31], pct: +qt[32], high: +qt[33], low: +qt[34], amount: (+qt[37] || 0) * 1e4,
      turnover: +qt[38], pe: +qt[39], vr: +qt[49] || null, mcap: +qt[45], fmcap: +qt[44], pb: +qt[46], up_limit: +qt[47], dn_limit: +qt[48],
      outer: +qt[7], inner: +qt[8], amp: +qt[43], vol: +qt[6] * 100, bids: [9, 11, 13, 15, 17].map(i => [+qt[i], +qt[i + 1]]), asks: [19, 21, 23, 25, 27].map(i => [+qt[i], +qt[i + 1]]) } : { price: last };
    const price = q.price || last;
    const h60 = Math.max(...rows.slice(-60).map(r => r.high));
    const vr = q.vr || (v[n - 1] / (vma5[n - 2] || v[n - 1]));
    const align = [ma5[n - 1] > ma10[n - 1], ma10[n - 1] > ma20[n - 1], ma20[n - 1] > ma60[n - 1], last > ma20[n - 1]].filter(Boolean).length;
    const slope20 = (ma20[n - 1] / ma20[n - 11] - 1) * 100;
    const atrp = atr / last * 100;
    const dev20 = (last / ma20[n - 1] - 1) * 100;
    const amt5 = rows.slice(-6, -1).reduce((s, r) => s + r.vol * r.close, 0) / 5;
    const F = [
      ['momentum', '动量', sig(0.5 * r5 + 0.5 * r20, 3, 6)], ['trend', '趋势', sig(align + slope20 * 5 / 10, 2, 1.2)],
      ['volume', '量能', sig(Math.min(vr, 6) <= 3 ? Math.min(vr, 6) : 3 - (Math.min(vr, 6) - 3) * 0.8, 1.3, 0.5)], ['capital', '资金', 50],
      ['strength', '强度', 50], ['volatility', '波动', sig(-Math.abs(atrp - 3.5), -1.5, 0.8)],
      ['value', '估值', sig(q.pe > 0 ? 2 - Math.log10(q.pe) : -1, 0.6, 0.35)], ['position', '位置', sig(100 * last / h60 - Math.max(0, rsi - 80) - 2 * Math.max(0, dev20 - 12), 88, 6)],
      ['liquidity', '流动性', sig(Math.log10(Math.max(amt5, 1)), 8.3, 0.4)], ['sector', '板块', 50]];
    const W = { momentum: .13, trend: .16, volume: .10, capital: .14, strength: .12, volatility: .06, value: .08, position: .11, liquidity: .10, sector: .08 };
    const fscore = F.reduce((s, f) => s + f[2] * W[f[0]], 0) / Object.values(W).reduce((s, x) => s + x, 0);
    const sigs = [];
    const h20 = Math.max(...rows.slice(-21, -1).map(r => r.high));
    if (last > h20 && vr >= 1.5) sigs.push({ id: 'breakout', name: '趋势突破', conf: 0.6, weight: 1, why: `突破20日最高 ${h20.toFixed(2)}，量比 ${vr.toFixed(1)}` });
    if (ma10[n - 1] > ma20[n - 1] && Math.abs(last / ma20[n - 1] - 1) < 0.02 && vma5[n - 1] < vma20[n - 1] * 0.85) sigs.push({ id: 'pullback', name: '多头回踩', conf: 0.55, weight: 1, why: '多头排列中缩量回踩20日线' });
    if (dif[n - 2] <= dea[n - 2] && dif[n - 1] > dea[n - 1]) sigs.push({ id: 'macd_cross', name: 'MACD金叉', conf: 0.5, weight: 1, why: 'DIF 上穿 DEA' });
    if (rsi < 25 && dev20 < -8) sigs.push({ id: 'oversold', name: '超跌反弹', conf: 0.5, weight: 1, why: `RSI6=${rsi.toFixed(0)}，偏离20日线 ${dev20.toFixed(1)}%` });
    const sscore = sigs.length ? Math.min(100, sigs[0].conf * 80 + sigs.slice(1, 3).reduce((s, x) => s + x.conf, 0) * 15) : 0;
    const score = Math.round((fscore * 0.75 + sscore * 0.25) * 10) / 10;
    // 交易计划（与电脑端同口径：2×ATR，不超过8%）
    const lows10 = Math.min(...rows.slice(-10).map(r => r.low));
    const sup = [ma10[n - 1], ma20[n - 1], lows10].filter(x => x && x < price).sort((a, b) => b - a);
    let stop = Math.max(price - 2 * atr, Math.min(sup[0] ? sup[0] * 0.985 : price - 2 * atr, price * 0.985)); stop = Math.max(stop, price * 0.92);
    const R = Math.max(price - stop, price * 0.01), t1 = price + 1.5 * R, t2 = price + 3 * R;
    const res = [h20, h60].filter(x => x > price * 1.005).sort((a, b) => a - b);
    const plan = { price, buy_zone: [r2(Math.min(sup[0] ? Math.max(sup[0], price - 0.5 * atr) : price - 0.5 * atr, price)), r2(price)], stop: r2(stop), stop_pct: r2((stop / price - 1) * 100),
      target1: r2(t1), target1_pct: r2((t1 / price - 1) * 100), target2: r2(t2), target2_pct: r2((t2 / price - 1) * 100), rr: 1.5, atr: r2(atr), hold_days: 10,
      supports: sup.slice(0, 3).map(r2), resistances: res.map(r2),
      rules: [`止损：收盘跌破 ${stop.toFixed(2)} 坚决卖出`, `第一止盈 ${t1.toFixed(2)} 卖一半，止损上移到成本`, `第二止盈 ${t2.toFixed(2)} 或跌破5日线清仓`, '时间止损：10 个交易日没走出来就换股'] };
    // 量价版主力行为
    let obv = 0; const ob = [0]; for (let i = 1; i < n; i++) { obv += c[i] > c[i - 1] ? v[i] : c[i] < c[i - 1] ? -v[i] : 0; ob.push(obv); }
    const obvUp = ob[n - 1] > ob[n - 21];
    let mfv = 0, vs = 0; rows.slice(-20).forEach(r => { const g = r.high - r.low; if (g > 0) { mfv += ((r.close - r.low) - (r.high - r.close)) / g * r.vol; vs += r.vol; } });
    const cmf = vs ? mfv / vs : 0, pos = last / h60;
    let lab = '不明显', tone = 'neutral', desc = '量价没有明确配合';
    if (pos > 0.9 && cmf < -0.05) { lab = '高位派发'; tone = 'down'; desc = '高位且资金指标流出，有出货嫌疑'; }
    else if (r5 > 8 && vma5[n - 1] > vma20[n - 1] * 1.3) { lab = '拉升'; tone = 'up'; desc = '放量快速上涨'; }
    else if (Math.abs(r20) < 8 && (obvUp || cmf > 0.05) && pos < 0.85) { lab = '建仓吸筹'; tone = 'up'; desc = '横盘中 OBV/CMF 显示资金流入'; }
    else if (cmf < -0.05 && last < ma20[n - 1]) { lab = '资金离场'; tone = 'down'; desc = '资金指标流出且跌破20日线'; }
    const factors = F.map(([key, label, sc]) => ({ key, label, score: Math.round(sc * 10) / 10, weight: W[key], explain: ['capital', 'strength', 'sector'].includes(key) ? '公开版没有这项数据，按中性 50 分' : '', formula: '' }));
    const strong = [...factors].sort((a, b) => b.score - a.score);
    const tones = score >= 78 ? ['买入', 'buy', '综合评分高：可按计划分批买入'] : score >= 68 ? ['轻仓试错', 'watch_buy', '条件不错：最多半仓'] : score >= 52 ? ['观望', 'hold', '没有明显优势'] : score >= 40 ? ['减仓', 'reduce', '偏弱：逢反弹减仓'] : ['回避', 'avoid', '走势差：不碰'];
    return { code, name: q.name || code, quote: q, score, fscore: Math.round(fscore * 10) / 10, sscore, prob: Math.max(0.2, Math.min(0.8, 0.3 + score / 200)), prob_src: 'lite',
      rating: tones[0], tone: tones[1], advice: tones[2] + '（公开版简化分析：缺少主力资金、行业强度和学习模型，仅供参考）', factors, signals: sigs, strategy_desc: {}, strategy_formula: {}, plan,
      main_force: { label: lab, tone, desc, evidence: [`OBV 20日${obvUp ? '上升' : '下降'}`, `CMF20 = ${cmf.toFixed(2)}`, `60日位置 ${(pos * 100).toFixed(0)}%`, '公开版没有逐日主力资金，仅用量价判断'], tags: [], has_flow: false },
      reasons: strong.slice(0, 3).filter(f => f.score >= 60).map(f => `${f.label}得分 ${f.score.toFixed(0)}`), risks: [...strong].reverse().slice(0, 3).filter(f => f.score < 40).map(f => `${f.label}得分仅 ${f.score.toFixed(0)}`).concat(rsi > 80 ? [`RSI6=${rsi.toFixed(0)} 过热`] : []),
      lessons: ['这是浏览器里的简化分析。电脑开着并开启「手机远程」时，手机会自动连上电脑，得到含主力资金、行业强度、数学模型和 AI 的完整分析。'],
      regime: { state: '--', desc: '' }, weights_ver: '-', flows: [], lite: true, kline: rows };
  }

  // ---------------- 当日信号（与电脑端 signals.plan 同口径的浏览器版）
  const rsiOf = (c, n = 6) => { if (c.length < n + 2) return null; let up = 0, dn = 0;
    for (let i = 1; i < c.length; i++) { const d = c[i] - c[i - 1], u = Math.max(d, 0), w = Math.max(-d, 0);
      if (i <= n) { up += u / n; dn += w / n; } else { up = (up * (n - 1) + u) / n; dn = (dn * (n - 1) + w) / n; } }
    return dn === 0 ? 100 : 100 - 100 / (1 + up / dn); };
  const cst = () => new Date(Date.now() + (new Date().getTimezoneOffset() + 480) * 60000);
  function session() { const d = cst(), wd = d.getDay(), hm = d.getHours() * 100 + d.getMinutes();
    const ymd = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    const trading = wd > 0 && wd < 6 && ((hm >= 930 && hm <= 1130) || (hm >= 1300 && hm <= 1500));
    const phase = wd === 0 || wd === 6 ? 'post' : hm < 930 ? 'pre' : hm > 1500 ? 'post' : 'intra';
    return { ymd, hm, trading, phase, time: `${ymd} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:00` }; }
  async function plan(code, a, pos) {
    const ss = session();
    let rows = (a.kline || []).slice();
    if (rows.length && rows[rows.length - 1].date === ss.ymd) rows = rows.slice(0, -1);
    if (rows.length < 20) throw new Error('K线不足');
    const y = rows[rows.length - 1];
    const P = (y.high + y.low + y.close) / 3, R1 = 2 * P - y.low, S1 = 2 * P - y.high, R2 = P + (y.high - y.low), S2 = P - (y.high - y.low);
    const trs = rows.slice(1).map((r, i) => Math.max(r.high - r.low, Math.abs(r.high - rows[i].close), Math.abs(r.low - rows[i].close)));
    const atr = trs.slice(-14).reduce((s, x) => s + x, 0) / Math.min(14, trs.length);
    const q = a.quote || {}, price = q.price || y.close;
    const live = ss.phase !== 'pre' && q.price && Math.abs(q.price - y.close) > 1e-9 || ss.trading;
    let vwap = null, rsi5 = null;
    if (live) {
      try { const it = await intraday(code); const pts = it.points || []; if (pts.length) vwap = pts[pts.length - 1].avg; } catch (e) { }
      try { const m5 = await minutes(code, 5, 48); rsi5 = rsiOf(m5.rows.map(r => r.close)); } catch (e) { }
    }
    const anchor = vwap || y.close;
    let low = anchor - 0.45 * atr, high = anchor + 0.45 * atr;
    if (low < S1 && S1 < anchor) low = S1;
    if (anchor < R1 && R1 < high) high = R1;
    if (high < low * 1.006) { const mid = (high + low) / 2; low = mid * 0.997; high = mid * 1.003; }
    const pl = a.plan, mf = a.main_force || {}, score = a.score, rating = a.rating, badMf = mf.tone === 'down';
    const sigs = [], actions = [];
    const add = (kind, label, p, why, tone = 'neutral', strength = 50) => sigs.push({ kind, label, price: p == null ? null : r2(p), why, tone, strength });
    if (pos) {
      const cost = pos.cost, pnl = (price / cost - 1) * 100, stop = Math.max(pos.stop || 0, pl.stop);
      if (price <= stop) { actions.push('止损'); add('stop', '止损离场', stop, `现价 ${price.toFixed(2)} 已跌破止损 ${stop.toFixed(2)}，按纪律离场`, 'down', 95); }
      else add('stop', '止损位', stop, `收盘跌破 ${stop.toFixed(2)} 就走（距现价 ${((stop / price - 1) * 100).toFixed(1)}%）`, 'down', 60);
      const t1 = pos.target1 || pl.target1;
      if (t1 && price >= t1) { actions.push('减仓'); add('reduce', '到止盈①减半', t1, `已到第一止盈 ${t1.toFixed(2)}（浮盈 ${pnl.toFixed(1)}%），卖出一半，止损上移到成本 ${cost.toFixed(2)}`, 'down', 85); }
      else add('take', '止盈①', t1, `到 ${(+t1).toFixed(2)} 先卖一半`, 'up', 55);
      add('take', '止盈②', pos.target2 || pl.target2, '到这里清仓或跌破5日线清仓', 'up', 50);
      if (score < 45 || badMf) { actions.push('减仓'); add('reduce', '转弱减仓', price, `评分 ${score.toFixed(0)}${badMf ? `，主力「${mf.label}」` : ''}：先减一半，反弹无力再清仓`, 'down', 75); }
      const sup = (pl.supports || [])[0];
      if (score >= 60 && !badMf && sup && pnl > -4 && ['buy', 'watch_buy', 'hold'].includes(a.tone)) { add('add', '回踩加仓', sup, `趋势未坏（评分 ${score.toFixed(0)}），回踩支撑 ${sup.toFixed(2)} 附近可加 1/3 仓`, 'up', 60); if (price <= sup * 1.01) actions.push('加仓'); }
      if (!actions.length) actions.push('持有');
    } else {
      const bz = pl.buy_zone;
      if (['buy', 'watch_buy'].includes(a.tone) && !badMf) {
        if (bz[0] <= price && price <= bz[1] * 1.005) { actions.push('买入'); add('entry', '买入区内', price, `评级「${rating}」，价格在买入区 ${bz[0]}~${bz[1]}，可按计划分批买`, 'up', 80); }
        else add('entry', '等回踩买入', bz[0], `评级「${rating}」但现价不在买入区，回踩到 ${bz[0]}~${bz[1]} 再买，不追高`, 'up', 60);
        const res = (pl.resistances || [])[0]; if (res) add('entry', '放量突破追入', res, `放量（量比>1.5）站上压力 ${res.toFixed(2)} 可小仓追入`, 'up', 50);
      } else add('entry', '观望', null, `评级「${rating}」（${score.toFixed(0)}分）${badMf ? `，主力「${mf.label}」` : ''}：暂不买，等评分≥68 或出现买点`, 'neutral', 40);
      add('stop', '若买入的止损', pl.stop, `买入后收盘跌破 ${pl.stop} 离场`, 'down', 40);
      add('take', '目标①/②', pl.target1, `${pl.target1} / ${pl.target2}`, 'up', 40);
      if (!actions.length) actions.push('观望');
    }
    const canSell = !!(pos && (pos.avail == null ? pos.qty : pos.avail) > 0);
    const tw = `今日做T区间：低吸 ${r2(low)} / 高抛 ${r2(high)}（枢轴 S1 ${r2(S1)} / R1 ${r2(R1)}${vwap ? `，VWAP ${r2(vwap)}` : '，按昨收估算'}）`;
    if (canSell) { add('t_high', '做T高抛', high, `冲到 ${r2(high)} 附近先卖 1/3 可卖仓位，回落到 ${r2(low)} 附近买回`, 'down', 55); add('t_low', '做T低吸', low, `回落到 ${r2(low)} 附近接回；跌破止损则不接`, 'up', 55); }
    else if (pos) add('t_low', '做T低吸（今日新仓）', low, '今天新买的不能卖，只能低吸摊低成本', 'up', 40);
    else add('t_low', '日内低吸参考', low, '没有持仓：可作为日内更好的买点参考', 'up', 35);
    if (ss.trading) {
      if (price <= low && price > (pl.stop || 0)) { add('t_fire', '⚡触及低吸位', price, `现价 ${price} ≤ 低吸位 ${r2(low)}${rsi5 != null && rsi5 < 30 ? `，5分钟RSI ${rsi5.toFixed(0)} 超卖` : ''}`, 'amber', 90); if (pos && !actions.includes('止损')) actions.unshift('做T'); }
      else if (price >= high && canSell) { add('t_fire', '⚡触及高抛位', price, `现价 ${price} ≥ 高抛位 ${r2(high)}${rsi5 != null && rsi5 > 70 ? `，5分钟RSI ${rsi5.toFixed(0)} 超买` : ''}`, 'amber', 90); if (!actions.includes('止损')) actions.unshift('做T'); }
    }
    const act = actions[0];
    const TONE = { 买入: 'up', 加仓: 'up', 持有: 'neutral', 做T: 'amber', 减仓: 'down', 卖出: 'down', 止损: 'down', 观望: 'neutral' };
    const base = { 买入: '条件满足，可以按计划买入', 加仓: '回踩到支撑、趋势未坏，可小幅加仓', 持有: '按计划持有，守住止损', 做T: '盘中触及做T价位，按区间高抛低吸', 减仓: '到达止盈或转弱，先减一半', 止损: '已破止损，按纪律离场', 观望: '没有优势，先观望' }[act] || '';
    const stopV = pos ? r2(Math.max(pos.stop || 0, pl.stop)) : pl.stop;
    return { code, name: a.name, phase: ss.phase, time: ss.time, action: act, actions, tone: TONE[act] || 'neutral', score, rating, main_force: mf.label, price: r2(price), pct: q.pct,
      levels: { pivot: r2(P), r1: r2(R1), s1: r2(S1), r2: r2(R2), s2: r2(S2), atr: Math.round(atr * 1000) / 1000, vwap: r2(vwap), rsi5: rsi5 == null ? null : Math.round(rsi5 * 10) / 10,
        t_low: r2(low), t_high: r2(high), buy_zone: pl.buy_zone, stop: stopV, target1: pos ? r2(pos.target1 || pl.target1) : pl.target1, target2: pos ? r2(pos.target2 || pl.target2) : pl.target2 },
      position: pos ? { qty: pos.qty, avail: pos.avail == null ? pos.qty : pos.avail, cost: Math.round(pos.cost * 1000) / 1000, pnl_pct: r2((price / pos.cost - 1) * 100) } : null,
      signals: sigs, t_note: tw, triggered: [], summary: `${base}（评分 ${score.toFixed(0)}，${rating}，主力「${mf.label || '--'}」）`, lite: true };
  }
  window.Lite = { klines, minutes, intraday, search, analyze, plan, session };
})();
