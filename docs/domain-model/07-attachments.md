# CMMS Domain Model — 07. Attachments (Media / Photo Attachments)

> Deliverable: the media slice (#7). Sources: `data/media/inventory/` (local staging area for the spare-part photos exported from eMaint) + the conventions in `data/media/README.md`.
> Status: **v1.0 — slice live (migration 0010)**. Binaries live in Cloudflare R2; PostgreSQL stores pointers + audit only (ADR-019).
> Design principles: polymorphic owner (inventory_item / work_order / asset), one owner may have many attachments (1:N), content-addressed key (idempotent), a single write path through `AttachmentService`, full audit columns, private R2 bucket + short-lived presigned URLs.
> **This slice does not touch MES (guardrail #9)** — it is pure photo management, unrelated to B2MML / DMZ FTP.

---

## 0. Module scope

Attachment is a **cross-entity polymorphic media pointer layer**: the binaries for spare-part / work-order / asset photos (and, later, PDFs) live in object storage (R2), while PostgreSQL keeps only `owner_type` + `owner_id` (a soft reference), the R2 coordinates, a content fingerprint, and audit columns. It replaces eMaint's `inventory_item.photo_ref` (an internal document id embedded in an `<img src=...>` tag) with an index we build ourselves from the filename convention "filename = item_code"; `photo_ref` is retired to legacy provenance.

**Why binaries do not go into PostgreSQL or git**: they would bloat the database / repository and slow backups (`pg_dump`). Object storage is the correct home for photos.

### 0.1 Verified state of the source data (`data/media/inventory/`)

| Item | Value | Note |
|---|---|---|
| Total media files | ~1,000, order of magnitude | mostly jpg, a few png |
| Filenames with a caption (`<code> <caption>.<ext>`) | majority | contains a space; everything after the first space is the caption |
| Filenames without a caption (`<code>.<ext>`) | a substantial minority | the loader must handle these with `caption=None` |
| Distinct owners (uppercase leading token) | close to the file count | most owners have exactly one image |
| Owners with multiple images (1:N, empirical) | a handful | **proves one owner can have many attachments** |
| Unparseable | 0 | every file has an extension and a non-empty leading token |

> ⚠ **The count is not stable, and the design does not depend on it**: the staging directory sits on a synced drive on the operator workstation, and a few filenames plus their absolute path exceed the host filesystem's path-length limit, so the locally enumerated file count drifts slightly with sync state. **This does not affect the correctness of the slice**: the loader scans whatever exists at deploy time, skips unmatched / unparseable files gracefully, and is idempotent. The design rests on the **structurally stable facts** (the filename convention, the caption-less subset, the existence of multi-image owners) — and the existence of multi-image owners (whether three of them or seven) is what proves `(owner_type, owner_id)` **cannot** be a unique key.

---

## 1. Entity: Attachment (+ AttachmentOwnerType lookup)

### 1.1 AttachmentOwnerType (fixed lookup, 3 values seeded by the migration)

| code | `owner_id` points at | Notes |
|---|---|---|
| `inventory_item` | `inventory_item.item_code` | spare-part / consumable photos (the only owner type with a data source in this slice) |
| `work_order` | `work_order.work_order_no` (text) | work-order photos / documents (future) |
| `asset` | `asset.asset_id` (EID) | equipment master photos (future) |

### 1.2 Attachment table

| Target column | Type | Required | Notes |
|---|---|---|---|
| `id` | bigint | ✅ PK | auto-increment |
| `owner_type` | string | ✅ FK → `attachment_owner_type.code` | polymorphic discriminator |
| `owner_id` | string | ✅ | **polymorphic soft reference** (no hard FK); canonicalised to uppercase (aligned with item_code / EID) |
| `r2_bucket` | string | ✅ | media bucket (`cmms-media`); private |
| `r2_key` | string | ✅ | `<prefix>/<OWNER_ID>/<sha8>.<ext>` (content-addressed) |
| `content_type` | string | ✅ | `image/jpeg` / `image/png` / … |
| `caption` | text | ⬜ | the description after the space in the filename (null if none) |
| `byte_size` | bigint | ✅ | binary size |
| `sha256` | string(64) | ✅ | content identity (full hex); the key embeds its first 8 hex chars |
| `original_filename` | string | ⬜ | source filename (provenance) |
| `is_deleted` | bool | ✅ (default false) | soft delete; the R2 object is retained for audit / undelete |
| `created_at/by`, `updated_at/by`, `source_actor`, `proposed_by`, `confirmed_by` | — | — | `AuditMixin` (ADR-005/016) |

---

## 2. Relationships and uniqueness

- **`owner_id` is a text soft reference with no hard FK**: depending on `owner_type` it points at a different master table (`inventory_item` / `work_order` / `asset`), which a single FK cannot express. Existence is enforced by the service's `_owner_exists` check (orphan attachments are rejected). This follows existing soft-reference precedent in the repo (`inventory_item.supplier` text, `asset.asset_subtype` text).
- **Unique key `(owner_type, owner_id, r2_key)`** (`uq_attachment_owner_key`): the requirement is literally "prevent the same key under the same owner". Because `r2_key` embeds `sha8`, the same content under the same owner is blocked as a duplicate (idempotent), while different content under the same owner creates a new row (supporting 1:N). **It is deliberately not a partial index**: the key is retained even for soft-deleted rows, so that deleting and re-adding cannot orphan an object; re-adding the same content hits the existing (possibly deleted) row.
- **Hot-path index `(owner_type, owner_id)`** (`ix_attachment_owner`): used by `list_attachments`.

---

## 3. R2 key rules — content-addressed

```
<owner_prefix>/<OWNER_ID>/<sha8>.<ext>
inventory/EC000001/9f86d081.jpg
```

- `owner_prefix`: `inventory_item` → `inventory`, `work_order` → `work_order`, `asset` → `asset` (aligned with the `data/media` subfolders).
- `OWNER_ID` is uppercase; `sha8` = the first 8 hex chars of the sha256; `ext` is lowercase.
- **Content-addressed semantics**: identical bytes → identical key → re-upload is idempotent and duplicates are collapsed per owner.
- **⚠ Known sharp edge**: the key only uses the first 32 bits of the sha256. If two *different* images **under the same owner** collide in their first 32 bits, the second one is silently short-circuited by the unique key + `on_conflict_do_nothing` (the full sha256 is still stored in the `sha256` column, but it does not participate in the key). With at most a handful of images per owner the probability is negligible; if image counts per owner grow substantially, simply take a longer hash prefix.

---

## 4. Governed operations (`AttachmentService` — the single write path)

| Operation | Kind | Notes |
|---|---|---|
| `get_attachment(id)` | read | single record |
| `list_attachments(owner_type, owner_id, *, include_deleted, limit, offset)` | read | excludes soft-deleted rows by default; `owner_id` is uppercased automatically |
| `presigned_url(att, *, ttl_seconds?)` | read | delegates to the backend to sign a short-lived GET URL; the bucket stays private |
| `add_attachment(*, owner_type, owner_id, data, ext, content_type, actor, caption?, original_filename?, idempotency_key?)` → `(Attachment, created)` | write | validate owner → upload to R2 → record the pointer; **idempotent** |
| `soft_delete_attachment(id, actor, *, reason?)` | write | sets `is_deleted=true` + audit; repeated deletes are idempotent; the R2 object is retained |

**Write flow (`add_attachment`)**: ① validate `owner_type` and that the owner exists → ② compute sha + key → ③ idempotency short-circuit (if the same key already exists under the same owner, return `created=False` without re-uploading) → ④ upload to R2 (**outside the DB transaction** — never hold network I/O inside a transaction) → ⑤ `async with self.write(actor)` writes the pointer via `pg_insert(...).on_conflict_do_nothing(...).returning(id)` (which also guards the concurrent-write race).

**Idempotency (guardrail #4)**: content-addressed key + unique key + `on_conflict_do_nothing` + pre-upload short-circuit. The `idempotency_key` parameter is reserved for the agent write path (consistent with ADR-006); for the loader path, the content-addressed key is already sufficient.

**Audit (guardrail #3)**: the 7 `AuditMixin` columns are populated by the service; the loader runs as `Actor.human("migration")` (→ `human:migration`).

---

## 5. Storage abstraction + ADR-019 (new app → R2 coupling)

`src/cmms/storage.py` (top-level, alongside `audit.py` / `db.py` / `config.py`):

- **`StorageBackend`** (Protocol): `put_object` / `presigned_get_url` / `delete_object` / `object_exists`.
- **`R2StorageBackend`**: a boto3 S3 client with `endpoint_url=R2` (the same S3-compatible usage as `infra/backup`, but **in-process**). `presigned_get_url` is a pure local SigV4 signing operation (zero I/O); `put`/`delete` are wrapped in `asyncio.to_thread` (the synchronous boto3 client must not block the event loop). boto3 is imported lazily, so deployments that do not use the media slice pay nothing for it.
- **`InMemoryStorageBackend`**: stores bytes in a dict and returns `memory://…` presigned URLs. **It ships with the app** (not test-only), so the loader and the tests run locally and in CI without R2.
- **`get_storage_backend()`**: singleton; if all three R2 credentials are present → R2, otherwise → InMemory.

> **★ New architectural coupling**: the previous posture was "the app process never touches R2; R2 only appears in the backup shell script". This slice is the **first time the app process touches R2** (upload + presign). Decisions: ① provision a **dedicated media bucket (`cmms-media`) with its own least-privilege token (`cmms-media-rw`)** — the backup token `db-backup-rw` **must not be reused**; ② accept boto3 as a dependency (presigning must be done in-process by a long-running server; shelling out to `aws presign` per request would fork a subprocess and is unacceptable); ③ keep the bucket private and only ever hand out short-lived URLs. See `infra/secrets-manifest.md`.

---

## 6. Loader (`data/media/inventory/` → R2 → pointers)

- Preload the set of `inventory_item.item_code` values → split files into matched / unmatched; **unmatched files are logged, never fatal** (count + samples).
- For each matched file, read the bytes and pass them to the service's `add_attachment` (idempotent).
- **Multiple images**: several files under one owner → several rows (owners are not de-duplicated).
- **★ Deliberate deviation in the transaction model**: the other loaders wrap the whole batch in a single `service.write()`; this loader uses **one transaction per file** (each file involves an R2 upload, and network I/O must not happen inside a DB transaction; per-file transactions also make an interrupted run resumable).
- **Prerequisite**: inventory must already be loaded (for owner existence + matched/unmatched classification).
- **★ Path-length limit on the staging workstation**: for a handful of very long filenames the Windows path limit makes `is_file()` return False → that file is skipped when the loader runs locally; the deployment target (Linux) has no such limit and loads them normally.

---

## 7. Target schema (migration 0010)

```sql
CREATE TABLE attachment_owner_type (
    code  text PRIMARY KEY,          -- inventory_item / work_order / asset (3 seeded values)
    label text NOT NULL
);

CREATE TABLE attachment (
    id                bigint GENERATED ... PRIMARY KEY,
    owner_type        text NOT NULL REFERENCES attachment_owner_type(code),
    owner_id          text NOT NULL,          -- polymorphic soft reference (no hard FK)
    r2_bucket         text NOT NULL,
    r2_key            text NOT NULL,          -- <prefix>/<OWNER_ID>/<sha8>.<ext>
    content_type      text NOT NULL,
    caption           text,
    byte_size         bigint NOT NULL,
    sha256            varchar(64) NOT NULL,
    original_filename text,
    is_deleted        boolean NOT NULL DEFAULT false,
    -- AuditMixin: created_at/by, updated_at/by, source_actor, proposed_by, confirmed_by
    ...
);

CREATE UNIQUE INDEX uq_attachment_owner_key ON attachment (owner_type, owner_id, r2_key);
CREATE INDEX        ix_attachment_owner     ON attachment (owner_type, owner_id);
```

---

## 8. External contract (API / MCP)

- **Read-only first**: writes (uploads) initially go only through the loader / CLI (too risky to expose up front); the API and MCP expose only list / get, returning `AttachmentWithUrl` (metadata + a short-lived presigned `url` + `url_expires_in`).
- **Never leak** `r2_bucket` / `r2_key` (internal coordinates) — externally, only the presigned URL is handed out.
- API routes: `GET /attachments?owner_type=&owner_id=`, `GET /attachments/{id}`.
- MCP tool: `get_attachments(owner_type, owner_id, limit)` (a domain operation; reads are open).

> **Impact on the downstream analytics consumer = none**: Attachment is an entirely **new** read surface; the downstream consumer does not consume any attachment contract today, and no already-consumed shape was changed. When it comes to bind images, it can pull and read this interface itself.

---

## 9. Provisioning decisions (non-blocking for the design)

1. **R2 media bucket + token**: a new `cmms-media` bucket and a new S3 token `cmms-media-rw` (scoped to that bucket only); **do not reuse** `db-backup-rw`. Set the `CMMS_R2_*` variables (local `.env` / cloud `flyctl secrets set`). If unset → fall back to InMemory.
2. **Formalise the app → R2 coupling (ADR-019)**: confirm that "the app process touches R2 media" is acceptable; boto3 becomes a dependency.
3. **`owner_id` case canonicalisation**: this slice always applies `.upper()` (aligned with all-uppercase item codes and EIDs). If a future `work_order` or `asset` PK is not all-uppercase, the normalisation rule will need to branch per `owner_type`.
