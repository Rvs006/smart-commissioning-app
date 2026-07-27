# Sync v2 wire format

Sync v2 transfers sealed evidence from an edge to a hub without rendering a
replacement report. The hub stores the report bytes it received and verifies
them again on every download.

The protocol identifier is `smart-commissioning-sync`, the version is `2.0`,
and the HTTP media type is:

```text
application/vnd.smart-commissioning.sync-v2+zip
```

## HTTP endpoints

An authenticated edge first reads:

```text
GET /api/v1/hub/sync/capabilities
X-Sync-Key: <one-time-provisioned-machine-key>
```

The capability response names versions `2.0` and `1.0`, selects `2.0`, and
publishes the current compressed-size and item-count limits. A v2 bundle is
then sent as the raw request body:

```text
POST /api/v1/hub/sync/v2/ingest
Content-Type: application/vnd.smart-commissioning.sync-v2+zip
X-Sync-Key: <one-time-provisioned-machine-key>
```

`application/octet-stream` is accepted for clients that cannot set the vendor
media type. Ordinary user authentication does not authorize these endpoints.

## ZIP members

Every archive contains one manifest and at least one item:

```text
manifest.json
items/<item_id>.json
artifacts/sha256/<artifact_sha256>    # report items only
```

`item_id` is the first 32 lowercase hexadecimal characters of SHA-256 over
`run_id + NUL + result_sha256`. Report bytes are addressed by their full
lowercase SHA-256. Two reports with the same bytes may share one stored content
object while keeping separate immutable run records.

Writers use a fixed ZIP timestamp of `1980-01-01T00:00:00`, mode `0600`, sorted
member names, and DEFLATE compression. The same sealed input, signing key, edge
identity, and creation time therefore produce the same bundle bytes.

Readers reject duplicate names, encrypted members, absolute paths, backslashes,
`..` traversal, undeclared files, unsupported paths, excessive member counts,
and archives above the configured uncompressed limit. Defaults are 500 items,
20 MiB compressed request bytes, and 200 MiB uncompressed bytes.

## `manifest.json`

The top-level object has no extension fields.

| Field | Type | Meaning |
| --- | --- | --- |
| `protocol` | string | Exactly `smart-commissioning-sync`. |
| `protocol_version` | string | Exactly `2.0`. |
| `bundle_id` | 64-char hex | Stable identity of the canonical unsigned bundle description. |
| `edge_id` | string | Edge identity bound to the machine credential. |
| `created_at` | timestamp | ISO 8601 timestamp with a timezone. |
| `items` | array | Ordered item descriptors. |
| `signature_algorithm` | string | Exactly `ed25519`. |
| `signing_key_id` | string | Fingerprint of `public_key_pem`, also bound to the credential. |
| `public_key_pem` | string | Ed25519 public key used for the bundle signature. |
| `signed_manifest_sha256` | 64-char hex | SHA-256 of the canonical signed body. |
| `signature` | base64 string | Ed25519 signature over the canonical signed body. |

Each item descriptor contains:

| Field | Type | Meaning |
| --- | --- | --- |
| `item_id` | 32-char hex | Stable item identifier. |
| `run_id` | string | Sealed run identifier. |
| `project_id` | string | Project used for scope authorization. |
| `site_id` | string | Site used for scope authorization. |
| `item_member` | string | Exactly `items/<item_id>.json`. |
| `item_sha256` | 64-char hex | SHA-256 of the item member bytes. |
| `result_sha256` | 64-char hex | Canonical terminal-result digest. |
| `artifact_member` | string or null | Exact content-addressed artifact path. |
| `artifact_sha256` | 64-char hex or null | Digest of report bytes. |
| `artifact_size` | integer or null | Exact report byte count. |

The three artifact fields are either all present or all null.

## Item member

An item is a strict JSON object with these fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | Exactly `2.0`. |
| `run` | Terminal run metadata with a restricted parameter set. |
| `result` | Canonical terminal result envelope and payload. |
| `seal` | Immutable context and result digests plus terminal state. |
| `execution_context` | Frozen non-report execution context, otherwise null. |
| `report_snapshot` | Frozen report snapshot, otherwise null. |
| `artifact_manifest` | Signed report artifact manifest, otherwise null. |

A non-report item carries one frozen `execution_context`; its SHA-256 must equal
the seal's `context_sha256`, and its project/site pair must equal the run and
authorized descriptor. A report item carries one `report_snapshot` and one
signed artifact manifest. The canonical snapshot SHA-256 must match both the
seal and the manifest's `snapshot_sha256`.

The result payload is parsed through `TerminalResultV1`. Its calculated digest,
run status, stage, summary, issues, result envelope, and seal must agree. One
disagreement makes that item `malformed`.

The signed artifact manifest is an exact-field object containing report ID,
snapshot SHA-256, filename, media type, byte size, renderer version, artifact
SHA-256, edge-relative source path, origin, signing-key ID, signing time,
algorithm, signature, public key, and signed-body SHA-256. Only Ed25519 is
accepted. The filename must be a basename, not a path.

## Canonical hashes and signatures

Canonical JSON is UTF-8 with keys sorted, no insignificant whitespace, no ASCII
escaping, and no NaN values.

The `bundle_id` is SHA-256 over the canonical manifest with `bundle_id`,
`signature`, and `signed_manifest_sha256` removed. The signed body is the
canonical manifest with `signature` and `signed_manifest_sha256` removed. Its
SHA-256 must equal `signed_manifest_sha256`, and its Ed25519 signature must
verify with `public_key_pem`.

The hub also calculates the public-key fingerprint. All three values must agree:

- the calculated fingerprint;
- `signing_key_id` in the bundle;
- the signing-key fingerprint bound to the authenticated edge credential.

## Verification order

The receiver follows this order:

1. Authenticate the dedicated Sync key.
2. Enforce compressed request size.
3. Parse safe ZIP structure and authenticate the outer manifest.
4. Check the descriptor's exact project/site pair before reading its item or
   looking up a run on the hub.
5. Validate item hash, frozen evidence, terminal result, and seal coherence.
6. Verify the report manifest signature, declared size, declared digest, and
   exact artifact bytes. The signed artifact origin must equal the authenticated
   bundle edge.
7. Insert an absent run and content-addressed artifact, classify an identical
   prior item, or report an immutable conflict.
8. Store and return one durable receipt for each descriptor.

The scope check comes before hub run lookup on purpose. An out-of-scope sender
gets `unauthorized` without learning whether another tenant has that run.

## Receipt response

The hub responds with JSON:

```json
{
  "protocol": "smart-commissioning-sync",
  "protocol_version": "2.0",
  "bundle_id": "<64 lowercase hex characters>",
  "edge_id": "edge-example",
  "receipts": [
    {
      "receipt_id": "<64 lowercase hex characters>",
      "item_id": "<32 lowercase hex characters>",
      "run_id": "run_example",
      "class": "accepted",
      "acknowledged": true,
      "retryable": false
    }
  ],
  "acknowledged_run_ids": ["run_example"],
  "all_acknowledged": true
}
```

Only `accepted` and `byte_identical` set `acknowledged` and appear in
`acknowledged_run_ids`. The sender verifies `protocol_version`, `bundle_id`, and
every receipt schema before it changes a local watermark.

## Secret boundary

Bundle construction rejects raw values under credential, password, token,
private-key, certificate, owner-token, and secret field names. A `secret://`
reference may cross because it names encrypted local state without exposing its
value. Raw and nested ZIP-compatible report bytes are scanned under bounded
member, expansion, and nesting limits. PEM private keys and certificates are
rejected during edge construction and again during hub ingestion.

Requests, receipts, database records, and normal logs never contain the raw
`X-Sync-Key`. Public examples use invented identifiers only.
