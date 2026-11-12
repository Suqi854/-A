import assert from 'node:assert/strict';
import {test} from 'node:test';
import {researchUiRoute,RESEARCH_UI_COMMIT} from '../cloudflare-relay/research-ui.js';
test('only the reviewed app asset is pinned, preserving data and API routes',async t=>{
 const original=globalThis.fetch; t.after(()=>globalThis.fetch=original);let calls=0;
 globalThis.fetch=async(url,options)=>{calls++;assert.equal(url,'https://raw.githubusercontent.com/Suqi854/-A/'+RESEARCH_UI_COMMIT+'/engine/web/js/app.js');assert.equal(options.cf.cacheTtl,86400);return new Response('reviewed script');};
 for(const p of ['/data/latest.json','/api/analyze','/','/css/app.css','/__aq5/live'])assert.equal(await researchUiRoute(new Request('https://example.test'+p)),null);
 assert.equal(calls,0);const r=await researchUiRoute(new Request('https://example.test/js/app.js?release=old'));
 assert.equal(await r.text(),'reviewed script');assert.equal(r.headers.get('x-aq5-release'),RESEARCH_UI_COMMIT);assert.match(r.headers.get('content-type'),/javascript/);
 assert.equal(await researchUiRoute(new Request('https://example.test/js/app.js',{method:'POST'})),null);
});
test('source failure cannot silently serve an older bundled script',async t=>{
 const original=globalThis.fetch;t.after(()=>globalThis.fetch=original);globalThis.fetch=async()=>new Response('missing',{status:404});
 assert.equal((await researchUiRoute(new Request('https://example.test/js/app.js'))).status,503);
});
