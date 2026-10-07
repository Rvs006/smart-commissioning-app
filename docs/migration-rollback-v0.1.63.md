# v0.1.63 migration and rollback

v0.1.63 retains the Alembic head `a6b7c8d9e0f1` and adds no database
migration. It adds the "Ignore Payloads Outside Applicability" setting to the
Configuration `validation` section and changes release identity. No existing
stored row, seal, evidence contract or report is rewritten. A configuration
saved on v0.1.63 carries the new key (Disabled unless changed), and a UDMI run
started with it Enabled records `ignore_unapproved_payloads` in its frozen run
parameters. The retained Sync v2 immutable-evidence head `a7b8c9d0e1f2`,
`sync_credentials`, and `sync_delivery_state` are unchanged. IP, BACnet, MQTT,
UDMI, report, evidence, authorization, and Nmap policy data are unchanged.

Before upgrading, finish or stop active runs and back up the database,
evidence, reports, encrypted configuration, and exact artifact hashes. Deploy
API, worker, and frontend from the same v0.1.63 release, then confirm health,
the visible version, one native scanner run, worker heartbeat,
one report download, and the evidence manifest.

To roll back to v0.1.62, stop v0.1.63 after active work ends and restore the
recorded v0.1.62 EXE or immutable image digests. No Alembic downgrade is
required because both releases use `a6b7c8d9e0f1`. Mixed-version operation is
temporary recovery work, not an accepted steady state. Do not run mixed
v0.1.62 and v0.1.63 API or worker processes. v0.1.62 ignores the saved
"Ignore Payloads Outside Applicability" key, so its UDMI runs flag payload types outside
a row's Payload applicability again, and its next Configuration save drops the
key. Runs completed on v0.1.63 keep their frozen verdicts.

A rollback to v0.1.27 or earlier requires the documented downgrade to
`f6a7b8c9d0e1` after exporting Sync v2 receipts and artifacts.
