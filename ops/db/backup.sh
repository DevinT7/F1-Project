#!/usr/bin/env bash
# Logical backup of a RaceIQ database (Phase 9). docs/runbook.md → Backups.
#
#   DATABASE_URL=postgresql://… ops/db/backup.sh [out-file]       # default: raceiq-<UTC timestamp>.dump
#
# pg_dump custom format (compressed, restorable table by table with pg_restore). Needs pg_dump ≥ the
# server's major version on PATH. With BACKUP_PASSPHRASE set the dump is encrypted (AES-256, PBKDF2) to
# <out-file>.enc and the plaintext is removed: the dump holds account emails and password hashes.
# Prints the file, its size and SHA-256. Managed daily backups (Render) are the first line; this is the
# portable copy and what the restore drill (ops/db/restore_drill.sh) proves restorable.
set -euo pipefail

src=${DATABASE_URL:?set DATABASE_URL}
out=${1:-raceiq-$(date -u +%Y%m%dT%H%M%SZ).dump}

pg_dump --format=custom --compress=6 --no-owner --no-privileges --file "$out" "$src"

if [ -n "${BACKUP_PASSPHRASE:-}" ]; then
  openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:BACKUP_PASSPHRASE -in "$out" -out "$out.enc"
  rm -f "$out"
  out="$out.enc"
fi

size=$(wc -c < "$out" | tr -d ' ')
sum=$(sha256sum "$out" | cut -d' ' -f1)
echo "backup: $out ($size bytes, sha256 $sum)"
