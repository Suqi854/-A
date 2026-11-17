import assert from 'node:assert/strict';
import {test} from 'node:test';
import {researchUiRoute,RESEARCH_UI_COMMIT} from '../cloudflare-relay/research-ui.js';

test('one release serves the fixed address shell and only the frontend assets',async t=>{
 const restore=globalThis.fetch,calls=[],commit='a'.repeat(40);
 t.after(()=>{globalThis.fetch=restore;});
 globalThis.fetch=async(url,options)=>{
   calls.push([url,options.method]);
   return new Response(url.endsWith('/index.html')?'<script src="js/charts.js"></script>':'release asset');
 };
 const env={AQ5_UI_COMMIT:commit};
 const read=(path,method='GET')=>researchUiRoute(new Request('https://relay.invalid'+path,{method}),env);
 const index=await read('/');
 assert.equal(index.headers.get('x-aq5-release'),commit);
 assert.match(await index.text(),/js\/static\.js.*\n.*js\/charts\.js/);
 assert.match(await (await read('/js/static.js')).text(),/AQ_STATIC = true/);
 for(const path of ['/js/app.js','/js/forecast.js','/css/app.css']){
   const response=await read(path+'?v=older');
   assert.equal(response.headers.get('x-aq5-release'),commit);
   assert.equal(await response.text(),'release asset');
 }
 assert.equal(await (await read('/index.html','HEAD')).text(),'');
 assert.equal(await read('/data/latest.json'),null);
 assert.equal(await read('/__aq5/forecast/request'),null);
 assert.equal(await read('/api/overview'),null);
 assert.equal(await read('/','POST'),null);
 assert.equal(calls.length,5);
 assert.ok(calls.every(([url])=>url.startsWith('https://raw.githubusercontent.com/Suqi854/-A/'+commit+'/engine/web/')));
});
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
