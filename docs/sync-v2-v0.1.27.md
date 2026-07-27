# Historical Sync v2 plan for v0.1.27

Status: superseded by `docs/sync-v2-v0.1.28.md`. v0.1.27 was narrowed to the
urgent Windows portable inline-heartbeat fix. The v0.1.26 compatibility boundary
below remains accurate; the full protocol, storage, credential-scope, and receipt
work now belongs to v0.1.28.

Full immutable evidence synchronization is deferred from v0.1.26. The current
v1 wire format remains readable, which avoids breaking an existing edge and hub
pair during the reliability rollout.

I prefer one visible unsynchronized conflict to a successful badge backed by
different report bytes.

## v0.1.26 boundary

- A sync import cannot update a locally sealed terminal result.
- An identical run ID and digest is safe to acknowledge.
- A different digest for the same run ID remains unsynchronized and visible for
  investigation.
- Secret values, owner tokens, and decrypted certificates remain excluded from
  bundles and responses.
- v1 does not transfer the full report snapshot, signed manifest, and exact
  artifact bytes. Operators must not treat a hub-regenerated report as the same
  signed edge artifact.

## Original acceptance plan, moved to v0.1.28

Sync v2 in v0.1.28 will transfer the sealed run digest, frozen report snapshot,
signed manifest, and exact content-addressed artifact bytes. The hub will bind
each edge credential to allowed project and site pairs, return acknowledged run
IDs, and store per-item receipts. The edge will mark only acknowledged or
byte-identical IDs as synchronized.

Required tests cover interrupted uploads, repeated uploads, out-of-scope
credentials, mixed acknowledgements, manifest signature failure, artifact hash
failure, and immutable ID conflicts. A v2 sender must still negotiate down to
the compatible v1 reader without changing terminal data.
