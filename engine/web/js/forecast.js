/* Private forecast viewer: no order requests, no invented probability or profit. */
(() => {
  'use strict';
  const esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const n = (x,d=2) => typeof x==='number' && Number.isFinite(x) ? x.toFixed(d) : '—';
  const pct = x => typeof x==='number' && Number.isFinite(x) ? n(x*100)+'%' : '未验证';
  const labels={chan_only:'纯缠论',math_only:'数学模型',fusion:'综合模型',legacy_control:'旧策略同规则对照'};
  const phases={not_started:'尚未启动',warming_up:'准备数据',waiting_signal:'等待信号',data_blocked:'数据异常，暂缓新交易',paused:'已暂停',awaiting_account:'等待账户',awaiting_valid_opening:'等待合格开局行情',awaiting_private_snapshot:'等待加密快照'};
  const reasons={long_data_missing:'长周期数据不足',short_data_missing:'短周期数据不足',model_unvalidated:'模型尚未验证',timeframe_conflict:'多级别方向冲突',waiting_signal:'等待信号',prediction_expired:'预测已到期',no_recent_confirmed_chan_point:'等待新确认的缠论买卖点',t1_locked_risk:'当日买入尚不可卖',open_t_cycle_priority:'优先处理未闭环做T',incomplete_structure_inputs:'结构或行情证据不足',position_slots_reserved:'待成交买单已占用持仓名额',long_risk_exit:'长周期风险退出',long_sell:'长周期卖出',short_sell:'短周期做T高抛',t_buyback:'做T买回',long_short_entry:'长短周期一致入场',stale_quote:'报价已过期',missing_quote:'缺少报价',pending:'等待结果',settled:'已对账',unknown:'结果未知',not_implemented:'尚未实现'};
  const explain=x=>reasons[x]||x||'等待';
  let timer=null, currentCode='', currentTF='day';

  function chart(item) {
    const s=item?.structures||{}, bars=s.klines||[], candles=bars.filter(b=>Number.isFinite(b.high)&&Number.isFinite(b.low));
    if (!candles.length) return '<div class="empty">尚无可绘制的结构。数据不足时不会补画买卖点。</div>';
    const W=860,H=300,left=45,top=20,bottom=265;
    const lo=Math.min(...candles.map(b=>b.low)), hi=Math.max(...candles.map(b=>b.high)), range=hi-lo||1;
    const y=p=>bottom-(p-lo)/range*(bottom-top), x=i=>left+i*(W-left-15)/Math.max(1,candles.length-1);
    const dates=candles.map(b=>b.end||b.at||b.date||b.time);
    const ix=t=>{let i=dates.findIndex(d=>d>=t);return i<0?dates.length-1:i;};
    let svg=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="缠论结构图"><line x1="${left}" y1="${bottom}" x2="${W}" y2="${bottom}" stroke="currentColor" opacity=".2"/>`;
    for(let k=0;k<5;k++){const p=lo+k*range/4;svg+=`<text x="0" y="${y(p)+4}" font-size="11" fill="currentColor">${n(p)}</text><line x1="${left}" y1="${y(p)}" x2="${W}" y2="${y(p)}" stroke="currentColor" opacity=".08"/>`;}
    candles.forEach((b,i)=>{svg+=`<line x1="${x(i)}" y1="${y(b.high)}" x2="${x(i)}" y2="${y(b.low)}" stroke="#8a9cac" stroke-width="3" opacity=".5"/>`;});
    for(const c of s.centers||[]){const a=c.low??c.ZD??c.low_price,b=c.high??c.ZG??c.high_price;if(!Number.isFinite(a)||!Number.isFinite(b))continue;svg+=`<rect x="${x(ix(c.start))}" y="${y(b)}" width="${Math.max(3,x(ix(c.end))-x(ix(c.start)))}" height="${Math.max(1,y(a)-y(b))}" fill="#b990ef" fill-opacity=".15" stroke="#b990ef" stroke-dasharray="${c.confirmed?'none':'4 3'}"/>`;}
    for(const [key,color,width] of [['strokes','#ffba62',1.4],['segments','#70b9ff',2]])for(const b of s[key]||[]){const a=b.start_price??b.begin_value??b.begin_price,z=b.end_price??b.end_value;if(!Number.isFinite(a)||!Number.isFinite(z))continue;svg+=`<line x1="${x(ix(b.start))}" y1="${y(a)}" x2="${x(ix(b.end))}" y2="${y(z)}" stroke="${color}" stroke-width="${width}" stroke-dasharray="${b.confirmed?'none':'4 3'}"/>`;}
    for(const p of s.buy_sell_points||[]){if(!p.confirmed||!Number.isFinite(p.price))continue;svg+=`<text x="${x(ix(p.at))}" y="${y(p.price)+(p.side==='buy'?16:-8)}" fill="${p.side==='buy'?'#e76c6c':'#39b892'}" font-size="11">${esc((p.side==='buy'?'买':'卖')+(p.types||[]).join('/'))}</text>`;}
    return svg+'</svg><div class="hint">灰：合并K线区间 · 橙：笔 · 蓝：线段 · 紫：中枢 · 虚线：待确认；图示为当时可用结构，不能倒推成历史可成交价。</div>';
  }

  function table(rows,headers,render) {
    return `<div class="scroll"><table class="t"><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(render).join('')||`<tr><td colspan="${headers.length}" class="muted">尚无记录</td></tr>`}</tbody></table></div>`;
  }
  function draw(f,options) {
    const st=f.status||{},ctl=f.controls||{}, rows=f.predictions||[], structures=f.structures||{};
    const codes=[...new Set([...rows.map(r=>r.code),...Object.values(structures).map(r=>r.code),...(f.coverage||[]).map(r=>r.code)])];
    if(!codes.includes(currentCode))currentCode=codes[0]||'';
    const selected=structures[currentCode+':'+currentTF];
    const coverage=(f.coverage||[]).find(c=>c.code===currentCode)?.coverage||[];
    const model=f.model_status||{},evals=f.evaluation?.evaluations||[],last=evals[0]?.report||{};
    const books=Object.entries(f.ledger?.books||{});
    return `<div class="fc-shell"><div class="fc-title"><div><h2>缠论 · 综合预测</h2><div class="hint">长短周期联动 · 独立模拟 · 结果对账 · 有门槛的自动纠偏</div></div><button class="btn" id="fc-refresh">刷新</button></div>
      ${f.locked?'<div class="empty">解锁个人数据后查看预测与模拟账本。<button class="btn" id="fc-unlock">解锁</button></div>':''}
      <div class="fc-cards"><article><span>执行状态</span><b>${esc(phases[st.phase]||st.phase||'未知')}</b><small>${esc(st.role==='cloud'?'云端':st.role==='pc'?'电脑端':'运行位置待确认')} · ${esc(st.last_execution||'尚无执行心跳')}</small></article>
      <article><span>当前操盘版本</span><b>${ctl.active_version?'综合模型 v'+esc(ctl.active_version):'原 AI 策略'}</b><small>${ctl.paused?'预测交易已暂停':'候选版本独立对照，未达标不接管'}</small></article>
      <article><span>研究覆盖</span><b>${n(st.covered,0)} / ${n(st.requested,0)}</b><small>最多40只 · 持仓优先 · 不代表全市场实时覆盖</small></article>
      <article><span>前瞻验证</span><b>${n(last.days??0,0)} / 60 交易日</b><small>还须逐期限样本、闭环交易、概率与收益门槛全部通过</small></article></div>
      ${st.asof && Date.now()-Date.parse(st.asof)>120000?`<div class="fc-warning">当前显示历史快照（${esc(st.asof)}），不能据此确认此刻的运行或行情状态。</div>`:''}
      ${st.error?`<div class="fc-warning">最近异常：${esc(st.error)}。请结合数据覆盖查看，异常不会自动变成“无信号”。</div>`:''}
      <div class="row"><label>股票 <select id="fc-code">${codes.map(c=>`<option ${c===currentCode?'selected':''}>${esc(c)}</option>`).join('')}</select></label><label>级别 <select id="fc-tf">${['day','60m','15m','5m'].map(c=>`<option ${c===currentTF?'selected':''}>${c}</option>`).join('')}</select></label><span class="hint">${esc(selected?.quality?.reason||'等待结构输入')}</span></div>
      <div class="fc-chart">${chart(selected)}</div>
      <h3>长短周期预测</h3>${table(rows.filter(r=>!currentCode||r.code===currentCode).slice(0,36),['期限','模型','形成时间','上涨概率','收益区间 P10 / P50 / P90','状态'],r=>`<tr><td>${esc(r.horizon)}</td><td>${esc(labels[r.variant]||r.variant)} ${r.model_version?'v'+esc(r.model_version):''}</td><td>${esc(r.asof)}</td><td>${pct(r.probability)}</td><td>${pct(r.quantiles?.p10)} / ${pct(r.quantiles?.p50)} / ${pct(r.quantiles?.p90)}</td><td>${esc(r.quality?.eligible?(r.reason||r.result?.status):r.quality?.reason||'数据不足')}</td></tr>`)}
      <h3>指标与理论覆盖</h3><div class="fc-coverage">${coverage.map(c=>`<article><b>${esc(c.name)}</b><span class="${c.count?'up':'muted'}">${c.count?c.count+'项已计算':'未覆盖'}</span><small>${esc(c.theory)} · ${esc(c.reason||'是否有效仍由样本外与前瞻结果检验')}</small></article>`).join('')||'<div class="hint">尚未完成第一轮分析；不会把缺失数据补成中性指标。</div>'}</div>
      <h3>独立模拟账本</h3>${table(books,['账户','现金','持仓数','收盘权益','闭环状态'],([name,b])=>`<tr><td>${esc(labels[name]||name)}</td><td>${n(b.cash)}</td><td>${Object.keys(b.holdings||{}).length}</td><td>${n(b.latest_mark?.equity??(b.latest_mark?.equity_fen/100))}</td><td>${(b.open_cycles||[]).length} 个未闭环</td></tr>`)}
      <h3>操作理由与等待记录</h3>${table((f.decisions||[]).slice(0,15),['时间','股票','账户','动作','原因'],r=>`<tr><td>${esc(r.at)}</td><td>${esc(r.code)}</td><td>${esc(r.book)}</td><td>${esc({wait:'等待',buy:'买入',sell:'卖出',blocked:'暂缓'}[r.action]||r.action)}</td><td>${esc(explain(r.reason))}</td></tr>`)}
      <details><summary>模型、纠偏与切换证据</summary>${table(model.versions||[],['版本','创建时间','状态','训练数据指纹'],r=>`<tr><td>${esc(r.version)}</td><td>${esc(r.created)}</td><td>${esc(r.status)}</td><td>${esc(r.training_hash?.slice(0,18))}</td></tr>`)}${table(evals,['日期','版本','类型','结果'],r=>`<tr><td>${esc(r.created)}</td><td>${esc(r.version)}</td><td>${esc(r.kind)}</td><td>${esc(r.report?.decision||(r.report?.passed?'通过':'未通过'))}</td></tr>`)}</details>
      ${!options.staticMode?'<div class="row"><button class="btn" data-fc-action="pause">暂停预测交易</button><button class="btn" data-fc-action="resume">恢复预测交易</button><button class="btn" data-fc-action="rollback">回滚已采用版本</button></div>':'<div class="hint">云端加密快照为只读；此页面不会在手机上执行交易。</div>'}
      <div class="hint">概率未验证时不显示假胜率；行情、财务或理论未覆盖会保留缺口。工程运行正常不代表预测有效或模拟盈利。</div></div>`;
  }
  async function render(options) {
    clearTimeout(timer);
    const root=document.getElementById('p-forecast');if(!root)return;
    try {
      const f=await options.api('forecast'+(currentCode?'?code='+encodeURIComponent(currentCode):''));
      root.innerHTML=draw(f,options);
      root.querySelector('#fc-refresh').onclick=()=>render(options);
      const unlock=root.querySelector('#fc-unlock');if(unlock)unlock.onclick=options.unlock;
      root.querySelector('#fc-code').onchange=e=>{currentCode=e.target.value;render(options);};
      root.querySelector('#fc-tf').onchange=e=>{currentTF=e.target.value;render(options);};
      root.querySelectorAll('[data-fc-action]').forEach(b=>b.onclick=async()=>{try{await options.api('forecast/control',{action:b.dataset.fcAction});await render(options);}catch(e){alert(e.message);}});
    } catch(e) { root.innerHTML=`<div class="empty">预测数据暂不可用：${esc(e.message)}</div>`; }
    if(location.hash.split('/')[0]==='#forecast')timer=setTimeout(()=>render(options),15000);
  }
  window.ForecastUI={render,chart,draw};
})();
