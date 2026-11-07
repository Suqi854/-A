const assert = require('node:assert/strict');
const { createHash, createHmac, pbkdf2Sync, randomBytes, webcrypto } = require('node:crypto');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const { gzipSync } = require('node:zlib');

const source = readFileSync(path.join(__dirname, '../web/js/app.js'), 'utf8');
const functions = ['sj', 'staticApi', 'updateMon', 'poll', 'privateStatus', 'refreshPrivate', 'unlockPrivate', 'autoUnlock', 'loadResearchPreview', 'researchRun'];
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

function harness({ snapshot = overview(), encrypted, isStatic = true, relay, preview } = {}) {
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
    api(route) { requests.push(route); return context.staticApi(route); },
    async fetch(url) {
      requests.push(url);
      if (url.startsWith('data/latest.json')) return Response.json(snapshot);
      if (url.startsWith('data/private.enc')) return new Response(encrypted);
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

