# GPU H200 (entiers + MIG) et L4 sur OpenShift AI : Kueue et hardware profiles

Livrable de déploiement **pour le cluster cible** (OpenShift + Red Hat OpenShift AI). Il est autonome : ce répertoire
contient tous les manifests, scripts et procédures nécessaires.

| Document | Contenu |
|---|---|
| `README.md` (ce fichier) | Objectif, architecture, **schéma des liens entre objets Kubernetes**, inventaire des fichiers, valeurs à adapter, points d'attention |
| `GUIDE-MISE-EN-OEUVRE.md` | **À lire en premier avec le client** : les questions à poser dans l'ordre, la fiche réponses, puis l'ordre d'application (« recette ») |
| `PROCEDURE-DEPLOIEMENT.md` | **Procédure pas à pas**, avec vérifications à chaque étape |
| `PROCEDURE-QUEUE-SPECIFIQUE.md` | Donner à un projet **une LocalQueue / un quota dédiés**, autres que la queue `default` |
| `RETOUR-ARRIERE.md` | Désinstallation complète, dans l'ordre |

---

## 1. Objectif

| GPU | Mode d'accès | Mécanisme |
|---|---|---|
| **H200 entiers** | Kueue (quotas, file d'attente) | Hardware profile `Queue` → LocalQueue `default` → ClusterQueue `default` → flavor `rf-h200` |
| **H200 découpés MIG** (`all-balanced` : 2×1g.18gb + 1×2g.35gb + 1×3g.71gb par GPU) | Kueue | Profils `Queue` par taille de tranche → flavor `rf-h200-mig` |
| **H200 profil MIG sur mesure** `h200-4full-4balanced` (*optionnel*) | Kueue | Config MIG personnalisée → flavor `rf-h200-mixte` (débordement GPU entiers + tranches) |
| **L4** | Hors Kueue | Hardware profile `Node` (nodeSelector `nvidia.com/gpu.product=NVIDIA-L4` + toleration), dans des projets sortis de Kueue |

Principes retenus :
- **Libre-service** : les utilisateurs créent leurs projets depuis le dashboard. Les projets sont automatiquement gérés par Kueue
  et reçoivent une LocalQueue `default`. Les hardware profiles sont globaux, sans affectation par projet.
- **Une seule ClusterQueue `default`** pour tous les H200 : les ResourceFlavors distinguent les types de nœuds, et les quotas à 0
  empêchent une demande d'arriver sur le mauvais type.
- **L4 hors Kueue** (« option A ») : un projet utilise **soit** les H200 (via Kueue), **soit** les L4 (via nodeSelector). C'est un admin qui sort le projet de Kueue.
- **Pas de MachineSet à créer** : Kueue et RHOAI ciblent les nœuds par leurs **labels**. Les MachineSets existants ne font que porter
  le label `nvidia.com/mig.config` et le taint GPU dans leur `spec.template`.

Versions de référence : OpenShift 4.20, RHOAI 3.5.1, NVIDIA GPU operator v26.3.3, Red Hat build of Kueue v1.3.2 (canal `stable-v1.3`), API Kueue `v1beta2`.

---

## 2. Schéma des liens entre objets Kubernetes

### 2.1 Vue d'ensemble

```
 ┌───────────────────────────── COUCHE GPU (NVIDIA GPU operator) ──────────────────────────────┐
 │                                                                                             │
 │  ClusterPolicy gpu-cluster-policy                ConfigMap custom-mig-parted-config         │
 │   spec.mig.strategy: mixed                       (optionnelle, ns nvidia-gpu-operator)      │
 │   spec.migManager.config.name ─────────────────► profils all-disabled, all-balanced,        │
 │        │                                         h200-4full-4balanced…                      │
 │        ▼ déploie                                                                            │
 │  DaemonSets : mig-manager, device-plugin, GFD                                               │
 │        │                                                                                    │
 │        ▼ sur chaque nœud GPU                                                                │
 │  Node  labels (admin / MachineSet) : nvidia.com/mig.config=<profil>                         │
 │        labels (GFD)                : nvidia.com/gpu.product=NVIDIA-H200 | NVIDIA-L4 …       │
 │        labels (mig-manager)        : nvidia.com/mig.config.state=success                    │
 │        taint                       : nvidia.com/gpu:NoSchedule (selon le client)           │
 │        allocatable (device-plugin) : nvidia.com/gpu, nvidia.com/mig-1g.18gb / 2g.35gb / 3g.71gb │
 └──────────────────────────────────────────────────▲──────────────────────────────────────────┘
                                                    │ nodeLabels + tolerations
 ┌──────────────────────────── COUCHE KUEUE ─────────┼──────────────────────────────────────────┐
 │  Subscription kueue-operator ──► CSV ──► (CR créé par RHOAI) Kueue "cluster"               │
 │                                           frameworks: Pod, Deployment, StatefulSet, Ray…    │
 │                                           → kueue-controller-manager + webhooks             │
 │                                                                                             │
 │  ResourceFlavor rf-h200        nodeLabels gpu.product=NVIDIA-H200, mig.config=all-disabled ─┤
 │  ResourceFlavor rf-h200-mig    nodeLabels gpu.product=NVIDIA-H200, mig.config=all-balanced ─┤
 │  ResourceFlavor rf-h200-mixte  nodeLabels …, mig.config=h200-4full-4balanced (optionnelle) ─┘
 │        ▲ flavors (ordre d'essai) + quotas
 │  ClusterQueue default  (annotation opendatahub.io/managed=false)
 │        ▲ spec.clusterQueue
 │  LocalQueue default  (une par projet géré, créée par RHOAI : autoCreateQueues)
 └────────▲────────────────────────────────────────────────────────────────────────────────────┘
          │ spec.scheduling.kueue.localQueueName: default
 ┌────────┴──────────────────── COUCHE RHOAI ──────────────────────────────────────────────────┐
 │  DataScienceCluster default-dsc : kueue.managementState=Unmanaged, autoCreateQueues=true    │
 │        └─► crée le CR Kueue "cluster" + les LocalQueues default des projets gérés          │
 │  OdhDashboardConfig : disableKueue=false                                                    │
 │        └─► nouveaux projets labellisés kueue.openshift.io/managed=true (+ kueue-managed)   │
 │                                                                                             │
 │  HardwareProfile h200-full / h200-mig-1g18 / -2g35 / -3g71   type Queue → LocalQueue default│
 │  HardwareProfile l4                                           type Node  → nodeSelector     │
 │                                                                 gpu.product=NVIDIA-L4 ──────┼──► nœuds L4
 └─────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Chemin d'un workload H200 (workbench, InferenceService…)

```
Utilisateur (dashboard) — projet géré par Kueue — choisit le profil h200-mig-2g35
   │
   ▼
Notebook / InferenceService
   annotation opendatahub.io/hardware-profile-name: h200-mig-2g35
   label      kueue.x-k8s.io/queue-name: default            ← posé par RHOAI depuis le profil
   resources  nvidia.com/mig-2g.35gb: 1, cpu, memory          ← posés depuis les identifiers du profil
   │
   ▼ (StatefulSet / Deployment → Pod avec scheduling gate kueue.x-k8s.io/admission)
Workload (kueue.x-k8s.io)  ──► LocalQueue default ──► ClusterQueue default
   │                                                   essai des flavors dans l'ordre :
   │                                                   rf-h200 : quota mig-2g.35gb = 0 → NoFit
   │                                                   rf-h200-mig : quota disponible → ADMIS
   ▼
Pod : gate levé, nodeSelector {gpu.product: NVIDIA-H200, mig.config: all-balanced}, tolerations injectés par la flavor
   │
   ▼ kube-scheduler
Nœud H200 en all-balanced (allocatable nvidia.com/mig-2g.35gb décrémenté)
```

Sans quota disponible, le workload reste en file d'attente : le pod reste `SchedulingGated`, et il est admis dès qu'un quota se libère.

### 2.3 Chemin d'un workload L4

```
Projet SORTI de Kueue (labels kueue.openshift.io/managed et kueue-managed retirés, pas de LocalQueue)
   └─ profil l4 (type Node) → Pod nodeSelector nvidia.com/gpu.product=NVIDIA-L4 + toleration → nœud L4
      (aucun Workload Kueue, aucun quota : seule la capacité des nœuds limite)
```

### 2.4 Correspondance profil → ressource → flavor → nœuds

| Hardware profile | Type | Ressource demandée | Flavor(s) possibles (ordre) | Nœuds (`nvidia.com/mig.config`) |
|---|---|---|---|---|
| `h200-full` | Queue | `nvidia.com/gpu` | `rf-h200` → `rf-h200-mixte` | `all-disabled` → `h200-4full-4balanced` |
| `h200-mig-1g18` | Queue | `nvidia.com/mig-1g.18gb` | `rf-h200-mig` → `rf-h200-mixte` | `all-balanced` → `h200-4full-4balanced` |
| `h200-mig-2g35` | Queue | `nvidia.com/mig-2g.35gb` | `rf-h200-mig` → `rf-h200-mixte` | idem |
| `h200-mig-3g71` | Queue | `nvidia.com/mig-3g.71gb` | `rf-h200-mig` → `rf-h200-mixte` | idem |
| `l4` | Node | `nvidia.com/gpu` | — (hors Kueue) | nœuds `gpu.product=NVIDIA-L4` |

---

## 3. Inventaire des fichiers

```
livrable-client-gpu-h200-l4-kueue/
├── README.md
├── GUIDE-MISE-EN-OEUVRE.md
├── PROCEDURE-DEPLOIEMENT.md
├── PROCEDURE-QUEUE-SPECIFIQUE.md
├── RETOUR-ARRIERE.md
├── manifests/
│   ├── 01-gpu-operator/
│   ├── 02-kueue-operator/
│   ├── 03-rhoai/
│   ├── 04-kueue-queues/
│   ├── 05-hardware-profiles/
│   └── 06-queue-specifique-exemple/
└── scripts/
```

| Fichier | Type | Rôle | Étape |
|---|---|---|---|
| **manifests/01-gpu-operator/** | | | |
| `clusterpolicy-mig-mixed.patch.yaml` | patch merge | ClusterPolicy : `mig.strategy: mixed` + `migManager.enabled`. Publication des ressources `nvidia.com/mig-*` | 1 |
| `clusterpolicy-mig-custom-config.patch.yaml` | patch merge | ClusterPolicy : mig-manager utilise `custom-mig-parted-config` (profil par défaut `all-disabled`). *Optionnel* | 2 |
| `custom-mig-parted-config.yaml` | ConfigMap | Config MIG complète = config par défaut v26.3.3 (81 profils) + 2 profils sur mesure. **À régénérer** pour la version du client (`scripts/generer-config-mig.sh`). *Optionnel* | 2 |
| `mig-profils-sur-mesure.fragment.yaml` | fragment YAML | **Source des profils sur mesure** (`h200-4full-4balanced`, `h200-all-1g18`), à modifier pour ajouter ou changer des profils | 2 |
| `mig-config-custom.yaml` | config mig-parted | Contenu de `config.yaml` de la ConfigMap (défaut + fragment). Généré | 2 |
| `mig-config-default-v26.3.3.yaml` | référence | Copie de `default-mig-parted-config` du GPU operator v26.3.3 (lecture seule, pour comparaison) | — |
| `machineset-gpu-exemple.snippet.yaml` | extrait | Labels `nvidia.com/mig.config` et taint GPU à reporter dans le `spec.template` des MachineSets GPU (pas un objet à appliquer) | 3 |
| **manifests/02-kueue-operator/** | | | |
| `kueue-operator.yaml` | Namespace + OperatorGroup + Subscription | Red Hat build of Kueue, canal `stable-v1.3`, approbation **manuelle** de l'InstallPlan | 4 |
| **manifests/03-rhoai/** | | | |
| `dsc-kueue-unmanaged.patch.yaml` | patch merge | DSC : `kueue: Unmanaged` (RHOAI crée le CR Kueue), `autoCreateQueues: false`, queues par défaut nommées `default` | 5 |
| `dsc-kueue-autocreatequeues.patch.yaml` | patch merge | DSC : `autoCreateQueues: true` (LocalQueue `default` dans chaque projet géré). **Après** la ClusterQueue protégée | 7 |
| `odhdashboardconfig-kueue.patch.yaml` | patch merge | Dashboard : `disableKueue: false` (label Kueue automatique sur les nouveaux projets, profils Queue proposés) | 6 |
| **manifests/04-kueue-queues/** | | | |
| `resourceflavors.yaml` | 3 × ResourceFlavor | `rf-h200`, `rf-h200-mig`, `rf-h200-mixte` (optionnelle) : nodeLabels + tolérations **à adapter** | 7 |
| `clusterqueue-default.yaml` | ClusterQueue | `default`, annotée `opendatahub.io/managed: "false"`, quotas **à adapter** au nombre de nœuds | 7 |
| **manifests/05-hardware-profiles/** | | | |
| `hardware-profiles-h200.yaml` | 4 × HardwareProfile | `h200-full`, `h200-mig-1g18`, `h200-mig-2g35`, `h200-mig-3g71` : type Queue → `default` | 8 |
| `hardware-profile-l4.yaml` | HardwareProfile | `l4` : type Node, nodeSelector `NVIDIA-L4` + toleration **à adapter** | 8 |
| **manifests/06-queue-specifique-exemple/** (voir `PROCEDURE-QUEUE-SPECIFIQUE.md`) | | | |
| `clusterqueue-equipe-a.yaml` | ClusterQueue | Quota réservé au projet `equipe-a` (namespaceSelector) | QS |
| `localqueue-default-redirigee.yaml` | LocalQueue | Variante A : la queue `default` du projet pointe vers `cq-equipe-a` | QS |
| `localqueue-equipe-a.yaml` | LocalQueue | Variante B : queue supplémentaire `equipe-a` dans le projet | QS |
| `hardware-profile-equipe-a.yaml` | HardwareProfile | Variante B : profil propre au projet (namespace `equipe-a`) → LocalQueue `equipe-a` | QS |
| **scripts/** | | | |
| `generer-config-mig.sh` | bash (lecture seule sur le cluster) | Régénère `custom-mig-parted-config.yaml` à partir de la config par défaut **du cluster cible** et du fragment, puis valide en dry-run | 2 |
| `projet-hors-kueue.sh <projet>` | bash | Sort un projet de Kueue (retire les **deux** labels et la LocalQueue, puis vérifie au bout de 60 s) pour l'usage des L4 | 9 |
| `projet-dans-kueue.sh <projet>` | bash | Rattache à Kueue un projet existant (labels + attente de la LocalQueue `default`) | 9 |
| `verifier-deploiement.sh [ns-gpu-operator]` | bash (lecture seule) | Contrôle de bout en bout : MIG, Kueue, DSC, dashboard, flavors, ClusterQueue, LocalQueues, profils | 10 |

Tous les manifests ont été validés avec `oc apply --dry-run=server` (ou `oc patch --dry-run=server`) contre
OpenShift 4.20.28, RHOAI 3.5.1, GPU operator v26.3.3 et Kueue v1.3.2.

---

## 4. Valeurs à adapter avant le déploiement

| Valeur | Fichiers | Comment l'obtenir |
|---|---|---|
| Namespace du GPU operator (`nvidia-gpu-operator`) | `custom-mig-parted-config.yaml`, scripts | `oc get clusterpolicy -o jsonpath='{.items[0].metadata.namespace}'` ou `oc get csv -A \| grep gpu-operator` |
| Nom de la ClusterPolicy (`gpu-cluster-policy`) | commandes de la procédure | `oc get clusterpolicy` |
| Nom de la DSC (`default-dsc`) | commandes de la procédure | `oc get datasciencecluster` |
| **Taint des nœuds GPU** (exemple : `nvidia.com/gpu:NoSchedule`) | `resourceflavors.yaml`, `hardware-profile-l4.yaml`, `machineset-gpu-exemple.snippet.yaml` | `oc get nodes -l nvidia.com/gpu.present=true -o custom-columns=N:.metadata.name,T:.spec.taints` |
| Valeur exacte de `nvidia.com/gpu.product` (`NVIDIA-H200`, `NVIDIA-L4`) | flavors, profil L4 | `oc get nodes -L nvidia.com/gpu.product` (posé par GFD) |
| **Nombre de nœuds par profil MIG** et leur allocatable CPU/mémoire | `clusterqueue-default.yaml` (quotas) | `oc get nodes -L nvidia.com/mig.config` et `oc describe node` |
| Profil sur mesure utilisé ou non | flavor `rf-h200-mixte`, entrée correspondante de la ClusterQueue, étape 2 | Si non utilisé : supprimer la flavor, son entrée dans la ClusterQueue, et sauter l'étape 2 |
| Canal Kueue | `kueue-operator.yaml` | Matrice de support RHOAI 3.x pour la version RHOAI du client |
| Bornes CPU/mémoire proposées | hardware profiles | Selon les usages |

---

## 5. Points d'attention

Ils sont tous intégrés à la procédure. Les voici rassemblés pour mémoire.

1. **Dashboard : `disableKueue` vaut `true` par défaut** (RHOAI 3.5). Sans le patch, les projets ne sont pas labellisés et les profils Queue ne sont pas proposés.
   Seuls les projets créés **après** le patch sont labellisés automatiquement.
2. **Le dashboard ne crée pas de LocalQueue.** C'est l'opérateur RHOAI qui la crée, et seulement avec `autoCreateQueues: true`. Il crée alors aussi
   **sa** ClusterQueue `default` et deux flavors (`default-flavor`, `nvidia-gpu-flavor`, inutilisées). → Appliquer **avant** la ClusterQueue
   annotée `opendatahub.io/managed: "false"`, que l'opérateur ne modifie pas.
3. **Sortir un projet de Kueue** (L4) : retirer **les deux** labels, `kueue.openshift.io/managed` **et** `kueue-managed`. Sinon RHOAI remet le premier.
4. **Dans un projet géré par Kueue, tout profil de type Node est refusé**, y compris un éventuel profil GPU historique sans queue.
5. **Le CR Kueue créé par RHOAI n'active pas `BatchJob`** (frameworks : Deployment, Pod, PyTorchJob, RayCluster, RayJob, StatefulSet, TrainJob).
   Les `batch/v1 Job` ne passent pas par Kueue. Si c'est nécessaire, ajouter `BatchJob` au CR `kueues.kueue.openshift.io/cluster`, en vérifiant que RHOAI ne l'écrase pas.
6. **`oc get kueue` est ambigu** : il désigne le composant RHOAI. Le CR de l'opérateur s'appelle `kueues.kueue.openshift.io/cluster`.
7. **`spec.clusterQueue` d'une LocalQueue est immuable** : pour la rediriger, il faut la supprimer puis la recréer.
8. **La config MIG personnalisée remplace entièrement la config par défaut** : partir d'une copie de la config par défaut du cluster (script fourni).
   Ne jamais modifier `default-mig-parted-config`, qui appartient à la ClusterPolicy.
9. **Pods créés par un administrateur** dans un projet géré par Kueue : s'ils reçoivent la SCC `anyuid`, Kueue ne peut plus les mettre à jour
   pour lever le scheduling gate, et ils restent `SchedulingGated`. Les workloads créés par les utilisateurs et par RHOAI ne sont pas concernés.
   Pour des pods de test créés en admin : annotation `openshift.io/required-scc: restricted-v2`.
10. **Stockage des workbenches** : si le stockage est local aux nœuds (LVM Storage, `WaitForFirstConsumer`), les nœuds GPU doivent eux-mêmes
    fournir du stockage. Sinon, un workbench avec PVC envoyé sur un nœud GPU reste `Pending`.
11. **Reconfiguration MIG** : mig-manager arrête les pods GPU du nœud, et un reset GPU ou un redémarrage peut être nécessaire. À planifier en maintenance.
12. **Labels de topologie** : ajouter `topology.kubernetes.io/zone` sur **une partie** seulement des nœuds modifie le placement de certains opérateurs
    de la plateforme (par exemple le registry interne). Garder des labels de zone cohérents sur tous les nœuds.
13. **Après un redémarrage d'un nœud GPU**, un pod GPU peut finir en `Failed / UnexpectedAdmissionError`, s'il a été admis avant que le device plugin soit prêt.
    Son contrôleur le remplace : il suffit de supprimer le pod en échec.
