# HANDOFF — GameBox Cloud (Cloudflare backend)

**For:** Claude Code
**Date:** 2026-09-13
**Status:** Architecture approved. Ready to build Phase 0 + Phase 1.
**Full spec:** `GameBox Cloud Architecture` PDF (in Alexis's workspace artifacts) — this file distills what you need to implement. Ask Zoey for the PDF if you need the deep rationale.

## 1. What we're building

An **optional, paid** cloud service for GameBox: portable library sync across PCs + encrypted save-game backup. The free local Windows app must keep working exactly as today — no account, no telemetry, no cloud dependency. Cloud is explicit opt-in only.

**Stack (all Cloudflare, free tier to start):**
- **Workers** (TypeScript) — REST API at `api.gamebox.<tbd>` (domain decision pending)
- **D1** — users, devices, token families, sync heads/history, backup manifests, subscriptions, audit
- **R2** (private bucket) — encrypted sync blobs (if > D1 limits) + encrypted save backup chunks. No egress fees.
- **Turnstile** — bot protection on sign-in
- **Transactional email** (MailChannels via Workers, or Resend) — magic links
- **Stripe** (Phase 3) — billing; webhooks update D1 entitlement cache

**Working offer (hypothesis, beta will validate):** $4.99/mo or $39/yr — unlimited PCs (fair use), portable library sync, encrypted save backup with 5 GB quota, 30-day version history, device revocation.

## 2. Owner decisions needed before production (Zoey)

1. **Price/quota:** approve or change $4.99 / $39 / 5 GB / 30-day history.
2. **Recovery posture:** strict E2E (unrecoverable if all devices + recovery key lost) vs service-assisted recovery (weaker privacy). Doc assumes strict E2E.
3. **First save coverage:** manual folders + emulator adapters, or fund a vetted PC-game save manifest catalog first.
4. **Beta size:** invite cap + per-user quota so the free Cloudflare tier has a deliberate boundary.

Defaults assumed: free app stays fully functional; sync is opt-in; Windows only for v1; R2 bucket private, nothing publicly shareable.

## 3. Repository layout to create

```
client/cloud/          # new Python package in this repo
    __init__.py
    cloud_state.py     # portable export/import, allowlist serializer
    auth.py            # device keypair, challenge poll, token storage (DPAPI/CredMan)
    crypto.py          # AES-256-GCM, account data key, key envelopes
    sync.py            # pull/merge/push, conflict retry, offline queue
    backups.py         # save discovery, chunked upload, restore (Phase 2)
worker/
    src/
        index.ts       # router
        routes/auth.ts
        routes/sync.ts
        routes/backups.ts
        routes/billing.ts   # Phase 3
        lib/db.ts      # D1 helpers, tenant isolation
        lib/tokens.ts  # JWT issue/verify, refresh rotation
        lib/ratelimit.ts
    migrations/
        0001_initial.sql
        0002_indexes.sql
    wrangler.toml
docs/
    privacy.md
    threat-model.md
    recovery.md
```

## 4. Phase 0 — Client foundation (do this first)

**Goal:** a portable-state module + secure credential storage + feature flag, with zero cloud calls.

1. Create `client/cloud/cloud_state.py`:
   - `export_portable(library_data, settings) -> dict` — **allowlist serializer**. Include ONLY: stable game identity (store namespace + store key, e.g. `steam:620`; never raw paths), favorite/hidden flags, tags, categories, completion status, score, notes, title override, play sessions (each with a UUID event id), saved views, visual preferences (accent etc.), schema version, account/device ids.
   - **MUST NEVER include:** install paths, folder contents, launch commands/executables, installed/missing status, ROMs/ISOs/binaries, launcher DB/registry data, cached artwork, tokens, keys.
   - Path-derived games (no store key): assign a random portable UUID and keep a local map so a later store match can link them — do not derive identity from the path.
   - `import_portable(doc) -> merge plan` — validate schema version, normalize ordering, stable event IDs.
2. Settings adapter: move tokens/crypto material to **Windows Credential Manager / DPAPI**. Never write tokens to `library.json`, `gamebox-settings.json`, logs, or crash dumps. Ordinary settings stay in the user data dir.
3. Feature flag: all cloud code paths no-op when signed out. App must work fully offline; never block startup on network.
4. **Exit gate:** a portable export of a real library contains no paths, launch commands, binaries, ROM references, cached artwork, or secrets. Write a test that asserts this on a fixture library.

## 5. Phase 1 — Account + metadata sync

### 5a. Worker + D1

`wrangler.toml`: one Worker, one D1 binding, one private R2 binding, Turnstile secret + JWT secret in `wrangler secret`.

**D1 schema (`worker/migrations/0001_initial.sql`):**

```sql
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at INTEGER NOT NULL
);
CREATE TABLE devices (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  name TEXT NOT NULL,
  public_key TEXT NOT NULL,
  platform TEXT NOT NULL DEFAULT 'windows',
  app_version TEXT,
  revoked_at INTEGER,
  last_seen INTEGER NOT NULL
);
CREATE TABLE token_families (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  device_id TEXT NOT NULL REFERENCES devices(id),
  token_hash TEXT NOT NULL UNIQUE,
  expires_at INTEGER NOT NULL,
  revoked_at INTEGER
);
CREATE TABLE key_envelopes (
  user_id TEXT NOT NULL,
  device_id TEXT NOT NULL REFERENCES devices(id),
  wrapped_key TEXT NOT NULL,
  key_version INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (user_id, device_id)
);
CREATE TABLE sync_heads (
  user_id TEXT PRIMARY KEY REFERENCES users(id),
  revision INTEGER NOT NULL,
  ciphertext BLOB NOT NULL,
  etag TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE sync_history (
  user_id TEXT NOT NULL REFERENCES users(id),
  revision INTEGER NOT NULL,
  ciphertext BLOB NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (user_id, revision)
);
CREATE TABLE subscriptions (
  user_id TEXT PRIMARY KEY REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'none',
  period_end INTEGER,
  updated_at INTEGER NOT NULL
);
CREATE TABLE webhook_events (
  provider_id TEXT PRIMARY KEY,
  received_at INTEGER NOT NULL
);
CREATE TABLE audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
-- backup tables land in Phase 2 (see section 6)
```

`0002_indexes.sql`: indexes on `devices(user_id, revoked_at)`, `sync_history(user_id, revision)`, `token_families(device_id)`, `audit_events(user_id, created_at)`, unique `webhook_events(provider_id)`.

**Tenant isolation rule:** `user_id` is derived ONLY from the verified access token — never from request body, query, or object path. Every D1 query includes it. Add a test per route that proves cross-account access fails.

### 5b. Auth — browser-assisted device flow (passwordless)

1. Client generates a device keypair + short-lived enrollment challenge, opens the system browser to the Cloud sign-in page.
2. `POST /auth/start` {email, device_name, device_public_key, turnstile_token} — Turnstile verify + IP/email-hash rate limit. Create challenge (single-use, ~10 min expiry). Generic response either way (no account-enumeration oracle).
3. Worker emails a one-time link (MailChannels/Resend). Clicking approves the named device.
4. Client polls `GET /auth/challenge/:id` with the challenge secret → on approval receives: access token (JWT, 15 min), refresh token (30-day, rotating family), and the account data key wrapped to the device public key.
5. `POST /auth/refresh` rotates; reuse of an old refresh token revokes the whole family (theft detection). `POST /auth/logout` revokes the family. `DELETE /devices/:id` requires step-up approval.
6. Store refresh token + device private key in Credential Manager/DPAPI.

### 5c. Sync protocol — revisions prevent silent overwrites

- `GET /sync?after=N` → latest revision (or 304/no-change) + ETag. `GET /me` → plan, quota, account state. `GET /devices` → trusted device list.
- `PUT /sync` {base_revision, idempotency_key, ciphertext, content_hash} — D1 commits ONLY if `base_revision` still matches head (compare-and-swap, atomic transaction). On mismatch → HTTP 409 with `{head_revision}`; client merges and retries once, then waits with jitter.
- Client flow: export portable → pull → merge locally (deterministic rules below) → compare-and-swap push.
- **Merge rules:** favorite/hidden → latest explicit edit; tags/categories → observed-remove set union; notes/title → latest edit + keep conflict copy (never silently discard text); completion/score → latest explicit edit; play sessions → union by event UUID; preferences → per-key latest edit. Server time orders revisions; field edits carry client timestamp + monotonic device counter.
- **Encryption:** random 256-bit Account Data Key; AES-256-GCM with unique nonce per object. Cloudflare receives ciphertext only. Sync blobs stay under a 1 MB app cap (D1 row limit is 2 MB); larger payloads go to R2.
- **Recovery key:** at setup show a printable recovery key; server stores only a verifier + wrapped key envelope, never the secret. Setup UI must plainly state: lose all devices + recovery key = data unrecoverable. Require recovery confirmation.
- Offline-first: bounded local queue, visible "last synced" time, pause/retry controls, bandwidth cap + metered-network option.

**Exit gate:** two PCs edit the same library offline and converge without losing notes or duplicating playtime. Cloudflare never sees plaintext.

## 6. Phase 2 — Encrypted save backup (beta)

**Hardest product problem is save discovery.** Launch promise: automatic where GameBox has a vetted rule, assisted everywhere else. Do NOT crawl the whole user profile.

1. Discovery order: (a) signed, versioned vetted manifest mapping game identity → save locations; (b) emulator adapters (known save/state/memory-card folders); (c) user-confirmed folder (remembered locally). Never claim universal coverage.
2. Trigger: `playtime.py` game-exit callback → debounce for final writes → snapshot allowlisted files → zip → AES-256-GCM.
3. Upload: `POST /backups/reserve` (quota check in D1 transaction, returns backup ID + scoped upload token) → `PUT /backups/:id/chunks/:n` (32 MB parts stream to R2 at `accounts/{opaque_id}/backups/{backup_id}/part-{000001}.bin` — object names reveal no email/title/path) → `POST /backups/:id/finalize` (hash + manifest verify).
4. D1 tables: `backup_sets(id, user_id, game_ref, state, created_at)`, `backup_chunks(backup_id, part_no, r2_key, bytes, sha256)`.
5. Retention: current version + versions from last 30 days, bounded by quota. Dedupe by plaintext content hash (per-user only, never cross-user). Nightly job deletes abandoned uploads/expired versions.
6. Restore: download → temp folder → verify archive + paths → back up current local save → atomic replace. Block executables, symlinks escaping the root, launcher caches, logs, crash dumps, oversized files. Two devices changed same lineage → keep both, ask user. Never auto-merge binary saves.

**Exit gate:** Cloudflare stores only ciphertext; interrupted uploads resume; restore never writes outside the selected save root.

## 7. Phase 3 — Billing + controlled launch (outline)

Stripe Checkout + Customer Portal. `POST /billing/webhook` with signature verification → idempotent D1 entitlement updates (dedupe on `webhook_events.provider_id`). Entitlement middleware checks D1 cache, never calls Stripe synchronously. Grace: 7 days after failed payment → sync read-only; downloads/deletion always allowed. Cancel: active through period end; keep data 30 days post-expiry with notices, then schedule deletion. Over quota: download/delete OK, no new backups. Never hold restore behind a new payment.
**Exit gate:** payment state changes idempotent; failed payment never blocks export or deletion.

## 8. Phase 4 — Coverage and scale (only on measured need)

Expand signed save-rule catalog; add Queues/Durable Objects ONLY if metrics show a real bottleneck, each with a rollback path.

## 9. Security minimums for launch

- Strict JSON schemas + body size limits on every route; no stack traces / raw provider errors to clients (stable codes: `auth_required`, `device_revoked`, `plan_required`, `quota_exceeded`, `sync_conflict`, `invalid_payload`, `rate_limited`; every error carries a request ID).
- Security headers, narrow CORS; secrets in wrangler secret store, never in git.
- Dependency + secret scanning in CI; D1 backup/export plan; abuse contact + incident runbook; privacy policy + retention schedule; structured logs with redacted payloads; no third-party analytics in the desktop client.
- Every schema/crypto-format change must stay backward-readable for ≥1 client release.

## 10. Test matrix (don't skip)

Unit: allowlist serializer, merge properties, key envelopes, path validation, quota math, token rotation. Property-based: merge idempotent, commutative where intended, never decreases unique play events. Integration: expired tokens, replayed webhooks, interrupted chunks, duplicate finalize. Security: tenant substitution, revoked devices, archive traversal, oversized bodies, lost devices, account deletion. Windows: upgrade/uninstall, Credential Manager access, antivirus interaction, locked save files, long paths.

## 11. What NOT to build

No game binaries/ROMs/ISOs in the cloud. No executable paths. No ads, sponsored ranking, usage-data sale, forced accounts, or launcher interception. No public R2 objects. No Durable Objects / Queues at launch.

---

**Suggested build order:** Phase 0 (client foundation + tests) → Phase 1a (Worker+D1+migrations) → 1b (auth) → 1c (sync) → Phase 2 → Phase 3. Open a PR per phase; do not merge without the exit-gate test passing.
