#!/usr/bin/env bash
# Sort un projet de Kueue pour qu'il puisse utiliser le hardware profile L4 (type Node).
# Retire LES DEUX labels (kueue.openshift.io/managed et l'ancien kueue-managed) : avec autoCreateQueues, RHOAI
# considère un namespace comme géré dès que l'un des deux est présent et remet alors l'autre.
# Usage : scripts/projet-hors-kueue.sh <projet>
set -euo pipefail
P="${1:?usage: $0 <projet>}"
if oc -n "$P" get workloads.kueue.x-k8s.io --no-headers 2>/dev/null | grep -q .; then
  echo "⚠ Le projet $P a encore des workloads Kueue :"; oc -n "$P" get workloads.kueue.x-k8s.io
  echo "  Les supprimer (workbenches/serving H200) avant de sortir le projet de Kueue."; exit 1
fi
oc label ns "$P" kueue.openshift.io/managed- kueue-managed-
oc -n "$P" delete localqueue default --ignore-not-found
echo "Vérification dans 60 s (RHOAI ne doit pas remettre les labels)…"; sleep 60
oc get ns "$P" -o jsonpath='{.metadata.labels}' | tr ',' '\n' | grep -i kueue && { echo "✗ labels Kueue revenus"; exit 1; } || echo "✓ $P hors Kueue"
oc -n "$P" get localqueues 2>&1
