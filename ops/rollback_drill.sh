#!/usr/bin/env bash
# Rollback drill on the docker compose stack (Phase 9). docs/runbook.md → Rollback.
#
#   PREVIOUS=v0.9.0 CURRENT=v0.9.1 ops/rollback_drill.sh
#
# Both releases' images must exist locally (raceiq-backend|worker|frontend:<tag>; build one with
# `RACEIQ_TAG=<tag> docker compose build`). The drill: deploy CURRENT → sign up a drill account and save
# a comparison → roll back to PREVIOUS (image switch only: no rebuild, the database and volumes stay) →
# the API serves PREVIOUS, the account signs in, the saved comparison is still there → roll forward to
# CURRENT → same checks. The same steps as production, where the image switch is a Render deploy of the
# earlier digest (deploy.yml → "Run workflow").
set -euo pipefail

prev=${PREVIOUS:?set PREVIOUS (the release to roll back to)}
cur=${CURRENT:?set CURRENT (the release that is rolled back)}
api=http://localhost:${BACKEND_PORT:-8000}
compose=(docker compose -f docker-compose.yml ${COMPOSE_EXTRA:+-f "$COMPOSE_EXTRA"})
email="rollback-drill-$(date +%s)@raceiq.local"
password="drill-$(date +%s)-Passw0rd!"

deploy() {
  local tag=$1 t0
  t0=$(date +%s)
  RACEIQ_TAG=$tag "${compose[@]}" up -d --no-build --wait backend worker frontend > /dev/null 2>&1
  for _ in $(seq 1 60); do
    version=$(curl -fsS --max-time 5 "$api/api/v1/health" 2>/dev/null | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')
    [ -n "$version" ] && break
    sleep 2
  done
  echo "deployed $tag in $(( $(date +%s) - t0 )) s → /api/v1/health version: ${version:-unreachable}"
}

check_data() {
  local token analyses
  token=$(curl -fsS -X POST "$api/api/v1/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$email\",\"password\":\"$password\"}" | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')
  [ -n "$token" ] || { echo "FAIL the drill account cannot sign in"; exit 1; }
  analyses=$(curl -fsS "$api/api/v1/me/analyses" -H "Authorization: Bearer $token")
  echo "$analyses" | grep -q '"rollback drill"' || { echo "FAIL the saved comparison is gone: $analyses"; exit 1; }
  echo "ok   account signs in, saved comparison present"
}

echo "== deploy $cur"
deploy "$cur"
token=$(curl -fsS -X POST "$api/api/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$email\",\"password\":\"$password\"}" | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')
[ -n "$token" ] || { echo "FAIL could not register the drill account"; exit 1; }
curl -fsS -X POST "$api/api/v1/me/analyses" -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
  -d '{"title":"rollback drill","params":{"session_key":"2024-05-Q","reference":{"driver":"VER"},"target":{"driver":"ALO"}}}' > /dev/null
check_data

echo "== roll back to $prev"
deploy "$prev"
[ "$version" != "$cur" ] || { echo "FAIL still serving $cur"; exit 1; }
check_data

echo "== roll forward to $cur"
deploy "$cur"
[ "$version" = "$cur" ] || { echo "FAIL expected $cur, got $version"; exit 1; }
check_data
echo "ROLLBACK DRILL PASSED"
