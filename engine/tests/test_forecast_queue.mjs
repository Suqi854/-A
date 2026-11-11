import assert from 'node:assert/strict';
import {forecastRoute} from '../cloudflare-relay/forecast-requests.js';
import {LiveSnapshot} from '../cloudflare-relay/live.js';

const originalFetch=globalThis.fetch, originalNow=Date.now;
let now=originalNow(), dispatches=0, dispatchOkay=true;
Date.now=()=>now;
globalThis.fetch=async(url,options)=>{
  assert.match(url,/actions\/workflows\/aq5-forecast-request\.yml\/dispatches$/);
  assert.equal(options.headers.Authorization,'Bearer dispatch-test');
  assert.deepEqual(JSON.parse(options.body),{ref:'main'});
  dispatches++;return new Response(null,{status:dispatchOkay?204:403});
};
function setup(){
  const mem=new Map();let tail=Promise.resolve();
  const storage={
    get:async key=>structuredClone(mem.get(key)),
    put:async(key,value)=>{assert.ok(JSON.stringify(value).length<128*1024);mem.set(key,structuredClone(value));},
    delete:async key=>mem.delete(key),
    list:async({prefix})=>new Map([...mem].filter(([key])=>key.startsWith(prefix)).map(([key,value])=>[key,structuredClone(value)])),
    transaction:fn=>{const task=tail.then(()=>fn(storage));tail=task.catch(()=>{});return task;}
  };
  const env={FORECAST_KEY:'reader-test',LIVE_KEY:'publisher-test',GH_TOKEN:'dispatch-test'};
  const object=new LiveSnapshot({storage},env);
  env.LIVE={idFromName:name=>{assert.equal(name,'forecast-requests');return name;},get:()=>object};
  const request=(action,method='GET',body,role='reader')=>forecastRoute(new Request('https://relay.test/__aq5/forecast/'+action,{
    method,headers:role==='reader'?{'x-aq5-forecast-token':'reader-test'}:role==='publisher'?{'x-aq5-live-key':'publisher-test'}:{},
    ...(body!==undefined?{body:JSON.stringify(body)}:{})}),env);
  return {mem,request,env};
}
try{
  let {request,mem,env}=setup();
  assert.equal(await forecastRoute(new Request('https://relay.test/elsewhere'),env),null);
  assert.equal((await request('request','OPTIONS')).status,204);
  assert.equal((await request('request','POST',{code:'sh600000'},'none')).status,403);
  assert.equal((await request('request','POST',{code:'sh600000'},'publisher')).status,403);
  assert.equal((await request('poll')).status,403);
  assert.equal((await request('request','POST',{code:'sz300001'})).status,400);
  assert.equal((await request('request','POST',{code:'sh688001'})).status,400);
  assert.equal((await request('request','POST',{code:'sh600000',junk:'x'.repeat(1024)})).status,400);
  const pair=await Promise.all([request('request','POST',{code:'sh600000'}),request('request','POST',{code:'sh600000'})]);
  const task=await pair[0].json();assert.equal(task.id,(await pair[1].json()).id);assert.equal(dispatches,1);
  assert.equal(task.status,'queued');assert.ok(!('previous_results'in task));assert.equal((await request('result?id='+task.id)).status,409);
  const poll=await (await request('poll','GET',undefined,'publisher')).json();
  assert.equal(poll.requests.length,1);assert.deepEqual(Object.keys(poll.requests[0]).sort(),['claim','code','id']);
  assert.equal((await (await request('poll','GET',undefined,'publisher')).json()).requests.length,0);
  const claim=poll.requests[0].claim;
  assert.equal((await (await request('request?id='+task.id)).json()).status,'processing');
  assert.equal((await request('complete','POST',{id:task.id,claim,data_b64:'plaintext',sha256:'0'.repeat(64)},'publisher')).status,400);
  const bytes=new Uint8Array(180000);bytes.set(new TextEncoder().encode('AQ5w1'));bytes.fill(42,5);
  const data_b64=Buffer.from(bytes).toString('base64'),sha256=Buffer.from(await crypto.subtle.digest('SHA-256',bytes)).toString('hex');
  const completion={id:task.id,claim,data_b64,sha256};
  assert.equal((await request('complete','POST',{...completion,sha256:'0'.repeat(64)},'publisher')).status,400);
  assert.equal((await request('complete','POST',completion)).status,403);
  assert.equal((await request('complete','POST',completion,'publisher')).status,200);
  assert.equal((await request('complete','POST',completion,'publisher')).status,200);
  const ready=await (await request('request?id='+task.id)).json();assert.equal(ready.sha256,sha256);assert.ok(!('code'in ready)&&!('data_b64'in ready));
  assert.deepEqual(ready.previous_results,[]);
  const downloaded=await request('result?id='+task.id,'GET',undefined,'none');
  assert.equal(downloaded.headers.get('content-type'),'application/octet-stream');
  assert.deepEqual(new Uint8Array(await downloaded.arrayBuffer()),bytes);
  assert.equal((await (await request('request','POST',{code:'sh600000'})).json()).id,task.id);
  assert.equal(dispatches,1);assert.ok([...mem.keys()].filter(k=>k.startsWith('result:')).length>1);
  now+=61*60*1000;
  assert.equal((await request('result?id='+task.id)).status,404);
  await request('poll','GET',undefined,'publisher');assert.equal(mem.size,0);

  // History is bounded ciphertext metadata, isolated by code and original creation TTL.
  ({request,mem}=setup());
  const seed=(number,overrides={})=>{
    const id=number.toString(16).padStart(32,'0');
    const row={id,code:'sh600000',status:'ready',created:now-20*60*1000,updated:now-number*1000,
      sha256:String(number%10).repeat(64),claim:'private-claim',chunks:2,data_b64:'private-ciphertext',message:'private-message',...overrides};
    mem.set('request:'+id,row);return row;
  };
  const current=seed(1,{created:now-60000});
  const oldest=seed(5), secondNewest=seed(3), newest=seed(2), thirdNewest=seed(4);
  seed(6,{code:'sz002911',updated:now-1100});
  seed(7,{created:now-60*60*1000,updated:now-1200}); // Boundary: recently completed but expired.
  seed(8,{status:'processing',updated:now-1300});
  seed(9,{updated:now}); // Newer completion is not a previous result.
  seed(10,{updated:current.updated});
  const expectedHistory=[newest,secondNewest,thirdNewest].map(({id,sha256,updated})=>({id,sha256,updated}));
  const beforeDispatches=dispatches;
  for(const response of [await request('request?id='+current.id),await request('request','POST',{code:current.code})]) {
    assert.equal(response.status,200);
    const result=await response.json();
    assert.equal(result.id,current.id);assert.equal(result.status,'ready');
    assert.deepEqual(Object.keys(result).sort(),['id','previous_results','sha256','status','updated']);
    assert.deepEqual(result.previous_results,expectedHistory);
    for(const candidate of result.previous_results)assert.deepEqual(Object.keys(candidate).sort(),['id','sha256','updated']);
    assert.ok(!result.previous_results.some(x=>x.id===oldest.id));
  }
  assert.equal(dispatches,beforeDispatches);
  assert.equal((await request('request?id='+current.id,'GET',undefined,'none')).status,403);
  now+=60*60*1000;
  assert.equal((await request('request?id='+current.id)).status,404);

  ({request,mem}=setup());dispatches=0;
  const queued=await (await request('request','POST',{code:'sz002911'})).json();
  const first=(await (await request('poll','GET',undefined,'publisher')).json()).requests[0];
  now+=10*60*1000;
  const second=(await (await request('poll','GET',undefined,'publisher')).json()).requests[0];
  assert.notEqual(first.claim,second.claim);
  assert.equal((await request('complete','POST',{...completion,id:queued.id,claim:first.claim},'publisher')).status,409);
  assert.equal((await request('complete','POST',{id:queued.id,claim:second.claim,error:true},'publisher')).status,200);
  assert.match((await (await request('request?id='+queued.id)).json()).error,/完整分析数据/);

  ({request,mem}=setup());dispatchOkay=false;
  assert.equal((await request('request','POST',{code:'sh600000'})).status,503);
  assert.equal([...mem.values()][0].status,'error');dispatchOkay=true;

  ({request,mem}=setup());
  for(let i=0;i<3;i++)assert.equal((await request('request','POST',{code:'sh60000'+i})).status,200);
  assert.equal((await request('request','POST',{code:'sh600003'})).status,429);
  assert.equal((await (await request('poll','GET',undefined,'publisher')).json()).requests.length,3);
  for(let i=3;i<20;i++){now+=61000;assert.equal((await request('request','POST',{code:'sh600'+String(i).padStart(3,'0')})).status,200);}
  now+=61000;assert.equal((await request('request','POST',{code:'sh600021'})).status,429);
  assert.equal(mem.size,20);
  console.log('forecast queue: authentication, isolation, concurrency, leases, encryption, chunks, expiry, limits and bounded ready history passed');
}finally{globalThis.fetch=originalFetch;Date.now=originalNow;}
