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
      turnover: +qt[38], pe: +qt[39], vr: +qt[49] || null, mcap: +qt[45], fmcap: +qt[44], pb: +qt[46], up_limit: +qt[47], dn_limit: +qt[48] } : { price: last };
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
  window.Lite = { klines, intraday, search, analyze };
})();
