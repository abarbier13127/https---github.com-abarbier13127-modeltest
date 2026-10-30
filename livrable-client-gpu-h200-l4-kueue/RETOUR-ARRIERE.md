# Retour arrière

Défait le déploiement de `PROCEDURE-DEPLOIEMENT.md`, **dans l'ordre inverse**. On compare ensuite avec la sauvegarde de l'étape 0 (`sauvegarde-<date>/`).

```bash
M=manifests; NS_GPU=nvidia-gpu-operator; CP=gpu-cluster-policy; DSC=default-dsc; B=sauvegarde-<date>
```

⚠️ **Avant de commencer**, lister les workloads qui passent par Kueue. Ils perdront leur quota et leur file d'attente :

```bash
oc get workloads -A
oc get ns -l kueue.openshift.io/managed=true -o name
```

## R1 — Queues spécifiques et hardware profiles

```bash
# queues spécifiques éventuelles (PROCEDURE-QUEUE-SPECIFIQUE.md)
oc get clusterqueue; oc get localqueues -A; oc get hardwareprofiles -A
oc delete hardwareprofile -n <projet> <profil-du-projet>      # variante B, par projet
oc delete clusterqueue cq-<equipe>                            # après avoir vidé / retiré ses LocalQueues
# profils du livrable
oc delete hardwareprofiles -n redhat-ods-applications -l gpu-kueue.livrable/profile=true
```

## R2 — LocalQueues, ClusterQueue, ResourceFlavors

Couper d'abord la création automatique, sinon RHOAI recrée les queues et les flavors.

```bash
oc patch datasciencecluster $DSC --type merge -p '{"spec":{"components":{"kueue":{"autoCreateQueues":false}}}}'
oc delete localqueues --all -A
oc delete clusterqueue default --ignore-not-found
oc delete resourceflavor rf-h200 rf-h200-mig rf-h200-mixte default-flavor nvidia-gpu-flavor --ignore-not-found
```

## R3 — Dashboard : désactiver Kueue

```bash
oc patch odhdashboardconfig odh-dashboard-config -n redhat-ods-applications --type json \
  -p '[{"op":"remove","path":"/spec/dashboardConfig/disableKueue"}]'
```

Les labels Kueue restent sur les projets, mais sans effet une fois Kueue retiré. Pour les retirer :

```bash
for n in $({ oc get ns -l kueue-managed=true -o name; oc get ns -l kueue.openshift.io/managed=true -o name; } | sort -u); do oc label $n kueue.openshift.io/managed- kueue-managed-; done
```

## R4 — DSC : Kueue `Removed`, puis désinstallation de l'opérateur

**La DSC doit repasser en `Removed` avant la désinstallation de l'opérateur.**

```bash
oc patch datasciencecluster $DSC --type merge -p '{"spec":{"components":{"kueue":{"managementState":"Removed"}}}}'
oc get kueues.kueue.openshift.io cluster                       # normalement supprimé par RHOAI, sinon :
oc delete kueues.kueue.openshift.io cluster --ignore-not-found --wait=true      # avant l'opérateur (finalizer)
oc -n openshift-kueue-operator delete subscription kueue-operator
oc -n openshift-kueue-operator delete csv -l operators.coreos.com/kueue-operator.openshift-kueue-operator
oc delete ns openshift-kueue-operator --wait=true
oc get crd -o name | grep -E 'kueue\.x-k8s\.io|kueue\.openshift\.io' | xargs -r oc delete     # OLM laisse les CRDs
```

## R5 — MIG : nœuds, config personnalisée, stratégie

⚠️ Vider les nœuds de leurs workloads GPU avant de changer leur profil MIG.

```bash
# nœuds : retour au profil initial (voir $B/nodes-mig.txt), par exemple tout en entier
oc label node <h200-...> nvidia.com/mig.config=all-disabled --overwrite
oc get nodes -l nvidia.com/gpu.product=NVIDIA-H200 -L nvidia.com/mig.config,nvidia.com/mig.config.state -w   # success

# config personnalisée (si étape 2 faite) — APRÈS que plus aucun nœud n'utilise un profil sur mesure
oc patch clusterpolicy $CP --type json -p '[{"op":"remove","path":"/spec/migManager/config"}]'
oc -n $NS_GPU get ds nvidia-mig-manager -o jsonpath='{range .spec.template.spec.volumes[?(@.name=="mig-parted-config")]}{.configMap.name}{"\n"}{end}'   # default-mig-parted-config
oc -n $NS_GPU delete cm custom-mig-parted-config

# stratégie MIG initiale (voir $B/clusterpolicy.yaml)
oc patch clusterpolicy $CP --type merge -p '{"spec":{"mig":{"strategy":"single"}}}'
```

Reporter aussi les changements dans les MachineSets, si le label `nvidia.com/mig.config` y a été ajouté.

## Vérification

```bash
diff <(oc get crd -o name | sort) $B/crd.txt && echo "CRD identiques"
oc get clusterpolicy $CP -o jsonpath='mig={.spec.mig} migManager={.spec.migManager} state={.status.state}{"\n"}'
oc get datasciencecluster $DSC -o jsonpath='kueue={.spec.components.kueue}{"\n"}'
oc get odhdashboardconfig odh-dashboard-config -n redhat-ods-applications -o jsonpath='{.spec.dashboardConfig}{"\n"}'
oc get hardwareprofiles -A
oc get ns | grep -i kueue                                         # attendu : rien
oc get co | awk 'NR==1 || $3!="True" || $4!="False" || $5!="False"'
```
