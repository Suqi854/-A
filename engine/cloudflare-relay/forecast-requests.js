// Authenticated, bounded requests; analysis results remain encrypted end-to-end.
const ROOT = '/__aq5/forecast/';
const TTL = 60 * 60 * 1000, LEASE = 10 * 60 * 1000, CHUNK = 64 * 1024;
const HEADERS = {'cache-control':'no-store','access-control-allow-origin':'*','x-content-type-options':'nosniff'};
const reply = (body, status=200) => Response.json(body,{status,headers:HEADERS});
const failure = (message,status=400) => reply({status:'error',error:message},status);
const validId = id => typeof id === 'string' && /^[a-f0-9]{32}$/.test(id);
const validCode = code => typeof code === 'string' && /^(sh60[0135]\d{3}|sz00[0-3]\d{3})$/.test(code);
const uuid = () => crypto.randomUUID().replaceAll('-','');
function equal(a,b) {
  if (typeof a !== 'string' || !a.length || typeof b !== 'string' || a.length !== b.length) return false;
  let diff=0; for(let i=0;i<a.length;i++) diff |= a.charCodeAt(i)^b.charCodeAt(i);
  return diff === 0;
}
function auth(req,env,kind) {
  return kind === 'publisher' ? equal(env.LIVE_KEY,req.headers.get('x-aq5-live-key'))
    : equal(env.FORECAST_KEY,req.headers.get('x-aq5-forecast-token'));
}
function status(row) {
  if (row.status === 'error') return {id:row.id,status:'error',error:row.message || '云端分析未完成，请稍后重新提交'};
  return {id:row.id,status:row.status,updated:row.updated,...(row.sha256?{sha256:row.sha256}:{})};
}
async function boundedJson(req,limit) {
  if (Number(req.headers.get('content-length'))>limit) throw new Error('size');
  const reader=req.body?.getReader(); if(!reader) throw new Error('empty');
  const chunks=[]; let size=0;
  while(true) {const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>limit){await reader.cancel();throw new Error('size');}chunks.push(value);}
  const bytes=new Uint8Array(size);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
  return JSON.parse(new TextDecoder().decode(bytes));
}
async function cleanup(tx,now) {
  const rows=await tx.list({prefix:'request:'});
  for(const [key,row] of rows) if(now-row.created>=TTL) {
    await tx.delete(key);for(let i=0;i<(row.chunks||0);i++)await tx.delete(`result:${row.id}:${i}`);rows.delete(key);
  }
  return rows;
}
async function dispatch(env) {
  if(!env.GH_TOKEN)return false;
  try {
    const response=await fetch('https://api.github.com/repos/Suqi854/-A/actions/workflows/aq5-forecast-request.yml/dispatches',{
      method:'POST',headers:{'User-Agent':'aq5-relay','Accept':'application/vnd.github+json','Content-Type':'application/json',
        'Authorization':'Bearer '+String(env.GH_TOKEN).trim()},body:JSON.stringify({ref:'main'}),signal:AbortSignal.timeout(10000)});
    return response.status===204;
  } catch {return false;}
}
export async function forecastRoute(req,env) {
  const url=new URL(req.url);if(!url.pathname.startsWith(ROOT))return null;
  if(req.method==='OPTIONS')return new Response(null,{status:204,headers:{...HEADERS,
    'access-control-allow-methods':'GET, POST, OPTIONS','access-control-allow-headers':'content-type, x-aq5-forecast-token, x-aq5-live-key'}});
  const action=url.pathname.slice(ROOT.length);
  if(!['request','result','poll','complete'].includes(action))return failure('请求地址无效',404);
  if(!env.LIVE)return failure('云端分析通道尚未就绪',503);
  if(action==='result') {if(req.method!=='GET')return failure('请求方式无效',405);if(!validId(url.searchParams.get('id')))return failure('任务编号无效');}
  else if(!auth(req,env,['poll','complete'].includes(action)?'publisher':'reader'))return failure('云端分析授权无效，请重新解锁',403);
  return env.LIVE.get(env.LIVE.idFromName('forecast-requests')).fetch(req);
}
export async function forecastFetch(req,ctx,env) {
  const url=new URL(req.url), action=url.pathname.slice(ROOT.length),now=Date.now();
  // Repeat authorization inside the object so future routing cannot bypass it.
  if(action!=='result'&&!auth(req,env,['poll','complete'].includes(action)?'publisher':'reader'))return failure('云端分析授权无效，请重新解锁',403);
  if(action==='request'&&req.method==='POST') {
    let body;try{body=await boundedJson(req,1024);}catch{return failure('股票请求格式无效');}
    if(!validCode(body?.code))return failure('请选择沪深主板股票');
    let row,needsDispatch=false,limited=false;
    await ctx.storage.transaction(async tx=>{
      const rows=await cleanup(tx,now);
      const same=[...rows.values()].filter(x=>x.code===body.code).sort((a,b)=>b.created-a.created)[0];
      if(same&&((same.status==='ready'&&now-same.updated<5*60*1000)||(['queued','processing'].includes(same.status)&&now-same.created<8*60*1000))) {row=same;return;}
      // Bound both retained requests and dispatch cost, including failed attempts.
      if(rows.size>=20||[...rows.values()].filter(x=>now-x.created<60000).length>=3){limited=true;return;}
      row={id:uuid(),code:body.code,status:'queued',created:now,updated:now};
      await tx.put('request:'+row.id,row);needsDispatch=true;
    });
    if(limited)return failure('云端分析任务较多，请稍后再试',429);
    if(needsDispatch&&!await dispatch(env)) {
      await ctx.storage.transaction(async tx=>{
        const current=await tx.get('request:'+row.id);
        if(current?.status==='queued'){row={...current,status:'error',message:'云端分析启动失败，请稍后重新提交',updated:Date.now()};await tx.put('request:'+row.id,row);}
        else if(current)row=current;
      });
    }
    return reply(status(row),row.status==='error'?503:200);
  }
  if(action==='poll'&&req.method==='GET') {
    const requests=[];
    await ctx.storage.transaction(async tx=>{
      const rows=await cleanup(tx,now);
      for(const row of [...rows.values()].sort((a,b)=>a.created-b.created)) {
        if(requests.length>=3)break;
        if(row.status!=='queued'&&!(row.status==='processing'&&now-row.updated>=LEASE))continue;
        if((row.attempts||0)>=3){await tx.put('request:'+row.id,{...row,status:'error',message:'云端分析多次超时，请稍后重新提交',updated:now});continue;}
        const claim=uuid();await tx.put('request:'+row.id,{...row,status:'processing',claim,attempts:(row.attempts||0)+1,updated:now});
        requests.push({id:row.id,code:row.code,claim});
      }
    });
    return reply({requests});
  }
  if(action==='complete'&&req.method==='POST') {
    let body;try{body=await boundedJson(req,768*1024);}catch{return failure('结果格式或大小无效');}
    if(!validId(body?.id)||!validId(body?.claim))return failure('任务编号或执行凭据无效');
    if(body.error!==true){
      try{
        if(typeof body.data_b64!=='string'||!/^[a-f0-9]{64}$/.test(body.sha256||''))throw new Error('shape');
        const bytes=Uint8Array.from(atob(body.data_b64),c=>c.charCodeAt(0));
        if(bytes.length<70||bytes.length>512*1024||new TextDecoder().decode(bytes.slice(0,5))!=='AQ5w1')throw new Error('encrypted');
        const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),b=>b.toString(16).padStart(2,'0')).join('');
        if(digest!==body.sha256)throw new Error('digest');
      }catch{return failure('加密结果校验失败');}
    }
    let result=409;
    await ctx.storage.transaction(async tx=>{
      const row=await tx.get('request:'+body.id);
      if(!row||now-row.created>=TTL){result=404;return;}
      if(row.claim!==body.claim)return;
      if(row.status==='ready'){result=row.sha256===body.sha256?200:409;return;}
      if(row.status!=='processing')return;
      if(body.error===true){await tx.put('request:'+row.id,{...row,status:'error',updated:now,message:'云端未取得完整分析数据，请稍后重试'});result=200;return;}
      const chunks=Math.ceil(body.data_b64.length/CHUNK);
      for(let i=0;i<chunks;i++)await tx.put(`result:${row.id}:${i}`,body.data_b64.slice(i*CHUNK,(i+1)*CHUNK));
      await tx.put('request:'+row.id,{...row,status:'ready',sha256:body.sha256,chunks,updated:now});result=200;
    });
    return result===200?reply({ok:true}):failure('任务已失效或由另一执行任务接续',result);
  }
  if(['request','result'].includes(action)&&req.method==='GET') {
    const id=url.searchParams.get('id');if(!validId(id))return failure('任务编号无效');
    let row,parts=[];
    await ctx.storage.transaction(async tx=>{
      row=await tx.get('request:'+id);
      if(!row||now-row.created>=TTL){row=null;return;}
      if(action==='result'&&row.status==='ready')for(let i=0;i<row.chunks;i++)parts.push(await tx.get(`result:${id}:${i}`));
    });
    if(!row)return failure('任务已过期，请重新分析',404);
    if(action==='request')return reply(status(row));
    if(row.status!=='ready')return failure('云端结果尚未就绪',409);
    if(parts.some(x=>typeof x!=='string'))return failure('云端结果暂不可用，请重新分析',503);
    return new Response(Uint8Array.from(atob(parts.join('')),c=>c.charCodeAt(0)),{headers:{...HEADERS,'content-type':'application/octet-stream'}});
  }
  return failure('请求方式无效',405);
}
