# Procédure : LocalQueue ou quota spécifique pour un projet donné

Par défaut, chaque projet géré par Kueue a une LocalQueue `default`, qui mène à la ClusterQueue partagée `default`
(voir le schéma du README, § 2). Cette procédure donne à **un projet précis** une file et un quota **dédiés**,
par exemple pour réserver 8 H200 entiers et 4 tranches 2g.35gb à l'équipe A.

Les exemples utilisent le projet `equipe-a` : remplacer ce nom partout (`metadata.namespace`, `namespaceSelector`, noms d'objets).
Les fichiers sont dans `manifests/06-queue-specifique-exemple/`.

## Choisir la variante

| | **Variante A — rediriger la queue `default` du projet** | **Variante B — queue supplémentaire + profil propre au projet** |
|---|---|---|
| Principe | La LocalQueue `default` **du projet** pointe vers `cq-equipe-a` | Nouvelle LocalQueue `equipe-a` + hardware profile créé **dans le projet** |
| Profils utilisés | Les profils H200 globaux, sans changement | Profil(s) propre(s) au projet, en plus des profils globaux |
| Effet | **Tout** le projet consomme le quota dédié | L'utilisateur choisit : quota dédié (profil du projet) ou quota partagé (profils globaux) |
| Fichiers | `clusterqueue-equipe-a.yaml`, `localqueue-default-redirigee.yaml` | `clusterqueue-equipe-a.yaml`, `localqueue-equipe-a.yaml`, `hardware-profile-equipe-a.yaml` |

Règles communes :
- **Pas de cohort** (les cohorts partagés ne sont pas supportés en RHOAI 3.5) : il n'y a pas d'emprunt entre ClusterQueues, et le quota dédié est **réservé**.
- **Ne pas dépasser la capacité réelle** : pour chaque flavor, la somme des `nominalQuota` de toutes les ClusterQueues doit rester inférieure ou égale à la capacité
  des nœuds. Sinon, Kueue admet des pods que le scheduler ne peut pas placer, et ils restent `Pending`.
  → **Baisser d'autant les quotas de la ClusterQueue `default`.**
- Les flavors (`rf-h200`, `rf-h200-mig`…) sont **partagées** : il n'y a pas de flavor à créer.
- Pas de préemption sur la ClusterQueue dédiée, si elle doit accueillir des workbenches.

---

## Étape 1 (A et B) — ClusterQueue dédiée et ajustement de `default`

Adapter `clusterqueue-equipe-a.yaml` : nom, `namespaceSelector` (`kubernetes.io/metadata.name: <projet>`), quotas par flavor.

```bash
M=manifests/06-queue-specifique-exemple
oc apply -f $M/clusterqueue-equipe-a.yaml
oc get clusterqueue cq-equipe-a -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.message}{"\n"}{end}'   # Active=True

# retirer de "default" ce qui est réservé (exemple : rf-h200 16 → 8 GPU, rf-h200-mig 2g.35gb 8 → 4)
#   → modifier manifests/04-kueue-queues/clusterqueue-default.yaml puis :
oc apply -f manifests/04-kueue-queues/clusterqueue-default.yaml
oc get clusterqueue -o custom-columns='CQ:.metadata.name,FLAVORS:.spec.resourceGroups[0].flavors[*].name'
```

Contrôle de non-dépassement, à comparer à la capacité réelle des nœuds :

```bash
for cq in $(oc get clusterqueue -o name); do oc get $cq -o jsonpath='{.metadata.name}: {range .spec.resourceGroups[0].flavors[*]}{.name}[{range .resources[?(@.nominalQuota!="0")]}{.name}={.nominalQuota} {end}] {end}{"\n"}'; done
```

---

## Variante A — Rediriger la LocalQueue `default` du projet

`spec.clusterQueue` d'une LocalQueue est **immuable** : il faut supprimer la LocalQueue créée par RHOAI, puis la recréer.
L'annotation `opendatahub.io/managed: "false"` empêche l'opérateur RHOAI (`autoCreateQueues`) de la remplacer.

```bash
P=equipe-a
oc -n $P get workloads                     # ⚠ aucun workload en cours : la suppression de la LocalQueue les rendrait orphelins
oc -n $P delete localqueue default
oc apply -f $M/localqueue-default-redirigee.yaml      # namespace et clusterQueue adaptés
oc -n $P get localqueue default -o custom-columns='LQ:.metadata.name,CQ:.spec.clusterQueue,MANAGED:.metadata.annotations.opendatahub\.io/managed'
sleep 60; oc -n $P get localqueue default -o jsonpath='{.spec.clusterQueue}{"\n"}'   # toujours cq-equipe-a (RHOAI ne l'a pas remplacée)
```

Utilisation : rien ne change pour l'utilisateur. Les profils H200 globaux (`localQueueName: default`) consomment désormais `cq-equipe-a`.

Retour à la queue partagée :

```bash
oc -n $P delete localqueue default       # RHOAI recrée la LocalQueue default → ClusterQueue default (autoCreateQueues)
sleep 60; oc -n $P get localqueue default -o jsonpath='{.spec.clusterQueue}{"\n"}'   # default
```

---

## Variante B — LocalQueue supplémentaire et hardware profile propre au projet

Les hardware profiles **propres à un projet** sont créés dans le namespace du projet et ne sont visibles que là. Ils requièrent
`disableProjectScoped: false` dans `odh-dashboard-config`, qui est la valeur par défaut :

```bash
oc get odhdashboardconfig odh-dashboard-config -n redhat-ods-applications -o jsonpath='disableProjectScoped={.spec.dashboardConfig.disableProjectScoped}{"\n"}'
```

```bash
P=equipe-a
oc apply -f $M/localqueue-equipe-a.yaml               # LocalQueue equipe-a → cq-equipe-a
oc apply -f $M/hardware-profile-equipe-a.yaml         # profil h200-full-equipe-a, namespace equipe-a, localQueueName: equipe-a
oc -n $P get localqueues -o wide                       # default (partagée) + equipe-a (dédiée)
oc -n $P get hardwareprofiles
```

Utilisation : dans ce projet, le dashboard propose le profil « NVIDIA H200 141 Go — réservé équipe A » en plus des profils globaux.
Pour des tranches MIG réservées, dupliquer le profil avec l'identifier `nvidia.com/mig-…` voulu, toujours avec `localQueueName: equipe-a`.

**Ne pas créer** de profil global (dans `redhat-ods-applications`) qui pointe vers `equipe-a` : il serait proposé dans tous les projets,
qui n'ont pas cette LocalQueue, et leurs workloads resteraient en attente.

Retrait :

```bash
oc -n $P delete hardwareprofile h200-full-equipe-a
oc -n $P delete localqueue equipe-a
```

---

## Vérification (A et B)

```bash
oc -n $P get workloads -o custom-columns='WL:.metadata.name,LQ:.spec.queueName,ADMIS:.status.conditions[?(@.type=="Admitted")].status,CQ:.status.admission.clusterQueue,FLAVOR:.status.admission.podSetAssignments[0].flavors'
oc get clusterqueue cq-equipe-a -o jsonpath='{range .status.flavorsUsage[*]}{.name}: {range .resources[*]}{.name}={.total} {end}{"\n"}{end}'
```

Attendu : les workloads du projet passés par la queue dédiée affichent `CQ=cq-equipe-a`, et l'usage apparaît sur `cq-equipe-a`.

## Supprimer complètement la queue dédiée

1. Variante A : retour à la queue partagée (voir plus haut). Variante B : retrait du profil et de la LocalQueue.
2. `oc delete clusterqueue cq-equipe-a`. Kueue refuse la suppression tant que des workloads y sont admis.
3. Restaurer les quotas de la ClusterQueue `default` (`manifests/04-kueue-queues/clusterqueue-default.yaml`).

Les manifests de ce répertoire ont été validés avec `oc apply --dry-run=server` (Kueue v1.3.2, RHOAI 3.5.1). L'immuabilité de
`spec.clusterQueue` a été vérifiée sur un cluster de recette. Le maintien de la LocalQueue redirigée de la variante A face à
`autoCreateQueues` repose sur l'annotation `opendatahub.io/managed: "false"`, le même mécanisme que pour la ClusterQueue `default`, qui a été vérifié en recette.
**À confirmer lors du premier déploiement** avec la vérification « sleep 60 » ci-dessus.
