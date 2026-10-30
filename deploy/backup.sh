#!/bin/bash
# A copy of the verifier's database, kept 14 days: every request, result and analysis that later disposals are
# compared with. Run by root each night from cron, beside docker-compose.yml and dev.env:
#   10 2 * * * root /opt/jev/deploy/backup.sh
# Restore one (this replaces what's in the database):
#   docker compose --env-file dev.env exec -T db pg_restore -U postgres -d verifier --clean --if-exists < FILE
# The copies are on the same server: copy them off it too, or a lost disk loses them with the database.
set -euo pipefail
cd "$(dirname "$0")"
dir=${BACKUP_DIR:-/var/backups/jev-verifier}
env_file=${ENV_FILE:-dev.env}
mkdir -p "$dir" && chmod 700 "$dir"
file="$dir/verifier-$(date +%F).dump"
docker compose --env-file "$env_file" exec -T db pg_dump -U postgres -Fc verifier > "$file.part"
mv "$file.part" "$file"
find "$dir" -name 'verifier-*.dump' -mtime +14 -delete
