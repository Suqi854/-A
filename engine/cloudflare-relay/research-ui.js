// Pin the reviewed application script independently of the active data publisher.
// An older long-running trading job may still publish its bundled static files.
export const RESEARCH_UI_COMMIT='243aa536765d4fff5fb83d33f585202dcae92d57';
export async function researchUiRoute(req){
  if(new URL(req.url).pathname!=='/js/app.js'||!['GET','HEAD'].includes(req.method))return null;
  const source='https://raw.githubusercontent.com/Suqi854/-A/'+RESEARCH_UI_COMMIT+'/engine/web/js/app.js';
  const response=await fetch(source,{method:req.method,cf:{cacheTtl:86400,cacheEverything:true}});
  if(!response.ok)return new Response('Reviewed application script temporarily unavailable',{status:503,headers:{'cache-control':'no-store'}});
  return new Response(req.method==='HEAD'?null:response.body,{status:200,headers:{
    'content-type':'application/javascript; charset=utf-8','cache-control':'no-cache',
    'x-content-type-options':'nosniff','x-aq5-mode':'static','x-aq5-release':RESEARCH_UI_COMMIT}});
}
