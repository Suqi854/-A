/* 自绘 Canvas 图表：K线（拖动/缩放/触屏）、分时、走势线、迷你图。无第三方依赖。 */
(function () {
  const C = {
    up: '#ff4d4f', down: '#17d17a', grid: '#141b22', axis: '#5f6d7b', text: '#9aa7b4', amber: '#ffb000',
    cyan: '#2fd3e6', white: '#e6edf3', bg: '#0c1014', ma: ['#ffffff', '#ffb000', '#d16cff', '#2fd3e6'],
    font: '11px "JetBrains Mono", Consolas, monospace'
  };

  function setup(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const r = canvas.getBoundingClientRect();
    const w = Math.max(10, r.width), h = Math.max(10, r.height);
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.font = C.font; ctx.textBaseline = 'middle';
    return { ctx, w, h };
  }

  function sma(a, n) {
    const o = []; let s = 0;
    for (let i = 0; i < a.length; i++) { s += a[i]; if (i >= n) s -= a[i - n]; o.push(i >= n - 1 ? s / n : null); }
    return o;
  }
  function ema(a, n) { const k = 2 / (n + 1), o = []; a.forEach((v, i) => o.push(i ? o[i - 1] + k * (v - o[i - 1]) : v)); return o; }
  function fmtV(v) { if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿'; if (v >= 1e4) return (v / 1e4).toFixed(0) + '万'; return Math.round(v) + ''; }
  function niceDigits(p) { return p >= 1000 ? 1 : p >= 100 ? 2 : 2; }

  class KChart {
    constructor(canvas, legendEl) {
      this.cv = canvas; this.lg = legendEl; this.rows = []; this.count = 90; this.end = 0; this.hover = null;
      this.overlays = []; this.sub = 'macd';
      this._bind();
      new ResizeObserver(() => this.draw()).observe(canvas);
    }
    setData(rows, keepView) {
      this.rows = rows || [];
      const c = this.rows.map(r => r.close), v = this.rows.map(r => r.vol);
      this.ma = [sma(c, 5), sma(c, 10), sma(c, 20), sma(c, 60)];
      const e1 = ema(c, 12), e2 = ema(c, 26), dif = e1.map((x, i) => x - e2[i]), dea = ema(dif, 9);
      this.macd = { dif, dea, hist: dif.map((x, i) => 2 * (x - dea[i])) };
      this.vma = sma(v, 5);
      // RSI6
      const rsi = [null]; let up = 0, dn = 0;
      for (let i = 1; i < c.length; i++) {
        const d = c[i] - c[i - 1], u = Math.max(d, 0), w = Math.max(-d, 0);
        if (i <= 6) { up += u / 6; dn += w / 6; } else { up = (up * 5 + u) / 6; dn = (dn * 5 + w) / 6; }
        rsi.push(i < 6 ? null : dn === 0 ? 100 : 100 - 100 / (1 + up / dn));
      }
      this.rsi = rsi;
      if (!keepView) { this.end = this.rows.length; this.count = Math.min(this.count, Math.max(30, this.rows.length)); }
      this.draw();
    }
    setOverlays(o) { this.overlays = o || []; this.draw(); }
    setSub(s) { this.sub = s; this.draw(); }
    _range() {
      const n = this.rows.length; let end = Math.min(n, Math.max(Math.min(this.count, n), Math.round(this.end)));
      const start = Math.max(0, end - Math.round(this.count));
      return [start, end];
    }
    _bind() {
      const cv = this.cv; const pts = new Map(); let last = null, pinch = null;
      cv.addEventListener('pointerdown', e => { cv.setPointerCapture(e.pointerId); pts.set(e.pointerId, { x: e.offsetX, y: e.offsetY }); last = e.offsetX;
        if (pts.size === 2) { const a = [...pts.values()]; pinch = { d: Math.abs(a[0].x - a[1].x), c: this.count }; } });
      cv.addEventListener('pointermove', e => {
        if (pts.has(e.pointerId)) {
          pts.set(e.pointerId, { x: e.offsetX, y: e.offsetY });
          if (pts.size === 2 && pinch) {
            const a = [...pts.values()], d = Math.abs(a[0].x - a[1].x);
            if (d > 10) this.count = Math.max(20, Math.min(this.rows.length, pinch.c * pinch.d / d));
          } else if (pts.size === 1 && last !== null) {
            const bw = this._bw || 6, dx = e.offsetX - last;
            if (Math.abs(dx) >= 1) { this.end = Math.max(Math.min(this.count, this.rows.length), Math.min(this.rows.length, this.end - dx / bw)); last = e.offsetX; }
          }
        }
        this.hover = { x: e.offsetX, y: e.offsetY }; this.draw();
      });
      const up = e => { pts.delete(e.pointerId); if (pts.size < 2) pinch = null; if (!pts.size) last = null; };
      cv.addEventListener('pointerup', up); cv.addEventListener('pointercancel', up);
      cv.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse') { this.hover = null; this.draw(); } });
      cv.addEventListener('wheel', e => {
        e.preventDefault();
        if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) { this.end = Math.max(this.count, Math.min(this.rows.length, this.end + e.deltaX / (this._bw || 6))); }
        else this.count = Math.max(20, Math.min(this.rows.length, this.count * (e.deltaY > 0 ? 1.12 : 0.89)));
        this.draw();
      }, { passive: false });
      cv.tabIndex = 0;
      cv.addEventListener('keydown', e => {
        if (e.key === 'ArrowLeft') this.end = Math.max(this.count, this.end - 3);
        else if (e.key === 'ArrowRight') this.end = Math.min(this.rows.length, this.end + 3);
        else if (e.key === 'ArrowUp') this.count = Math.max(20, this.count * 0.85);
        else if (e.key === 'ArrowDown') this.count = Math.min(this.rows.length, this.count * 1.15);
        else return;
        e.preventDefault(); this.draw();
      });
    }
    draw() {
      if (!this.rows.length) return;
      const { ctx, w, h } = setup(this.cv);
      const [s, e] = this._range(); const R = this.rows.slice(s, e); const n = R.length;
      const padR = 58, padB = 18, W = w - padR;
      const hMain = (h - padB) * 0.62, hVol = (h - padB) * 0.15, hSub = (h - padB) * 0.23;
      const y0 = 0, y1 = hMain, y2 = hMain + hVol, y3 = h - padB;
      const bw = W / Math.max(n, 1); this._bw = bw;
      const cw = Math.max(1, Math.min(bw * 0.72, 18));
      let hi = -Infinity, lo = Infinity;
      for (let i = 0; i < n; i++) { hi = Math.max(hi, R[i].high); lo = Math.min(lo, R[i].low);
        for (const m of this.ma) { const v = m[s + i]; if (v != null) { hi = Math.max(hi, v); lo = Math.min(lo, v); } } }
      for (const o of this.overlays) if (o.price && o.price < hi * 1.15 && o.price > lo * 0.85) { hi = Math.max(hi, o.price); lo = Math.min(lo, o.price); }
      const pad = (hi - lo) * 0.06 || hi * 0.01; hi += pad; lo -= pad;
      const py = p => y0 + 8 + (hi - p) / (hi - lo) * (hMain - 16);
      const X = i => i * bw + bw / 2;
      // 网格
      ctx.strokeStyle = C.grid; ctx.lineWidth = 1; ctx.fillStyle = C.axis; ctx.textAlign = 'left';
      for (let k = 0; k <= 4; k++) {
        const p = lo + (hi - lo) * k / 4, y = py(p);
        ctx.beginPath(); ctx.moveTo(0, Math.round(y) + .5); ctx.lineTo(W, Math.round(y) + .5); ctx.stroke();
        ctx.fillText(p.toFixed(niceDigits(p)), W + 4, y);
      }
      [y1, y2].forEach(y => { ctx.strokeStyle = '#1f2832'; ctx.beginPath(); ctx.moveTo(0, y + .5); ctx.lineTo(w, y + .5); ctx.stroke(); });
      // 日期
      ctx.textAlign = 'center'; ctx.fillStyle = C.axis;
      const step = Math.max(1, Math.round(n / (W / 90)));
      for (let i = 0; i < n; i += step) { ctx.fillText(R[i].date.slice(2), X(i), y3 + 9); ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(X(i) + .5, 0); ctx.lineTo(X(i) + .5, y3); ctx.stroke(); }
      // 区间叠加
      for (const o of this.overlays) {
        if (o.zone) { const a = py(o.zone[1]), b = py(o.zone[0]); ctx.fillStyle = o.fill || 'rgba(255,176,0,.08)'; ctx.fillRect(0, a, W, Math.max(1, b - a)); }
      }
      // 蜡烛
      for (let i = 0; i < n; i++) {
        const r = R[i], up = r.close >= r.open, col = up ? C.up : C.down, x = X(i);
        ctx.strokeStyle = col; ctx.fillStyle = col;
        ctx.beginPath(); ctx.moveTo(Math.round(x) + .5, py(r.high)); ctx.lineTo(Math.round(x) + .5, py(r.low)); ctx.stroke();
        const top = py(Math.max(r.open, r.close)), bh = Math.max(1, Math.abs(py(r.open) - py(r.close)));
        if (up && cw > 3) { ctx.fillStyle = '#0c1014'; ctx.fillRect(x - cw / 2, top, cw, bh); ctx.strokeRect(Math.round(x - cw / 2) + .5, Math.round(top) + .5, Math.round(cw) - 1, Math.max(1, Math.round(bh) - 1)); }
        else ctx.fillRect(x - cw / 2, top, cw, bh);
      }
      // 均线
      this.ma.forEach((m, k) => {
        ctx.strokeStyle = C.ma[k]; ctx.lineWidth = 1; ctx.beginPath(); let st = false;
        for (let i = 0; i < n; i++) { const v = m[s + i]; if (v == null) continue; const y = py(v); if (!st) { ctx.moveTo(X(i), y); st = true; } else ctx.lineTo(X(i), y); }
        ctx.stroke();
      });
      // 水平线（止损/止盈）
      ctx.setLineDash([5, 4]);
      for (const o of this.overlays) {
        if (!o.price) continue; const y = py(o.price); if (y < 0 || y > hMain) continue;
        ctx.strokeStyle = o.color; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
        ctx.setLineDash([]); ctx.fillStyle = o.color; ctx.fillRect(W, y - 8, padR, 16); ctx.fillStyle = '#000'; ctx.textAlign = 'left';
        ctx.fillText(o.price.toFixed(2), W + 3, y); ctx.textAlign = 'right'; ctx.fillStyle = o.color; ctx.fillText(o.label, W - 4, y - 8); ctx.setLineDash([5, 4]);
      }
      ctx.setLineDash([]);
      // 区间最高/最低标注
      let hiI = 0, loI = 0; for (let i = 0; i < n; i++) { if (R[i].high > R[hiI].high) hiI = i; if (R[i].low < R[loI].low) loI = i; }
      ctx.fillStyle = C.white; ctx.textAlign = X(hiI) > W / 2 ? 'right' : 'left';
      ctx.fillText((X(hiI) > W / 2 ? '' : '← ') + R[hiI].high.toFixed(2) + (X(hiI) > W / 2 ? ' →' : ''), X(hiI) + (X(hiI) > W / 2 ? -4 : 4), py(R[hiI].high) - 6);
      ctx.textAlign = X(loI) > W / 2 ? 'right' : 'left';
      ctx.fillText((X(loI) > W / 2 ? '' : '← ') + R[loI].low.toFixed(2) + (X(loI) > W / 2 ? ' →' : ''), X(loI) + (X(loI) > W / 2 ? -4 : 4), py(R[loI].low) + 8);
      // 成交量
      let vmax = 0; for (const r of R) vmax = Math.max(vmax, r.vol);
      for (let i = 0; i < n; i++) { const r = R[i]; const hh = r.vol / (vmax || 1) * (hVol - 6); ctx.fillStyle = r.close >= r.open ? 'rgba(255,77,79,.75)' : 'rgba(23,209,122,.75)'; ctx.fillRect(X(i) - cw / 2, y2 - hh, cw, hh); }
      ctx.strokeStyle = C.amber; ctx.beginPath(); let st = false;
      for (let i = 0; i < n; i++) { const v = this.vma[s + i]; if (v == null) continue; const y = y2 - v / (vmax || 1) * (hVol - 6); if (!st) { ctx.moveTo(X(i), y); st = true; } else ctx.lineTo(X(i), y); }
      ctx.stroke();
      ctx.fillStyle = C.axis; ctx.textAlign = 'left'; ctx.fillText(fmtV(vmax), W + 4, y1 + 8);
      // 副图
      const sub = y2 + 4, subH = hSub - 8;
      if (this.sub === 'macd') {
        let m = 1e-9; for (let i = s; i < e; i++) m = Math.max(m, Math.abs(this.macd.dif[i]), Math.abs(this.macd.dea[i]), Math.abs(this.macd.hist[i]));
        const zy = sub + subH / 2, sy = v => zy - v / m * subH / 2;
        for (let i = 0; i < n; i++) { const v = this.macd.hist[s + i]; ctx.fillStyle = v >= 0 ? C.up : C.down; ctx.fillRect(X(i) - Math.max(1, cw / 3), Math.min(zy, sy(v)), Math.max(1, cw * 2 / 3), Math.abs(sy(v) - zy)); }
        [['dif', C.white], ['dea', C.amber]].forEach(([k, col]) => { ctx.strokeStyle = col; ctx.beginPath(); for (let i = 0; i < n; i++) { const y = sy(this.macd[k][s + i]); i ? ctx.lineTo(X(i), y) : ctx.moveTo(X(i), y); } ctx.stroke(); });
        ctx.fillStyle = C.axis; ctx.fillText('MACD', W + 4, sub + 8);
      } else {
        const sy = v => sub + (100 - v) / 100 * subH;
        ctx.strokeStyle = '#2a333d'; [20, 50, 80].forEach(v => { ctx.beginPath(); ctx.moveTo(0, sy(v)); ctx.lineTo(W, sy(v)); ctx.stroke(); ctx.fillStyle = C.axis; ctx.fillText(v, W + 4, sy(v)); });
        ctx.strokeStyle = C.cyan; ctx.beginPath(); let f = false;
        for (let i = 0; i < n; i++) { const v = this.rsi[s + i]; if (v == null) continue; f ? ctx.lineTo(X(i), sy(v)) : (ctx.moveTo(X(i), sy(v)), f = true); }
        ctx.stroke();
      }
      // 十字光标
      let hi_ = n - 1;
      if (this.hover && this.hover.x < W) {
        hi_ = Math.max(0, Math.min(n - 1, Math.floor(this.hover.x / bw)));
        ctx.strokeStyle = 'rgba(255,176,0,.6)'; ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(X(hi_) + .5, 0); ctx.lineTo(X(hi_) + .5, y3); ctx.stroke();
        if (this.hover.y < hMain) { ctx.beginPath(); ctx.moveTo(0, this.hover.y + .5); ctx.lineTo(W, this.hover.y + .5); ctx.stroke();
          const p = hi - (this.hover.y - 8) / (hMain - 16) * (hi - lo); ctx.setLineDash([]); ctx.fillStyle = C.amber; ctx.fillRect(W, this.hover.y - 8, padR, 16); ctx.fillStyle = '#000'; ctx.textAlign = 'left'; ctx.fillText(p.toFixed(2), W + 3, this.hover.y); }
        ctx.setLineDash([]);
        ctx.fillStyle = C.amber; ctx.textAlign = 'center'; const tx = Math.max(36, Math.min(W - 36, X(hi_))); ctx.fillRect(tx - 36, y3, 72, padB); ctx.fillStyle = '#000'; ctx.fillText(R[hi_].date, tx, y3 + 9);
      }
      if (this.lg) {
        const r = R[hi_], prev = this.rows[s + hi_ - 1], pc = prev ? (r.close / prev.close - 1) * 100 : 0;
        const cls = pc >= 0 ? 'up' : 'down', f = v => v == null ? '--' : v.toFixed(2);
        this.lg.innerHTML = `<span>${r.date}</span><span>开 <b class="${cls}">${f(r.open)}</b></span><span>高 <b class="${cls}">${f(r.high)}</b></span><span>低 <b class="${cls}">${f(r.low)}</b></span><span>收 <b class="${cls}">${f(r.close)}</b></span><span class="${cls}">${pc >= 0 ? '+' : ''}${pc.toFixed(2)}%</span><span>量 ${fmtV(r.vol)}</span>`
          + ['MA5', 'MA10', 'MA20', 'MA60'].map((k, j) => `<span style="color:${C.ma[j]}">${k} ${f(this.ma[j][s + hi_])}</span>`).join('');
      }
    }
  }

  class Intraday {
    constructor(canvas, legendEl) { this.cv = canvas; this.lg = legendEl; this.d = null; this.hover = null;
      canvas.addEventListener('pointermove', e => { this.hover = e.offsetX; this.draw(); });
      canvas.addEventListener('pointerleave', () => { this.hover = null; this.draw(); });
      new ResizeObserver(() => this.draw()).observe(canvas); }
    setData(d) { this.d = d; this.draw(); }
    draw() {
      const d = this.d; if (!d || !d.points || !d.points.length) return;
      const { ctx, w, h } = setup(this.cv);
      const padL = 52, padR = 52, padB = 18, W = w - padL - padR, hP = (h - padB) * 0.72, hV = (h - padB) * 0.24;
      const pts = d.points, prev = d.prev || pts[0].p;
      let m = 0; for (const p of pts) m = Math.max(m, Math.abs(p.p - prev), Math.abs((p.avg || p.p) - prev)); m = m * 1.1 || prev * 0.01;
      const py = p => 6 + (prev + m - p) / (2 * m) * (hP - 12), N = 241, X = i => padL + i / (N - 1) * W;
      ctx.strokeStyle = C.grid; ctx.fillStyle = C.axis;
      for (let k = 0; k <= 4; k++) { const p = prev + m - 2 * m * k / 4, y = py(p); ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + W, y); ctx.stroke();
        ctx.textAlign = 'right'; ctx.fillStyle = p > prev ? C.up : p < prev ? C.down : C.axis; ctx.fillText(p.toFixed(2), padL - 4, y);
        ctx.textAlign = 'left'; ctx.fillText(((p / prev - 1) * 100).toFixed(2) + '%', padL + W + 4, y); }
      ctx.strokeStyle = '#3a4653'; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(padL, py(prev)); ctx.lineTo(padL + W, py(prev)); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = C.axis; ctx.textAlign = 'center';
      [['09:30', 0], ['10:30', 60], ['11:30/13:00', 120], ['14:00', 180], ['15:00', 240]].forEach(([t, i]) => ctx.fillText(t, Math.min(Math.max(X(i), padL + 20), padL + W - 20), h - 9));
      const last = pts[pts.length - 1].p, col = last >= prev ? C.up : C.down;
      const g = ctx.createLinearGradient(0, 0, 0, hP); g.addColorStop(0, last >= prev ? 'rgba(255,77,79,.25)' : 'rgba(23,209,122,.25)'); g.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.beginPath(); pts.forEach((p, i) => i ? ctx.lineTo(X(i), py(p.p)) : ctx.moveTo(X(i), py(p.p))); ctx.lineTo(X(pts.length - 1), hP); ctx.lineTo(X(0), hP); ctx.fillStyle = g; ctx.fill();
      ctx.strokeStyle = C.white; ctx.lineWidth = 1.2; ctx.beginPath(); pts.forEach((p, i) => i ? ctx.lineTo(X(i), py(p.p)) : ctx.moveTo(X(i), py(p.p))); ctx.stroke();
      ctx.strokeStyle = C.amber; ctx.lineWidth = 1; ctx.beginPath(); pts.forEach((p, i) => i ? ctx.lineTo(X(i), py(p.avg || p.p)) : ctx.moveTo(X(i), py(p.avg || p.p))); ctx.stroke();
      let vm = 0; pts.forEach(p => vm = Math.max(vm, p.v)); const vb = h - padB;
      pts.forEach((p, i) => { const hh = p.v / (vm || 1) * hV; ctx.fillStyle = (i && p.p < pts[i - 1].p) ? C.down : C.up; ctx.fillRect(X(i) - 0.6, vb - hh, Math.max(1, W / N - 0.4), hh); });
      ctx.fillStyle = col; ctx.beginPath(); ctx.arc(X(pts.length - 1), py(last), 3, 0, 7); ctx.fill();
      let k = pts.length - 1;
      if (this.hover != null && this.hover > padL) { k = Math.max(0, Math.min(pts.length - 1, Math.round((this.hover - padL) / W * (N - 1))));
        ctx.strokeStyle = 'rgba(255,176,0,.6)'; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(X(k), 0); ctx.lineTo(X(k), h - padB); ctx.stroke(); ctx.setLineDash([]); }
      if (this.lg) { const p = pts[k], pc = (p.p / prev - 1) * 100;
        this.lg.innerHTML = `<span>${p.t}</span><span>价 <b class="${pc >= 0 ? 'up' : 'down'}">${p.p.toFixed(2)}</b></span><span class="${pc >= 0 ? 'up' : 'down'}">${pc >= 0 ? '+' : ''}${pc.toFixed(2)}%</span><span style="color:${C.amber}">均价 ${(p.avg || p.p).toFixed(2)}</span><span>量 ${fmtV(p.v)}</span>`; }
    }
  }

  function spark(canvas, vals, base) {
    if (!vals || vals.length < 2) return;
    const { ctx, w, h } = setup(canvas);
    let hi = Math.max(...vals), lo = Math.min(...vals); if (base != null) { hi = Math.max(hi, base); lo = Math.min(lo, base); }
    const py = v => 2 + (hi - v) / ((hi - lo) || 1) * (h - 4), X = i => i / (vals.length - 1) * w;
    const up = vals[vals.length - 1] >= (base != null ? base : vals[0]);
    if (base != null) { ctx.strokeStyle = '#2a333d'; ctx.setLineDash([2, 3]); ctx.beginPath(); ctx.moveTo(0, py(base)); ctx.lineTo(w, py(base)); ctx.stroke(); ctx.setLineDash([]); }
    const g = ctx.createLinearGradient(0, 0, 0, h); g.addColorStop(0, up ? 'rgba(255,77,79,.3)' : 'rgba(23,209,122,.3)'); g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.beginPath(); vals.forEach((v, i) => i ? ctx.lineTo(X(i), py(v)) : ctx.moveTo(0, py(v))); ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.fillStyle = g; ctx.fill();
    ctx.strokeStyle = up ? C.up : C.down; ctx.lineWidth = 1.2; ctx.beginPath(); vals.forEach((v, i) => i ? ctx.lineTo(X(i), py(v)) : ctx.moveTo(0, py(v))); ctx.stroke();
  }

  function lines(canvas, labels, series, opts = {}) {
    const { ctx, w, h } = setup(canvas);
    if (!labels.length) { ctx.fillStyle = C.axis; ctx.textAlign = 'center'; ctx.fillText('暂无数据', w / 2, h / 2); return; }
    const padR = 56, padB = 18, W = w - padR, H = h - padB;
    let hi = -Infinity, lo = Infinity; series.forEach(s => s.values.forEach(v => { if (v != null) { hi = Math.max(hi, v); lo = Math.min(lo, v); } }));
    const pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.02 || 1; hi += pad; lo -= pad;
    const py = v => 6 + (hi - v) / (hi - lo) * (H - 12), X = i => labels.length === 1 ? W / 2 : i / (labels.length - 1) * W;
    ctx.strokeStyle = C.grid; ctx.fillStyle = C.axis; ctx.textAlign = 'left';
    for (let k = 0; k <= 4; k++) { const v = lo + (hi - lo) * k / 4; ctx.beginPath(); ctx.moveTo(0, py(v)); ctx.lineTo(W, py(v)); ctx.stroke(); ctx.fillText(opts.fmt ? opts.fmt(v) : v.toFixed(2), W + 4, py(v)); }
    ctx.textAlign = 'center'; const step = Math.max(1, Math.round(labels.length / (W / 90)));
    for (let i = 0; i < labels.length; i += step) ctx.fillText(String(labels[i]).slice(2), X(i), h - 9);
    series.forEach(s => { ctx.strokeStyle = s.color; ctx.lineWidth = s.width || 1.4; ctx.beginPath(); let f = false;
      s.values.forEach((v, i) => { if (v == null) return; f ? ctx.lineTo(X(i), py(v)) : (ctx.moveTo(X(i), py(v)), f = true); }); ctx.stroke(); });
    if (opts.base != null) { ctx.strokeStyle = '#3a4653'; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(0, py(opts.base)); ctx.lineTo(W, py(opts.base)); ctx.stroke(); ctx.setLineDash([]); }
  }

  window.Charts = { KChart, Intraday, spark, lines, fmtV };
})();
