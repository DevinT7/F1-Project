#!/usr/bin/env bash
# Restore drill (Phase 9): prove a backup restores into a working database. docs/runbook.md → Backups.
#
#   SOURCE_URL=postgresql://…/raceiq ops/db/restore_drill.sh [dump-file]
#
# 1. take a fresh backup of SOURCE_URL (or use the given dump; `.enc` files need BACKUP_PASSPHRASE)
# 2. restore it into a scratch database on the same server (DRILL_DB, default raceiq_restore_drill),
#    with TimescaleDB's pre/post-restore steps when the source uses TimescaleDB
# 3. verify: the Alembic revision matches, every table in `public` has the same row count, and the
#    restored telemetry is readable (one lap's samples come back in order)
# 4. drop the scratch database (KEEP_DRILL_DB=1 keeps it) and print timings
# Exit status 0 only when every check passes. Needs pg_dump / pg_restore / psql on PATH.
set -euo pipefail

src=${SOURCE_URL:?set SOURCE_URL}
drill_db=${DRILL_DB:-raceiq_restore_drill}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

base=${src%%\?*}
query=${src#"$base"}
target="${base%/*}/${drill_db}${query}"
admin="${base%/*}/postgres${query}"

t0=$(date +%s)
if [ $# -ge 1 ]; then
  dump=$1
  if [[ "$dump" == *.enc ]]; then
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:BACKUP_PASSPHRASE -in "$dump" -out "$work/raceiq.dump"
    dump="$work/raceiq.dump"
  fi
else
  dump="$work/raceiq.dump"
  pg_dump --format=custom --compress=6 --no-owner --no-privileges --file "$dump" "$src"
fi
t_dump=$(date +%s)
echo "backup: $(wc -c < "$dump" | tr -d ' ') bytes in $((t_dump - t0)) s"

psql -X -q -v ON_ERROR_STOP=1 "$admin" -c "DROP DATABASE IF EXISTS ${drill_db}" -c "CREATE DATABASE ${drill_db}"
timescale=$(psql -X -At "$src" -c "SELECT count(*) FROM pg_extension WHERE extname = 'timescaledb'")
if [ "$timescale" = "1" ]; then
  psql -X -q -v ON_ERROR_STOP=1 "$target" -c "CREATE EXTENSION IF NOT EXISTS timescaledb" -c "SELECT timescaledb_pre_restore()" > /dev/null
fi
pg_restore --no-owner --no-privileges --exit-on-error --dbname "$target" "$dump" 2> "$work/restore.err" || {
  # the extension object itself is already in place (created above): that one error is expected
  if grep -v -E 'extension "timescaledb" already exists|COMMENT ON EXTENSION|^pg_restore: (warning|error): (could not execute query|from TOC entry)|^Command was:|^$' "$work/restore.err" | grep -q .; then
    cat "$work/restore.err" >&2; exit 1
  fi
}
if [ "$timescale" = "1" ]; then
  psql -X -q -v ON_ERROR_STOP=1 "$target" -c "SELECT timescaledb_post_restore()" > /dev/null
fi
t_restore=$(date +%s)
echo "restore: $((t_restore - t_dump)) s into ${drill_db} (timescaledb: $([ "$timescale" = 1 ] && echo yes || echo no))"

fail=0
rev_src=$(psql -X -At "$src" -c "SELECT version_num FROM alembic_version")
rev_dst=$(psql -X -At "$target" -c "SELECT version_num FROM alembic_version")
if [ "$rev_src" = "$rev_dst" ]; then echo "ok   alembic revision $rev_dst"; else echo "FAIL alembic revision $rev_src → $rev_dst"; fail=1; fi

total=0
for table in $(psql -X -At "$src" -c "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"); do
  a=$(psql -X -At "$src" -c "SELECT count(*) FROM public.\"$table\"")
  b=$(psql -X -At "$target" -c "SELECT count(*) FROM public.\"$table\"")
  total=$((total + b))
  if [ "$a" = "$b" ]; then printf 'ok   %-28s %10s rows\n' "$table" "$b"; else printf 'FAIL %-28s %10s → %s rows\n' "$table" "$a" "$b"; fail=1; fi
done

has_telemetry=$(psql -X -At "$target" -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename = 'telemetry_samples'")
if [ "$has_telemetry" = "1" ]; then
  ordered=$(psql -X -At "$target" -c "
    WITH one AS (SELECT lap_id FROM telemetry_samples LIMIT 1)
    SELECT coalesce(bool_and(sample_idx = rn - 1), true) FROM (
      SELECT sample_idx, row_number() OVER (ORDER BY sample_idx) AS rn
      FROM telemetry_samples WHERE lap_id = (SELECT lap_id FROM one)) s")
  if [ "$ordered" = "t" ]; then echo "ok   telemetry readable (one lap's samples contiguous and ordered)"; else echo "FAIL telemetry samples out of order"; fail=1; fi
fi

if [ "${KEEP_DRILL_DB:-0}" != "1" ]; then
  psql -X -q "$admin" -c "DROP DATABASE IF EXISTS ${drill_db}"
fi
t_end=$(date +%s)
if [ $fail -eq 0 ]; then
  echo "RESTORE DRILL PASSED: $total rows verified; backup $((t_dump - t0)) s, restore $((t_restore - t_dump)) s, total $((t_end - t0)) s"
else
  echo "RESTORE DRILL FAILED" >&2
  exit 1
fi
