# Procédure de déploiement : H200 (entiers + MIG) et L4 avec Kueue et hardware profiles

Le principe est décrit dans `README.md`, avec le schéma des liens entre objets et l'inventaire des fichiers. Les commandes ci-dessous se lancent depuis
la racine de ce répertoire, avec un compte `cluster-admin`. **Adapter d'abord les valeurs listées au § 4 du README**
(taints, quotas, namespace du GPU operator…).

```bash
cd livrable-client-gpu-h200-l4-kueue
M=manifests
NS_GPU=nvidia-gpu-operator          # namespace du GPU operator
CP=gpu-cluster-policy               # nom de la ClusterPolicy
DSC=default-dsc                     # nom de la DataScienceCluster
```

| Étape | Objet | Impact |
|---|---|---|
| 0 | Prérequis et sauvegarde | aucun |
| 1 | ClusterPolicy → MIG `mixed` | **redémarrage du device plugin et de GFD sur les nœuds GPU** |
| 2 | *(optionnel)* config MIG personnalisée | redéploiement de mig-manager |
| 3 | Labels `nvidia.com/mig.config` sur les nœuds H200 | **arrêt des pods GPU des nœuds reconfigurés** |
| 4 | Opérateur Red Hat build of Kueue | aucun pour les workloads existants |
| 5 | DSC `kueue: Unmanaged` | aucun (webhooks limités aux projets labellisés) |
| 6 | Dashboard `disableKueue: false` | les **nouveaux** projets du dashboard passent sous Kueue |
| 7 | ResourceFlavors, ClusterQueue, `autoCreateQueues` | LocalQueue `default` dans les projets gérés |
| 8 | Hardware profiles | nouveaux profils visibles dans le dashboard |
| 9 | Projets : libre-service, projets L4, projets existants | selon les projets |
| 10 | Vérification et tests fonctionnels | aucun |

---

## Étape 0 — Prérequis et sauvegarde

```bash
oc version; oc get clusterversion
oc get csv -A | grep -E 'gpu-operator|rhods-operator|cert-manager|kueue'
oc get clusterpolicy; oc get datasciencecluster
oc get nodes -L nvidia.com/gpu.product,nvidia.com/mig.capable,nvidia.com/mig.config -l nvidia.com/gpu.present=true
oc get nodes -l nvidia.com/gpu.present=true -o custom-columns='NODE:.metadata.name,TAINTS:.spec.taints'
oc get machinesets -n openshift-machine-api
oc get ns -l kueue.openshift.io/managed=true; oc get ns -l kueue-managed=true    # projets déjà gérés par Kueue ?
```

Prérequis :
- **NVIDIA GPU operator** installé, et ClusterPolicy `ready` ;
- **RHOAI 3.5** installé, et DSC `Ready` ;
- **cert-manager Operator for Red Hat OpenShift** installé : c'est une dépendance du Red Hat build of Kueue ;
- les nœuds H200 ont `nvidia.com/mig.capable=true`.

Sauvegarde de l'état initial, utilisée par `RETOUR-ARRIERE.md` :

```bash
B=sauvegarde-$(date +%F); mkdir -p $B
oc get clusterpolicy $CP -o yaml > $B/clusterpolicy.yaml
oc get datasciencecluster $DSC -o yaml > $B/dsc.yaml
oc get odhdashboardconfig odh-dashboard-config -n redhat-ods-applications -o yaml > $B/odhdashboardconfig.yaml
oc get hardwareprofiles -A -o yaml > $B/hardwareprofiles.yaml
oc get nodes -L nvidia.com/mig.config -l nvidia.com/gpu.present=true > $B/nodes-mig.txt
oc get crd -o name | sort > $B/crd.txt
oc get ns -l kueue.openshift.io/managed=true -o name > $B/ns-kueue.txt
```

---

## Étape 1 — ClusterPolicy : MIG en stratégie `mixed`

Avec `mixed`, chaque type de tranche est publié sous son propre nom : `nvidia.com/mig-1g.18gb`, `-2g.35gb`, `-3g.71gb`.
C'est indispensable pour distinguer les tailles de tranches dans les quotas et les profils.

```bash
oc get clusterpolicy $CP -o jsonpath='mig={.spec.mig} migManager={.spec.migManager}{"\n"}'
oc patch clusterpolicy $CP --type merge --patch-file $M/01-gpu-operator/clusterpolicy-mig-mixed.patch.yaml
oc get clusterpolicy $CP -o jsonpath='{.status.state}{"\n"}'          # → ready
oc -n $NS_GPU get pods -o wide | grep -E 'device-plugin|feature-discovery|mig-manager'
```

⚠️ Fenêtre de maintenance : le device plugin et GFD redémarrent sur les nœuds GPU.

---

## Étape 2 — *(Optionnel)* Config MIG personnalisée

**À faire seulement si un profil sur mesure est utilisé**, ici `h200-4full-4balanced`. Pour les seuls `all-disabled` et `all-balanced`,
la config par défaut `default-mig-parted-config` suffit : passer à l'étape 3, et retirer `rf-h200-mixte` des fichiers de l'étape 7.

- **Ne jamais modifier `default-mig-parted-config`** : elle appartient à la ClusterPolicy, qui l'écraserait.
- **La config personnalisée remplace entièrement la config par défaut** : elle doit donc contenir **tous** les profils utilisés, dont `all-disabled`.
  Le script part de la config par défaut **du cluster cible**, qui dépend de la version de l'opérateur, et y ajoute
  `manifests/01-gpu-operator/mig-profils-sur-mesure.fragment.yaml`.

```bash
# 2.1 — générer la ConfigMap (lecture seule sur le cluster ; vérifie la présence des profils requis + dry-run)
scripts/generer-config-mig.sh $NS_GPU

# 2.2 — créer la ConfigMap (clé config.yaml)
oc apply -f $M/01-gpu-operator/custom-mig-parted-config.yaml

# 2.3 — faire pointer la ClusterPolicy dessus (profil par défaut des nœuds sans label : all-disabled)
oc patch clusterpolicy $CP --type merge --patch-file $M/01-gpu-operator/clusterpolicy-mig-custom-config.patch.yaml

# 2.4 — vérifier que mig-manager monte la nouvelle ConfigMap et voit les profils sur mesure
oc -n $NS_GPU get ds nvidia-mig-manager -o jsonpath='{range .spec.template.spec.volumes[?(@.name=="mig-parted-config")]}{.configMap.name}{"\n"}{end}'   # → custom-mig-parted-config
oc -n $NS_GPU rollout status ds/nvidia-mig-manager
oc -n $NS_GPU exec ds/nvidia-mig-manager -- sh -c 'grep -l h200-4full-4balanced /mig-parted-config/*'
```

Pour ajouter un autre profil sur mesure : le décrire dans le fragment, relancer 2.1 et 2.2, puis créer **sa** ResourceFlavor et son
entrée dans la ClusterQueue (étape 7). Une ConfigMap modifiée est relue au prochain changement de label `nvidia.com/mig.config` d'un nœud.
Pour réappliquer le même profil, passer le nœud par `all-disabled`, puis revenir au profil.

---

## Étape 3 — Découper les nœuds H200 (label `nvidia.com/mig.config`)

Un profil par nœud (ou par MachineSet) :

| Profil | Nœuds | Ressources publiées par nœud 8 × H200 | Flavor |
|---|---|---|---|
| `all-disabled` | H200 entiers | `nvidia.com/gpu: 8` | `rf-h200` |
| `all-balanced` | H200 découpés | `mig-1g.18gb: 16`, `mig-2g.35gb: 8`, `mig-3g.71gb: 8` | `rf-h200-mig` |
| `h200-4full-4balanced` *(sur mesure)* | mixtes | `gpu: 4`, `mig-1g.18gb: 8`, `mig-2g.35gb: 4`, `mig-3g.71gb: 4` | `rf-h200-mixte` |

⚠️ Avant de changer le profil d'un nœud, **le vider de ses workloads GPU**, car mig-manager arrête les pods GPU du nœud.

```bash
oc label node <h200-entier-1> <h200-entier-2> nvidia.com/mig.config=all-disabled --overwrite
oc label node <h200-mig-1>                    nvidia.com/mig.config=all-balanced --overwrite
oc label node <h200-mixte-1>                  nvidia.com/mig.config=h200-4full-4balanced --overwrite   # si étape 2 faite

# suivi : pending → success (un reset GPU ou un redémarrage du nœud peut être nécessaire selon le driver)
oc get nodes -l nvidia.com/gpu.product=NVIDIA-H200 -L nvidia.com/mig.config,nvidia.com/mig.config.state -w
oc -n $NS_GPU logs ds/nvidia-mig-manager --tail=50
```

**MachineSets** : reporter le label `nvidia.com/mig.config` et le taint GPU dans le `spec.template` de chaque MachineSet GPU
(voir `manifests/01-gpu-operator/machineset-gpu-exemple.snippet.yaml`). Les nouveaux nœuds seront ainsi découpés dès leur arrivée.

Vérification :

```bash
oc get nodes -l nvidia.com/gpu.present=true -o custom-columns='NODE:.metadata.name,PRODUCT:.metadata.labels.nvidia\.com/gpu\.product,MIG:.metadata.labels.nvidia\.com/mig\.config,STATE:.metadata.labels.nvidia\.com/mig\.config\.state,GPU:.status.allocatable.nvidia\.com/gpu,1G18:.status.allocatable.nvidia\.com/mig-1g\.18gb,2G35:.status.allocatable.nvidia\.com/mig-2g\.35gb,3G71:.status.allocatable.nvidia\.com/mig-3g\.71gb'
```

Attendu : `STATE=success` et les ressources du tableau ci-dessus. Les nœuds L4 restent sans `mig.config` (`mig.capable=false`).

---

## Étape 4 — Red Hat build of Kueue Operator

```bash
oc apply -f $M/02-kueue-operator/kueue-operator.yaml
oc -n openshift-kueue-operator get installplan          # RequiresApproval
# contenu attendu : 1 CSV kueue-operator.v1.3.x, 1 CRD kueues.kueue.openshift.io, objets RBAC
oc -n openshift-kueue-operator get installplan <ip> -o jsonpath='{range .status.plan[*]}{.resource.kind} {.resource.name}{"\n"}{end}'
oc -n openshift-kueue-operator patch installplan <ip> --type merge -p '{"spec":{"approved":true}}'
oc -n openshift-kueue-operator get csv,pods              # CSV Succeeded, openshift-kueue-operator Running
```

Les CRDs `kueue.x-k8s.io` (ResourceFlavor, ClusterQueue, LocalQueue…) n'existent qu'après l'étape 5.

---

## Étape 5 — DSC : `kueue: Unmanaged`

```bash
oc get datasciencecluster $DSC -o jsonpath='{.spec.components.kueue}{"\n"}'
oc patch datasciencecluster $DSC --type merge --patch-file $M/03-rhoai/dsc-kueue-unmanaged.patch.yaml
oc get datasciencecluster $DSC -o jsonpath='{range .status.conditions[?(@.type=="KueueReady")]}{.status} {.message}{"\n"}{end}'   # True
# CR Kueue créé par RHOAI (⚠ "oc get kueue" désigne le composant RHOAI, pas ce CR)
oc get kueues.kueue.openshift.io cluster -o jsonpath='{.spec.config.integrations.frameworks}{"\n"}'
oc get kueues.kueue.openshift.io cluster -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.message}{"\n"}{end}'
oc -n openshift-kueue-operator get pods                  # + kueue-controller-manager x2 Running
oc get crd | grep kueue.x-k8s.io                         # 11 CRDs
# les webhooks Kueue ne visent que les namespaces labellisés → aucun impact sur les workloads existants
oc get mutatingwebhookconfiguration kueue-mutating-webhook-configuration -o jsonpath='{.webhooks[?(@.name=="mpod.kb.io")].namespaceSelector}{"\n"}'
```

Attendu : `KueueReady=True`, CR Kueue `Available=True`. Frameworks actifs : `Deployment, Pod, PyTorchJob, RayCluster, RayJob, StatefulSet, TrainJob`.
Les workbenches (StatefulSet) et le serving (Deployment) sont couverts. **`BatchJob` ne l'est pas** (voir le README, § 5).
Le composant RHOAI `default-kueue` affiche `DeploymentsAvailable=False` : c'est normal en mode `Unmanaged`.

---

## Étape 6 — Dashboard : activer Kueue

```bash
oc get odhdashboardconfig odh-dashboard-config -n redhat-ods-applications -o jsonpath='{.spec.dashboardConfig}{"\n"}'
oc patch odhdashboardconfig odh-dashboard-config -n redhat-ods-applications --type merge \
  --patch-file $M/03-rhoai/odhdashboardconfig-kueue.patch.yaml
sleep 30; oc get odhdashboardconfig odh-dashboard-config -n redhat-ods-applications -o jsonpath='disableKueue={.spec.dashboardConfig.disableKueue}{"\n"}'   # false, et le reste
```

Les projets créés depuis le dashboard **à partir de maintenant** reçoivent les labels `kueue.openshift.io/managed=true`.
Les projets existants ne changent pas (voir l'étape 9).

---

## Étape 7 — ResourceFlavors, ClusterQueue `default` et LocalQueues automatiques

**Avant d'appliquer** : adapter les **tolérations** des flavors au taint réel des nœuds GPU, et les **quotas** de la ClusterQueue au nombre réel
de nœuds de chaque profil (formules en tête de `clusterqueue-default.yaml`). Si le profil sur mesure n'est pas utilisé, supprimer `rf-h200-mixte`
de `resourceflavors.yaml` et de la ClusterQueue.

```bash
# 7.1 — flavors + ClusterQueue (annotée opendatahub.io/managed=false) — AVANT autoCreateQueues
oc apply -f $M/04-kueue-queues/resourceflavors.yaml
oc apply -f $M/04-kueue-queues/clusterqueue-default.yaml
oc get clusterqueue default -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.message}{"\n"}{end}'    # Active=True
oc get clusterqueue default -o jsonpath='{range .spec.resourceGroups[0].flavors[*]}{.name}: {range .resources[*]}{.name}={.nominalQuota} {end}{"\n"}{end}'
oc get clusterqueue default -o jsonpath='{.spec}' > cq-default-avant.json

# 7.2 — création automatique des LocalQueues "default" dans les projets gérés
oc patch datasciencecluster $DSC --type merge --patch-file $M/03-rhoai/dsc-kueue-autocreatequeues.patch.yaml
sleep 40
oc get localqueues -A                                       # default → default dans chaque projet géré
oc get resourceflavors                                       # + default-flavor, nvidia-gpu-flavor (créées par RHOAI, inutilisées)
diff <(cat cq-default-avant.json) <(oc get clusterqueue default -o jsonpath='{.spec}') && echo "ClusterQueue default intacte"
```

Les flavors `default-flavor` et `nvidia-gpu-flavor` ne sont référencées par aucune ClusterQueue utilisée. RHOAI les recrée tant que
`autoCreateQueues: true` : les laisser en place.

---

## Étape 8 — Hardware profiles

**Avant d'appliquer** : adapter la toleration du profil `l4` au taint réel des nœuds L4, et les bornes CPU/mémoire de chaque profil.

```bash
oc apply -f $M/05-hardware-profiles/hardware-profiles-h200.yaml
oc apply -f $M/05-hardware-profiles/hardware-profile-l4.yaml
oc get hardwareprofiles -n redhat-ods-applications -l gpu-kueue.livrable/profile=true \
  -o custom-columns='PROFIL:.metadata.name,AFFICHÉ:.metadata.annotations.opendatahub\.io/display-name,TYPE:.spec.scheduling.type,QUEUE:.spec.scheduling.kueue.localQueueName,NODESELECTOR:.spec.scheduling.node.nodeSelector'
```

Dans le dashboard (Settings → Hardware profiles), les 5 profils apparaissent. Les profils `Queue` ne sont proposés que dans les projets gérés
par Kueue, et `l4` seulement dans les projets hors Kueue.

---

## Étape 9 — Projets

| Cas | Action |
|---|---|
| **Nouveau projet H200** | L'utilisateur le crée depuis le dashboard → labels Kueue + LocalQueue `default` automatiques. Rien à faire |
| **Projet L4** | L'utilisateur crée le projet, puis un admin le sort de Kueue : `scripts/projet-hors-kueue.sh <projet>` |
| **Projet existant** (créé avant l'étape 6) qui doit utiliser les H200 | `scripts/projet-dans-kueue.sh <projet>` |
| **Projet qui doit disposer d'un quota dédié** | `PROCEDURE-QUEUE-SPECIFIQUE.md` |

`projet-hors-kueue.sh` retire **les deux** labels (`kueue.openshift.io/managed` et `kueue-managed`) et la LocalQueue, puis vérifie
au bout de 60 s que RHOAI ne les a pas remis. Il refuse d'agir si le projet a encore des workloads Kueue.

---

## Étape 10 — Vérification et tests fonctionnels

```bash
scripts/verifier-deploiement.sh $NS_GPU         # attendu : RÉSULTAT : OK
```

Tests dans le dashboard, avec un compte **non administrateur** :

1. **Créer un projet H200** depuis le dashboard, puis vérifier :
   `oc get ns <p> --show-labels | tr ',' '\n' | grep kueue` et `oc -n <p> get localqueue default`.
2. **Créer un workbench** avec `h200-mig-2g35` dans ce projet, puis vérifier l'admission et le placement :
   ```bash
   oc -n <p> get workloads -o custom-columns='WL:.metadata.name,ADMIS:.status.conditions[?(@.type=="Admitted")].status,FLAVOR:.status.admission.podSetAssignments[0].flavors'
   oc -n <p> get pod <workbench>-0 -o jsonpath='{.spec.nodeName} {.spec.nodeSelector}{"\n"}'     # nœud all-balanced
   ```
3. Refaire le test avec `h200-full`. Attendu : flavor `rf-h200`, sur un nœud `all-disabled`.
4. **Saturation** : demander plus de GPU que le quota disponible. Attendu : le workload reste en attente, avec le message
   `insufficient unused quota … in flavor rf-h200`, puis il est admis dès qu'un workload se termine.
   ```bash
   oc get clusterqueue default -o jsonpath='admis={.status.admittedWorkloads} attente={.status.pendingWorkloads}{"\n"}'
   oc -n <p> get workload <wl> -o jsonpath='{.status.conditions[?(@.type=="QuotaReserved")].message}{"\n"}'
   ```
5. **Projet L4** : créer un projet, lancer `scripts/projet-hors-kueue.sh <p>`, puis créer un workbench avec le profil `l4`. Attendu : pod sur un nœud
   `NVIDIA-L4`, sans Workload Kueue. Vérifier aussi que les profils H200 n'y sont pas utilisables.

Suivi courant :

```bash
oc get clusterqueue default -o jsonpath='{range .status.flavorsUsage[*]}{.name}: {range .resources[*]}{.name}={.total} {end}{"\n"}{end}'
oc get localqueues -A
oc get workloads -A
```
