import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';
const source=readFileSync(new URL('../web/js/app.js',import.meta.url),'utf8');
const implementation=source.match(/^  async function requestDecision\(.*?^  }/ms)[0];
const code='sh600000', id='a'.repeat(32), hash='b'.repeat(64), proposal='c'.repeat(64);
function harness({kind='analysis',data,queued=false,afterDecrypt,afterDerive,failSubmit=false}={}){
 const calls=[], S={page:kind==='screen'?'pick':'anly',anlyCode:code,priv:{},privateSession:{key:{}}};
 let ticks=0;
 const context=vm.createContext({S,Date,TextEncoder,Uint8Array,AbortSignal,
   normCode:v=>String(v||'').toLowerCase(),relayBase:()=> 'https://relay.example',
   crypto:{subtle:{deriveBits:async()=>{afterDerive?.(S);return new Uint8Array(32).buffer;}}},
   unlockPrivate:async()=>{afterDecrypt?.(S);return structuredClone(data);},
   setTimeout:fn=>fn(),fetch:async(url,opt)=>{calls.push({url,opt});if(failSubmit)throw new Error('network');return {ok:true,json:async()=>({id,status:queued&&ticks++===0?'queued':'ready',sha256:hash})};}
 });
 vm.runInContext(implementation,context);
 return {calls,S,run:(payload={})=>context.requestDecision(kind,code,payload)};
}
test('analysis decrypts and binds proposal source to the exact task; candles cache stays session scoped',async()=>{
 const h=harness({data:{code,fusion:{code,routes:{}},plan:{},kline:[{close:10}]}});
 const data=await h.run();assert.equal(data.fusion.cloud_request_id,id);
 assert.equal(h.S.decisionResults[code].session,h.S.privateSession);
 assert.deepEqual(JSON.parse(h.calls[0].opt.body),{kind:'analysis',code});
});
test('screen requests no dummy symbol and keeps both route contracts',async()=>{
 const h=harness({kind:'screen',queued:true,data:{kind:'screen',routes:{value:{},emotion:{}}}});
 assert.equal((await h.run()).kind,'research_preview');
 assert.deepEqual(JSON.parse(h.calls[0].opt.body),{kind:'screen'});assert.equal(h.calls.length,2);
});
test('terminal screen refresh is read-only and scoped to the completed build cache key',async()=>{
 const h=harness({kind:'screen',data:{kind:'screen',routes:{value:{},emotion:{}}}});
 await h.run({read_only:true,cache_key:id});
 assert.deepEqual(JSON.parse(h.calls[0].opt.body),{kind:'screen',read_only:true,cache_key:id});
});
test('confirmation only sends identifiers, includes source request and clears old analysis cache',async()=>{
 const h=harness({kind:'confirm',data:{kind:'confirm',code,route:'emotion',confirmed_target:{id:proposal}}});
 h.S.decisionResults={[code]:{old:true}};
 await h.run({route:'emotion',proposal_id:proposal,source_request_id:id,previous_id:null,price:999});
 const body=JSON.parse(h.calls[0].opt.body);assert.equal(body.source_request_id,id);assert.equal(body.price,undefined);
 assert.equal(h.S.decisionResults[code],undefined);
});
test('wrong symbol or missing fusion fails closed instead of using stale public analysis',async()=>{
 for(const data of [{code:'sz000001'}, {code,plan:{},kline:[]}]){
   await assert.rejects(harness({data}).run(),/不匹配|不完整/);
 }
});
test('locking during key derivation prevents request submission',async()=>{
 const h=harness({afterDerive:S=>{S.priv=null;}});await assert.rejects(h.run(),/解锁状态/);assert.equal(h.calls.length,0);
});
test('locking or navigating during decrypt cannot publish private data',async()=>{
 const h=harness({data:{code,fusion:{code},plan:{},kline:[]},afterDecrypt:S=>{S.page='pick';}});
 await assert.rejects(h.run(),/页面/);assert.equal(h.S.decisionResults,undefined);
});
test('uncertain submission is not repeated',async()=>{
 const h=harness({failSubmit:true});
 await assert.rejects(h.run(),/不自动重复提交/);assert.equal(h.calls.length,1);
});
