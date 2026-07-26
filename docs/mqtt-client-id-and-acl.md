# MQTT client IDs and broker ACLs in v0.1.26

Every MQTT connection now gets a client ID derived from four inputs: deployment,
run ID, attempt number, and channel. The final value is `sc-` plus 20 lowercase
base32 characters, exactly 23 ASCII bytes. Separate discovery, capture, validation, and publish
connections therefore cannot evict one another by reusing a fixed client ID.
Client-ID eviction is a miserable way to lose a field capture, so this check is
worth keeping in every broker rollout.

## Deployment identity

Set one stable, non-secret value in `infra/.env`:

```dotenv
SMART_COMMISSIONING_DEPLOYMENT_ID=commissioning-host-01
```

Use ASCII letters, digits, and hyphens. Keep it unchanged when containers are
rebuilt. Portable mode derives its deployment identity from the stable local
installation context.

## Broker policy

Do not allow one literal client ID such as `smart-commissioning-tool`. Allow the
generated prefix or, when the broker supports it, bind authorization to the
authenticated username and topic permissions instead of the full client ID.

Example Mosquitto ACL for an account dedicated to commissioning:

```text
user smart-commissioning
topic read site/+/devices/+/state
topic read site/+/devices/+/metadata
topic read site/+/devices/+/pointset
topic write site/+/devices/+/config
```

Keep the topic scope narrower than the example when the site prefix is known.
The dynamic ID does not widen topic access. Username, TLS client certificate,
and topic ACL checks still apply to every new connection.

If a broker plugin matches client IDs, configure the `sc-*` pattern rather than
a single exact value. Test the rule with two simultaneous
connections. Both must stay connected, and the broker log must show different
ASCII IDs of 23 bytes or fewer.

## Rollout check

1. Apply the ACL change before starting v0.1.26 workers.
2. Start two non-conflicting MQTT runs and confirm neither receives a duplicate
   client-ID disconnect.
3. Start two requests for the same canonical profile. One must be accepted and
   one must return HTTP 409 with the active run ID.
4. Search broker and application logs for usernames and client IDs. Secret
   values, owner tokens, and private-key material must be absent.
