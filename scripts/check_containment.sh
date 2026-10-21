#!/usr/bin/env bash
# Proves that no running container except granite-gateway can reach outside, that granite-gateway is the
# only container on the llm-egress network, and that the check itself works.
# Each "blocked" test is paired with a positive control that must succeed, so a broken probe
# (no DNS, no network at all) can't make the test pass for the wrong reason.
#
# Usage: scripts/check_containment.sh                      (stack up, .env in the repo root)
#        COMPOSE_ARGS="--env-file /path/to/env" scripts/check_containment.sh
# Exit code 0 = contained. Anything else = do not put real policies in.
set -u
cd "$(dirname "$0")/.."

OUTSIDE_URL="${OUTSIDE_URL:-https://example.com}"
OUTSIDE_IP="${OUTSIDE_IP:-1.1.1.1}"
# shellcheck disable=SC2206
DC=(docker compose ${COMPOSE_ARGS:-})
fail=0
checked=0

if ! services=$("${DC[@]}" ps --status running --services 2>&1); then
  echo "FAIL  could not list running services: $services"
  exit 1
fi

probe() {  # probe <service> <host> <port>  -> exit 0 if a TCP connection opens
  "${DC[@]}" exec -T "$1" python3 -c "
import socket, sys
try:
    socket.create_connection(('$2', $3), timeout=5).close()
except OSError:
    sys.exit(1)
" >/dev/null 2>&1
}

probe_url() {  # probe_url <service> <url>  -> exit 0 if the URL answers
  "${DC[@]}" exec -T "$1" python3 -c "
import sys, urllib.request
try:
    urllib.request.urlopen('$2', timeout=5)
except Exception:
    sys.exit(1)
" >/dev/null 2>&1
}

running() { grep -qx "$1" <<<"$services"; }

check() {  # check <service> <control-host> <control-port>
  local svc=$1
  if ! running "$svc"; then echo "SKIP  $svc is not running"; return; fi
  checked=$((checked + 1))
  if probe "$svc" "$2" "$3"; then
    echo "OK    $svc can reach $2:$3 (positive control)"
  else
    echo "FAIL  $svc cannot reach $2:$3, so the probe itself is broken; result below is not trustworthy"; fail=1
  fi
  if probe_url "$svc" "$OUTSIDE_URL" || probe "$svc" "$OUTSIDE_IP" 443; then
    echo "FAIL  $svc can reach the internet"; fail=1
  else
    echo "OK    $svc cannot reach the internet"
  fi
}

check policy-api db 5432
check policy-api granite-gateway 8000
check open-webui policy-api 8000
check judge-tuned db 5432
check judge-candidate db 5432
check argilla argilla-db 5432
check argilla-worker argilla-db 5432
check lora-trainer db 5432

# --- The one way out: granite-gateway must be the only container on llm-egress -------------------
egress_members() {  # services whose containers are attached to a network named *llm-egress
  local svc cid
  for svc in $services; do
    for cid in $("${DC[@]}" ps -q "$svc" 2>/dev/null); do
      if docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$cid" 2>/dev/null \
           | tr ' ' '\n' | grep -q 'llm-egress$'; then echo "$svc"; fi
    done
  done
}
if running granite-gateway; then
  members=$(egress_members | sort -u | tr '\n' ' ' | sed 's/ $//')
  checked=$((checked + 1))
  if [ "$members" = "granite-gateway" ]; then
    echo "OK    granite-gateway is the only container on llm-egress"
  else
    echo "FAIL  containers on llm-egress: '${members:-none}' (must be exactly: granite-gateway)"; fail=1
  fi
  # The gateway answers its own health route, and refuses every route except chat and model list.
  gw() { "${DC[@]}" exec -T granite-gateway wget -q -S -O /dev/null "http://127.0.0.1:8000$1" 2>&1 | head -1; }
  if gw /health | grep -q "200"; then echo "OK    granite-gateway is healthy"; else echo "FAIL  granite-gateway /health did not answer"; fail=1; fi
  if gw /v1/embeddings | grep -q "404"; then echo "OK    granite-gateway refuses other routes (/v1/embeddings -> 404)"; else echo "FAIL  granite-gateway passes a route it should refuse"; fail=1; fi
  # Nothing else on the backend may reach the Granite server directly (only through the gateway).
  url="${GRANITE_URL:-$(grep -E '^GRANITE_URL=' .env 2>/dev/null | head -1 | cut -d= -f2-)}"
  hostport=$(sed -E 's#^[a-z]+://##; s#/.*$##' <<<"$url")
  host=${hostport%%:*}; port=${hostport##*:}
  [ "$port" = "$hostport" ] && { case "$url" in https:*) port=443;; *) port=80;; esac; }
  if [ -n "$host" ]; then
    for svc in policy-api open-webui; do
      running "$svc" || continue
      if probe "$svc" "$host" "$port"; then echo "FAIL  $svc can reach the Granite server directly (it must go through granite-gateway)"; fail=1
      else echo "OK    $svc cannot reach the Granite server directly"; fi
    done
  fi
else
  echo "SKIP  granite-gateway is not running"
fi

if [ "$checked" -eq 0 ]; then
  echo "FAIL  no services were checked; start the stack first (a check that tested nothing is not a pass)"
  exit 1
fi
if [ "$fail" -eq 0 ]; then echo "PASS  contained"; else echo "FAIL  NOT contained. Fix this before loading real policies."; fi
exit "$fail"
