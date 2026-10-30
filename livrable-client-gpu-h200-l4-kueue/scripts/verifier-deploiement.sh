#!/usr/bin/env bash
# Vérification du déploiement (LECTURE SEULE). Usage : scripts/verifier-deploiement.sh [namespace-gpu-operator]
NS="${1:-nvidia-gpu-operator}"
ok(){ echo "  ✓ $*"; }; ko(){ echo "  ✗ $*"; ERR=$((ERR+1)); }; ERR=0
echo "== 1. GPU operator / MIG"
S=$(oc get clusterpolicy -o jsonpath='{.items[0].spec.mig.strategy}'); [ "$S" = mixed ] && ok "mig.strategy=mixed" || ko "mig.strategy=$S (attendu mixed)"
echo "  ClusterPolicy: $(oc get clusterpolicy -o jsonpath='{.items[0].status.state}') | config MIG montée par mig-manager: $(oc -n $NS get ds nvidia-mig-manager -o jsonpath='{range .spec.template.spec.volumes[?(@.name=="mig-parted-config")]}{.configMap.name}{end}' 2>/dev/null)"
oc get nodes -l nvidia.com/gpu.present=true -o custom-columns='NODE:.metadata.name,PRODUCT:.metadata.labels.nvidia\.com/gpu\.product,MIG.CONFIG:.metadata.labels.nvidia\.com/mig\.config,STATE:.metadata.labels.nvidia\.com/mig\.config\.state,GPU:.status.allocatable.nvidia\.com/gpu,1G18:.status.allocatable.nvidia\.com/mig-1g\.18gb,2G35:.status.allocatable.nvidia\.com/mig-2g\.35gb,3G71:.status.allocatable.nvidia\.com/mig-3g\.71gb'
F=$(oc get nodes -l nvidia.com/mig.config.state=failed -o name); [ -z "$F" ] && ok "aucun nœud en mig.config.state=failed" || ko "nœuds en échec MIG : $F"
echo "== 2. Kueue"
P=$(oc -n openshift-kueue-operator get csv -o jsonpath='{range .items[?(@.spec.displayName=="Red Hat build of Kueue")]}{.metadata.name} {.status.phase}{end}'); echo "  CSV: $P"
A=$(oc get kueues.kueue.openshift.io cluster -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null); [ "$A" = True ] && ok "CR Kueue Available" || ko "CR Kueue Available=$A"
echo "  frameworks: $(oc get kueues.kueue.openshift.io cluster -o jsonpath='{.spec.config.integrations.frameworks}' 2>/dev/null)"
echo "== 3. RHOAI"
K=$(oc get datasciencecluster -o jsonpath='{.items[0].spec.components.kueue.managementState}/{.items[0].spec.components.kueue.autoCreateQueues}'); [ "$K" = "Unmanaged/true" ] && ok "DSC kueue Unmanaged + autoCreateQueues" || ko "DSC kueue=$K (attendu Unmanaged/true)"
D=$(oc get odhdashboardconfig odh-dashboard-config -n redhat-ods-applications -o jsonpath='{.spec.dashboardConfig.disableKueue}'); [ "$D" = false ] && ok "dashboard disableKueue=false" || ko "dashboard disableKueue='$D' (attendu false)"
echo "== 4. Queues"
oc get resourceflavors -o custom-columns='FLAVOR:.metadata.name,NODELABELS:.spec.nodeLabels'
M=$(oc get clusterqueue default -o jsonpath='{.metadata.annotations.opendatahub\.io/managed}' 2>/dev/null); [ "$M" = false ] && ok "ClusterQueue default protégée (opendatahub.io/managed=false)" || ko "ClusterQueue default sans annotation managed=false"
oc get clusterqueue -o wide
oc get clusterqueue default -o jsonpath='{range .status.flavorsUsage[*]}  usage {.name}: {range .resources[*]}{.name}={.total} {end}{"\n"}{end}'
echo "  projets gérés par Kueue / LocalQueues :"; oc get localqueues -A -o custom-columns='PROJET:.metadata.namespace,LQ:.metadata.name,CQ:.spec.clusterQueue,ATTENTE:.status.pendingWorkloads,ADMIS:.status.admittedWorkloads'
for n in $(oc get ns -l kueue.openshift.io/managed=true -o jsonpath='{.items[*].metadata.name}'); do oc -n $n get localqueue default >/dev/null 2>&1 || ko "projet $n géré sans LocalQueue default"; done
echo "== 5. Hardware profiles"
oc get hardwareprofiles -A -o custom-columns='NS:.metadata.namespace,PROFIL:.metadata.name,TYPE:.spec.scheduling.type,QUEUE:.spec.scheduling.kueue.localQueueName,NODESELECTOR:.spec.scheduling.node.nodeSelector,RESSOURCE:.spec.identifiers[0].identifier'
echo; [ $ERR -eq 0 ] && echo "RÉSULTAT : OK" || echo "RÉSULTAT : $ERR point(s) à corriger"
