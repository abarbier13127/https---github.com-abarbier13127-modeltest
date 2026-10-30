#!/usr/bin/env bash
# =============================================================================
# Crée le Secret open-webui-secret-key (WEBUI_SECRET_KEY) s'il n'existe pas.
# Clé qui signe les sessions (JWT) et chiffre certains réglages en base :
# la changer déconnecte tout le monde. Ne jamais la régénérer sans raison.
#
#   ./scripts/create-secret-key.sh
# =============================================================================
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-/home/alf/Documents/_dev/SNO/ds/kubeconfig-noingress}"
NS=aidocs

if oc -n "$NS" get secret open-webui-secret-key >/dev/null 2>&1; then
  echo "Secret open-webui-secret-key déjà présent : rien à faire."
  exit 0
fi
oc -n "$NS" create secret generic open-webui-secret-key \
  --from-literal=WEBUI_SECRET_KEY="$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
