import { forecastFetch } from './forecast-requests.js';
import { fullResearchFetch } from './full-research.js';
// A bounded, encrypted live channel. No personal plaintext is accepted in public data.
const HEADERS = { 'cache-control': 'no-store', 'access-control-allow-origin': '*', 'x-content-type-options': 'nosniff' };
const reply = (body, status = 200) => Response.json(body, { status, headers: HEADERS });
const hex = bytes => Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
async function authorized(req, env) {
  const a = new TextEncoder().encode(env.LIVE_KEY || '');
  const b = new TextEncoder().encode(req.headers.get('x-aq5-live-key') || '');
  if (!a.length || a.length !== b.length) return false;
  let diff = 0; for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}
function publicOnly(p) {
  if (!p || typeof p !== 'object' || Array.isArray(p)) throw new Error('public data required');
  const allowed = new Set(['time', 'phase', 'version', 'pick_quotes', 'indices', 'quote_time']);
  if (Object.keys(p).some(k => !allowed.has(k))) throw new Error('unapproved public field');
  const quote = q => {
    if (!q || typeof q !== 'object' || Object.keys(q).some(k => !['code', 'name', 'price', 'pct', 'time', 'chg', 'amount'].includes(k))) throw new Error('invalid public quote');
    for (const key of ['price', 'pct', 'chg', 'amount']) if (q[key] != null && !Number.isFinite(q[key])) throw new Error('invalid numeric quote');
  };
  for (const q of Object.values(p.pick_quotes || {})) quote(q);
  for (const q of p.indices || []) quote(q);
  if (Object.keys(p.pick_quotes || {}).length > 40 || (p.indices || []).length > 12) throw new Error('quote limit');
  return p;
}
export async function liveRoute(req, env) {
  const url = new URL(req.url);
  if (!['/__aq5/live', '/__aq5/live/private.enc'].includes(url.pathname)) return null;
  if (req.method === 'OPTIONS') return new Response(null, { status: 204, headers: { ...HEADERS, 'access-control-allow-methods': 'GET, OPTIONS' } });
  if (!env.LIVE) return reply({ available: false, reason: 'live channel not configured' }, 503);
  if (!['GET', 'POST', 'DELETE'].includes(req.method)) return reply({ error: 'method' }, 405);
  if (req.method !== 'GET' && !await authorized(req, env)) return reply({ error: 'forbidden' }, 403);
  const channel = url.searchParams.get('channel') || 'main';
  if (!['main', 'diagnostic'].includes(channel)) return reply({ error: 'channel' }, 400);
  const stub = env.LIVE.get(env.LIVE.idFromName(channel));
  return stub.fetch(req);
}
export class LiveSnapshot {
  constructor(ctx, env) { this.ctx = ctx; this.env = env; }
  async fetch(req) {
    const url = new URL(req.url);
    if (url.pathname.startsWith('/__aq5/forecast/')) return forecastFetch(req, this.ctx, this.env);
    if (url.pathname.startsWith('/__aq5/research/')) return fullResearchFetch(req, this.ctx, this.env);
    if (req.method === 'DELETE') { await this.ctx.storage.delete('snapshot'); return reply({ ok: true }); }
    if (req.method === 'POST') {
      if (url.pathname !== '/__aq5/live') return reply({ error: 'path' }, 404);
      const text = await req.text();
      if (new TextEncoder().encode(text).length > 512 * 1024) return reply({ error: 'payload too large' }, 413);
      let body, bytes;
      try {
        body = JSON.parse(text);
        if (!Number.isInteger(body.sequence) || body.sequence < 0 || typeof body.stream_id !== 'string' || body.stream_id.length > 80) throw new Error('sequence');
        if (!['pc', 'cloud', 'diagnostic'].includes(body.role) || (body.role === 'diagnostic' && url.searchParams.get('channel') !== 'diagnostic')) throw new Error('role');
        if (!Number.isFinite(body.sent_at) || Math.abs(Date.now() - body.sent_at) > 120000) throw new Error('publisher clock');
        publicOnly(body.public);
        bytes = Uint8Array.from(atob(body.private_b64), c => c.charCodeAt(0));
        if (bytes.length < 70 || new TextDecoder().decode(bytes.slice(0, 5)) !== 'AQ5w1') throw new Error('encrypted package required');
        const digest = hex(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)));
        if (digest !== body.private_pack_sha256) throw new Error('package digest');
      } catch { return reply({ error: 'invalid encrypted snapshot' }, 400); }
      const received = Date.now();
      let accepted = false;
      await this.ctx.storage.transaction(async tx => {
        const old = await tx.get('snapshot');
        if (old && (body.sent_at < old.sent_at || (body.stream_id === old.stream_id && body.sequence <= old.sequence))) return;
        await tx.put('snapshot', { ...body, received_at: received }); accepted = true;
      });
      return reply({ ok: accepted, received_at: received }, accepted ? 200 : 409);
    }
    const data = await this.ctx.storage.get('snapshot');
    const age = data ? Date.now() - data.received_at : null;
    if (!data || age > 45000) return reply({ available: false, age_ms: age, reason: data ? 'publisher heartbeat expired' : 'no live publisher' });
    if (url.pathname.endsWith('/private.enc')) {
      if (url.searchParams.get('v') !== data.private_pack_sha256) return reply({ error: 'snapshot changed' }, 409);
      return new Response(Uint8Array.from(atob(data.private_b64), c => c.charCodeAt(0)), { headers: { ...HEADERS, 'content-type': 'application/octet-stream' } });
    }
    return reply({ available: true, schema: 1, stream_id: data.stream_id, sequence: data.sequence, role: data.role,
      sent_at: data.sent_at, received_at: data.received_at, age_ms: age, public: data.public,
      private_pack_sha256: data.private_pack_sha256 });
  }
}
