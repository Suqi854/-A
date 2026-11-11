import assert from 'node:assert/strict';
import worker from '../cloudflare-relay/worker.js';

const originalFetch=globalThis.fetch, originalNow=Date.now;
const at=Date.parse('2026-11-11T07:10:00Z'); // Beijing 15:10, within duty.
Date.now=()=>at;
const short={status:'completed',run_number:63,created_at:'2026-11-11T06:15:55Z',
  run_started_at:'2026-11-11T06:16:00Z',updated_at:'2026-11-11T06:18:18Z'};
async function kick(runs){
  let dispatches=0, saved;
  globalThis.fetch=async(url,options={})=>{
    assert.ok(String(url).startsWith('https://api.github.com/repos/Suqi854/-A/actions/workflows/aq5-cloud.yml/'));
    if(options.method==='POST'){
      dispatches++;assert.deepEqual(JSON.parse(options.body),{ref:'main',inputs:{mode:'run'}});
      return new Response(null,{status:204});
    }
    return Response.json({workflow_runs:runs});
  };
  let pending;
  await worker.scheduled({}, {GH_TOKEN:'test-only',KV:{put:async(k,v)=>{saved=JSON.parse(v);}}},
    {waitUntil:p=>{pending=p;}});
  await pending;
  return {dispatches,saved};
}
try{
  for(const conclusion of ['failure','cancelled','timed_out','action_required']){
    const result=await kick([{...short,conclusion}]);
    assert.equal(result.dispatches,1,conclusion+' must not look like a closed day');
    assert.equal(result.saved.started,true);
  }
  const closed=await kick([{...short,conclusion:'success'}]);
  assert.equal(closed.dispatches,0);assert.match(closed.saved.msg,/1 小时/);
  const running=await kick([{...short,status:'in_progress',conclusion:null}]);
  assert.equal(running.dispatches,0);assert.match(running.saved.msg,/在运行/);
  console.log('cloud kick: four failure states retry; successful short exit and live run avoid duplicate dispatch');
}finally{globalThis.fetch=originalFetch;Date.now=originalNow;}
