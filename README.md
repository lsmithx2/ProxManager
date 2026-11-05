# ProxManager

An early multi-customer Proxmox VE management application for **Cloudflare Pages**, with Pages Functions, D1 storage, Cloudflare Access authentication and an outbound-only Python host agent. This is a working foundation, not a production security certification. No live servers have been modified.

## Included

- Customer-scoped inventory, memory/disk usage, load, uptime, Proxmox version and last-seen telemetry.
- Customer memberships: owners provision/revoke agents, operators request actions, viewers have read-only access.
- Reboot scheduling, package-index refresh, and package upgrades as fixed allowlisted actions.
- Per-host 256-bit credentials, stored as SHA-256 hashes in D1; credential displayed only at creation. To rotate, revoke the old registration and create a new one.
- Expiring queues, atomic dispatch, durable local receipts, result retry and customer-scoped audit events.
- Host-side opt-in policy. All remote actions are disabled on a fresh installation.

## Design

```mermaid
flowchart LR
  User[Customer browser] --> Access[Cloudflare Access / identity provider]
  Access --> Pages[Pages dashboard and Functions]
  Pages --> D1[(D1: customers, memberships, hosts, jobs, audit)]
  Host[Proxmox host agent] -->|Outbound HTTPS polling + results| Pages
  Host --> Policy[Local action allowlist]
  Policy --> Commands[Fixed maintenance commands]
```

Cloudflare Access JWTs are cryptographically verified by the API, including issuer, audience and expiry. A verified email must also have a D1 membership for the requested customer. Customer IDs supplied by the browser are never treated as authorization. Each agent credential resolves to exactly one server. Proxmox API credentials are not stored in the cloud.

## Deploy to Cloudflare Pages

Prerequisites: Node.js 22+, Python 3.11+ for tests, and a Cloudflare account with Pages, D1 and Access. The application origin is **https://proxmanager.pages.dev**; no custom domain is required. No Sites hosting account is required.

1. Install development tooling: `npm install`. Authenticate using `npx wrangler login`.
2. Create storage: `npx wrangler d1 create proxmox-control`. Put the returned database ID in `wrangler.toml`.
3. Run migrations: `npx wrangler d1 migrations apply proxmox-control --remote`.
4. Set up Cloudflare Access for the dashboard hostname. For a Pages-provided address, first create the Pages project (step 7), enable its Access policy, then follow [Cloudflare's production pages.dev instructions](https://developers.cloudflare.com/pages/platform/known-issues/#enable-access-on-your-pagesdev-domain): edit the generated Access application's hostname to remove the wildcard, then re-enable the Pages access policy to protect preview subdomains separately. Confirm both production and previews are protected. Configure the identity provider, customer access policies and MFA at the provider. Set `ACCESS_TEAM` to `https://YOUR-TEAM.cloudflareaccess.com` and `ACCESS_AUD` to the **production** application's audience, in `wrangler.toml` or Pages runtime variables. The API fails closed until these are configured.
5. The `/api/agent/*` paths and the public `/install.sh` download must be reachable without interactive Access login. Create narrowly scoped Access applications with Bypass policies for those paths only. The installer contains public code, not customer tokens. These endpoints still require the application's per-host bearer credential. Keep the rest of the hostname behind the normal Access application. Test the path-specific configuration before installing an agent.
6. Provision customers and memberships using reviewed D1 SQL (below). There is no public signup or membership administration endpoint in this version.
7. Create the Pages project with `npx wrangler pages project create proxmanager --production-branch main`, then run `npm run deploy`. If the `proxmanager` project already exists, reuse it. The output folder is `public`; Functions are deployed from `functions`. Confirm the production D1 binding and variables in the Pages dashboard. The production origin and installer URL are `https://proxmanager.pages.dev` and `https://proxmanager.pages.dev/install.sh`. For a custom domain later, attach and validate it in Pages **before** creating its Access application, then update the audience and each host's configured origin. Authentication must be configured for that hostname too.
8. If using Git integration, connect this repository to Pages with build command `npm run build` and output directory `public`. The build embeds the current agent and service into the installer. Verify bindings and variables for production separately from previews. Do not bind unprotected preview deployments to production storage.
9. Open the protected hostname, add a server, and follow **Agent setup**. Store the displayed token securely; it cannot be retrieved later.

Example customer provisioning, using D1's dashboard SQL console or a reviewed SQL file with `npx wrangler d1 execute proxmox-control --remote --file customer.sql`:

```sql
INSERT INTO tenants(id,name) VALUES('customer-unique-id','Example Customer');
INSERT INTO memberships(tenant_id,email,role)
VALUES('customer-unique-id','owner@example.com','owner');
-- Additional accounts: 'operator' or 'viewer'. Email must match Access identity.
```

Use unique opaque customer IDs and lowercase emails. Customer provisioning and changes to membership currently require an administrator with D1 access. Do not commit real customer email lists or agent configuration files.

## Agent installation

In the dashboard, choose **Add server** and save the token shown once. In a root shell on the Proxmox host, run:

```sh
curl -fsSL --proto '=https' --tlsv1.2 https://proxmanager.pages.dev/install.sh | bash
```

Paste the token into the installer's hidden prompt. It writes root-owned configuration with mode `0600`, installs the agent and systemd service, then enables and starts it. The token is never included in the command line, URL, shell history or installer output. An interactive terminal, curl, systemd and Python 3.11+ are required. The command does not install packages or reboot the host.

New installations use `https://proxmanager.pages.dev` and `allowed_actions: []`. To permit maintenance, edit `/etc/proxmanager/config.json`, explicitly add any of `refresh-updates`, `install-updates`, `reboot`, then restart `proxmanager`. Reinstallation preserves existing configuration and job receipts. It refuses to replace a running agent; wait until maintenance is complete and stop the agent before reinstalling.

The public installer embeds the agent and service from the same source revision, validates the embedded payload before changing files, and requires no GitHub access or separate downloads on the host. Its hashes detect corruption; authenticity relies on HTTPS and control of the Pages deployment. Only a complete script invokes installation. It does not verify the server token against the API before starting; check the dashboard for the first heartbeat and use `journalctl -u proxmanager` for connection failures.

For review before execution, download `/install.sh` to a file, inspect it and run `bash install.sh` as root. If deploying directly from GitHub, run `npm run build` as the Pages build command. For local deployment, `npm run deploy` builds the installer automatically. The generated `public/install.sh` is also committed, and tests reject a stale copy.

For an offline source copy, `sh agent/install.sh` invokes the same installer and prompts for the token. The hosted installer requires `/install.sh` to be excluded from interactive Access authentication; keep the dashboard protected and retain per-host authentication on `/api/agent/*`.

The service runs as root because host package management and reboots need privilege. Its code, configuration and installation directory must remain root-owned and unwritable by untrusted users. Compromise of the cloud application, a customer owner/operator account, or the host credential is security-sensitive. The cloud can request any action that the local policy allows; this is not a sandbox against a compromised control plane. Splitting telemetry from a narrowly privileged local executor is a recommended next hardening step before a public production rollout.

`install-updates` runs `apt-get -y --no-remove upgrade`, using the host's configured repositories. It does not perform major release upgrades or a `dist-upgrade`. Package configuration may still require operator attention and a command can fail or time out. Check backups, repository configuration, cluster quorum/HA state and guest placement before enabling it. No automatic guest migration or maintenance sequencing is implemented.

## Delivery and failure behavior

- The agent polls every 20 seconds. A host is shown online when its last heartbeat is within 90 seconds. A long-running package action pauses its polling and can temporarily show it as offline.
- One queued/dispatched action per server. Jobs expire after two minutes if not dispatched. The agent checks expiry before execution.
- Dispatch is atomic and never automatically retried. A lost response may leave an action in `dispatched` even if it never ran. This deliberately avoids retrying an uncertain reboot or upgrade.
- A local SQLite receipt is committed before execution. Agent restart converts unfinished actions to `unknown` and never reruns them. Results retry until acknowledged. Retain `/var/lib/proxmanager` across upgrades.
- `succeeded` for reboot means the reboot was **scheduled**, not verified complete. Verify the host returns with a lower uptime.
- A dispatched action older than one hour can be acknowledged as unknown in Activity log after inspecting the host. This releases the queue but does not stop a still-running operation.
- Revocation blocks subsequent agent calls and cancels queued actions. A previously dispatched operation may still run. Treat the host as disconnected until its configuration is replaced.
- Audit entries record lifecycle events but are mutable by a database administrator. External immutable audit retention is not implemented.

## Validate locally

```sh
npm test
python -m unittest discover -s tests -p 'test_*.py'
```

API tests exercise real SQLite queries, signed JWT validation, tenant isolation, role restrictions, cross-origin rejection, command confirmation, dispatch, agent scoping and revocation. Agent and installer tests use mocked execution and isolated paths; tests never install on the local machine, reboot or update a host. Installer checks cover payload integrity, shell syntax, hidden-token configuration, file permissions, policy preservation and refusal to interrupt a running agent. Set `TEST_PYTHON` if Python is not available as `python`.

`npm run dev` starts Wrangler's local Pages environment. Apply D1 migrations with `--local` first. Authentication remains enforced locally: real Access identity requires a protected hostname. There is no development authentication bypass.

## Before a customer production pilot

Complete an independent security review, Linux/Proxmox integration tests on disposable hosts, concurrent load tests on Cloudflare, customer onboarding/offboarding procedures, database backup/restore drills, and Cloudflare rate-limit policies for agent and user APIs. Add signed release distribution, agent version/upgrade policy, credential rotation UX, MFA/session policy and external audit retention. This foundation does not include billing, self-service signup, guest management, graphs/history, SSO per customer or automated cluster maintenance. Use a dedicated test deployment and test hosts first.

References: [Pages D1 bindings](https://developers.cloudflare.com/pages/functions/bindings/), [Cloudflare Access JWT validation](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/), [Proxmox administration guide](https://pve.proxmox.com/pve-docs/pve-admin-guide.html).
