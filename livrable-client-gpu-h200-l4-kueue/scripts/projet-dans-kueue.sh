#!/usr/bin/env bash
# Rattache à Kueue un projet existant (créé avant l'activation de Kueue dans le dashboard, ou sorti auparavant).
# Les projets créés depuis le dashboard APRÈS l'activation sont labellisés automatiquement.
# Usage : scripts/projet-dans-kueue.sh <projet>
set -euo pipefail
P="${1:?usage: $0 <projet>}"
oc label ns "$P" kueue.openshift.io/managed=true kueue-managed=true --overwrite
echo "Attente de la LocalQueue default (créée par RHOAI, autoCreateQueues: true)…"
for i in $(seq 1 30); do oc -n "$P" get localqueue default >/dev/null 2>&1 && break; sleep 5; done
oc -n "$P" get localqueues -o wide
