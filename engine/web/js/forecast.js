/* Uses the original terminal panels. Stock analysis requests never place orders. */
(() => {
  'use strict';
  const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const finite=x=>typeof x==='number'&&Number.isFinite(x),num=x=>finite(x)?x.toFixed(2):'待确认';
  const pct=x=>finite(x)?num(x*100)+'%':'学习中';
  const levels={day:'日线','60m':'60分钟','15m':'15分钟','5m':'5分钟'};
  const horizons={'1d':'未来1个交易日','3d':'未来3个交易日','5d':'未来5个交易日','20d':'未来20个交易日','30m':'未来30交易分钟','60m':'未来60交易分钟'};
  const variants={chan_only:'纯缠论',math_only:'数学模型',fusion:'综合模型',legacy_control:'原策略对照'};
  const phases={not_started:'等待分析服务',warming_up:'正在准备行情',waiting_signal:'分析运行中',data_blocked:'行情暂不可用',paused:'预测交易已暂停',awaiting_account:'等待模拟账户',awaiting_valid_opening:'等待完整开局行情',awaiting_private_snapshot:'等待同步分析结果'};
  const reasons={ok:'数据已核对',long_data_missing:'长周期数据不足',short_data_missing:'短周期数据不足',model_unvalidated:'概率尚未验证',timeframe_conflict:'长短周期方向不一致',waiting_signal:'等待明确的结构信号',prediction_expired:'本次预测已到期',no_recent_confirmed_chan_point:'尚无新确认的买卖点',t1_locked_risk:'当日买入的持仓尚不可卖',open_t_cycle_priority:'先处理尚未完成的做T',incomplete_structure_inputs:'结构或行情数据仍不足',long_risk_exit:'长期结构触及风险边界',long_sell:'长期结构转弱',short_sell:'短期结构提示做T高抛',t_buyback:'做T买回',long_short_entry:'长短周期形成一致信号',stale_quote:'报价已过期',missing_quote:'暂未取得报价',pending:'等待结果',settled:'已完成对账',unknown:'结果尚无法确认',not_implemented:'暂未实现',no_point_in_time_disclosure:'尚无可用的时点财报',no_qualified_industry_members:'板块样本暂不足',no_timestamped_observed_announcements:'暂无已核对时间的公告',missing_or_stale_quote:'报价缺失或已过期',insufficient_completed_bars:'完整K线数量不足',stale_completed_bar:'K线尚未更新',missing_bars:'K线存在缺口',adjustment_unverified:'复权口径尚未核对',candidate:'正在对照验证',active:'已采用',rejected:'未通过验证',retired:'保留的历史版本',rolled_back:'已回滚'};
  const explain=x=>reasons[x]||(/[\u3400-\u9fff]/.test(String(x||''))?String(x):'相关数据仍在更新');
  const normalize=raw=>{const x=String(raw||'').trim().toLowerCase();return /^(sh|sz)\d{6}$/.test(x)?x:/^\d{6}$/.test(x)?(x[0]==='6'?'sh':'sz')+x:'';};
  const short=code=>String(code||'').replace(/^(sh|sz)/,'');
  const names=new Map(),requests=new Map();
  let timer=null,searchTimer=null,currentCode='',currentTF='day',lastRouteCode=null,lastData={},lastOptions={};
  let loadSequence=0,searchSequence=0,requestSequence=0,suggestions=[],suggestionIndex=0,searchText='',composing=false;
  const symbolText=code=>(names.get(code)?names.get(code)+' ':'')+short(code);
  const timestamp=value=>{const x=String(value||'');return Date.parse(/^\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d$/.test(x)?x.replace(' ','T')+'+08:00':x);};
  const displayTime=value=>{const at=timestamp(value);return Number.isFinite(at)?new Date(at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).replace(/\//g,'-'):String(value||'');};
  const panel=(title,body,right='')=>`<section class="panel"><div class="ph"><h3>${title}</h3>${right?`<div class="r">${right}</div>`:''}</div><div class="pb">${body}</div></section>`;
  const table=(rows,headers,render,cls='')=>`<div class="scroll"><table class="t ${cls}"><thead><tr>${headers.map(x=>`<th>${esc(x)}</th>`).join('')}</tr></thead><tbody>${rows.map(render).join('')||`<tr><td colspan="${headers.length}" class="muted">暂无记录</td></tr>`}</tbody></table></div>`;
  function remember(f){
    const symbols=Array.isArray(f.symbols)?f.symbols:Object.values(f.symbols||{});
    for(const row of [...symbols,f.symbol||{},...(f.predictions||[])]){const code=normalize(row.code);if(code&&row.name)names.set(code,String(row.name));}
    if(currentCode&&searchText===short(currentCode))searchText=symbolText(currentCode);
  }
  function knownCodes(f){
    const symbols=Array.isArray(f.symbols)?f.symbols:Object.values(f.symbols||{});
    return [...new Set([...symbols,...(f.predictions||[]),...Object.values(f.structures||{}),...(f.coverage||[])].map(x=>normalize(x.code)).filter(Boolean))];
  }
  function chart(item){
    const s=item?.structures||{},bars=(s.klines||[]).filter(b=>finite(b.high)&&finite(b.low));
    if(!bars.length)return '<div class="empty">该级别的结构图尚未更新，收到完整行情后显示。</div>';
    const W=Math.min(860,Math.max(320,document.getElementById('p-forecast')?.clientWidth||860)),H=W<600?240:300,left=46,bottom=H-35,low=Math.min(...bars.map(b=>b.low)),high=Math.max(...bars.map(b=>b.high)),range=high-low||1;
    const y=p=>bottom-(p-low)/range*(H-53),x=i=>left+i*(W-left-15)/Math.max(1,bars.length-1);
    const dates=bars.map(b=>b.end||b.at||b.date||b.time||''),ix=t=>{const i=dates.findIndex(d=>d>=t);return i<0?dates.length-1:i;};
    let svg=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(levels[currentTF])}缠论结构图">`;
    for(let k=0;k<5;k++){const p=low+k*range/4;svg+=`<text x="0" y="${y(p)+4}" font-size="11" fill="var(--muted)">${num(p)}</text><line x1="${left}" y1="${y(p)}" x2="${W}" y2="${y(p)}" stroke="var(--line)"/>`;}
    for(const c of s.centers||[]){const a=c.low??c.ZD??c.low_price,b=c.high??c.ZG??c.high_price;if(!finite(a)||!finite(b))continue;svg+=`<rect x="${x(ix(c.start))}" y="${y(b)}" width="${Math.max(3,x(ix(c.end))-x(ix(c.start)))}" height="${Math.max(1,y(a)-y(b))}" fill="var(--cyan)" fill-opacity=".1" stroke="var(--cyan)" stroke-dasharray="${c.confirmed?'none':'4 3'}"/>`;}
    bars.forEach((b,i)=>{svg+=`<line x1="${x(i)}" y1="${y(b.high)}" x2="${x(i)}" y2="${y(b.low)}" stroke="var(--muted)" stroke-width="3" opacity=".55"/>`;});
    for(const [key,color,width] of [['strokes','var(--amber)',1.4],['segments','var(--cyan)',2.2]])for(const b of s[key]||[]){const a=b.start_price??b.begin_value??b.begin_price,z=b.end_price??b.end_value;if(!finite(a)||!finite(z))continue;svg+=`<line x1="${x(ix(b.start))}" y1="${y(a)}" x2="${x(ix(b.end))}" y2="${y(z)}" stroke="${color}" stroke-width="${width}" stroke-dasharray="${b.confirmed?'none':'4 3'}"/>`;}
    for(const p of s.buy_sell_points||[]){if(!p.confirmed||!finite(p.price))continue;svg+=`<text x="${x(ix(p.at))}" y="${y(p.price)+(p.side==='buy'?16:-8)}" fill="${p.side==='buy'?'var(--up)':'var(--down)'}" font-size="11">${esc((p.side==='buy'?'买':'卖')+(p.types||[]).join('/'))}</text>`;}
    svg+=`<text x="${left}" y="${H-10}" font-size="11" fill="var(--muted)">${esc(String(dates[0]).slice(0,16).replace('T',' '))}</text><text x="${W-10}" y="${H-10}" text-anchor="end" font-size="11" fill="var(--muted)">${esc(String(dates[dates.length-1]).slice(0,16).replace('T',' '))}</text>`;
    return svg+'</svg><div class="hint">灰线：合并K线区间 · 主题色：笔 · 青色：线段和中枢 · 虚线：尚待确认。买卖标记表示结构信号。</div>';
  }
  function structureSummary(f,code){
    const selected=f.structures?.[code+':'+currentTF],supplied=(!f.symbol?.code||normalize(f.symbol.code)===code)?f.summary:null;
    if(supplied&&typeof supplied==='object')return {...supplied,available:!!(supplied.summary||supplied.stance||selected)};
    if(!selected)return {available:false};
    const s=selected.structures||{},segment=(s.segments||[]).filter(x=>x.confirmed).slice(-1)[0],center=(s.centers||[]).filter(x=>x.confirmed).slice(-1)[0];
    return {available:true,stance:segment?.direction==='up'?'结构偏强':segment?.direction==='down'?'结构偏弱':'方向待确认',
      summary:segment?'当前级别最近确认的线段'+(segment.direction==='up'?'向上。':'向下。')+'需结合其他级别与后续行情观察。':'当前级别还没有足够的已确认线段，先观察结构形成。',
      support:center?.low??center?.ZD,resistance:center?.high??center?.ZG,updated:selected.asof,quality:selected.quality,warnings:[]};
  }
  function priceArea(x){
    if(finite(x))return num(x)+' 元';
    if(Array.isArray(x)&&x.length&&x.every(finite))return x.map(num).join(' ～ ')+' 元';
    if(x&&finite(x.low)&&finite(x.high))return num(x.low)+' ～ '+num(x.high)+' 元';
    return '尚未确认';
  }
  function diagnostics(f,options){
    const st=f.status||{},ctl=f.controls||{},coverage=(f.coverage||[]).find(x=>x.code===currentCode)?.coverage||[],books=Object.entries(f.ledger?.books||{});
    const bookName=key=>{const [base,version]=key.split('@');return (variants[base]||'模拟账户')+(version?' · 版本'+version:'');};
    const content=`<div class="row hint"><span>${esc(phases[st.phase]||'状态待同步')}</span><span>${esc(st.role==='cloud'?'云端运行':st.role==='pc'?'电脑端运行':'运行位置待确认')}</span><span>覆盖 ${finite(st.covered)?st.covered:0} / ${finite(st.requested)?st.requested:0} 只</span><span>${ctl.active_version?'已采用版本 '+esc(ctl.active_version):'当前沿用原 AI 策略'}</span></div>
      <h4>数据与指标覆盖</h4>${table(coverage,['类别','覆盖','说明'],r=>`<tr><td>${esc(r.name)}</td><td>${r.count?esc(r.count)+'项':'暂无'}</td><td>${esc(r.reason?explain(r.reason):'已计算，效果仍需检验')}</td></tr>`)}
      <h4>独立模拟记录</h4>${table(books,['账户','现金','持仓数','未闭环'],([key,b])=>`<tr><td>${esc(bookName(key))}</td><td>${num(b.cash)}</td><td>${Object.keys(b.holdings||{}).length}</td><td>${(b.open_cycles||[]).length}</td></tr>`)}
      <h4>模型与验证</h4><p class="hint">候选模型先完成至少60个交易日及其他样本、收益和风险门槛，才允许接管。</p>${table(f.model_status?.versions||[],['版本','状态','建立时间'],r=>`<tr><td>${esc(r.version)}</td><td>${esc(explain(r.status))}</td><td>${esc(r.created)}</td></tr>`)}
      ${table(f.evaluation?.evaluations||[],['时间','版本','结果'],r=>`<tr><td>${esc(r.created)}</td><td>${esc(r.version)}</td><td>${esc(r.report?.passed?'已通过':explain(r.report?.decision))}</td></tr>`)}
      <h4>最近处理记录</h4>${table((f.decisions||[]).filter(r=>!currentCode||r.code===currentCode).slice(0,12),['时间','动作','说明'],r=>`<tr><td>${esc(r.at)}</td><td>${esc({wait:'等待',buy:'模拟买入',sell:'模拟卖出',blocked:'暂缓'}[r.action]||'观察')}</td><td>${esc(explain(r.reason))}</td></tr>`)}
      ${!options.staticMode?'<div class="row fc-controls"><button class="btn" data-fc-action="pause">暂停预测交易</button><button class="btn" data-fc-action="resume">恢复预测交易</button><button class="btn" data-fc-action="rollback">回滚采用版本</button></div>':''}`;
    return `<details class="fc-diagnostics"><summary>运行与研究记录</summary>${panel('运行与研究记录',content)}</details>`;
  }
  function draw(f,options={}){
    remember(f);if(!currentCode)currentCode=normalize(options.code)||normalize(f.symbol?.code)||knownCodes(f)[0]||'';
    const selected=f.structures?.[currentCode+':'+currentTF],summary=structureSummary(f,currentCode),request=requests.get(currentCode);
    const rows=(f.predictions||[]).filter(r=>r.code===currentCode&&r.variant==='fusion'),latest=new Map();
    for(const row of rows)if(!latest.has(row.horizon)||timestamp(row.asof)>timestamp(latest.get(row.horizon).asof))latest.set(row.horizon,row);
    const probabilities=[...latest.values()].filter(r=>r.validated&&finite(r.probability)),updated=summary.updated||selected?.asof;
    const age=Date.now()-timestamp(updated),stale=Number.isFinite(age)&&age>900000;
    const search=`<div class="row fc-search-row"><label class="fc-search"><span class="hint">股票名称或代码</span><span class="fc-input-wrap"><input class="in" id="fc-query" type="search" value="${esc(searchText||symbolText(currentCode))}" placeholder="输入名称 / 代码，如 浦发银行 或 600000" autocomplete="off" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="fc-suggestions"><span class="suggest" id="fc-suggestions" role="listbox"></span></span></label><button class="btn pri" id="fc-analyze" type="button">分析</button></div><div id="fc-search-message" class="hint" role="status" aria-live="polite"></div>`;
    const header=panel('个股综合预测',search,'<button class="btn sm" id="fc-refresh">刷新</button>');
    let notice='';
    if(f.locked)notice='<div class="empty">先解锁个人数据，再查看完整分析。<button class="btn" id="fc-unlock">解锁查看</button></div>';
    else if(request?.status==='requesting'||request?.status==='queued')notice=`<p class="fc-notice" role="status">${esc(request.message||('正在请求云端分析 '+symbolText(currentCode)+'…'))}</p>`;
    else if(request?.status==='error')notice=`<p class="fc-notice" role="status">${esc(explain(request.message||'分析请求暂未成功，请点击分析重试。'))}</p>`;
    if(!currentCode)return `<div class="fc-shell">${header}${notice}${panel('开始分析','<div class="empty">输入股票名称或代码，选择股票后查看结构分析。</div>')}</div>`;
    const title=`${esc(names.get(currentCode)||'个股分析')} <small>${esc(short(currentCode))}</small>`;
    const stance=({up:'结构偏强',down:'结构偏弱',neutral:'震荡观察',mixed:'方向分歧',waiting:'方向待确认'}[summary.stance]||summary.stance||'等待分析');
    let analysis;
    if(summary.available)analysis=`<div class="row"><strong>${esc(explain(stance))}</strong><span class="chip">结构分析</span></div><p class="fc-summary">${esc(summary.summary||'结构已更新，请结合图中的中枢、线段和确认信号观察。')}</p>
      <dl class="kv fc-price-levels"><dt>结构支撑参考</dt><dd>${esc(priceArea(summary.support))}</dd><dt>结构阻力参考</dt><dd>${esc(priceArea(summary.resistance))}</dd></dl>
      <p class="hint">${summary.quality?.eligible===false?'当前行情未通过校验，暂不生成新的走势判断。'+(summary.quality.reason?' '+esc(explain(summary.quality.reason)):''):probabilities.length?'已显示当前可用的概率预测。':'走势分析已更新，概率尚未验证。'}${updated?' 更新于 '+esc(displayTime(updated)):''}</p>
      ${stale?'<p class="fc-notice">以下为上次结构分析，行情可能已变化。点击“分析”请求更新。</p>':''}${(summary.warnings||[]).map(w=>`<p class="hint">${esc(explain(typeof w==='object'?w.reason:w))}</p>`).join('')}`;
    else analysis=`<div class="empty">${request?.status==='queued'||request?.status==='requesting'?'正在等待这只股票的云端分析。':'这只股票尚无缓存分析，点击“分析”发起请求。'}<br><span class="hint">将保留当前选择，不会切换到其他股票。</span></div>`;
    const toolbar=`<label class="fc-timeframe-label" for="fc-tf">级别</label><select class="in" id="fc-tf">${Object.entries(levels).map(([key,label])=>`<option value="${key}" ${key===currentTF?'selected':''}>${label}</option>`).join('')}</select>`;
    const visible=Object.keys(horizons).map(h=>latest.get(h)).filter(r=>r&&r.validated&&finite(r.probability));
    const predictions=visible.length?table(visible,['预测期限','上涨概率','收益区间'],r=>`<tr><td>${esc(horizons[r.horizon])}</td><td>${pct(r.probability)}</td><td>${pct(r.quantiles?.p10)} ～ ${pct(r.quantiles?.p90)}</td></tr>`,'fc-forecast-table'):'<p class="hint">概率尚未验证，暂不显示上涨概率和收益区间。当前可先查看真实行情形成的结构与走势解读。</p>';
    return `<div class="fc-shell">${header}${notice}${panel(title,analysis)}${panel('缠论结构图',`<div class="fc-chart">${chart(selected)}</div>`,toolbar)}${panel('长短周期预测',predictions)}${diagnostics(f,options)}</div>`;
  }
  function paint(options=lastOptions){const root=document.getElementById('p-forecast');if(root){root.innerHTML=draw(lastData,options);bind(root,options);}}
  function message(text){const el=document.getElementById('fc-search-message');if(el)el.textContent=text;}
  function drawSuggestions(){
    const box=document.getElementById('fc-suggestions'),input=document.getElementById('fc-query');if(!box||!input)return;
    box.innerHTML=suggestions.map((r,i)=>`<div id="fc-suggestion-${i}" role="option" aria-selected="${i===suggestionIndex}" class="${i===suggestionIndex?'on':''}" data-fc-code="${esc(r.code)}"><span>${esc(r.name||'名称待同步')}</span><span>${esc(short(r.code))}</span></div>`).join('');
    box.style.display=suggestions.length?'block':'none';input.setAttribute('aria-expanded',String(!!suggestions.length));
    if(suggestions.length)input.setAttribute('aria-activedescendant','fc-suggestion-'+suggestionIndex);else input.removeAttribute('aria-activedescendant');
  }
  async function search(query,options){
    const serial=++searchSequence;let found=[];
    try{const result=await options.api('search?q='+encodeURIComponent(query));found=Array.isArray(result)?[...result]:[...(result.items||result.results||[])];}catch{}
    if(serial!==searchSequence)return [];
    for(const [code,name] of names)if(name.includes(query)||short(code).includes(query)||code.includes(query.toLowerCase()))found.push({code,name});
    const seen=new Set();suggestions=found.map(r=>({code:normalize(r.code),name:r.name||''})).filter(r=>r.code&&!seen.has(r.code)&&seen.add(r.code)).slice(0,12);
    for(const r of suggestions)if(r.name)names.set(r.code,r.name);
    suggestionIndex=0;drawSuggestions();return suggestions;
  }
  async function choose(symbol,options){
    const code=normalize(symbol?.code);if(!code)return;
    ++searchSequence;clearTimeout(searchTimer);suggestions=[];currentCode=code;if(symbol.name)names.set(code,symbol.name);searchText=symbolText(code);
    const serial=++requestSequence;requests.set(code,{status:'requesting',serial});paint(options);
    if(lastData.locked){requests.delete(code);if(options.unlock)await options.unlock();return;}
    const visible=()=>currentCode===code&&location.hash.split('/')[0]==='#forecast';
    const onProgress=progress=>{
      if(requests.get(code)?.serial!==serial)return;
      const message=typeof progress==='string'?explain(progress):explain(progress?.message||'已提交云端，正在等待分析结果。');
      requests.set(code,{status:'queued',serial,message});if(visible())paint(options);
    };
    try{
      if(options.requestForecast){
        const result=await options.requestForecast(code,{onProgress});
        if(requests.get(code)?.serial!==serial)return;
        const state=result?.status||result?.state||'queued';
        if(state==='error'||result?.ok===false)throw new Error(result?.message||result?.error||'云端暂未接收请求，请稍后重试。');
        requests.set(code,{status:state==='ready'?'ready':'queued',serial,message:'已提交云端，正在等待分析结果。'});
        if(result?.data&&visible()){lastData=result.data;remember(lastData);paint(options);}
      }else requests.set(code,{status:'error',serial,message:'此连接暂不能请求新的云端分析，请刷新后重试。'});
      if(visible())await render(options,{keepSelection:true});
    }catch(error){
      if(requests.get(code)?.serial!==serial)return;
      requests.set(code,{status:'error',serial,message:explain(error.message)});if(visible())paint(options);
    }
  }
  async function analyze(options){
    const input=document.getElementById('fc-query'),value=(input?.value||'').trim();searchText=value;
    if(value===symbolText(currentCode)&&currentCode)return choose({code:currentCode,name:names.get(currentCode)},options);
    const code=normalize(value);if(code)return choose({code,name:names.get(code)||''},options);
    if(!value){message('请输入股票名称或六位代码。');input?.focus();return;}
    const found=await search(value,options);if(found.length===1)return choose(found[0],options);
    if(found.length){message('请选择下方匹配的股票。');input?.focus();return;}
    message('未找到匹配股票，请检查名称或六位代码。');
  }
  function bind(root,options){
    const input=root.querySelector('#fc-query'),box=root.querySelector('#fc-suggestions');
    root.querySelector('#fc-refresh').onclick=()=>render(options,{keepSelection:true});root.querySelector('#fc-analyze').onclick=()=>analyze(options);
    if(input){
      input.oncompositionstart=()=>{composing=true;};input.oncompositionend=()=>{composing=false;input.oninput();};
      input.oninput=()=>{searchText=input.value;clearTimeout(searchTimer);++searchSequence;suggestions=[];drawSuggestions();if(composing)return;const value=input.value.trim();if(!value)return;searchTimer=setTimeout(()=>search(value,options),150);};
      input.onkeydown=event=>{
        if(event.isComposing||composing)return;
        if(event.key==='ArrowDown'||event.key==='ArrowUp'){suggestionIndex=Math.max(0,Math.min(suggestions.length-1,suggestionIndex+(event.key==='ArrowDown'?1:-1)));drawSuggestions();event.preventDefault();}
        else if(event.key==='Enter'){event.preventDefault();if(suggestions[suggestionIndex])choose(suggestions[suggestionIndex],options);else analyze(options);}
        else if(event.key==='Escape'){suggestions=[];drawSuggestions();event.preventDefault();}
      };
      input.onblur=()=>setTimeout(()=>{if(box)box.style.display='none';},180);
    }
    if(box){box.onmousedown=e=>e.preventDefault();box.onclick=e=>{const target=e.target.closest('[data-fc-code]');if(target)choose({code:target.dataset.fcCode,name:names.get(target.dataset.fcCode)||''},options);};}
    const tf=root.querySelector('#fc-tf');if(tf)tf.onchange=e=>{currentTF=e.target.value;paint(options);};
    const unlock=root.querySelector('#fc-unlock');if(unlock)unlock.onclick=options.unlock;
    root.querySelectorAll('[data-fc-action]').forEach(button=>button.onclick=async()=>{try{await options.api('forecast/control',{action:button.dataset.fcAction});await render(options,{keepSelection:true});}catch(error){message(explain(error.message));}});
  }
  function schedule(options){
    clearTimeout(timer);if(location.hash.split('/')[0]!=='#forecast')return;
    timer=setTimeout(()=>{if(document.activeElement?.id==='fc-query'){schedule(options);return;}render(options,{keepSelection:true});},15000);
  }
  async function render(options,flags={}){
    clearTimeout(timer);lastOptions=options;const root=document.getElementById('p-forecast');if(!root)return;
    const route=normalize(options.code);if(!flags.keepSelection&&route&&route!==lastRouteCode){currentCode=route;searchText=symbolText(route);lastRouteCode=route;}
    const serial=++loadSequence,requestedCode=currentCode;
    try{
      const result=await options.api('forecast'+(requestedCode?'?code='+encodeURIComponent(requestedCode):''));
      if(serial!==loadSequence||requestedCode!==currentCode)return;
      lastData=result||{};remember(lastData);const state=requests.get(currentCode);
      if(state?.status==='queued'&&(lastData.request?.status==='ready'||lastData.request_status==='ready'))requests.set(currentCode,{...state,status:'ready'});
      paint(options);
    }catch(error){if(serial!==loadSequence)return;if(currentCode)requests.set(currentCode,{status:'error',message:'暂时无法读取分析结果，请点击刷新重试。'});paint(options);message('暂时无法读取分析结果，请稍后刷新。');}
    schedule(options);
  }
  window.ForecastUI={render,chart,draw,normalize};
})();
