# Sync v2 operations

This runbook covers online delivery, offline carry, receipts, retry, backup,
migration, rollback, and mixed v1/v2 operation for v0.1.28.

## Before the first transfer

The edge needs `deployment_role=edge` or `standalone`, a stable edge identity,
its Ed25519 private signing key, a hub URL, and the provisioned Sync machine key.
The hub needs `deployment_role=hub`, the v0.1.28 database migration, persistent
artifact storage, and a credential bound to the edge ID, signing-key fingerprint,
and exact project/site pair.

Useful hub limits are:

```text
MAX_SYNC_BUNDLE_BYTES=20971520
MAX_SYNC_UNCOMPRESSED_BYTES=209715200
MAX_SYNC_ITEMS=500
```

Confirm the live values through the authenticated capabilities endpoint before
sending a large batch.

## Online delivery

From the edge backend environment:

```sh
python -m app.scripts.sync --protocol auto --hub-url https://hub.example.invalid
```

`auto` probes `/api/v1/hub/sync/capabilities`. It selects v2 when the hub
advertises `2.0`. Only an explicit 404 or 406 selects the v1 reader. A 401,
timeout, TLS failure, 5xx response, or malformed capability document stops the
command. Authentication and transport failures never cause a quiet downgrade.

Use `--dry-run` to list pending sealed runs without building a bundle or changing
a watermark. Use repeated `--run-id` options for a controlled subset.

## Receipt and watermark behavior

| Receipt class | Acknowledged | Retryable flag | Edge action |
| --- | --- | --- | --- |
| `accepted` | yes | no | Record receipt and advance that run. |
| `byte_identical` | yes | no | Record receipt and advance that run. |
| `conflict` | no | no | Preserve both sides; investigate different sealed evidence. |
| `unauthorized` | no | no | Correct credential scope; do not infer hub contents. |
| `malformed` | no | yes | Repair the exporter or frozen evidence, then retry deliberately. |
| `manifest_signature_failed` | no | yes | Check report signing material and manifest integrity. |
| `artifact_hash_failed` | no | yes | Recover the exact edge artifact; do not regenerate it on the hub. |
| `artifact_size_failed` | no | yes | Recover the exact edge artifact and declared size. |
| `missing_artifact` | no | yes | Restore the edge artifact referenced by the sealed report. |
| `partial_bundle` | no | yes | Resend the complete deterministic bundle. |

The edge applies receipts by item ID and checks the response bundle ID first.
Only accepted or byte-identical run IDs get `synced_at`. In a mixed response,
those rows advance independently and every other row remains pending.

The CLI exits 0 when all v2 items are acknowledged, 1 for a valid mixed or
non-acknowledged response, and 2 for setup, build, negotiation, or transport
failure.

## Interrupted upload and retry

If the sender loses the response, resend the same bundle bytes. The hub compares
the item hash and immutable run ID. A previously accepted item returns
`byte_identical`; it does not replace the run, result, seal, snapshot, manifest,
or artifact.

Do not mark a run by assuming that an HTTP request probably reached the hub. A
receipt is the proof. This is especially easy to get wrong after a proxy timeout
at 59 seconds, when the hub may already have committed the item.

A `conflict` means the same run ID is already sealed with different evidence.
Automatic retry cannot solve it. Keep the edge watermark pending, preserve the
bundle and both audit records, and trace how the duplicate run ID was created.

## Offline carry

Build a v2 carry file on the edge:

```sh
python -m app.scripts.sync --protocol v2 --output runs.scbundle
```

The command never advances a v2 watermark. `--mark-synced` is rejected because
an output file is not a hub receipt.

On the hub, provide the same scoped machine key through the secret environment
and ingest the carried file:

```sh
SMART_COMMISSIONING_SYNC_KEY="$SYNC_KEY_FROM_SECRET_STORE" \
  python -m app.scripts.ingest runs.scbundle
```

The hub prints the normal v2 receipt document and exits 0 only when all items
are acknowledged. Exit 3 means at least one valid item stayed pending. Transfer
the receipt output back through the approved audit process before changing edge
state. The current edge CLI does not import an offline receipt file, so operators
must leave its v2 watermark pending and use the next online v2 exchange to apply
durable per-item proof.

## Mixed-version behavior

- A v0.1.28 edge talks v2 to a v0.1.28 hub after authenticated capability
  negotiation.
- A v0.1.28 edge can send the unchanged v1 format to a genuine v1 reader after
  a 404 or 406 capability response.
- A v0.1.27 edge can keep using the v1 ingest endpoint on a v0.1.28 hub.
- Explicit `--protocol v1` advances only run IDs the v1 response proves inserted
  or byte-identical.
- An automatic v1 fallback leaves the v2 watermark pending because v1 has no
  complete artifact receipt. It advances the legacy watermark only for run IDs
  the v1 response explicitly proves inserted or byte-identical.
- V1 never changes the terminal result, seal, frozen snapshot, or edge report
  bytes. It carries less evidence; it does not rewrite evidence.

## Backup and migration

Quiesce new runs and Sync uploads before changing the schema. Back up one
consistent set:

- the full database, including `sync_credentials`, `sync_credential_scopes`,
  `sync_artifacts`, `sync_receipts`, and `sync_delivery_state`;
- the complete report artifact root, including `sha256/<prefix>/<digest>`;
- runtime state, encrypted secret store, edge identity, and report-signing key.

A complete backup pairs the copied database with its artifact directory. Record
file counts, byte totals, and SHA-256 values for the archive.

v0.1.28 moves Alembic from `f6a7b8c9d0e1` to `a7b8c9d0e1f2` with five additive
tables. Existing run, context, result, seal, report, v1 Sync, secret, and artifact
rows are unchanged. Start the v0.1.28 API first so migration reaches the new
head, then start the matching worker and frontend images.

The detailed commands and stop conditions are in
[`migration-rollback-v0.1.28.md`](migration-rollback-v0.1.28.md).

## Rollback

An older API or worker must not start against the v0.1.28 head because startup
requires an exact Alembic revision. Prefer rolling forward with corrected
v0.1.28 images while retaining Sync evidence.

A schema downgrade drops all five Sync v2 tables. Before approval:

1. Stop edge uploads, worker, frontend, and API.
2. Export hashed credential metadata, scopes, artifact pointers, receipts, and
   delivery watermarks to restricted audit storage.
3. Archive every content-addressed object referenced by `sync_artifacts`.
4. Test the database and artifact restore in an isolated deployment.
5. Downgrade with the v0.1.28 API image to `f6a7b8c9d0e1`.
6. Deploy the exact prior API, worker, and frontend versions together.

After either upgrade or rollback, compare run/result/seal counts, download one
known report twice, and hash both files. Reopen uploads only after the counts and
bytes match the recorded boundary.

## Operational checks

- Confirm capabilities report version `2.0` and the intended limits.
- Send one authorized report and require `accepted`.
- Resend the same bytes and require `byte_identical`.
- Download the hub report twice and compare both SHA-256 values with the edge.
- Send a controlled out-of-scope descriptor and require a generic
  `unauthorized` receipt.
- Send one allowed and one denied item together; require only the allowed run ID
  in `acknowledged_run_ids`.
- Scan bundle bytes, response text, persisted evidence, and logs for the test
  secret sentinel.
- Keep the bundle, redacted receipt document, image digests, database revision,
  and hash results with the release evidence.
