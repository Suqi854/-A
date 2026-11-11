const assert=require('node:assert/strict');
const {test}=require('node:test');
const {readFileSync}=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=readFileSync(path.join(__dirname,'../web/js/forecast.js'),'utf8');
const css=readFileSync(path.join(__dirname,'../web/css/app.css'),'utf8');
const decode=x=>String(x||'').replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
function fixture(code='sh600000',name='浦发银行'){
  return {symbol:{code,name},symbols:[{code,name}],status:{phase:'waiting_signal',role:'cloud'},
    summary:{stance:'结构偏强',summary:'当前线段向上，中枢上沿附近仍需观察。',support:10.,resistance:10.8,updated:'2026-11-09T14:00:00+08:00',quality:{eligible:true},warnings:[]},
    structures:{[code+':day']:{code,timeframe:'day',asof:'2026-11-09T14:00:00+08:00',quality:{eligible:true},structures:{
      klines:[{date:'2026-11-06',low:9.8,high:10.2},{date:'2026-11-09',low:10.,high:10.8}],strokes:[],segments:[],centers:[],buy_sell_points:[]}}},
    predictions:[{code,horizon:'3d',variant:'fusion',probability:null,validated:false,quantiles:{p10:null,p90:null},asof:'2026-11-09T14:00:00+08:00'}],
    coverage:[{code,coverage:[{name:'财务',count:0,reason:'no_point_in_time_disclosure'}]}],ledger:{books:{}},model_status:{versions:[]}};
}
function harness({initial=fixture(),search=async()=>[],request,code}={}){
  const nodes=new Map(),timers=new Map(),requests=[],calls=[];let timerId=0;
  const doc={activeElement:null,getElementById:id=>id==='p-forecast'?root:nodes.get(id)||null};
  class Element{
    constructor(id){this.id=id;this.style={};this.attributes={};this.dataset={};this.value='';this._html='';this.textContent='';}
    set innerHTML(html){this._html=html;if(this.id==='p-forecast'){
      nodes.clear();for(const match of html.matchAll(/<([a-z]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)){
        const node=new Element(match[3]);const value=match[2].match(/\bvalue="([^"]*)"/);if(value)node.value=decode(value[1]);nodes.set(node.id,node);
      }
    }}
    get innerHTML(){return this._html;}
    querySelector(selector){return selector[0]==='#'?nodes.get(selector.slice(1))||null:null;}
    querySelectorAll(){return [];}
    setAttribute(k,v){this.attributes[k]=v;}
    removeAttribute(k){delete this.attributes[k];}
    focus(){doc.activeElement=this;}
  }
  const root=new Element('p-forecast'),snapshots=new Map([[initial.symbol?.code,initial]]);
  class Clock extends Date{static now(){return Date.parse('2026-11-09T14:00:10+08:00');}}
  const context=vm.createContext({window:{},document:doc,location:{hash:'#forecast'},Date:Clock,
    setTimeout(fn,delay){const id=++timerId;timers.set(id,{fn,delay});return id;},clearTimeout(id){timers.delete(id);},console});
  vm.runInContext(source,context);
  const options={code,staticMode:true,unlock:async()=>{},api:async route=>{
    calls.push(route);if(route.startsWith('search?'))return search(decodeURIComponent(route.slice(route.indexOf('=')+1)));
    const selected=new URLSearchParams(route.split('?')[1]).get('code');return snapshots.get(selected)||initial;
  },requestForecast:async(code,opts)=>{requests.push(code);return request?request(code,opts,snapshots):{status:'queued'};}};
  return {ui:context.window.ForecastUI,context,root,nodes,timers,options,calls,requests,snapshots,
    async render(){await context.window.ForecastUI.render(options);},
    async timersAt(delay){for(const [id,t] of [...timers])if(t.delay===delay){timers.delete(id);await t.fn();}await flush();},
    async input(value){const input=nodes.get('fc-query');input.value=value;input.oninput();await this.timersAt(150);},
    async key(key){nodes.get('fc-query').onkeydown({key,preventDefault(){}});await flush();},
    async clickCode(code){nodes.get('fc-suggestions').onclick({target:{closest:()=>({dataset:{fcCode:code}})}});await flush();}
  };
}

test('main view uses original panels and Chinese levels, diagnostics are collapsed',async()=>{
  const h=harness();await h.render();const html=h.root.innerHTML,main=html.split('<details')[0];
  assert.match(main,/class="panel"/);assert.match(main,/class="ph"/);assert.match(main,/class="in"/);
  assert.match(main,/股票名称或代码/);assert.match(main,/>分析<\/button>/);
  assert.match(main,/>日线<\/option>/);assert.match(main,/>60分钟<\/option>/);assert.match(main,/>15分钟<\/option>/);assert.match(main,/>5分钟<\/option>/);
  assert.doesNotMatch(main,/fc-cards|60个交易日|独立模拟记录|训练数据指纹/);
  assert.match(html,/<details class="fc-diagnostics">/);assert.doesNotMatch(html,/<details[^>]*\bopen/);
  assert.match(main,/走势分析已更新，概率尚未验证/);assert.doesNotMatch(main,/<table/);
  assert.doesNotMatch(main,/no_point_in_time_disclosure|waiting_signal/);
});
test('route selection without cached data never falls back to the first stock',async()=>{
  const h=harness({code:'sz000001'});await h.render();
  assert.match(h.root.innerHTML,/<small>000001<\/small>/);assert.match(h.root.innerHTML,/尚无缓存分析/);
  assert.doesNotMatch(h.root.innerHTML,/当前线段向上/);assert.equal(h.calls[0],'forecast?code=sz000001');
});
test('name search uses existing API and Enter selects name plus code',async()=>{
  const wait=deferred();const h=harness({search:async q=>q==='平安'?[{code:'sz000001',name:'平安银行'}]:[],request:async(code,{onProgress})=>{
    onProgress({message:'已进入云端队列，正在等待分析。'});return wait.promise;
  }});
  await h.render();await h.input('平安');
  assert.match(h.nodes.get('fc-suggestions').innerHTML,/平安银行/);assert.match(h.nodes.get('fc-suggestions').innerHTML,/000001/);
  await h.key('Enter');assert.deepEqual(h.requests,['sz000001']);
  assert.match(h.root.innerHTML,/平安银行 <small>000001/);assert.match(h.root.innerHTML,/已进入云端队列/);
  assert.doesNotMatch(h.root.innerHTML,/当前线段向上/);
});
test('click selection supports touch click and preserves unknown-stock queue state on refresh',async()=>{
  const h=harness({search:async()=>[{code:'sz000001',name:'平安银行'}]});await h.render();await h.input('平安');
  await h.clickCode('sz000001');await h.render();
  assert.match(h.root.innerHTML,/平安银行 <small>000001/);assert.match(h.root.innerHTML,/等待分析结果|等待这只股票/);
  assert.equal(h.calls[h.calls.length-1],'forecast?code=sz000001');
});
test('six digit analysis button requests the entered stock without a cached symbol',async()=>{
  const h=harness();await h.render();h.nodes.get('fc-query').value='600036';await h.nodes.get('fc-analyze').onclick();
  assert.deepEqual(h.requests,['sh600036']);assert.match(h.root.innerHTML,/<small>600036<\/small>/);
});
test('ambiguous name requires selecting a result, does not analyze an arbitrary first result',async()=>{
  const h=harness({search:async()=>[{code:'sh600000',name:'浦发银行'},{code:'sz000001',name:'平安银行'}]});await h.render();
  h.nodes.get('fc-query').value='银行';await h.nodes.get('fc-analyze').onclick();
  assert.equal(h.requests.length,0);assert.match(h.nodes.get('fc-search-message').textContent,/请选择/);
});
test('new stock selection is not overwritten by an older slow request',async()=>{
  const waits={sz000001:deferred(),sh600036:deferred()};const h=harness({request:async(code)=>waits[code].promise});await h.render();
  h.nodes.get('fc-query').value='000001';h.nodes.get('fc-analyze').onclick();await flush();
  h.nodes.get('fc-query').value='600036';h.nodes.get('fc-analyze').onclick();await flush();
  waits.sz000001.resolve({status:'ready',data:fixture('sz000001','平安银行')});await flush();
  assert.match(h.root.innerHTML,/<small>600036<\/small>/);assert.doesNotMatch(h.root.innerHTML,/平安银行 <small>/);
});
test('ready result shows selected stock interpretation and support/resistance',async()=>{
  const h=harness({request:async(code,opts,cache)=>{const data=fixture(code,'招商银行');cache.set(code,data);return {status:'ready',data};}});await h.render();
  h.nodes.get('fc-query').value='600036';await h.nodes.get('fc-analyze').onclick();
  assert.match(h.root.innerHTML,/招商银行 <small>600036/);assert.match(h.root.innerHTML,/结构支撑参考/);
  assert.match(h.root.innerHTML,/10\.00 元/);assert.match(h.root.innerHTML,/10\.80 元/);
});
test('request finishing after leaving forecast does not redraw it',async()=>{
  const wait=deferred();const h=harness({request:async()=>wait.promise});await h.render();
  h.nodes.get('fc-query').value='000001';h.nodes.get('fc-analyze').onclick();await flush();
  const before=h.root.innerHTML;h.context.location.hash='#mon';
  wait.resolve({status:'ready',data:fixture('sz000001','平安银行')});await flush();assert.equal(h.root.innerHTML,before);
});
test('stale analysis has a visible time warning without inventing probability',async()=>{
  const data=fixture();data.summary.updated='2026-11-06T15:00:00+08:00';const h=harness({initial:data});await h.render();
  assert.match(h.root.innerHTML,/上次结构分析，行情可能已变化/);assert.match(h.root.innerHTML,/概率尚未验证/);
});
test('safe empty results and request failures retain the search control and selection',async()=>{
  const h=harness({initial:{symbols:[],predictions:[]},request:async()=>{throw new Error('NetworkError');}});await h.render();
  assert.match(h.root.innerHTML,/输入股票名称或代码/);h.nodes.get('fc-query').value='600000';await h.nodes.get('fc-analyze').onclick();
  assert.match(h.root.innerHTML,/<small>600000<\/small>/);assert.ok(h.nodes.get('fc-analyze'));assert.doesNotMatch(h.root.innerHTML,/NetworkError/);
});
test('failed data validation cannot claim that current analysis is ready',async()=>{
  const data=fixture();data.structures={};data.summary={stance:'waiting',summary:'暂未取得完整行情。',quality:{eligible:false,reason:'missing_bars'},updated:'2026-11-09T06:00:00Z'};
  const h=harness({initial:data});await h.render();const html=h.root.innerHTML;
  assert.match(html,/当前行情未通过校验/);assert.match(html,/K线存在缺口/);assert.doesNotMatch(html,/走势分析已更新/);
  assert.match(html,/2026-11-09 14:00:00/);
});
test('mobile rules and chart inherit terminal theme variables',()=>{
  assert.match(css,/\.fc-search input\.in \{ width:100%; min-width:0;/);assert.match(css,/min-height:40px/);
  const h=harness();const svg=h.ui.chart(fixture().structures['sh600000:day']);assert.match(svg,/var\(--muted\)/);assert.match(svg,/viewBox/);
  assert.doesNotMatch(css.slice(css.indexOf('.fc-shell')),/fc-cards|border-radius:8px/);
});
