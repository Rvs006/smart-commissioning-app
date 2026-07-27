# Inline execution heartbeat in v0.1.27

The heartbeat belongs at the ownership boundary. A network packet, progress bar,
or database write can show activity, but none of those events proves that the
executor still owns the run.

## Failure diagnosed from field evidence

Windows portable forces inline execution so it can run without Redis. In
v0.1.26, `RunService.claim_owned_run` created a fixed 60-second lease and the
asynchronous dispatcher started the processor without a renewal thread.
`backend/app/main.py` runs lifecycle recovery every five seconds. The resulting
60-to-65-second failure window matches the observed 1 minute 3 second terminal
failure.

The relevant v0.1.26 behavior was split across these boundaries:

- `backend/app/services/run_service.py` claimed the 60-second lease;
- `backend/app/services/run_dispatch.py` started asynchronous inline work;
- `backend/app/main.py` recovered expired leases every five seconds;
- `core/smart_commissioning_core/db/run_lifecycle.py` fenced the owner and sealed
  a genuine expired run as failed;
- `worker/app/tasks.py` already renewed hosted-worker leases every 15 seconds.

Progress updates write `updated_at`; lease recovery reads `lease_expires_at`.
Keeping those signals separate is intentional.

## v0.1.27 ownership sequence

1. The dispatcher claims the run with an owner token, attempt, and lease.
2. It starts `InlineRunHeartbeat` immediately after a successful claim.
3. The heartbeat protects dispatch publication, frozen-context lookup, secret
   reference resolution, and processor execution.
4. The processor receives an `OwnedRunStore`, so every progress and terminal
   write remains fenced by the original owner token and an unexpired lease.
5. Terminal finalization commits at most one immutable outcome.
6. The guard signals the heartbeat to stop and joins its thread after the
   processor's finalization path returns.

Asynchronous mode returns the accepted run ID after the executor thread starts.
Synchronous mode blocks its caller in the same guarded function while a separate
heartbeat thread renews ownership. The Stop run path keeps its existing
cooperative cancellation behavior.

## Timing policy

The release defaults are:

| Setting | Default | Constraint |
| --- | ---: | --- |
| Run lease | 60 seconds | 15 to 300 seconds |
| Heartbeat interval | 15 seconds | At least 1 second and no more than one third of the lease |
| Lifecycle recovery pass | 5 seconds | Preserved from v0.1.26 |

The lease remains short enough for real crash recovery. A nine-hour or 48-hour
lease would hide dead owners for an unacceptable period, so capture duration
never determines lease duration.

Tests may inject shorter intervals for deterministic coverage. Production and
release defaults stay at 60 and 15 seconds.

## Renewal outcomes

### Renewal succeeds

The lifecycle store extends `lease_expires_at` for the same run, owner token, and
attempt. A maintenance pass after the original boundary sees a live owner and
leaves the run active.

Claim, heartbeat, progress, and first finalization sample their validity time
after acquiring the lifecycle row lock. A database write wait cannot consume a
new owner's lease or let an old owner cross its expiry deadline with a stale
pre-wait timestamp.

### Renewal returns `False`

The database no longer recognizes the original ownership tuple, or the lease
expired before the renewal began. The heartbeat marks ownership lost and stops.
The stale `OwnedRunStore` rejects later progress and terminal writes, so a
recovered result cannot be replaced.

A heartbeat can race with normal finalization after the terminal transaction has
already cleared the lease. The guard treats an existing non-conflicting terminal
outcome as normal completion rather than a false ownership-loss warning.

### Renewal raises a transient exception

The heartbeat logs the run ID and exception class, then retries with bounded
timing while tracking the last successful renewal window. A brief SQLite lock
gets another attempt before the lease boundary. Retry pacing stays bounded so a
database outage does not become a write storm.

Exception text and tracebacks are omitted from heartbeat warnings because a
database connection error can contain a local path, username, or credential.

## Cleanup contract

The heartbeat stops and joins after each of these paths:

- successful terminal finalization;
- normal processor failure;
- cooperative cancellation;
- frozen-context or secret-reference resolution failure;
- an unexpected processor exception;
- heartbeat-confirmed ownership loss;
- executor-thread startup failure after the heartbeat has started.

The join allowance covers SQLite's configured busy timeout plus a small margin.
A thread that remains alive is logged as an error and blocks release acceptance.

## Traffic independence

MQTT traffic does not drive lease renewal. A broker may publish nothing during a
valid capture window, and an inline IP or BACnet run may spend time inside one
slow operation. The heartbeat also ignores `updated_at`, issue writes, device
writes, point writes, topic writes, and progress percentage.

This independence preserves two properties at once: a quiet healthy owner stays
alive, while a crashed owner eventually crosses its final lease boundary and is
recovered by the existing maintenance service.

## Required evidence

Release validation uses controlled clocks and events instead of waiting a minute
or nine hours. It proves renewal across the original boundary, expiry after the
last renewal, multiple asynchronous renewals, synchronous renewal, quiet-broker
behavior, cancellation, ownership loss, stale-write rejection, database-lock
waits that cross expiry, and thread cleanup. The production dispatcher and real
lifecycle store must appear in at least one shortened-interval integration test.

v0.1.27 deliberately keeps this guard in the generic backend inline path. The
v0.1.28 lifecycle work will extract shared heartbeat infrastructure for backend
inline and hosted worker execution after the independently validated Windows
hotfix is published.
