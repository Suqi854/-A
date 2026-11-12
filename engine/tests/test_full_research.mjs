import assert from 'node:assert/strict';
import {test} from 'node:test';
import {readFileSync} from 'node:fs';
import {fullResearchRoute} from '../cloudflare-relay/full-research.js';
import {LiveSnapshot} from '../cloudflare-relay/live.js';

function setup(t){
 const originalFetch=globalThis.fetch,originalNow=Date.now;
 let clock=Date.parse('2026-11-12T03:00:00Z'),active=[],dispatches=0,checks=0,dispatchStatus=204,uncertain=false;
 const mem=new Map([['snapshot',{sentinel:'do not touch live snapshot'}]]);let tail=Promise.resolve();
 const storage={get:async key=>structuredClone(mem.get(key)),put:async(key,value)=>mem.set(key,structuredClone(value)),
   delete:async key=>mem.delete(key),transaction:fn=>{const work=tail.then(()=>fn(storage));tail=work.catch(()=>{});return work;}};
 const env={FORECAST_KEY:'reader',LIVE_KEY:'publisher',GH_TOKEN:'token'};
 const object=new LiveSnapshot({storage},env);
 env.LIVE={idFromName:name=>{assert.equal(name,'full-research');return name;},get:()=>object};
 Date.now=()=>clock;
 globalThis.fetch=async(url,opt)=>{
   assert.ok(url.startsWith('https://api.github.com/repos/Suqi854/-A/actions/workflows/aq5-full-research.yml/'));
   assert.equal(opt.headers.Authorization,'Bearer token');
   if(url.includes('/runs?')){checks++;return Response.json({workflow_runs:active});}
   assert.ok(url.endsWith('/dispatches'));const b=JSON.parse(opt.body);assert.equal(b.ref,'main');assert.match(b.inputs.build_id,/^[a-f0-9]{32}$/);
   dispatches++;if(uncertain)throw Error('uncertain');return new Response(null,{status:dispatchStatus});
 };
 t.after(()=>{globalThis.fetch=originalFetch;Date.now=originalNow;});
 const request=(action,method='GET',body,role='reader')=>fullResearchRoute(new Request('https://relay.test/__aq5/research/'+action,{
   method,headers:role==='reader'?{'x-aq5-forecast-token':'reader'}:role==='publisher'?{'x-aq5-live-key':'publisher'}:{},
   ...(body===undefined?{}:{body:JSON.stringify(body)})}),env);
 return {request,mem,env,get dispatches(){return dispatches;},get checks(){return checks;},advance:ms=>clock+=ms,
   setActive:rows=>active=rows,setDispatchStatus:status=>dispatchStatus=status,setUncertain:()=>uncertain=true};
}

test('auth and namespace isolation preserve original live storage',async t=>{
 const h=setup(t);assert.equal(await fullResearchRoute(new Request('https://relay.test/elsewhere'),h.env),null);
 assert.equal((await h.request('start','OPTIONS')).status,204);
 assert.equal((await h.request('start','POST',{},'none')).status,403);
 assert.equal((await h.request('progress','POST',{})).status,403);
 assert.equal((await h.request('status')).status,200);
 assert.equal((await h.request('invalid')).status,404);
 assert.deepEqual(h.mem.get('snapshot'),{sentinel:'do not touch live snapshot'});
});
test('parallel starts dispatch exactly once and ignore client workflow/ref override',async t=>{
 const h=setup(t);const responses=await Promise.all([h.request('start','POST',{ref:'evil',workflow:'trade.yml'}),h.request('start','POST',{})]);
 const results=await Promise.all(responses.map(x=>x.json()));
 assert.equal(results.filter(x=>x.started).length,1);assert.equal(h.dispatches,1);
 const again=await(await h.request('start','POST',{})).json();assert.equal(again.reason,'already_running');assert.equal(h.checks,1);
});
test('publisher heartbeat is bound to build identity and bounded counts',async t=>{
 const h=setup(t),start=await(await h.request('start','POST',{})).json(),id=start.build.build_id;
 const update={build_id:id,phase:'running',stage:'emotion',completed:100,total:3195,coverage:{emotion:{completed:100,total:3195,unknown:3}}};
 assert.equal((await h.request('progress','POST',update)).status,403);
 assert.equal((await h.request('progress','POST',update,'publisher')).status,200);
 assert.equal((await h.request('progress','POST',{...update,build_id:'f'.repeat(32)},'publisher')).status,409);
 assert.equal((await h.request('progress','POST',{...update,completed:-1},'publisher')).status,400);
 assert.equal((await h.request('progress','POST',{...update,completed:4000},'publisher')).status,400);
 assert.equal((await h.request('progress','POST',{...update,completed:50},'publisher')).status,409);
 assert.equal((await h.request('progress','POST',{...update,phase:'complete'},'publisher')).status,409);
 assert.equal((await(await h.request('status')).json()).build.completed,100);
});
test('stale heartbeat checks GitHub actual active workflow before dispatching',async t=>{
 const h=setup(t),start=await(await h.request('start','POST',{})).json();h.advance(16*60*1000);
 h.setActive([{id:123,status:'in_progress',display_title:'全沪深主板研究 '+start.build.build_id}]);
 const result=await(await h.request('start','POST',{})).json();assert.equal(result.reason,'github_pending');assert.equal(h.dispatches,1);
 assert.equal(result.build.run_id,'123');assert.equal(result.build.build_id,start.build.build_id);
});
test('completed snapshot gets brief anti-repeat TTL without a force bypass',async t=>{
 const h=setup(t),start=await(await h.request('start','POST',{})).json();
 await h.request('progress','POST',{build_id:start.build.build_id,phase:'complete',completed:3195,total:3195,snapshot_commit:'a'.repeat(40)},'publisher');
 assert.equal((await(await h.request('start','POST',{force:true})).json()).reason,'recent_complete');assert.equal(h.dispatches,1);
 h.advance(16*60*1000);assert.equal((await(await h.request('start','POST',{})).json()).started,true);assert.equal(h.dispatches,2);
 assert.equal((await h.request('progress','POST',{build_id:start.build.build_id,phase:'complete'},'publisher')).status,409);
});
test('partial source gaps can resume after minimal dispatch interval',async t=>{
 const h=setup(t),start=await(await h.request('start','POST',{})).json();h.advance(61000);
 await h.request('progress','POST',{build_id:start.build.build_id,phase:'partial',completed:3195,total:3195,coverage:{emotion:{completed:3195,total:3195,unknown:20}}},'publisher');
 assert.equal((await(await h.request('start','POST',{})).json()).started,true);assert.equal(h.dispatches,2);
});
test('uncertain dispatch retains queued ownership and is not repeated',async t=>{
 const h=setup(t);h.setUncertain();assert.equal((await h.request('start','POST',{})).status,503);
 assert.equal((await(await h.request('start','POST',{})).json()).reason,'already_running');assert.equal(h.dispatches,1);
});
test('definite dispatch rejection is explicit and rate limited',async t=>{
 const h=setup(t);h.setDispatchStatus(403);assert.equal((await h.request('start','POST',{})).status,503);
 assert.equal((await(await h.request('status')).json()).build.phase,'error');
 assert.equal((await(await h.request('start','POST',{})).json()).reason,'retry_later');assert.equal(h.dispatches,1);
});
test('invalid progress cannot publish arbitrary commit, secrets or huge body',async t=>{
 const h=setup(t),start=await(await h.request('start','POST',{})).json(),id=start.build.build_id;
 assert.equal((await h.request('progress','POST',{build_id:id,phase:'running',snapshot_commit:'state'},'publisher')).status,400);
 assert.equal((await h.request('progress','POST',{build_id:id,phase:'running',junk:'x'.repeat(5000)},'publisher')).status,400);
 const result=await(await h.request('progress','POST',{build_id:id,phase:'running',secret:'never echo'},'publisher')).json();
 assert.equal(JSON.stringify(result).includes('never echo'),false);
});
test('dedicated workflow is bounded, on-demand and does not start account worker',()=>{
 const source=readFileSync(new URL('../../.github/workflows/aq5-full-research.yml',import.meta.url),'utf8');
 assert.match(source,/timeout-minutes: 90/);assert.match(source,/AQ5_DATA: \$\{\{ runner.temp \}\}\/aq5-full-research/);
 assert.match(source,/python -B -m aq5\.decision_full/);assert.match(source,/cancel-in-progress: false/);
 assert.doesNotMatch(source,/cron:|schedule:|aq5\.cloud|aq5\.server|load_models/);
});
