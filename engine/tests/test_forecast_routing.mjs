import assert from 'node:assert/strict';
import worker from '../cloudflare-relay/worker.js';
const originalFetch=globalThis.fetch, calls=[];
let kvReads=0, target='https://test-pc.trycloudflare.com';
globalThis.fetch=async(url)=>{calls.push(String(url));return new Response(String(url).includes('github.io')?'cloud-page':'pc-api',{status:200});};
const env={SECRET:'management-test',FORECAST_KEY:'reader-test',LIVE_KEY:'publisher-test',
  KV:{get:async key=>{kvReads++;return key==='target'?target:null;},put:async(key,value)=>{if(key==='target')target=value;}},
  LIVE:{idFromName:name=>name,get:name=>({fetch:async()=>Response.json({channel:name})})}};
const request=(path,method='GET',headers={},body)=>worker.fetch(new Request('https://relay.test'+path,{method,headers,...(body?{body:JSON.stringify(body)}:{})}),env);
try{
  assert.equal(await(await request('/')).text(),'cloud-page');
  assert.equal(await(await request('/js/app.js?v=fix')).text(),'cloud-page');
  assert.equal(kvReads,0);assert.deepEqual(calls,['https://suqi854.github.io/-A/','https://suqi854.github.io/-A/js/app.js?v=fix']);
  assert.equal((await request('/','POST')).status,405);
  assert.equal((await request('/__aq5/unknown')).status,404);
  assert.equal(await(await request('/api/auth/me')).text(),'pc-api');
  assert.equal(calls.at(-1),'https://test-pc.trycloudflare.com/api/auth/me');
  assert.equal((await(await request('/__aq5/alive')).json()).alive,true);
  assert.equal(calls.at(-1),'https://test-pc.trycloudflare.com/api/auth/me');
  assert.equal((await request('/__aq5/target','POST',{}, {target:'https://other.trycloudflare.com'})).status,403);
  assert.equal((await request('/__aq5/target','POST',{'x-aq5-secret':'management-test'}, {target:'https://other.trycloudflare.com'})).status,200);
  assert.equal(target,'https://other.trycloudflare.com');
  assert.equal((await(await request('/__aq5/live')).json()).channel,'main');
  assert.equal((await request('/__aq5/forecast/request','POST',{}, {code:'sh600000'})).status,403);
  assert.equal((await(await request('/__aq5/forecast/request','POST',{'x-aq5-forecast-token':'reader-test'}, {code:'sh600000'})).json()).channel,'forecast-requests');
  target=null;assert.equal((await(await request('/__aq5/alive')).json()).alive,false);
  assert.equal((await request('/api/auth/me')).status,503);
  assert.equal(await(await request('/')).text(),'cloud-page');
  console.log('forecast routing: cloud-first pages, explicit PC API, duty liveness and protected routes passed');
}finally{globalThis.fetch=originalFetch;}
