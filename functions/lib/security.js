export const ACTIONS = new Set(['reboot', 'refresh-updates', 'install-updates']);
export async function hash(value) {
 return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value))), b => b.toString(16).padStart(2,'0')).join('');
}
export function secret() { return Array.from(crypto.getRandomValues(new Uint8Array(32)), b=>b.toString(16).padStart(2,'0')).join(''); }
function decode(s) { return Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')),c=>c.charCodeAt(0)); }
let keyCache;
export async function admin(request, env) {
 const team = env.ACCESS_TEAM;
 if (!/^https:\/\/[a-z0-9-]+\.cloudflareaccess\.com$/.test(team || '') || !env.ACCESS_AUD) throw new Error('Authentication is not configured');
 const raw = request.headers.get('Cf-Access-Jwt-Assertion') || '';
 if (raw.length > 16000) throw new Error('Unauthorized');
 const parts = raw.split('.');
 if (parts.length !== 3) throw new Error('Unauthorized');
 const header = JSON.parse(new TextDecoder().decode(decode(parts[0])));
 const claims = JSON.parse(new TextDecoder().decode(decode(parts[1])));
 const now = Math.floor(Date.now()/1000);
 if (header.alg !== 'RS256' || claims.iss !== team || !Number.isFinite(claims.exp) || claims.exp <= now || (claims.nbf && claims.nbf > now) || !Array.isArray(claims.aud) || !claims.aud.includes(env.ACCESS_AUD)) throw new Error('Unauthorized');
 if (!keyCache || keyCache.team !== team || keyCache.until < Date.now() || !keyCache.keys.some(k=>k.kid === header.kid)) {
  const response = await fetch(team + '/cdn-cgi/access/certs', {redirect:'error'});
  if (!response.ok) throw new Error('Unauthorized');
  keyCache = {team, until:Date.now()+300000, keys:(await response.json()).keys};
 }
 const jwk = keyCache.keys.find(k=>k.kid === header.kid);
 if (!jwk) throw new Error('Unauthorized');
 const key = await crypto.subtle.importKey('jwk',jwk,{name:'RSASSA-PKCS1-v1_5',hash:'SHA-256'},false,['verify']);
 if (!await crypto.subtle.verify('RSASSA-PKCS1-v1_5',key,decode(parts[2]),new TextEncoder().encode(parts[0]+'.'+parts[1]))) throw new Error('Unauthorized');
 const email = String(claims.email || '').toLowerCase();
 if (!email || !email.includes('@')) throw new Error('Unauthorized');
 return email;
}
export function sameOrigin(request) {
 if (request.headers.get('Origin') !== new URL(request.url).origin) throw new Error('Invalid origin');
 if (!request.headers.get('Content-Type')?.startsWith('application/json')) throw new Error('JSON required');
}
export async function body(request) {
 // Stream limit applies even when Content-Length is omitted or forged.
 const reader = request.body?.getReader(); let size=0, chunks=[];
 if (!reader) throw new Error('JSON required');
 while (true) { const {done,value}=await reader.read(); if(done) break; size+=value.length; if(size>32768) {await reader.cancel(); throw new Error('Request too large');} chunks.push(value); }
 const bytes = new Uint8Array(size); let offset=0; for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
 return JSON.parse(new TextDecoder().decode(bytes));
}
