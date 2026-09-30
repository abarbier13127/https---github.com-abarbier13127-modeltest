#!/usr/bin/env bash
# =============================================================================
# Rend les manifestes Guardrails pour un environnement cible.
#
#   ./render.sh params/<environnement>.env
#
# Produit build/<environnement>/*.yaml, prêts pour « oc apply -f ».
# Seules les variables listées ci-dessous sont substituées : les autres « $ »
# (ex. $(OWUI_TOKEN), regex) restent intacts. Rien n'est appliqué au cluster.
# =============================================================================
set -euo pipefail

VARS=(GR_NAMESPACE LLM_NAMESPACE LLM_ISVC_NAME LLM_MODEL_NAME LLM_BASE_URL
      LLM_CALLER_ROLE OWUI_NAMESPACE OWUI_SA OWUI_TOKEN_SECRET)

here="$(cd "$(dirname "$0")" && pwd)"
params="${1:?usage : $0 params/<environnement>.env}"
[[ -f "$params" ]] || { echo "fichier introuvable : $params" >&2; exit 1; }

set -a; source "$params"; set +a

missing=()
for v in "${VARS[@]}"; do [[ -n "${!v:-}" ]] || missing+=("$v"); done
if (( ${#missing[@]} )); then
  echo "variables manquantes dans $params : ${missing[*]}" >&2; exit 1
fi

env_name="$(basename "$params" .env)"
out="$here/build/$env_name"
mkdir -p "$out"
shell_format="$(printf '${%s} ' "${VARS[@]}")"

for f in "$here"/manifests/*.yaml; do
  envsubst "$shell_format" < "$f" > "$out/$(basename "$f")"
done

# Contrôle : aucune variable connue ne doit subsister
if grep -n -E "\\\$\{($(IFS='|'; echo "${VARS[*]}"))\}" "$out"/*.yaml; then
  echo "ERREUR : variables non substituées ci-dessus" >&2; exit 1
fi

echo "Manifestes rendus dans ${out#$PWD/} :"
ls -1 "$out"
