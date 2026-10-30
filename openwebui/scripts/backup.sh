#!/usr/bin/env bash
# =============================================================================
# Sauvegarde d'Open WebUI depuis le pod en cours d'exécution.
#
#   ./scripts/backup.sh [étiquette]      # ex. ./scripts/backup.sh avant-v0.13
#
# Produit backups/<date>-<étiquette>/ :
#   webui.db          copie cohérente (API backup de SQLite, base ouverte)
#   data-sans-db.tgz  le reste de /app/backend/data (uploads, cache, vector_db)
# Lecture seule vis-à-vis des données du pod.
# =============================================================================
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-/home/alf/Documents/_dev/SNO/ds/kubeconfig-noingress}"
NS=aidocs
here="$(cd "$(dirname "$0")/.." && pwd)"
out="$here/backups/$(date +%F)-${1:-manuel}"
mkdir -p "$out"

oc -n "$NS" exec deploy/open-webui -- python3 -c '
import sqlite3
s = sqlite3.connect("/app/backend/data/webui.db")
d = sqlite3.connect("/tmp/webui-backup.db")
s.backup(d); d.close()'
oc -n "$NS" exec deploy/open-webui -- cat /tmp/webui-backup.db > "$out/webui.db"
oc -n "$NS" exec deploy/open-webui -- rm -f /tmp/webui-backup.db
oc -n "$NS" exec deploy/open-webui -- tar czf - -C /app/backend --exclude='data/webui.db*' data > "$out/data-sans-db.tgz"

python3 -c "
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print('intégrité :', c.execute('pragma integrity_check').fetchone()[0],
      '| utilisateurs :', c.execute('select count(*) from user').fetchone()[0],
      '| chats :', c.execute('select count(*) from chat').fetchone()[0])" "$out/webui.db"
ls -la "$out"
