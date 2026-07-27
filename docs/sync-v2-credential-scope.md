# Sync v2 credential scope

Sync v2 uses a machine credential that belongs to one edge identity, one bundle
signing key, and one or more exact project/site pairs. A project-wide wildcard
does not exist.

## Security model

The raw key travels only in the `X-Sync-Key` request header. The hub stores its
SHA-256, never the raw value. Each database credential record contains:

- a generated credential ID;
- the edge ID;
- the SHA-256 of the machine key;
- the expected 16-character Ed25519 public-key fingerprint;
- active state and creation/last-used timestamps.

Scopes live in separate rows keyed by credential ID, project ID, and site ID.
The receiver requires an exact pair match for every descriptor. It performs
that check before item parsing or hub run lookup.

`X-API-Key` remains the user/API authentication header. It cannot authorize a
Sync v2 request. `X-Sync-Key` cannot authorize ordinary user routes. On a
non-hub deployment, the dedicated Sync routes return 404.

## Provision a credential

Provision credentials inside the hub runtime so the plaintext key is shown once
and only its hash reaches the database:

```sh
python -m app.scripts.sync_credentials \
  --edge-id edge-example \
  --signing-key-fingerprint 0123456789abcdef \
  --scope project-example site-north \
  --scope project-example site-south
```

The command refuses to run unless `deployment_role=hub`. Capture the final line
through an approved secret channel, put it in the edge secret store as
`SMART_COMMISSIONING_SYNC_KEY` or `sync_hub_api_key`, then clear the terminal
scrollback used for provisioning. Do not paste it into `.env.example`, release
evidence, tickets, screenshots, or shell history.

The fingerprint must come from the edge's active Sync bundle signing public key.
Using a made-up fingerprint produces a credential that can authenticate its raw
key but cannot authenticate a bundle.

## Add or remove access

Treat a scope change as a credential rotation:

1. Provision a replacement credential with the complete intended scope set.
2. Install the new raw key on the edge through the secret store.
3. Send one controlled item and confirm its `accepted` or `byte_identical`
   receipt.
4. Deactivate the prior credential through the approved database administration
   procedure by setting its `is_active` value false.
5. Record credential IDs and scope pairs in the audit log. Never record raw keys.

Keeping the old credential active after a scope reduction defeats the reduction.
Delete no receipt or delivery-state rows during rotation; they are evidence of
what the hub acknowledged.

## Authorization failures

An unknown, inactive, or missing Sync key gets the same generic HTTP 401. An
authenticated credential with the wrong project/site pair gets an
`unauthorized` item receipt with `acknowledged=false` and `retryable=false`.
The response does not reveal whether the run ID, project, site, digest, filename,
or report already exists for another tenant.

A mixed bundle is evaluated one descriptor at a time. Allowed items can be
accepted while denied items remain pending on the edge.

## Rotation and recovery checklist

- Keep the edge private signing key and Sync machine key in separate secret
  records.
- Back up `sync_credentials` and `sync_credential_scopes` with the hub database.
- After a database restore, verify active credential IDs and scope counts before
  reopening ingest.
- If a raw key may have leaked, deactivate it first, then provision a new one.
- If the edge signing key changes, provision a credential bound to the new
  fingerprint. Changing only the machine key does not update that binding.
- Search logs and release artifacts for a test sentinel after acceptance. A
  clean scan is part of the v0.1.28 gate.
