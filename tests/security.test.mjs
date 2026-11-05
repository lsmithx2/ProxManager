import {test,before} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {onRequest} from '../functions/api/[[path]].js';
import {hash,body,admin} from '../functions/lib/security.js';
const team='https://test.cloudflareaccess.com',aud='test-audience';
let keys,jwk;
before(async()=>{
 keys=await crypto.subtle.generateKey({name:'RSASSA-PKCS1-v1_5',modulusLength:2048,publicExponent:new Uint8Array([1,0,1]),hash:'SHA-256'},true,['sign','verify']);
 jwk={...await crypto.subtle.exportKey('jwk',keys.publicKey),kid:'test-key'};
 globalThis.fetch=async url=>{assert.equal(url,team+'/cdn-cgi/access/certs');return Response.json({keys:[jwk]});};
});
const encode=x=>Buffer.from(JSON.stringify(x)).toString('base64url');
async function jwt(email,extra={}){const text=encode({alg:'RS256',kid:'test-key'})+'.'+encode({iss:team,aud:[aud],exp:Math.floor(Date.now()/1000)+300,email,...extra});const signature=await crypto.subtle.sign('RSASSA-PKCS1-v1_5',keys.privateKey,new TextEncoder().encode(text));return text+'.'+Buffer.from(signature).toString('base64url');}
// Execute the actual queries against SQLite, including atomic D1-style batches.
class DB{
 constructor(path){this.path=path;}
 call(statements){const code=`import json,sqlite3,sys\nc=sqlite3.connect(sys.argv[1])\nc.row_factory=sqlite3.Row\nresults=[]\nwith c:\n for s in json.load(sys.stdin):\n  cur=c.execute(s['sql'],s['args'])\n  rows=[dict(r) for r in cur.fetchall()]\n  results.append(rows)\nprint(json.dumps(results))`;
 const r=spawnSync(process.env.TEST_PYTHON||'python',['-c',code,this.path],{input:JSON.stringify(statements),encoding:'utf8'});if(r.status!==0)throw new Error(r.stderr);return JSON.parse(r.stdout);}
 prepare(sql){const db=this;return {sql,args:[],bind(...args){this.args=args;return this;},async first(){return db.call([this])[0][0]||null;},async all(){return {results:db.call([this])[0]};},async run(){db.call([this]);return {success:true};}};}
 async batch(statements){return this.call(statements);}
}
async function fixture(fn){const dir=mkdtempSync(join(tmpdir(),'proxmanager-')),path=join(dir,'test.sqlite'),db=new DB(path);try{const schema=readFileSync('migrations/0001.sql','utf8');for(const sql of schema.split(';').filter(s=>s.trim()))await db.prepare(sql).run();await db.batch([
 db.prepare("INSERT INTO tenants VALUES('a','Alpha'),('b','Beta')"),
 db.prepare("INSERT INTO memberships VALUES('a','owner@a.test','owner'),('a','viewer@a.test','viewer'),('a','operator@a.test','operator'),('b','owner@b.test','owner')"),
 db.prepare("INSERT INTO nodes(id,tenant_id,name,token_hash,created_at,last_seen) VALUES('node-a','a','host-a',?,1,?),('node-b','b','host-b',?,1,?)").bind(await hash('a'.repeat(64)),Math.floor(Date.now()/1000),await hash('b'.repeat(64)),Math.floor(Date.now()/1000))]);await fn({DB:db,ACCESS_TEAM:team,ACCESS_AUD:aud});}finally{rmSync(dir,{recursive:true,force:true});}}
async function request(env,path,{email='owner@a.test',tenant='a',data,token,origin='https://app.test'}={}){const headers={'Content-Type':'application/json','X-Tenant-ID':tenant,Origin:origin};if(token)headers.Authorization='Bearer '+token;else headers['Cf-Access-Jwt-Assertion']=await jwt(email);return onRequest({env,request:new Request('https://app.test/api/'+path,{method:data?'POST':'GET',headers,body:data?JSON.stringify(data):undefined})});}
test('JWT rejects expired, wrong audience, wrong issuer and tampered signatures',async()=>{for(const extra of [{exp:1},{aud:['other']},{iss:'https://evil.test'}]){const raw=await jwt('owner@a.test',extra);await assert.rejects(()=>admin(new Request('https://app.test',{headers:{'Cf-Access-Jwt-Assertion':raw}}),{ACCESS_TEAM:team,ACCESS_AUD:aud}));}const raw=await jwt('owner@a.test');const parts=raw.split('.');parts[2]=(parts[2][0]==='A'?'B':'A')+parts[2].slice(1);await assert.rejects(()=>admin(new Request('https://app.test',{headers:{'Cf-Access-Jwt-Assertion':parts.join('.')}}),{ACCESS_TEAM:team,ACCESS_AUD:aud}));});
test('tenant membership scopes inventory and audit',()=>fixture(async env=>{const response=await request(env,'overview');assert.equal(response.status,200);const data=await response.json();assert.deepEqual(data.nodes.map(n=>n.id),['node-a']);assert.equal((await request(env,'overview',{tenant:'b'})).status,403);}));
test('cross-tenant server actions and revocation are denied',()=>fixture(async env=>{for(const path of ['jobs','revoke','resolve'])assert.equal((await request(env,path,{data:{node_id:'node-b',action:'reboot',confirm:'host-b'}})).status,404);}));
test('viewer cannot create, act or revoke',()=>fixture(async env=>{for(const path of ['nodes','jobs','revoke'])assert.equal((await request(env,path,{email:'viewer@a.test',data:{name:'x',node_id:'node-a',action:'reboot',confirm:'host-a'}})).status,403);}));
test('cross-origin write is denied',()=>fixture(async env=>{assert.equal((await request(env,'jobs',{origin:'https://evil.test',data:{node_id:'node-a',action:'reboot',confirm:'host-a'}})).status,400);}));
test('allowlist and confirmation are enforced',()=>fixture(async env=>{for(const data of [{action:'shell',confirm:'host-a'},{action:'reboot',confirm:'wrong'}])assert.equal((await request(env,'jobs',{data:{node_id:'node-a',...data}})).status,400);}));
test('atomic delivery happens once and agent result is bound to its host',()=>fixture(async env=>{
 const response=await request(env,'jobs',{data:{node_id:'node-a',action:'reboot',confirm:'host-a'}});assert.equal(response.status,201);const {id}=await response.json();
 assert.equal((await request(env,'jobs',{data:{node_id:'node-a',action:'reboot',confirm:'host-a'}})).status,409);
 const poll=()=>request(env,'agent/poll',{token:'a'.repeat(64),data:{metrics:{hostname:'host-a',uptime:10,memory_percent:20,disk_percent:30}}});
 assert.equal((await (await poll()).json()).job.id,id);assert.equal((await (await poll()).json()).job,null);
 await request(env,'agent/result',{token:'b'.repeat(64),data:{id,status:'succeeded',result:'forged'}});
 assert.equal((await env.DB.prepare('SELECT status FROM jobs WHERE id=?').bind(id).first()).status,'dispatched');
 await request(env,'agent/result',{token:'a'.repeat(64),data:{id,status:'succeeded',result:'scheduled'}});
 assert.equal((await env.DB.prepare('SELECT status FROM jobs WHERE id=?').bind(id).first()).status,'succeeded');
}));
test('revocation immediately rejects the agent',()=>fixture(async env=>{assert.equal((await request(env,'revoke',{data:{node_id:'node-a'}})).status,200);assert.equal((await request(env,'agent/poll',{token:'a'.repeat(64),data:{}})).status,401);}));
test('operators can request actions but cannot issue or revoke credentials',()=>fixture(async env=>{assert.equal((await request(env,'jobs',{email:'operator@a.test',data:{node_id:'node-a',action:'refresh-updates',confirm:'host-a'}})).status,201);for(const path of ['nodes','revoke'])assert.equal((await request(env,path,{email:'operator@a.test',data:{node_id:'node-a',name:'new-host'}})).status,403);}));
test('new credentials are shown once and only their hashes are stored',()=>fixture(async env=>{const response=await request(env,'nodes',{data:{name:'new-host'}});assert.equal(response.status,201);const value=await response.json();assert.match(value.token,/^[a-f0-9]{64}$/);const stored=await env.DB.prepare('SELECT token_hash,tenant_id FROM nodes WHERE id=?').bind(value.id).first();assert.equal(stored.token_hash,await hash(value.token));assert.equal(stored.tenant_id,'a');const overview=await (await request(env,'overview')).json();assert.equal(JSON.stringify(overview).includes(value.token),false);assert.equal(JSON.stringify(overview).includes(stored.token_hash),false);}));
test('expired jobs are never dispatched and offline hosts reject actions',()=>fixture(async env=>{await env.DB.prepare("INSERT INTO jobs(id,node_id,action,created_at,expires_at,actor) VALUES('expired','node-a','reboot',1,2,'owner@a.test')").run();const response=await request(env,'agent/poll',{token:'a'.repeat(64),data:{metrics:{hostname:'host-a',uptime:1,memory_percent:1,disk_percent:1}}});assert.equal((await response.json()).job,null);assert.equal((await env.DB.prepare("SELECT status FROM jobs WHERE id='expired'").first()).status,'expired');await env.DB.prepare("UPDATE nodes SET last_seen=1 WHERE id='node-a'").run();assert.equal((await request(env,'jobs',{data:{node_id:'node-a',action:'reboot',confirm:'host-a'}})).status,409);}));
test('oversized request bodies are rejected',async()=>{await assert.rejects(()=>body(new Request('https://app.test',{method:'POST',body:'x'.repeat(32769)})),/too large/);});
