import {ACTIONS,hash,secret,admin,sameOrigin,body} from '../lib/security.js';
const json = (data,status=200)=>Response.json(data,{status,headers:{'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
const audit = (db,tenant,at,actor,event,node=null,job=null)=>db.prepare('INSERT INTO audit(tenant_id,at,actor,event,node_id,job_id) VALUES(?,?,?,?,?,?)').bind(tenant,at,actor,event,node,job);
export async function onRequest({request,env}) {
 try { return await route(request,env); }
 catch(error) {
  if (String(error.message).includes('UNIQUE constraint failed')) return json({error:'An action is already active for this server'},409);
  console.error('API request failed:', error.name);
  return json({error:'Request rejected. Check authentication, input, and server configuration.'},400);
 }
}
async function route(request,env) {
 const path=new URL(request.url).pathname, now=Math.floor(Date.now()/1000), db=env.DB;
 if (!db) return json({error:'Database is not configured'},503);
 if (path.startsWith('/api/agent/')) {
  if(request.method!=='POST') return json({error:'Method not allowed'},405);
  const token=request.headers.get('Authorization')?.match(/^Bearer ([a-f0-9]{64})$/)?.[1];
  if(!token) return json({error:'Unauthorized'},401);
  const node=await db.prepare('SELECT id,tenant_id FROM nodes WHERE token_hash=? AND revoked=0').bind(await hash(token)).first();
  if(!node) return json({error:'Unauthorized'},401);
  const data=await body(request);
  if(path==='/api/agent/poll') {
   const m=data.metrics;
   if(!m || typeof m.hostname!=='string' || m.hostname.length>255 || !Number.isFinite(m.uptime) || !Number.isFinite(m.memory_percent) || !Number.isFinite(m.disk_percent)) return json({error:'Invalid metrics'},400);
   await db.prepare('UPDATE nodes SET last_seen=?,metrics=? WHERE id=? AND revoked=0').bind(now,JSON.stringify(m),node.id).run();
   // Never redeliver a dispatched action. Uncertain results require operator review.
   await db.prepare("UPDATE jobs SET status='expired' WHERE node_id=? AND status='queued' AND expires_at<=?").bind(node.id,now).run();
   const job=await db.prepare("UPDATE jobs SET status='dispatched', dispatched_at=? WHERE id=(SELECT id FROM jobs WHERE node_id=? AND status='queued' AND expires_at>? ORDER BY created_at LIMIT 1) RETURNING id,action,expires_at").bind(now,node.id,now).first();
   return json({job:job||null});
  }
  if(path==='/api/agent/result') {
   if(typeof data.id!=='string' || !['succeeded','failed','unknown'].includes(data.status) || typeof data.result!=='string') return json({error:'Invalid result'},400);
   const changed=await db.prepare("UPDATE jobs SET status=?,result=?,completed_at=? WHERE id=? AND node_id=? AND status='dispatched' RETURNING id").bind(data.status,data.result.slice(0,4000),now,data.id,node.id).first();
   if(changed) await audit(db,node.tenant_id,now,`agent:${node.id}`,`action.${data.status}`,node.id,data.id).run();
   return json({ok:true});
  }
  return json({error:'Not found'},404);
 }
 let actor; try {actor=await admin(request,env);} catch {return json({error:'Sign in with an allowed Cloudflare Access account'},401);}
 if(request.method==='GET' && path==='/api/tenants') {
  const tenants=await db.prepare('SELECT t.id,t.name,m.role FROM tenants t JOIN memberships m ON t.id=m.tenant_id WHERE m.email=? ORDER BY t.name').bind(actor).all();
  return json({tenants:tenants.results,actor});
 }
 const tenant=request.headers.get('X-Tenant-ID');
 const membership=await db.prepare('SELECT role FROM memberships WHERE tenant_id=? AND email=?').bind(tenant||'',actor).first();
 if(!membership) return json({error:'Customer access denied'},403);
 if(request.method==='GET' && path==='/api/overview') {
  const [nodes,jobs,events]=await Promise.all([
   db.prepare('SELECT id,name,created_at,last_seen,metrics,revoked FROM nodes WHERE tenant_id=? ORDER BY name').bind(tenant).all(),
   db.prepare('SELECT jobs.* FROM jobs JOIN nodes ON nodes.id=jobs.node_id WHERE nodes.tenant_id=? ORDER BY jobs.created_at DESC LIMIT 100').bind(tenant).all(),
   db.prepare('SELECT * FROM audit WHERE tenant_id=? ORDER BY id DESC LIMIT 100').bind(tenant).all()
  ]);
  return json({nodes:nodes.results.map(n=>({...n,metrics:n.metrics?JSON.parse(n.metrics):null})),jobs:jobs.results,events:events.results,actor});
 }
 if(request.method!=='POST') return json({error:'Not found'},404);
 if(membership.role==='viewer') return json({error:'Read-only account'},403);
 sameOrigin(request); const data=await body(request);
 if(path==='/api/nodes') {
  if(membership.role!=='owner') return json({error:'Customer owner role required'},403);
  if(typeof data.name!=='string' || !/^[a-zA-Z0-9][a-zA-Z0-9 ._-]{0,63}$/.test(data.name)) return json({error:'Enter a server name of 1–64 letters, numbers, spaces, dots, underscores or hyphens'},400);
  const id=crypto.randomUUID(),token=secret();
  await db.batch([db.prepare('INSERT INTO nodes(id,tenant_id,name,token_hash,created_at) VALUES(?,?,?,?,?)').bind(id,tenant,data.name,await hash(token),now),audit(db,tenant,now,actor,'node.created',id)]);
  return json({id,token},201);
 }
 if(typeof data.node_id!=='string') return json({error:'Server required'},400);
 const node=await db.prepare('SELECT * FROM nodes WHERE id=? AND tenant_id=? AND revoked=0').bind(data.node_id,tenant).first();
 if(!node) return json({error:'Server not found'},404);
 if(path==='/api/revoke') {
  if(membership.role!=='owner') return json({error:'Customer owner role required'},403);
  await db.batch([db.prepare('UPDATE nodes SET revoked=1 WHERE id=?').bind(node.id),db.prepare("UPDATE jobs SET status='cancelled' WHERE node_id=? AND status='queued'").bind(node.id),audit(db,tenant,now,actor,'node.revoked',node.id)]);
  return json({ok:true});
 }
 if(path==='/api/jobs') {
  if(!ACTIONS.has(data.action) || data.confirm!==node.name) return json({error:'Action and exact server name confirmation required'},400);
  if(!node.last_seen || now-node.last_seen>90) return json({error:'Server is offline'},409);
  const id=crypto.randomUUID();
  await db.batch([db.prepare('INSERT INTO jobs(id,node_id,action,created_at,expires_at,actor) VALUES(?,?,?,?,?,?)').bind(id,node.id,data.action,now,now+120,actor),audit(db,tenant,now,actor,'action.queued',node.id,id)]);
  return json({id},201);
 }
 if(path==='/api/resolve') {
  const changed=await db.prepare("UPDATE jobs SET status='unknown',result='Operator acknowledged uncertain outcome',completed_at=? WHERE id=? AND node_id=? AND status='dispatched' AND dispatched_at<? RETURNING id").bind(now,data.job_id||'',node.id,now-3600).first();
  if(!changed) return json({error:'Only dispatched actions older than one hour can be acknowledged'},409);
  await audit(db,tenant,now,actor,'action.acknowledged',node.id,changed.id).run(); return json({ok:true});
 }
 return json({error:'Not found'},404);
}
