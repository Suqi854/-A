const assert = require('node:assert/strict');
const { createHash, createHmac, pbkdf2Sync, randomBytes, webcrypto } = require('node:crypto');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const { gzipSync } = require('node:zlib');

const source = readFileSync(path.join(__dirname, '../web/js/app.js'), 'utf8');
const functions = ['sj', 'staticApi', 'updateMon', 'poll', 'privateStatus', 'refreshPrivate', 'unlockPrivate', 'autoUnlock', 'loadResearchPreview', 'researchRun', 'staticQuotes', 'quoteValues'];
const app = functions.map(name => source.match(new RegExp(`^  (?:async )?function ${name}\\(.*?^  }`, 'ms'))?.[0] || '').join('\n');
const live = source.slice(source.indexOf('  const LIVE_AGE ='), source.indexOf('  // ================================================================== F1 监控'));
const formatters = source.slice(source.indexOf('  const esc ='), source.indexOf('  const S ='));
const oldTime = '2026-10-28 10:01:00', newTime = '2026-10-28 10:02:00';
const account = (name, equity = 100) => ({ name, equity, positions: [] });
const pack = (time, equity) => ({ time, watchlist: [], me: account('我的模拟盘'),
  plan: { exists: true, account: account('计划模拟盘', equity) } });
const overview = (time = oldTime) => ({ time, watchlist: [], indices: [], overseas: [], top: [], inflow: [], events: [],
  accounts: { ai: account('AI自动盘') }, last_run: { items: [{ code: 'sh600000', price: 10, stop: 9, target1: 11 }] },
  pick_quotes: { sh600000: { price: 10.5, pct: 0 } } });

function harness({ snapshot = overview(), encrypted, isStatic = true, relay, preview, quotes, analysis } = {}) {
  const nodes = new Map();
  const $ = selector => {
    if (!nodes.has(selector)) nodes.set(selector, { children: [], dataset: {}, querySelectorAll: () => [] });
    return nodes.get(selector);
  };
  const requests = [];
  let deriveCalls = 0, simRenders = 0, remembered, now = Date.parse('2026-10-28T10:02:10+08:00');
  class Clock extends Date { static now() { return now; } }
  const S = { page: 'sim', acct: 'plan' };
  const context = vm.createContext({ $, S, STATIC: isStatic, TextDecoder, TextEncoder, Uint8Array, DataView,
    Response, Blob, DecompressionStream, URL, URLSearchParams, AbortSignal, Date: Clock, document: { hidden: false }, window: { crypto: webcrypto },
    crypto: { subtle: {
      deriveBits(...args) { deriveCalls++; return webcrypto.subtle.deriveBits(...args); },
      importKey: webcrypto.subtle.importKey.bind(webcrypto.subtle),
      sign: webcrypto.subtle.sign.bind(webcrypto.subtle),
      digest: webcrypto.subtle.digest.bind(webcrypto.subtle),
    } },
    normCode: code => code,
    nameCell: (_, code) => code,
    lsGet: () => remembered,
    lsSet: (_, value) => { remembered = value; },
    panel(title, body) { return title + body; },
    header() {}, async renderSim() { simRenders++; },
    api(route) { requests.push(route); return !isStatic && route.startsWith('quotes?') ? Promise.resolve({}) : context.staticApi(route); },
    async fetch(url) {
      requests.push(url);
      if (url.startsWith('data/latest.json')) return Response.json(snapshot);
      if (url.startsWith('data/private.enc')) return new Response(encrypted);
      if (url.startsWith('data/quotes.json') && quotes) return Response.json(quotes);
      if (url.startsWith('data/analysis/') && analysis) return Response.json(analysis);
      if (url.startsWith('data/research.json')) return Response.json(preview);
      if (relay && url.startsWith(relayURL)) return relay(url);
      throw new Error('Unexpected request');
    },
  });
  vm.runInContext(formatters + '\n' + app + '\n' + live, context);
  return { S, context, $, requests, advance: ms => { now += ms; }, derives: () => deriveCalls, renders: () => simRenders };
}

test('locked overview omits plan, unlocked overview and monitor show its private account', async () => {
  const h = harness();
  assert.deepEqual(Object.keys((await h.context.staticApi('overview')).accounts), ['ai']);
  assert.equal(h.requests.some(url => url.startsWith('data/private.enc')), false);
  h.S.priv = pack(oldTime, 100);
  const ov = await h.context.staticApi('overview');
  assert.equal(ov.accounts.plan.equity, 100);
  h.context.updateMon(ov);
  assert.match(h.$('#monAcct').innerHTML, /计划模拟盘/);
  assert.match(h.$('#monAcct').innerHTML, /10:01/);
  h.S.priv = { ...h.S.priv, plan: null };
  assert.equal((await h.context.staticApi('overview')).accounts.plan, undefined);
});

test('published trading-session snapshot does not claim the market is currently trading', () => {
  const body = source.match(/^  function header\(.*?^  }/ms)[0];
  for (const isStatic of [true, false]) {
    const h = harness({isStatic});
    h.context.PHASE = {trading:['交易中','up']};
    h.context.updateBell = () => {};
    h.S.seen = new Set();
    vm.runInContext(body,h.context);
    h.context.header({...overview(),phase:'trading'});
    assert.match(h.$('#phase').innerHTML,isStatic ? /行情快照/ : /交易中/);
    assert.equal(h.$('#phase').className.includes(' live'),!isStatic);
    if(isStatic) assert.equal(h.$('#mockTag').textContent,'云端版');
  }
});

test('static quotes are visible immediately, 0% survives, and old snapshots show placeholders', () => {
  const h = harness();
  const ov = overview();
  h.context.updateMon(ov);
  assert.match(h.$('#monPick').innerHTML, />10\.50<.*>0\.00%<.*>\+5\.00%</s);
  assert.deepEqual(h.requests, []);
  delete ov.pick_quotes;
  h.S.pq = { sh600000: { price: 99, pct: 10 } };
  h.context.updateMon(ov);
  assert.match(h.$('#monPick').innerHTML, /class="flat">--<.*<b>--<\/b>/s);
  assert.doesNotMatch(h.$('#monPick').innerHTML, /99\.00/);
});

test('live monitor retains its quote polling', () => {
  const h = harness({ isStatic: false });
  h.S.pq = { sh600000: { price: 10.2, pct: 0 } };
  h.context.updateMon(overview());
  assert.match(h.$('#monPick').innerHTML, />10\.20</);
  assert.deepEqual(h.requests, ['quotes?codes=sh600000']);
});

const password = 'synthetic test password';
function encryptedPack(snapshot) {
  const salt = randomBytes(16), nonce = randomBytes(16);
  const keys = pbkdf2Sync(password, salt, 200000, 64, 'sha256');
  const plain = gzipSync(JSON.stringify(snapshot)), encrypted = Buffer.alloc(plain.length);
  for (let offset = 0; offset < plain.length; offset += 32) {
    const block = Buffer.alloc(24);
    nonce.copy(block);
    block.writeBigUInt64BE(BigInt(offset / 32), 16);
    const stream = createHmac('sha256', keys.subarray(0, 32)).update(block).digest();
    for (let i = 0; i < 32 && offset + i < plain.length; i++) encrypted[offset + i] = plain[offset + i] ^ stream[i];
  }
  const head = Buffer.concat([Buffer.from('AQ5w1'), salt, nonce]);
  const tag = createHmac('sha256', keys.subarray(32)).update(head).update(encrypted).digest();
  return Buffer.concat([head, tag, encrypted]);
}

async function seed(h) {
  h.S.priv = pack(oldTime, 100);
  h.S.privateSession = { marker: oldTime, key: await webcrypto.subtle.importKey('raw',
    new TextEncoder().encode(password), 'PBKDF2', false, ['deriveBits']) };
}

test('new matching private publication refreshes once and redraws the open plan page', async () => {
  const encrypted = encryptedPack(pack(newTime, 120));
  const snapshot = { ...overview(newTime), private_pack_sha256: createHash('sha256').update(encrypted).digest('hex') };
  const h = harness({ snapshot, encrypted });
  await seed(h);
  await Promise.all([h.context.poll(), h.context.staticApi('overview')]);
  await h.context.poll();
  assert.equal(h.S.ov.accounts.plan.equity, 120);
  assert.equal((await h.context.staticApi('plan')).static_time, newTime);
  assert.equal(h.requests.filter(url => url.startsWith('data/private.enc')).length, 1);
  assert.equal(h.derives(), 1);
  assert.equal(h.renders(), 1);
});

test('mismatched publication keeps the previous private data and marks refresh failure', async () => {
  const snapshot = { ...overview(newTime), private_pack_sha256: '0'.repeat(64) };
  const h = harness({ snapshot, encrypted: encryptedPack(pack(newTime, 120)) });
  await seed(h);
  await h.context.poll();
  assert.equal(h.S.ov.accounts.plan.equity, 100);
  assert.equal(h.derives(), 0);
  h.context.updateMon(h.S.ov);
  assert.match(h.$('#monAcct').innerHTML, /10:01.*刷新失败/s);
});
test('legacy publication accepts a new private snapshot and rejects an older one', async () => {
  const snapshot = overview(newTime);
  const newer = harness({ snapshot, encrypted: encryptedPack(pack(newTime, 120)) });
  await seed(newer);
  assert.equal((await newer.context.staticApi('overview')).accounts.plan.equity, 120);
  const older = harness({ snapshot, encrypted: encryptedPack(pack('2026-10-28 10:00:00', 80)) });
  await seed(older);
  const stale = await older.context.staticApi('overview');
  assert.equal(stale.accounts.plan.equity, 100);
  assert.equal(stale.private_refresh_failed, true);
});

test('automatic unlock retries transient failures and clears a confirmed invalid password', async () => {
  const h = harness();
  h.context.lsSet('aq5_pw', password);
  let attempts = 0;
  h.context.doUnlock = async () => {
    attempts++;
    if (attempts === 1) throw new Error('Temporary publication mismatch');
    throw Object.assign(new Error('Wrong password'), { code: 'PRIVATE_PASSWORD' });
  };
  await h.context.poll();
  assert.equal(h.context.lsGet('aq5_pw'), password);
  await h.context.poll();
  assert.equal(h.context.lsGet('aq5_pw'), null);
  await h.context.poll();
  assert.equal(attempts, 2);
});

const relayURL = 'https://relay.example';
const relaySnapshot = () => ({ ...overview(), live_relay: relayURL });
const relayPacket = (overrides = {}) => ({ available: true, schema: 1, role: 'cloud', stream_id: 'stream-a', sequence: 1,
  age_ms: 1000, public: { time: newTime, phase: 'trading', quote_time: '20261028100209', pick_quotes: { sh600000: { price: 11 } } },
  private_pack_sha256: 'a'.repeat(64), ...overrides });

test('locked relay poll is single flight and overlays only public fields', async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const h = harness({ relay: () => pending });
  h.S.ov = relaySnapshot();
  const first = h.context.pollLive();
  await h.context.pollLive();
  assert.deepEqual(h.requests, [relayURL + '/__aq5/live']);
  release(Response.json(relayPacket()));
  await first;
  const shown = h.context.displayOverview(h.S.ov);
  assert.equal(shown.pick_quotes.sh600000.price, 11);
  assert.equal(shown.last_run, h.S.ov.last_run);
  assert.equal(shown.accounts.plan, undefined);
  assert.equal(h.S.livePrivate, undefined);
  assert.deepEqual(h.requests, [relayURL + '/__aq5/live']);
});

test('expired relay falls back to published snapshot and rejects old source quote', async () => {
  let packet = relayPacket();
  const h = harness({ relay: () => Response.json(packet) });
  h.S.ov = relaySnapshot();
  await h.context.pollLive();
  assert.equal(h.context.displayOverview(h.S.ov).pick_quotes.sh600000.price, 11);
  h.advance(45001);
  assert.equal(h.context.displayOverview(h.S.ov), h.S.ov);
  packet = relayPacket({ sequence: 2 });  // Transport is fresh, provider time remains stale.
  await h.context.pollLive();
  assert.equal(h.context.displayOverview(h.S.ov), h.S.ov);
  packet = relayPacket({ available: false });
  await h.context.pollLive();
  assert.equal(h.context.activeRelay(), null);
});

test('relay digest mismatch retains verified mini-pack, then unavailability restores static private data', async () => {
  const encrypted = encryptedPack({ ...pack(newTime, 120), intraday: { sh600000: { balanced: { phase: 'watch' } } } });
  const hash = createHash('sha256').update(encrypted).digest('hex');
  let packet = relayPacket({ private_pack_sha256: hash });
  const h = harness({ relay: url => url.includes('/private.enc') ? new Response(encrypted) : Response.json(packet) });
  h.S.ov = relaySnapshot();
  await seed(h);
  h.S.priv.profile = { nickname: 'kept locally' };
  await h.context.pollLive();
  assert.equal(h.context.privateForDisplay().plan.account.equity, 120);
  assert.equal(h.context.privateForDisplay().profile.nickname, 'kept locally');
  assert.equal(h.context.privateForDisplay().intraday.sh600000.balanced.phase, 'watch');
  packet = relayPacket({ sequence: 2, private_pack_sha256: 'b'.repeat(64) });
  await h.context.pollLive();
  assert.equal(h.context.privateForDisplay().plan.account.equity, 120);
  assert.equal(h.context.displayOverview(h.S.ov).private_refresh_failed, true);
  assert.equal(h.derives(), 1);
  packet = relayPacket({ available: false });
  await h.context.pollLive();
  assert.equal(h.context.privateForDisplay().plan.account.equity, 100);
});

test('an in-flight encrypted response cannot unlock a page after the session is cleared', async () => {
  const encrypted = encryptedPack(pack(newTime, 120));
  const hash = createHash('sha256').update(encrypted).digest('hex');
  let release, started;
  const privateStarted = new Promise(resolve => { started = resolve; });
  const privateResponse = new Promise(resolve => { release = resolve; });
  const h = harness({ relay: url => {
    if (url.includes('/private.enc')) { started(); return privateResponse; }
    return Response.json(relayPacket({ private_pack_sha256: hash }));
  } });
  h.S.ov = relaySnapshot();
  await seed(h);
  const polling = h.context.pollLive();
  await privateStarted;
  h.S.priv = null;
  h.S.privateSession = null;
  release(new Response(encrypted));
  await polling;
  assert.equal(h.context.privateForDisplay(), null);
  assert.equal(h.S.livePrivate, undefined);
});
test('research preview displays missing quote time without creating a new run', async () => {
  const h = harness({ preview: { kind: 'research_preview', not_new_selection: true, quote_time_status: 'unknown',
    generated_at: newTime, short_term: {}, value_quality: {} } });
  h.S.page = 'pick';
  await h.context.loadResearchPreview(true);
  assert.match(h.$('#researchPreview').innerHTML, /研究预览.*行情时点未提供.*等待下一交易日真实选股样本/s);
  assert.equal(h.requests.filter(url => url.startsWith('data/research.json')).length, 1);
  assert.equal(h.requests.some(url => url.startsWith('data/runs.json')), false);
  await h.context.loadResearchPreview(false);
  assert.equal(h.$('#researchPreview').hidden, true);
});



test('static pick quotes use published prices, preserve zero change, and filter requested codes', async () => {
  const h = harness();
  const qs = await h.context.staticApi('quotes?codes=sh600000,sz000001');
  assert.equal(qs.sh600000.price, 10.5);
  assert.equal(qs.sh600000.pct, 0);
  assert.equal(qs.sz000001, undefined);
});

test('public quote snapshot supports historical picks and never replaces a newer quote with an older one', async () => {
  const snapshot = overview();
  snapshot.pick_quotes.sh600000.time = '20261028100200';
  const h = harness({ snapshot, quotes: { quotes: {
    sh600000: { price: 9, time: '20261028100100' },
    sz000001: { price: 11, pct: -1, time: '20261028100100' },
    sz000002: { price: 0 },
  } } });
  const qs = await h.context.staticApi('quotes?codes=sh600000,sz000001,sz000002');
  assert.equal(qs.sh600000.price, 10.5);
  assert.equal(qs.sz000001.price, 11);
  assert.equal(qs.sz000002, undefined);
  await h.context.staticApi('quotes?codes=sz000001');
  assert.equal(h.requests.filter(x => x.startsWith('data/quotes.json')).length, 1);
});

test('relative return uses only an actual quote and a positive selection price', () => {
  const h = harness();
  assert.equal(h.context.quoteValues({ price: 10.5, pct: 0, time: '20261028100200' }, 10).relative.toFixed(1), '5.0');
  assert.equal(h.context.quoteValues({ price: 10.5 }, 0).relative, null);
  assert.equal(h.context.quoteValues(null, 10).price, null);
  assert.equal(h.context.quoteValues({ price: -1 }, 10).relative, null);
});

test('published analysis and daily candles work with no direct browser market provider', async () => {
  const h = harness({ analysis: { code: 'sh600000', score: 55, kline: [{ date: '2026-10-28', close: 10.5 }] } });
  assert.equal((await h.context.staticApi('analyze?code=sh600000')).score, 55);
  assert.equal((await h.context.staticApi('kline?code=sh600000')).rows.length, 1);
});


test('undated, future, or pre-selection quotes cannot create relative returns', () => {
  const h = harness();
  assert.equal(h.context.quoteValues({ price: 10.5 }, 10, oldTime).relative, null);
  assert.equal(h.context.quoteValues({ price: 10.5, time: '20261028100200' }, 10, '').relative, null);
  assert.equal(h.context.quoteValues({ price: 10.5, time: '20261028095900' }, 10, oldTime).relative, null);
  assert.equal(h.context.quoteValues({ price: 10.5, time: '20261029100200' }, 10, oldTime).relative, null);
});

test('an incomplete private analysis cannot hide published daily candles', async () => {
  const h = harness({ analysis: { code: 'sh600000', score: 55, kline: [{ date: '2026-10-28', close: 10.5 }] } });
  h.S.priv = { analysis: { sh600000: { code: 'sh600000', kline: [] } } };
  assert.equal((await h.context.staticApi('analyze?code=sh600000')).score, 55);
  assert.equal((await h.context.staticApi('kline?code=sh600000')).rows.length, 1);
});

const functionSource = name => source.match(new RegExp(`^  (?:async )?function ${name}\\(.*?^  }`, 'ms'))[0];

test('cloud unlock prefers encrypted cloud data without probing or warning about the computer', async () => {
  const calls=[], notices=[], S={timers:{rc:123}};
  const context=vm.createContext({S,TextEncoder,DecompressionStream,crypto:webcrypto,window:{crypto:webcrypto},
    document:{body:{classList:{add(){}}}},$:()=>({}),syncLook(){},lsSet(){},
    unlockPrivate:async()=>({...pack(newTime,100),profile:{},remote:{url:'https://computer.invalid',token:'private'}}),
    pollLive:async()=>calls.push('cloud-live'),poll:async()=>calls.push('cloud-snapshot'),
    connectRemote:async()=>{throw new Error('must not probe computer');},
    toast:message=>notices.push(message),show(){},location:{hash:'#mon'},PAGES:['mon'],
    clearInterval:id=>calls.push('clear-old-reconnect'),setInterval:()=>{throw new Error('must not schedule reconnect');}});
  vm.runInContext(functionSource('doUnlock'),context);
  await context.doUnlock('synthetic-password',false,{time:newTime,private_pack_sha256:'synthetic-hash'});
  assert.equal(calls[0],'cloud-live');
  assert.equal(calls[1],'cloud-snapshot');
  assert.equal(S.timers.rc,undefined);
  assert.equal(notices.length,1);
  assert.match(notices[0],/云端个人数据已解锁/);
  assert.doesNotMatch(notices.join(' '),/连不上|连接电脑|重连|重试/);
});

test('computer connection is opt-in even when an encrypted snapshot contains remote credentials', async () => {
  let requests=0;
  const context=vm.createContext({S:{privateSession:{remote:{url:'https://computer.invalid',token:'private'}}},
    fetch:async()=>{requests++;return Response.json({authorized:false});},AbortController,setTimeout,clearTimeout,
    toast(){},Response});
  vm.runInContext(functionSource('connectRemote'),context);
  assert.equal(await context.connectRemote(),false);
  assert.equal(requests,0);
  assert.equal(await context.connectRemote(true),false);
  assert.ok(requests>0);
});

test('cloud refresh failure is described as cloud staleness, not a computer disconnection', async () => {
  const h=harness();h.context.fetch=async()=>{throw new Error('cloud unavailable');};
  await h.context.poll();
  assert.equal(h.$('#lastUpd').textContent,'云端数据暂未更新');
});

test('default monitoring page retains original panels and omits internal training diagnostics', () => {
  const main={innerHTML:''};
  const context=vm.createContext({S:{priv:{},timers:{}},STATIC:true,$:sel=>sel==='#p-mon'?main:null,$$:()=>[],
    panel:(title,body)=>title+body,drawLive(){},loadSectors(){},loadBrief(){},loadMarket(){},loadIdxSpark(){},
    clearInterval(){},setInterval(){return 1;}});
  vm.runInContext(functionSource('renderMon'),context);context.renderMon();
  assert.match(main.innerHTML,/主要指数/);assert.match(main.innerHTML,/模拟账户/);
  assert.doesNotMatch(main.innerHTML,/INTRADAY|T LAB|id="tLive"|id="tTraining"/);
  const learn=functionSource('renderLearn');
  assert.match(learn,/<details id="legacyDiagnostics" class="c12">/);
  assert.doesNotMatch(learn,/<details[^>]*legacyDiagnostics[^>]*\bopen\b/);
});

test('global stock selection stays on forecast but preserves existing analysis navigation elsewhere', () => {
  const calls=[],node={style:{},blur(){}};
  const context=vm.createContext({S:{page:'forecast'},$:()=>node,normCode:x=>x==='600000'?'sh600000':x,
    go:(...args)=>calls.push(args)});
  vm.runInContext(source.match(/^  function pickCode.*$/m)[0],context);
  context.pickCode('600000');assert.deepEqual(calls[0],['forecast','sh600000']);
  context.S.page='mon';context.pickCode('sz000001');assert.deepEqual(calls[1],['anly','sz000001']);
});

test('forecast route passes the selected stock and existing app services to its viewer', () => {
  let options;
  const noOp=()=>{},node={classList:{toggle(){}},dataset:{},id:'p-forecast'};
  const context=vm.createContext({S:{},STATIC:true,$:()=>({}),$$:()=>[node],normCode:x=>'sh'+x,
    window:{ForecastUI:{render:o=>{options=o;}}},api:noOp,showUnlock:noOp,go:noOp,requestForecast:noOp,
    renderMon:noOp,renderPick:noOp,renderAnly:noOp,renderSim:noOp,renderLearn:noOp,renderBt:noOp,renderLog:noOp,renderSet:noOp});
  vm.runInContext(functionSource('show'),context);context.show('forecast','600000');
  assert.equal(options.code,'sh600000');
  for(const key of ['api','search','selectCode','analyze','requestForecast','unlock'])assert.equal(typeof options[key],'function');
});

function forecastRequestHarness({fetchTask,decrypt,derive}={}) {
  let clock=1000;
  class Clock extends Date {static now(){return clock;}}
  const S={priv:{},privateSession:{key:{synthetic:true}},page:'forecast'};
  const requests=[],derivations=[],decryptions=[];
  const context=vm.createContext({S,Date:Clock,TextEncoder,Uint8Array,AbortSignal,Response,
    normCode:c=>/^[0-9]{6}$/.test(c)?'sh'+c:c,
    relayBase:()=> 'https://cloud.invalid',
    crypto:{subtle:{deriveBits:async(...args)=>{derivations.push(args);return derive?derive(...args):new Uint8Array(32).fill(42).buffer;}}},
    fetch:async(url,options)=>{requests.push({url,options});return fetchTask?fetchTask(url,options,S,requests.length):Response.json({id:'job-1',status:'ready',sha256:'a'.repeat(64)});},
    unlockPrivate:async(...args)=>{decryptions.push(args);return decrypt?decrypt(S,...args):{symbol:{code:'sh600000'},summary:'synthetic'};},
    setTimeout:(fn,ms)=>{clock+=ms;fn();},
  });
  vm.runInContext(functionSource('requestForecast'),context);
  return {context,S,requests,derivations,decryptions};
}

test('cloud forecast derives dedicated token and decrypts verified ready result without contacting the computer',async()=>{
  const progress=[];
  const h=forecastRequestHarness({fetchTask:(url,options,S,n)=>Response.json({id:'job-1',status:n===1?'queued':n===2?'processing':'ready',sha256:'a'.repeat(64)})});
  const result=await h.context.requestForecast('600000',{onProgress:(text,status)=>progress.push([text,status])});
  assert.equal(result.status,'ready');assert.equal(result.data.symbol.code,'sh600000');
  assert.equal(h.derivations.length,1);
  const [algorithm,key,bits]=h.derivations[0];
  assert.equal(new TextDecoder().decode(algorithm.salt),'aq5-forecast-request-v1');
  assert.equal(algorithm.iterations,200000);assert.equal(algorithm.hash,'SHA-256');assert.equal(bits,256);
  assert.equal(key,h.S.privateSession.key);
  assert.equal(h.requests.filter(r=>r.options.method==='POST').length,1);
  assert.equal(h.requests[0].options.headers['x-aq5-forecast-token'],'2a'.repeat(32));
  assert.equal(h.requests[0].options.body,'{"code":"sh600000"}');
  assert.ok(h.requests.every(r=>r.url.startsWith('https://cloud.invalid/__aq5/forecast/request')));
  assert.equal(h.decryptions[0][1],'a'.repeat(64));
  assert.equal(h.decryptions[0][2],'https://cloud.invalid/__aq5/forecast/result?id=job-1');
  assert.deepEqual(progress.map(x=>x[1]),['queued','processing']);
  assert.doesNotMatch(progress.map(x=>x[0]).join(' '),/电脑|本机/);
  assert.equal(h.S.forecastResults.sh600000.data,result.data);
});

test('cloud forecast stops polling after eight minutes and never repeats an uncertain POST',async()=>{
  const timeout=forecastRequestHarness({fetchTask:()=>Response.json({id:'job-1',status:'queued'})});
  await assert.rejects(timeout.context.requestForecast('sh600000'),e=>e.code==='FORECAST_TIMEOUT');
  assert.equal(timeout.requests.filter(r=>r.options.method==='POST').length,1);
  assert.ok(timeout.requests.length<=96);
  assert.equal(timeout.S.forecastResults,undefined);
  const uncertain=forecastRequestHarness({fetchTask:()=>{throw new Error('network');}});
  await assert.rejects(uncertain.context.requestForecast('sh600000'),/不会重复提交/);
  assert.equal(uncertain.requests.length,1);
});

test('late cloud forecast result cannot survive locking or a stock selection change',async()=>{
  for(const mutate of [S=>{S.privateSession=null;S.priv=null;},S=>{S.forecastSelection='sz000001';}]){
    const h=forecastRequestHarness({decrypt:S=>{mutate(S);return {symbol:{code:'sh600000'}};}});
    await assert.rejects(h.context.requestForecast('sh600000'),e=>e.code==='FORECAST_CANCELLED');
    assert.equal(h.S.forecastResults,undefined);
  }
});

test('cloud forecast requires unlock, validates response identity, and bounds network retries',async()=>{
  const locked=forecastRequestHarness();locked.S.priv=null;
  await assert.rejects(locked.context.requestForecast('sh600000'),/先解锁/);assert.equal(locked.requests.length,0);
  const wrong=forecastRequestHarness({decrypt:()=>({symbol:{code:'sz000001'}})});
  await assert.rejects(wrong.context.requestForecast('sh600000'),/所选股票不一致/);
  const missingIdentity=forecastRequestHarness({decrypt:()=>({summary:'no stock identity'})});
  await assert.rejects(missingIdentity.context.requestForecast('sh600000'),/所选股票不一致/);
  const retry=forecastRequestHarness({fetchTask:(url,opt,S,n)=>{if(n===1)return Response.json({id:'job-1',status:'queued'});throw new Error('network');}});
  await assert.rejects(retry.context.requestForecast('sh600000'),/云端状态暂时不可用/);
  assert.equal(retry.requests.length,4);
});

test('static forecast cache is scoped to requested code and the current unlocked session',async()=>{
  const h=harness();h.S.priv={...pack(oldTime,100),forecast:{marker:'original'}};h.S.privateSession={key:{}};
  const result={symbol:{code:'sh600000'},summary:'requested'};
  h.S.forecastResults={sh600000:{session:h.S.privateSession,data:result}};
  assert.equal(await h.context.staticApi('forecast?code=sh600000'),result);
  assert.equal((await h.context.staticApi('forecast?code=sz000001')).marker,'original');
  assert.equal((await h.context.staticApi('forecast')).marker,'original');
  h.S.privateSession={key:{}};
  assert.equal((await h.context.staticApi('forecast?code=sh600000')).marker,'original');
  h.S.priv=null;
  assert.equal((await h.context.staticApi('forecast?code=sh600000')).locked,true);
});
