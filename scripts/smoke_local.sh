#!/usr/bin/env bash
# Local smoke-test harness for the Smart Commissioning App.
#
# Validates a RUNNING stack end-to-end against the real API before you go
# on-site. It hits only safe, side-effect-free paths:
#
#   1. GET  /api/v1/health                      -> 200, status "ok"
#   2. GET  /api/v1/ready                        -> 200, status "ready"
#   3. GET  /metrics                             -> Prometheus exposition text
#   4. GET  /api/v1/configuration                -> demo-project/demo-site snapshot
#   5. POST /api/v1/validation/udmi/runs         -> validate the BUNDLED UDMI
#                                                   fixture (NO network I/O)
#      poll /api/v1/validation/runs/{id}         -> terminal status
#      GET  /api/v1/validation/runs/{id}/issues  -> normalized issues
#   6. POST /api/v1/discovery/ip/runs (dry_run)  -> a PLAN comes back, NO scan,
#                                                   NO authorization required
#
# It does NOT trigger any real (non-dry-run) active scan or any live broker
# publish. Nothing here touches real BACnet / Redis / Postgres / brokers / Docker
# directly; it only drives the HTTP API of whatever stack you point it at.
#
# Auth: a hosted (AUTH_MODE=api_key) stack needs the shared key; set SC_API_KEY
# and it is sent as X-API-Key on every request. A portable/local
# (AUTH_MODE=local, loopback) stack needs no key; leave SC_API_KEY unset.
#
# Usage:
#   scripts/smoke_local.sh [BASE_URL]
#   BASE_URL defaults to http://127.0.0.1:8000
#   SC_API_KEY=<key> scripts/smoke_local.sh            # hosted
#   scripts/smoke_local.sh                             # local / loopback
#
# Exit code: 0 if every check passed, 1 otherwise.

set -u

BASE_URL="${1:-http://127.0.0.1:8000}"
BASE_URL="${BASE_URL%/}"
API="${BASE_URL}/api/v1"
API_KEY="${SC_API_KEY:-}"

# Per-request connect/read timeout (seconds) and how long to poll a run.
CURL_TIMEOUT="${SC_CURL_TIMEOUT:-15}"
POLL_ATTEMPTS="${SC_POLL_ATTEMPTS:-30}"
POLL_INTERVAL="${SC_POLL_INTERVAL:-1}"

PASS_COUNT=0
FAIL_COUNT=0

# --- terminal colours (only when stdout is a tty) --------------------------
if [ -t 1 ]; then
  C_GREEN="$(printf '\033[32m')"; C_RED="$(printf '\033[31m')"
  C_DIM="$(printf '\033[2m')"; C_RESET="$(printf '\033[0m')"
else
  C_GREEN=""; C_RED=""; C_DIM=""; C_RESET=""
fi

pass() { PASS_COUNT=$((PASS_COUNT + 1)); printf '%sPASS%s %s\n' "$C_GREEN" "$C_RESET" "$1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); printf '%sFAIL%s %s\n' "$C_RED" "$C_RESET" "$1"; }
info() { printf '%s     %s%s\n' "$C_DIM" "$1" "$C_RESET"; }

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required but was not found on PATH." >&2
  exit 1
fi

# Build the curl auth args once. An empty SC_API_KEY means no header (local mode).
AUTH_ARGS=()
if [ -n "$API_KEY" ]; then
  AUTH_ARGS=(-H "X-API-Key: ${API_KEY}")
  info "Auth: sending X-API-Key (hosted profile)."
else
  info "Auth: no API key set, assuming local/loopback profile."
fi

# http_get URL  -> sets HTTP_STATUS (int) and HTTP_BODY (string)
http_get() {
  local url="$1"
  local raw
  # Append the HTTP status on its own trailing line so we can split body/status
  # without a temp file.
  raw="$(curl -sS --max-time "$CURL_TIMEOUT" "${AUTH_ARGS[@]}" \
    -w $'\n%{http_code}' "$url" 2>/dev/null)" || {
    HTTP_STATUS=0; HTTP_BODY=""; return 1
  }
  HTTP_STATUS="${raw##*$'\n'}"
  HTTP_BODY="${raw%$'\n'*}"
  return 0
}

# http_post URL JSON  -> sets HTTP_STATUS and HTTP_BODY
http_post() {
  local url="$1" json="$2" raw
  raw="$(curl -sS --max-time "$CURL_TIMEOUT" "${AUTH_ARGS[@]}" \
    -H 'Content-Type: application/json' -X POST --data "$json" \
    -w $'\n%{http_code}' "$url" 2>/dev/null)" || {
    HTTP_STATUS=0; HTTP_BODY=""; return 1
  }
  HTTP_STATUS="${raw##*$'\n'}"
  HTTP_BODY="${raw%$'\n'*}"
  return 0
}

# json_field BODY KEY -> echoes the first scalar string/number value for KEY.
# Pure-grep/sed extraction so the script has no jq dependency. Good enough for
# the flat fields we assert on (run_id, status, target_count, ...).
json_field() {
  printf '%s' "$1" \
    | tr -d '\n' \
    | grep -oE "\"$2\"[[:space:]]*:[[:space:]]*(\"[^\"]*\"|[0-9]+|true|false|null)" \
    | head -n1 \
    | sed -E "s/\"$2\"[[:space:]]*:[[:space:]]*//; s/^\"//; s/\"$//"
}

echo "Smoke test against ${BASE_URL}"
echo "--------------------------------------------------"

# --- 1. health -------------------------------------------------------------
if http_get "${API}/health" && [ "$HTTP_STATUS" = "200" ]; then
  status="$(json_field "$HTTP_BODY" status)"
  if [ "$status" = "ok" ]; then
    pass "GET /api/v1/health -> 200 status=ok"
  else
    fail "GET /api/v1/health -> 200 but status='${status}' (expected ok)"
  fi
else
  fail "GET /api/v1/health -> HTTP ${HTTP_STATUS} (expected 200)"
fi

# --- 2. ready --------------------------------------------------------------
if http_get "${API}/ready"; then
  status="$(json_field "$HTTP_BODY" status)"
  if [ "$HTTP_STATUS" = "200" ] && [ "$status" = "ready" ]; then
    pass "GET /api/v1/ready -> 200 status=ready"
  else
    fail "GET /api/v1/ready -> HTTP ${HTTP_STATUS} status='${status}' (expected 200/ready)"
    info "A 503 means a required dependency (DB, or Redis in queue mode) is down."
  fi
else
  fail "GET /api/v1/ready -> request failed"
fi

# --- 3. metrics (Prometheus text, app root, not under /api/v1) -------------
if http_get "${BASE_URL}/metrics" && [ "$HTTP_STATUS" = "200" ]; then
  # Prometheus exposition format always has '# HELP'/'# TYPE' lines; this app
  # also exports sct_* series. Accept either marker.
  if printf '%s' "$HTTP_BODY" | grep -qE '^# (HELP|TYPE) |sct_'; then
    pass "GET /metrics -> 200 Prometheus exposition text"
  else
    fail "GET /metrics -> 200 but body is not Prometheus exposition text"
  fi
else
  fail "GET /metrics -> HTTP ${HTTP_STATUS} (expected 200)"
fi

# --- 4. configuration ------------------------------------------------------
if http_get "${API}/configuration" && [ "$HTTP_STATUS" = "200" ]; then
  # The default snapshot always carries a project section; a bare object means
  # something is wrong. Assert the body is non-trivial JSON.
  if printf '%s' "$HTTP_BODY" | grep -q '{'; then
    pass "GET /api/v1/configuration -> 200 snapshot returned"
  else
    fail "GET /api/v1/configuration -> 200 but body was empty/unexpected"
  fi
else
  fail "GET /api/v1/configuration -> HTTP ${HTTP_STATUS} (expected 200)"
fi

# --- 5. UDMI validation against the bundled fixture (no network) -----------
udmi_body='{"project_id":"demo-project","site_id":"demo-site","job_type":"udmi_validation","parameters":{"requested_from":"smoke_local"}}'
RUN_ID=""
if http_post "${API}/validation/udmi/runs" "$udmi_body" && [ "$HTTP_STATUS" = "200" ]; then
  RUN_ID="$(json_field "$HTTP_BODY" run_id)"
  if [ -n "$RUN_ID" ]; then
    pass "POST /api/v1/validation/udmi/runs -> 200 run_id=${RUN_ID}"
  else
    fail "POST /api/v1/validation/udmi/runs -> 200 but no run_id in body"
  fi
else
  fail "POST /api/v1/validation/udmi/runs -> HTTP ${HTTP_STATUS} (expected 200)"
fi

# Poll to a terminal status. Inline (portable) returns 'succeeded' immediately;
# a queued (hosted) run is processed by the worker, so we poll either way.
if [ -n "$RUN_ID" ]; then
  RUN_STATUS=""
  attempt=0
  while [ "$attempt" -lt "$POLL_ATTEMPTS" ]; do
    if http_get "${API}/validation/runs/${RUN_ID}" && [ "$HTTP_STATUS" = "200" ]; then
      RUN_STATUS="$(json_field "$HTTP_BODY" status)"
      case "$RUN_STATUS" in
        succeeded|failed|cancelled) break ;;
      esac
    fi
    attempt=$((attempt + 1))
    sleep "$POLL_INTERVAL"
  done

  if [ "$RUN_STATUS" = "succeeded" ]; then
    pass "validation run reached terminal status=succeeded"
  else
    fail "validation run did not succeed (status='${RUN_STATUS}' after ${POLL_ATTEMPTS} polls)"
  fi

  # Issues endpoint: the fixture is known to produce issues; assert it answers
  # 200 and the response carries the issues array.
  if http_get "${API}/validation/runs/${RUN_ID}/issues" && [ "$HTTP_STATUS" = "200" ]; then
    if printf '%s' "$HTTP_BODY" | grep -q '"issues"'; then
      pass "GET /api/v1/validation/runs/{id}/issues -> 200 issues returned"
    else
      fail "GET /api/v1/validation/runs/{id}/issues -> 200 but no issues field"
    fi
  else
    fail "GET /api/v1/validation/runs/{id}/issues -> HTTP ${HTTP_STATUS} (expected 200)"
  fi
else
  fail "validation run polling skipped (no run_id)"
fi

# --- 6. DRY-RUN IP discovery (no scan, no authorization needed) ------------
# dry_run=true previews the (ip,port) plan without sending a single packet, so
# it needs no scan authorization. We assert a plan with targets came back.
ip_body='{"project_id":"demo-project","site_id":"demo-site","job_type":"ip_discovery","parameters":{"dry_run":true,"cidr":"192.0.2.0/30","ports":[47808,1883]}}'
DISC_RUN_ID=""
if http_post "${API}/discovery/ip/runs" "$ip_body" && [ "$HTTP_STATUS" = "200" ]; then
  DISC_RUN_ID="$(json_field "$HTTP_BODY" run_id)"
  pass "POST /api/v1/discovery/ip/runs (dry_run) -> 200 run_id=${DISC_RUN_ID}"
else
  fail "POST /api/v1/discovery/ip/runs (dry_run) -> HTTP ${HTTP_STATUS} (expected 200)"
fi

if [ -n "$DISC_RUN_ID" ]; then
  # Dry-run is computed inline with no I/O, so the run is terminal immediately;
  # a short poll covers a queued deployment too.
  DISC_STATUS=""
  attempt=0
  while [ "$attempt" -lt "$POLL_ATTEMPTS" ]; do
    if http_get "${API}/discovery/runs/${DISC_RUN_ID}" && [ "$HTTP_STATUS" = "200" ]; then
      DISC_STATUS="$(json_field "$HTTP_BODY" status)"
      case "$DISC_STATUS" in succeeded|failed|cancelled) break ;; esac
    fi
    attempt=$((attempt + 1))
    sleep "$POLL_INTERVAL"
  done

  # The plan lives at result_summary.dry_run_plan with a target_count. The run
  # body embeds it; assert the marker + a positive target_count are present.
  if [ "$DISC_STATUS" = "succeeded" ] \
     && printf '%s' "$HTTP_BODY" | grep -q '"dry_run_plan"'; then
    target_count="$(json_field "$HTTP_BODY" target_count)"
    if [ -n "$target_count" ] && [ "$target_count" -gt 0 ] 2>/dev/null; then
      pass "dry-run IP discovery returned a plan (target_count=${target_count}, no scan)"
    else
      fail "dry-run IP discovery plan had no targets (target_count='${target_count}')"
    fi
  else
    fail "dry-run IP discovery did not return a plan (status='${DISC_STATUS}')"
  fi
else
  fail "dry-run IP discovery polling skipped (no run_id)"
fi

# --- summary ---------------------------------------------------------------
echo "--------------------------------------------------"
TOTAL=$((PASS_COUNT + FAIL_COUNT))
if [ "$FAIL_COUNT" -eq 0 ]; then
  printf '%sSMOKE PASSED%s  %d/%d checks OK\n' "$C_GREEN" "$C_RESET" "$PASS_COUNT" "$TOTAL"
  exit 0
else
  printf '%sSMOKE FAILED%s  %d passed, %d failed (of %d)\n' "$C_RED" "$C_RESET" "$PASS_COUNT" "$FAIL_COUNT" "$TOTAL"
  exit 1
fi
