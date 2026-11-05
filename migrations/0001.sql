CREATE TABLE tenants (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE memberships (
 tenant_id TEXT NOT NULL REFERENCES tenants(id), email TEXT NOT NULL COLLATE NOCASE,
 role TEXT NOT NULL CHECK(role IN ('owner','operator','viewer')), PRIMARY KEY(tenant_id,email)
);
CREATE TABLE nodes (
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
 created_at INTEGER NOT NULL, last_seen INTEGER, metrics TEXT, revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE jobs (
 id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id), action TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
 dispatched_at INTEGER, completed_at INTEGER, actor TEXT NOT NULL, result TEXT
);
CREATE INDEX job_queue ON jobs(node_id,status,created_at);
CREATE UNIQUE INDEX one_active_job ON jobs(node_id) WHERE status IN ('queued','dispatched');
CREATE TABLE audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL REFERENCES tenants(id), at INTEGER NOT NULL, actor TEXT NOT NULL,
 event TEXT NOT NULL, node_id TEXT, job_id TEXT
);
