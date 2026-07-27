# Sync v2 plan for v0.1.28

Sync v2 moves from v0.1.27 to v0.1.28. The urgent v0.1.27 release stays focused
on the Windows portable inline-lease failure and its independent executable
validation. Shipping that small cut first is the safer choice than mixing a wire
protocol, credential scope, receipt storage, and database migration into the
heartbeat hotfix.

## v0.1.27 boundary

v0.1.27 keeps the v1 reader and sender compatibility path introduced before the
hotfix. Its evidence rules remain:

- a sync import cannot update a locally sealed terminal result;
- an identical run ID and terminal digest may be acknowledged;
- a different digest for the same run ID stays visible and unsynchronized;
- repeated local report downloads return the exact stored bytes;
- secret values, owner tokens, API credentials, signing private keys, and
  decrypted certificates stay outside sync payloads, receipts, and logs;
- v1 does not transfer the complete frozen report snapshot, signed artifact
  manifest, or content-addressed report bytes.

Operators should preserve the edge artifact as the signed source of record
during mixed v0.1.27 operation. A hub-rendered file has separate provenance.

v0.1.27 adds no sync table, receipt table, credential-scope table, or schema
migration.

## v0.1.28 wire bundle

Each v2 item will carry these immutable fields:

- sealed run ID;
- canonical terminal-result digest;
- frozen execution and report snapshot;
- signed artifact manifest;
- exact content-addressed report bytes;
- filename, media type, byte size, renderer version, and SHA-256;
- artifact origin and signing-key ID;
- supported signature algorithm and signature metadata.

Example documentation and fixtures will use neutral identifiers such as
`run_edge_example`, `demo-project`, and `demo-site`.

## Hub verification and storage

Before acknowledgement, the hub will verify the declared byte size, artifact
SHA-256, signed-manifest digest, and supported manifest signature. It will store
the exact edge bytes without rendering a replacement. A later hub download must
match the edge artifact byte for byte.

An edge credential will be bound to approved project and site pairs. An
out-of-scope item will receive a scoped rejection without another tenant's run,
project, site, digest, filename, or artifact metadata.

Sealed evidence remains write-once on both sides. A matching run ID and digest is
idempotent. A matching run ID with a different digest becomes an explicit
conflict receipt and remains unsynchronized.

## Receipts and retries

The response will return one receipt per submitted item plus the acknowledged run
IDs. Accepted, byte-identical, conflict, unauthorized, malformed, signature
failure, hash failure, size failure, missing artifact, and partial-bundle results
remain distinct.

Only accepted or byte-identical run IDs advance the edge synchronization
watermark. Mixed batches advance those items independently. Interrupted uploads
are safely retryable, and repeating an accepted upload returns an idempotent
receipt without replacing stored bytes.

## Mixed-version operation

A v2 sender will negotiate down to a v1 reader. Downgrade changes transport
capability only; the sealed terminal result, digest, and local report artifact
remain unchanged. The edge keeps the item pending when v1 cannot acknowledge the
complete v2 artifact contract.

v0.1.28 will use an additive migration for receipt, credential-scope, artifact,
or synchronization state required by the final model. Deployment documentation
will cover a pre-migration backup, API and worker ordering, mixed v1/v2 operation,
application rollback with the additive schema retained, and full restore when an
older application cannot read that schema safely.

## Blocking v0.1.28 evidence

The release must cover successful transfer, exact-byte round trip, repeated
download equality, interrupted retry, idempotent repeat, unauthorized project,
unauthorized site, mixed acknowledgements, signature failure, hash failure, size
failure, missing artifact, partial bundle, immutable run-ID conflict,
byte-identical duplicate, v2-to-v1 negotiation, every receipt watermark class,
and secret exclusion from requests, responses, logs, and stored evidence.

Final hosted acceptance must use real API, worker, frontend, Postgres, and Redis
containers from the release SHA. It will transfer an edge artifact to the hub,
download it again, and compare the two files byte for byte before publication.
