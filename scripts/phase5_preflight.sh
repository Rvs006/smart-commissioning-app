#!/usr/bin/env bash
# PRE-SITE preflight for the Smart Commissioning App.
#
# Runs on the technician laptop BEFORE any live action. SAFE / side-effect-free
# only: it never scans, never publishes/subscribes to the broker, never prints
# secret material. Mirrors the auth + curl patterns of scripts/smoke_local.sh.
#
#   1. GET  /api/v1/health                      -> 200 status=ok
#   2. GET  /api/v1/ready                        -> 200 status=ready
#   3. GET  /metrics                             -> Prometheus exposition text
#   4. GET  /api/v1/configuration                -> snapshot present
#   5. cert refs (certificates section)          -> non-empty secret:// refs
#   6. POST /api/v1/discovery/ip/runs  (dry_run) -> plan, NO packets
#   7. POST /api/v1/discovery/mqtt/runs(dry_run) -> plan w/ broker host:port
#   8. TCP connect to broker host:port           -> reachable, NO MQTT bytes
#
# Usage:
#   scripts/phase5_preflight.sh [BASE_URL]
#   BASE_URL defaults to http://127.0.0.1:8000
#   SC_API_KEY=<key> scripts/phase5_preflight.sh     # hosted (AUTH_MODE=api_key)
#   scripts/phase5_preflight.sh                       # local / loopback
#
# Exit code: 0 if every check passed, 1 otherwise.
set -u

BASE_URL="${1:-http://127.0.0.1:8000}"
BASE_URL="${BASE_URL%/}"
API="${BASE_URL}/api/v1"
API_KEY="${SC_API_KEY:-}"

CURL_TIMEOUT="${SC_CURL_TIMEOUT:-15}"
POLL_ATTEMPTS="${SC_POLL_ATTEMPTS:-30}"
POLL_INTERVAL="${SC_POLL_INTERVAL:-1}"
TCP_TIMEOUT="${SC_TCP_TIMEOUT:-5}"

# Demo project/site; override to preflight a specific site's config + broker.
PROJECT_ID="${SC_PROJECT_ID:-demo-project}"
SITE_ID="${SC_SITE_ID:-demo-site}"

PASS_COUNT=0
FAIL_COUNT=0

if [ -t 1 ]; then
  C_GREEN="$(printf '\033[32m')"; C_RED="$(printf '\033[31m')"
  C_DIM="$(printf '\033[2m')"; C_RESET="$(printf '\033[0m')"
else
  C_GREEN=""; C_RED=""; C_DIM=""; C_RESET=""
fi
pass() { PASS_COUNT=$((PASS_COUNT + 1)); printf '%sPASS%s %s\n' "$C_GREEN" "$C_RESET" "$1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); printf '%sFAIL%s %s\n' "$C_RED" "$C_RESET" "$1"; }
info() { printf '%s     %s%s\n' "$C_DIM" "$1" "$C_RESET"; }

command -v curl >/dev/null 2>&1 || { echo "curl is required but not on PATH." >&2; exit 1; }

AUTH_ARGS=()
if [ -n "$API_KEY" ]; then
  AUTH_ARGS=(-H "X-API-Key: ${API_KEY}")
  info "Auth: sending X-API-Key (hosted profile)."
else
  info "Auth: no API key set, assuming local/loopback profile."
fi

# http_get URL -> HTTP_STATUS (int) + HTTP_BODY (string)
http_get() {
  local url="$1" raw
  raw="$(curl -sS --max-time "$CURL_TIMEOUT" "${AUTH_ARGS[@]}" \
    -w $'\n%{http_code}' "$url" 2>/dev/null)" || { HTTP_STATUS=0; HTTP_BODY=""; return 1; }
  HTTP_STATUS="${raw##*$'\n'}"; HTTP_BODY="${raw%$'\n'*}"; return 0
}
http_post() {
  local url="$1" json="$2" raw
  raw="$(curl -sS --max-time "$CURL_TIMEOUT" "${AUTH_ARGS[@]}" \
    -H 'Content-Type: application/json' -X POST --data "$json" \
    -w $'\n%{http_code}' "$url" 2>/dev/null)" || { HTTP_STATUS=0; HTTP_BODY=""; return 1; }
  HTTP_STATUS="${raw##*$'\n'}"; HTTP_BODY="${raw%$'\n'*}"; return 0
}
# json_field BODY KEY -> first scalar value for KEY (no jq dependency).
json_field() {
  printf '%s' "$1" | tr -d '\n' \
    | grep -oE "\"$2\"[[:space:]]*:[[:space:]]*(\"[^\"]*\"|[0-9]+|true|false|null)" \
    | head -n1 | sed -E "s/\"$2\"[[:space:]]*:[[:space:]]*//; s/^\"//; s/\"$//"
}

echo "Pre-site preflight against ${BASE_URL}  (project=${PROJECT_ID} site=${SITE_ID})"
echo "--------------------------------------------------"

# --- 1. health -------------------------------------------------------------
if http_get "${API}/health" && [ "$HTTP_STATUS" = "200" ] \
   && [ "$(json_field "$HTTP_BODY" status)" = "ok" ]; then
  pass "GET /api/v1/health -> 200 status=ok"
else
  fail "GET /api/v1/health -> HTTP ${HTTP_STATUS} (expected 200/ok)"
fi

# --- 2. ready --------------------------------------------------------------
if http_get "${API}/ready"; then
  rstatus="$(json_field "$HTTP_BODY" status)"
  if [ "$HTTP_STATUS" = "200" ] && [ "$rstatus" = "ready" ]; then
    pass "GET /api/v1/ready -> 200 status=ready"
  else
    fail "GET /api/v1/ready -> HTTP ${HTTP_STATUS} status='${rstatus}' (expected 200/ready)"
    info "A 503 means a required dependency (DB, or Redis in queue mode) is down."
  fi
else
  fail "GET /api/v1/ready -> request failed (is the stack running?)"
fi

# --- 3. metrics ------------------------------------------------------------
if http_get "${BASE_URL}/metrics" && [ "$HTTP_STATUS" = "200" ] \
   && printf '%s' "$HTTP_BODY" | grep -qE '^# (HELP|TYPE) |sct_'; then
  pass "GET /metrics -> 200 Prometheus exposition text"
else
  fail "GET /metrics -> HTTP ${HTTP_STATUS} (expected 200 Prometheus text)"
fi

# --- 4. configuration present ----------------------------------------------
CFG_BODY=""
if http_get "${API}/configuration?project_id=${PROJECT_ID}&site_id=${SITE_ID}" \
   && [ "$HTTP_STATUS" = "200" ] && printf '%s' "$HTTP_BODY" | grep -q '"mqtt"'; then
  CFG_BODY="$HTTP_BODY"
  pass "GET /api/v1/configuration -> 200 snapshot present"
else
  fail "GET /api/v1/configuration -> HTTP ${HTTP_STATUS} (expected 200 snapshot)"
fi

# --- 5. secret:// cert refs resolve (presence + shape; NO material) --------
# The certificates section returns secret://... refs VERBATIM (opaque pointers,
# never the material). Assert each of the 3 TLS cert fields is a non-empty
# secret:// ref. Actual on-disk decryption is server-side / on-site only.
if [ -n "$CFG_BODY" ]; then
  secret_count=0; value_count=0; empty=""
  for field in "CA Certificate" "Client Certificate" "Private Key"; do
    val="$(printf '%s' "$CFG_BODY" | tr -d '\n' \
        | grep -oE "\"${field}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" \
        | sed -E "s/.*:[[:space:]]*\"([^\"]*)\"$/\1/")"
    if [ -z "$val" ]; then
      empty="${empty} '${field}'"
    elif printf '%s' "$val" | grep -q '^secret://'; then
      secret_count=$((secret_count + 1))
    else
      value_count=$((value_count + 1))   # plain path / value — valid TLS config too
    fi
  done
  if [ -z "$empty" ]; then
    pass "MQTT-TLS cert fields all set (${secret_count} secret:// ref, ${value_count} path/value)"
    info "Confirm live TLS load on site: secret:// refs decrypt server-side; the in-memory CA + temp client-cert load is on-site-untested."
  else
    # Empty cert fields are NOT a hard failure: the site's broker may be
    # plaintext (no TLS). Surface it so the operator sets them before any TLS run.
    info "MQTT-TLS cert field(s) empty:${empty} — OK if this broker is plaintext; set them before a TLS (8883) broker run."
  fi
else
  fail "cert-ref check skipped (no configuration snapshot)"
fi

# --- 6. DRY-RUN IP discovery (plan only, no packets) -----------------------
ip_body="{\"project_id\":\"${PROJECT_ID}\",\"site_id\":\"${SITE_ID}\",\"job_type\":\"ip_discovery\",\"parameters\":{\"dry_run\":true,\"cidr\":\"192.0.2.0/30\",\"ports\":[47808,1883]}}"
DISC_ID=""
if http_post "${API}/discovery/ip/runs" "$ip_body" && [ "$HTTP_STATUS" = "200" ]; then
  DISC_ID="$(json_field "$HTTP_BODY" run_id)"
fi
if [ -n "$DISC_ID" ]; then
  st=""; attempt=0
  while [ "$attempt" -lt "$POLL_ATTEMPTS" ]; do
    if http_get "${API}/discovery/runs/${DISC_ID}" && [ "$HTTP_STATUS" = "200" ]; then
      st="$(json_field "$HTTP_BODY" status)"
      case "$st" in succeeded|failed|cancelled) break ;; esac
    fi
    attempt=$((attempt + 1)); sleep "$POLL_INTERVAL"
  done
  tc="$(json_field "$HTTP_BODY" target_count)"
  if [ "$st" = "succeeded" ] && printf '%s' "$HTTP_BODY" | grep -q '"dry_run_plan"' \
     && [ -n "$tc" ] && [ "$tc" -gt 0 ] 2>/dev/null; then
    pass "dry-run IP discovery returned a plan (target_count=${tc}, no scan)"
  else
    fail "dry-run IP discovery did not return a plan (status='${st}')"
  fi
else
  fail "POST /api/v1/discovery/ip/runs (dry_run) -> HTTP ${HTTP_STATUS} (expected 200)"
fi

# --- 7. DRY-RUN MQTT discovery (plan only; resolves configured broker) -----
mqtt_body="{\"project_id\":\"${PROJECT_ID}\",\"site_id\":\"${SITE_ID}\",\"job_type\":\"mqtt_discovery\",\"parameters\":{\"dry_run\":true}}"
MQ_ID=""; BROKER_HOST=""; BROKER_PORT=""
if http_post "${API}/discovery/mqtt/runs" "$mqtt_body" && [ "$HTTP_STATUS" = "200" ]; then
  MQ_ID="$(json_field "$HTTP_BODY" run_id)"
fi
if [ -n "$MQ_ID" ]; then
  st=""; attempt=0
  while [ "$attempt" -lt "$POLL_ATTEMPTS" ]; do
    if http_get "${API}/discovery/runs/${MQ_ID}" && [ "$HTTP_STATUS" = "200" ]; then
      st="$(json_field "$HTTP_BODY" status)"
      case "$st" in succeeded|failed|cancelled) break ;; esac
    fi
    attempt=$((attempt + 1)); sleep "$POLL_INTERVAL"
  done
  if [ "$st" = "succeeded" ] && printf '%s' "$HTTP_BODY" | grep -q '"dry_run_plan"'; then
    BROKER_HOST="$(json_field "$HTTP_BODY" broker_host)"
    BROKER_PORT="$(json_field "$HTTP_BODY" broker_port)"
    pass "dry-run MQTT discovery returned a plan (broker ${BROKER_HOST}:${BROKER_PORT}, no connection)"
  else
    fail "dry-run MQTT discovery did not return a plan (status='${st}')"
  fi
else
  fail "POST /api/v1/discovery/mqtt/runs (dry_run) -> HTTP ${HTTP_STATUS} (expected 200)"
fi

# --- 8. Broker TCP reachability (NO MQTT bytes, no handshake) --------------
# A pure TCP connect-and-close. We send no CONNECT/SUBSCRIBE/PUBLISH, so the
# broker sees only an opened+closed socket. Proves the firewall/route is open;
# the real TLS/auth handshake stays an ON-SITE step.
if [ -n "$BROKER_HOST" ] && [ -n "$BROKER_PORT" ]; then
  ok=1
  # Prefer bash /dev/tcp; fall back to nc if present.
  if (exec 3<>"/dev/tcp/${BROKER_HOST}/${BROKER_PORT}") 2>/dev/null; then
    exec 3>&- 3<&- 2>/dev/null || true
    ok=0
  elif command -v nc >/dev/null 2>&1; then
    nc -z -w "$TCP_TIMEOUT" "$BROKER_HOST" "$BROKER_PORT" >/dev/null 2>&1 && ok=0
  else
    info "Neither bash /dev/tcp nor nc available; cannot TCP-probe broker."
    ok=2
  fi
  case "$ok" in
    0) pass "TCP reach ${BROKER_HOST}:${BROKER_PORT} (socket opened+closed, no MQTT bytes)" ;;
    2) info "broker TCP probe SKIPPED — no bash /dev/tcp and no nc on this host. Probe from the on-site machine (the PowerShell preflight uses Test-NetConnection), or install nc." ;;
    *) fail "TCP reach ${BROKER_HOST}:${BROKER_PORT} FAILED (firewall/route/broker down?)" ;;
  esac
else
  fail "broker TCP probe skipped (no broker host:port from MQTT dry-run plan)"
fi

# --- summary ---------------------------------------------------------------
echo "--------------------------------------------------"
TOTAL=$((PASS_COUNT + FAIL_COUNT))
if [ "$FAIL_COUNT" -eq 0 ]; then
  printf '%sPREFLIGHT PASSED%s  %d/%d checks OK\n' "$C_GREEN" "$C_RESET" "$PASS_COUNT" "$TOTAL"
  exit 0
else
  printf '%sPREFLIGHT FAILED%s  %d passed, %d failed (of %d)\n' "$C_RED" "$C_RESET" "$PASS_COUNT" "$FAIL_COUNT" "$TOTAL"
  exit 1
fi
