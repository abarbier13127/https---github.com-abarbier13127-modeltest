#!/usr/bin/env bash
# Génère la ConfigMap custom-mig-parted-config = config MIG par défaut DU CLUSTER CIBLE + profils sur mesure.
# Lecture seule sur le cluster : écrit les fichiers, n'applique rien.
# Usage : scripts/generer-config-mig.sh [namespace-gpu-operator]   (défaut : nvidia-gpu-operator)
set -euo pipefail
NS="${1:-nvidia-gpu-operator}"
D="$(cd "$(dirname "$0")/.." && pwd)/manifests/01-gpu-operator"
FRAG="$D/mig-profils-sur-mesure.fragment.yaml"
OUT_SRC="$D/mig-config-custom.yaml"
OUT_CM="$D/custom-mig-parted-config.yaml"
VER=$(oc -n "$NS" get csv -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | grep -m1 -E '^gpu-operator' || echo inconnu)

oc -n "$NS" get cm default-mig-parted-config -o jsonpath='{.data.config\.yaml}' > "$D/mig-config-default-cluster.yaml"
{ cat "$D/mig-config-default-cluster.yaml"; echo; cat "$FRAG"; } > "$OUT_SRC"
{ cat <<HDR
# Config MIG par défaut du cluster ($VER) + profils sur mesure de mig-profils-sur-mesure.fragment.yaml.
# Générée par scripts/generer-config-mig.sh le $(date -u +%F). Clé obligatoire : config.yaml.
apiVersion: v1
kind: ConfigMap
metadata:
  name: custom-mig-parted-config
  namespace: $NS
data:
  config.yaml: |
HDR
  sed 's/^\(.\)/    \1/' "$OUT_SRC"; } > "$OUT_CM"

python3 - "$OUT_CM" <<'PY'
import sys, yaml
cm = yaml.safe_load(open(sys.argv[1]))
cfg = yaml.safe_load(cm["data"]["config.yaml"])["mig-configs"]
need = ["all-disabled", "all-balanced", "h200-4full-4balanced"]
missing = [p for p in need if p not in cfg]
print(f"{len(cfg)} profils ; requis présents : {not missing}" + (f" ; MANQUANTS : {missing}" if missing else ""))
sys.exit(1 if missing else 0)
PY
oc apply --dry-run=server -f "$OUT_CM"
echo "OK → $OUT_CM (à appliquer avec : oc apply -f $OUT_CM)"
