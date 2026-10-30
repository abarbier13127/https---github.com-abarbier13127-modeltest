# Guide de mise en œuvre chez le client — la recette

Ce guide accompagne l'atelier avec le client, comme une recette : on réunit d'abord les **ingrédients** (les réponses aux questions),
on **prépare** (on adapte les fichiers selon ces réponses), on **cuit** (on applique dans l'ordre), puis on **goûte** (on vérifie).

Les commandes détaillées sont dans `PROCEDURE-DEPLOIEMENT.md`. Les numéros « Étape N » y renvoient. Le principe et le schéma
des objets sont dans `README.md`.

```
 1. Ingrédients ─► 2. Fiche réponses ─► 3. Préparation ─► 4. Cuisson ─► 5. Dégustation ─► 6. Service
   (questions)      (à remplir)          (fichiers)        (cluster)      (tests)            (passage aux utilisateurs)
```

---

## 1. Les ingrédients : les questions à poser, dans l'ordre

L'ordre compte : les réponses des premiers blocs conditionnent les questions suivantes.
Pour chaque question : **pourquoi** on la pose, **comment** obtenir la réponse, et ce qu'elle **change** dans le livrable.

### Bloc A — Le socle (bloquant : sans ces réponses, on ne commence pas)

| # | Question | Pourquoi | Comment obtenir la réponse | Ce que ça change |
|---|---|---|---|---|
| A1 | Quelles versions d'**OpenShift**, de **RHOAI** et du **NVIDIA GPU operator** ? | Le livrable est validé pour OCP 4.20, RHOAI 3.5.1 et GPU operator v26.3.3. Le comportement de Kueue et du dashboard dépend de la version de RHOAI | `oc get clusterversion` ; `oc get csv -A \| grep -E 'rhods\|gpu-operator'` | Si RHOAI ≠ 3.5 : relire les points d'attention du README, § 5. Si GPU operator ≠ v26.3.3 : régénérer la config MIG (A5, C3) |
| A2 | Quel **canal du Red Hat build of Kueue** est supporté pour cette version de RHOAI ? | Kueue doit correspondre à la matrice de support | Matrice « Red Hat OpenShift AI: Supported Configurations for 3.x » (access.redhat.com) | `manifests/02-kueue-operator/kueue-operator.yaml` → `channel` |
| A3 | **cert-manager Operator** installé ? | Dépendance obligatoire du Red Hat build of Kueue | `oc get csv -A \| grep cert-manager` | Si non : l'installer avant l'étape 4 |
| A4 | **Kueue** est-il déjà utilisé (intégré à RHOAI, ou un autre Kueue) ? | On ne peut pas avoir deux contrôleurs Kueue. Le mode `Managed` (intégré) est déprécié | `oc get datasciencecluster -o jsonpath='{.items[0].spec.components.kueue}'` ; `oc get crd \| grep kueue` | Si Kueue est déjà en `Managed` avec des workloads : prévoir une **migration** (hors périmètre de ce livrable) |
| A5 | Namespace du GPU operator, et noms de la ClusterPolicy et de la DSC ? | Ils sont utilisés dans toutes les commandes | `oc get clusterpolicy` ; `oc get csv -A \| grep gpu-operator` ; `oc get datasciencecluster` | Variables `NS_GPU`, `CP`, `DSC` de la procédure, et `namespace:` de la ConfigMap MIG |

### Bloc B — Le parc GPU (définit les flavors et les quotas)

| # | Question | Pourquoi | Comment obtenir la réponse | Ce que ça change |
|---|---|---|---|---|
| B1 | Combien de **nœuds H200** et de **nœuds L4**, et combien de GPU par nœud ? | Ce sont les bases des quotas | `oc get nodes -L nvidia.com/gpu.product,nvidia.com/gpu.count -l nvidia.com/gpu.present=true` | Quotas de `clusterqueue-default.yaml`, `maxCount` des profils |
| B2 | Valeur exacte de **`nvidia.com/gpu.product`** pour chaque modèle ? | Les flavors et le profil L4 sélectionnent sur ce label (posé par GFD) | `oc get nodes -L nvidia.com/gpu.product` (par exemple `NVIDIA-H200`, `NVIDIA-H200-NVL`, `NVIDIA-L4`) | `nodeLabels` des flavors, `nodeSelector` du profil `l4` |
| B3 | Les nœuds GPU ont-ils un **taint** ? Lequel ? | Les flavors et le profil L4 doivent le tolérer, sinon aucun pod n'y est placé | `oc get nodes -l nvidia.com/gpu.present=true -o custom-columns=N:.metadata.name,T:.spec.taints` | `tolerations` de `resourceflavors.yaml` et `hardware-profile-l4.yaml`. Pas de taint : supprimer les blocs |
| B4 | Les nœuds GPU viennent-ils de **MachineSets** ou sont-ils en **bare-metal** fixe ? | Les labels MIG et les taints doivent survivre au remplacement d'un nœud | `oc get machinesets -n openshift-machine-api` | MachineSets : labels et taints dans le `spec.template` (étape 3). Bare-metal : `oc label` / `oc adm taint` nœud par nœud |
| B5 | **CPU et mémoire allouables** par type de nœud ? | Ils sont aussi quotés dans la ClusterQueue (`cpu`, `memory`) | `oc get node <n> -o jsonpath='{.status.allocatable}'` | `nominalQuota` cpu/memory de chaque flavor |
| B6 | **Zones** (`topology.kubernetes.io/zone`) : présentes et cohérentes sur **tous** les nœuds ? | Un label de zone sur une partie seulement des nœuds perturbe le placement de certains opérateurs de la plateforme | `oc get nodes -L topology.kubernetes.io/zone` | Signaler et corriger avant tout, hors livrable |

### Bloc C — Le découpage MIG des H200 (définit la ClusterPolicy et les labels)

| # | Question | Pourquoi | Comment obtenir la réponse | Ce que ça change |
|---|---|---|---|---|
| C1 | Quels **usages** : des GPU entiers (entraînement, gros modèles) et/ou des tranches (notebooks, petits modèles) ? Dans quelles proportions ? | C'est ce qui détermine combien de nœuds on découpe | Atelier avec les équipes data et IA | Répartition des nœuds entre les profils (C2) |
| C2 | Pour chaque nœud H200, **quel profil MIG** : `all-disabled` (entier), `all-balanced` (2×1g.18gb + 1×2g.35gb + 1×3g.71gb), un autre profil standard, ou **sur mesure** ? | Un profil par nœud. Chaque profil utilisé sur des nœuds = une ResourceFlavor | Atelier. Liste des profils standard : `oc -n $NS_GPU get cm default-mig-parted-config -o jsonpath='{.data.config\.yaml}' \| grep -B2 -A6 0x2335` | Labels `nvidia.com/mig.config` (étape 3), flavors, ClusterQueue |
| C3 | Un **profil sur mesure** est-il nécessaire (par exemple 4 GPU entiers + 4 découpés sur un même nœud) ? | Il exige une config MIG personnalisée et une ClusterPolicy modifiée | Réponse à C2 | **Oui** : étape 2, éditer `mig-profils-sur-mesure.fragment.yaml`, garder `rf-h200-mixte`. **Non** : sauter l'étape 2, supprimer `rf-h200-mixte` |
| C4 | Quelles **tailles de tranche** exposer aux utilisateurs ? | Une taille = un hardware profile, et une ressource dans `coveredResources` | Réponse à C1/C2 | Profils `h200-mig-*` à garder, ajouter ou supprimer. Nouvelle taille : l'ajouter dans `coveredResources` et dans chaque flavor |
| C5 | La stratégie MIG actuelle est-elle `single` ? Des workloads utilisent-ils `nvidia.com/gpu` sur des H200 déjà découpés ? | Le passage en `mixed` change le nom des ressources publiées | `oc get clusterpolicy -o jsonpath='{.items[0].spec.mig}'` ; `oc get pods -A -o json \| grep -c mig-` | Prévenir les équipes, et adapter leurs workloads qui demandent des tranches sous le nom `nvidia.com/gpu` |
| C6 | **Fenêtre de maintenance** possible pour la reconfiguration MIG ? | mig-manager arrête les pods GPU du nœud, et un reset GPU ou un redémarrage peut être nécessaire | Exploitation du client | Planning des étapes 1 à 3 |

### Bloc D — L'organisation des projets et des quotas (définit Kueue et les profils)

| # | Question | Pourquoi | Comment obtenir la réponse | Ce que ça change |
|---|---|---|---|---|
| D1 | Les utilisateurs créent-ils **eux-mêmes** leurs projets depuis le dashboard ? | Le libre-service repose sur le label automatique (dashboard) et sur la LocalQueue automatique (`autoCreateQueues`) | Mode de gouvernance du client | Si les projets sont créés par l'admin ou par GitOps : poser les labels et la LocalQueue dans le gabarit du projet (`scripts/projet-dans-kueue.sh` comme modèle) |
| D2 | Les **L4 restent-ils hors Kueue** (accès libre par nodeSelector) ? Un projet a-t-il besoin **à la fois** des H200 et des L4 ? | Option A : un projet utilise soit les H200 (Kueue), soit les L4. Un profil de type Node est refusé dans un projet géré par Kueue | Atelier | Oui / non : livrable tel quel. Besoin mixte : mettre les L4 dans Kueue (ClusterQueue et LocalQueue dédiées, hors livrable) |
| D3 | Des équipes ont-elles besoin d'un **quota réservé** ? Lesquelles, et combien ? | Il faut alors une ClusterQueue dédiée par équipe, sans emprunt entre équipes (pas de cohort en RHOAI 3.5) | Atelier | `PROCEDURE-QUEUE-SPECIFIQUE.md`. Baisser d'autant les quotas de `default` |
| D4 | Pour ces équipes : **tout** le projet sur le quota réservé (variante A), ou un **choix** entre quota réservé et quota partagé (variante B) ? | Les deux variantes ne s'appliquent pas de la même façon | Atelier | Variante A : LocalQueue redirigée. Variante B : LocalQueue et profil propres au projet |
| D5 | **Projets existants** à faire passer sous Kueue ? Lesquels ? | Seuls les projets créés après l'activation sont labellisés automatiquement | `oc get ns -l opendatahub.io/dashboard=true` | `scripts/projet-dans-kueue.sh <projet>` (étape 9). Vérifier qu'aucun workload GPU ne tourne pendant le basculement |
| D6 | Profils GPU **déjà existants** dans le dashboard ? Doivent-ils rester ? | Dans un projet géré par Kueue, tout profil sans queue (type Node) est refusé | `oc get hardwareprofiles -A` | Les remplacer par les profils du livrable, ou les réserver aux projets hors Kueue |
| D7 | Quelles **bornes CPU/mémoire** proposer dans chaque profil ? | Le confort des utilisateurs, et le respect des quotas cpu/memory | Atelier | `defaultCount`/`maxCount` des hardware profiles |

### Bloc E — Les workloads (vérifie que Kueue couvre bien les usages)

| # | Question | Pourquoi | Comment obtenir la réponse | Ce que ça change |
|---|---|---|---|---|
| E1 | Quels types de workloads : **workbenches**, **InferenceService** (KServe), **Ray**, **PyTorchJob / TrainJob**, **`batch/v1 Job`** ? | Le CR Kueue créé par RHOAI couvre Deployment, Pod, StatefulSet, Ray, PyTorchJob et TrainJob, **pas `BatchJob`** | Atelier, et `oc get jobs -A` dans les projets data | Si des Job batch doivent passer par Kueue : ajouter `BatchJob` au CR `kueues.kueue.openshift.io/cluster`, en vérifiant que RHOAI ne l'écrase pas |
| E2 | Les workbenches ont-ils du **stockage persistant** ? Avec quelle StorageClass ? Est-elle **locale au nœud** (LVM, local) ? | Un PVC local à un nœud non GPU empêche le placement sur un nœud GPU | `oc get sc` (provisioner, `volumeBindingMode`) | StorageClass locale : les nœuds GPU doivent fournir ce stockage, sinon il faut une StorageClass réseau |
| E3 | Des pods sont-ils créés **par des comptes admin** dans des projets gérés (scripts, tests) ? | Avec la SCC `anyuid`, Kueue ne peut pas lever le scheduling gate, et le pod reste `SchedulingGated` | Exploitation | Imposer `openshift.io/required-scc: restricted-v2` sur ces pods |

### Bloc F — L'exploitation

| # | Question | Pourquoi | Ce que ça change |
|---|---|---|---|
| F1 | **Qui** sort un projet de Kueue (L4), et qui crée les queues dédiées ? | Ce sont des actions admin, pas du libre-service | Procédure d'exploitation (scripts fournis) |
| F2 | Les manifests passent-ils par **GitOps** (Argo CD) ? | Certains objets sont **gérés par RHOAI** (LocalQueues, flavors par défaut) et ne doivent pas être pilotés par Argo | Mettre dans Git : flavors, ClusterQueue(s), hardware profiles, patchs. **Ne pas y mettre** : LocalQueues `default`, `default-flavor`, `nvidia-gpu-flavor` |
| F3 | Faut-il une **supervision** des files d'attente ? | Les workloads en attente sont le premier symptôme d'un quota sous-dimensionné | `oc get clusterqueue -o wide`, et les métriques Kueue (hors livrable) |

---

## 2. La fiche réponses (à remplir pendant l'atelier)

```
CLUSTER
  OCP ............ RHOAI ............ GPU operator ............ cert-manager [oui/non]
  Canal Kueue supporté ........................   Kueue existant [non / Managed / autre]
  NS_GPU=...................  CP=...................  DSC=...................
PARC
  H200 : ..... nœuds × ..... GPU   gpu.product=...................   allocatable cpu=..... mem=.....
  L4   : ..... nœuds × ..... GPU   gpu.product=...................   allocatable cpu=..... mem=.....
  Taint GPU : key=............ value=............ effect=............   [aucun]
  MachineSets GPU [oui : noms ..........................] / bare-metal
  Zones cohérentes [oui/non]
MIG
  Profil par nœud :  all-disabled → ..... nœuds | all-balanced → ..... | sur mesure (nom ..........) → .....
  Profil sur mesure [oui/non]  →  définition : ......................................................
  Tailles exposées : [ ] 1g.18gb  [ ] 2g.35gb  [ ] 3g.71gb  [ ] autre ......
  Stratégie actuelle ........   Fenêtre de maintenance ...................
PROJETS / QUOTAS
  Libre-service dashboard [oui/non]   L4 hors Kueue [oui/non]   Projets mixtes H200+L4 [oui/non]
  Quotas réservés : équipe ........ → H200 ..... / tranches ..... (variante A/B)
                    équipe ........ → H200 ..... / tranches ..... (variante A/B)
  Projets existants à basculer : ........................................
  Profils GPU existants à retirer : ........................................
WORKLOADS
  Workbenches [ ]  InferenceService [ ]  Ray [ ]  PyTorchJob/TrainJob [ ]  batch Job [ ] → BatchJob à ajouter
  StorageClass workbenches ............ locale au nœud [oui/non]
EXPLOITATION
  GitOps [oui/non]   Responsable sortie Kueue / queues dédiées ...................
```

---

## 3. La préparation (avant de toucher au cluster)

On adapte les fichiers du livrable d'après la fiche, sur un poste et dans Git.

| Ordre | Si… | Action sur les fichiers | Fichiers |
|---|---|---|---|
| P1 | toujours | Renseigner les variables `NS_GPU`, `CP`, `DSC` (A5) | `PROCEDURE-DEPLOIEMENT.md` (en-tête), `custom-mig-parted-config.yaml` (`namespace:`) |
| P2 | toujours | Mettre le **canal Kueue** supporté (A2) | `02-kueue-operator/kueue-operator.yaml` |
| P3 | taint GPU (B3) | Remplacer la toleration exemple `nvidia.com/gpu` par le **vrai taint**, ou supprimer les blocs `tolerations` s'il n'y a pas de taint | `04-kueue-queues/resourceflavors.yaml`, `05-hardware-profiles/hardware-profile-l4.yaml` |
| P4 | `gpu.product` ≠ valeurs du livrable (B2) | Corriger `NVIDIA-H200` / `NVIDIA-L4` | flavors, profil `l4` |
| P5 | pas de profil sur mesure (C3 = non) | Supprimer `rf-h200-mixte` (flavor **et** entrée dans la ClusterQueue) | `resourceflavors.yaml`, `clusterqueue-default.yaml` |
| P6 | profil sur mesure (C3 = oui) | Décrire le ou les profils dans le fragment. Un profil supplémentaire = une flavor supplémentaire, ajoutée en fin de liste dans la ClusterQueue | `01-gpu-operator/mig-profils-sur-mesure.fragment.yaml`, flavors, ClusterQueue |
| P7 | toujours | Calculer les **quotas** à partir de la répartition C2, de B1 et de B5 (formules en tête du fichier), **moins** les quotas réservés (D3) | `04-kueue-queues/clusterqueue-default.yaml` |
| P8 | tailles de tranche (C4) | Garder, supprimer ou ajouter des profils `h200-mig-*`. Une nouvelle taille va aussi dans `coveredResources` et dans chaque flavor | `05-hardware-profiles/hardware-profiles-h200.yaml`, ClusterQueue |
| P9 | toujours | Ajuster les bornes CPU/mémoire et les libellés des profils (D7) | `05-hardware-profiles/*` |
| P10 | quotas réservés (D3) | Copier les exemples `06-…` par équipe, puis renommer `equipe-a`, le namespace, les quotas et la variante | `06-queue-specifique-exemple/*` |
| P11 | MachineSets (B4) | Préparer le patch de chaque MachineSet GPU (label `nvidia.com/mig.config` + taint) | modèle : `01-gpu-operator/machineset-gpu-exemple.snippet.yaml` |
| P12 | BatchJob nécessaire (E1) | Préparer l'ajout de `BatchJob` aux frameworks du CR Kueue | (hors livrable) |
| P13 | toujours | Valider à blanc, contre le cluster du client | `oc apply --dry-run=server -f manifests/04-kueue-queues/ -f manifests/05-hardware-profiles/` (après l'étape 5, quand les CRDs Kueue existent) |

---

## 4. La cuisson (application sur le cluster, dans cet ordre)

Ne pas inverser les étapes marquées 🔒 : l'ordre est imposé par le fonctionnement de RHOAI et de Kueue.

| Ordre | Action | Étape de la procédure | Condition | Coupure / risque | Contrôle avant de passer à la suite |
|---|---|---|---|---|---|
| 1 | Sauvegarde de l'état initial | 0 | toujours | aucun | répertoire `sauvegarde-<date>/` rempli |
| 2 | ClusterPolicy → `mig.strategy: mixed` | 1 | toujours | 🔧 device plugin et GFD redémarrent (fenêtre C6) | ClusterPolicy `ready` |
| 3 | Générer et appliquer la config MIG personnalisée, puis patcher la ClusterPolicy | 2 | C3 = oui | mig-manager redéployé | mig-manager monte `custom-mig-parted-config` |
| 4 | 🔒 Vider puis labelliser les nœuds H200 (`nvidia.com/mig.config`) / patcher les MachineSets | 3 | toujours | 🔧 pods GPU des nœuds arrêtés, reset ou redémarrage possible | `mig.config.state=success`, ressources publiées conformes |
| 5 | Installer le Red Hat build of Kueue, et approuver l'InstallPlan | 4 | toujours | aucun | CSV `Succeeded` |
| 6 | 🔒 DSC `kueue: Unmanaged` (`autoCreateQueues: false`) | 5 | toujours | aucun (webhooks limités aux projets labellisés) | `KueueReady=True`, CR Kueue `Available`, CRDs `kueue.x-k8s.io` présents |
| 7 | Dashboard `disableKueue: false` | 6 | toujours | les **nouveaux** projets passent sous Kueue | `disableKueue=false` stable au bout de 30 s |
| 8 | 🔒 ResourceFlavors, puis ClusterQueue `default` **annotée `managed=false`** | 7.1 | toujours | aucun | ClusterQueue `Active=True` ; sauvegarde de sa spec |
| 9 | 🔒 DSC `autoCreateQueues: true` (**après** la 8) | 7.2 | toujours | RHOAI crée des LocalQueues et des flavors par défaut | LocalQueue `default` dans les projets gérés ; ClusterQueue **inchangée** (diff) |
| 10 | Queues dédiées par équipe (ClusterQueue, LocalQueue, profil propre) et baisse des quotas `default` | QS | D3 = oui | aucun | Σ des quotas par flavor ≤ capacité |
| 11 | Hardware profiles H200 et L4 | 8 | toujours | profils visibles dans le dashboard | 5 profils listés |
| 12 | Projets existants → `projet-dans-kueue.sh` ; projets L4 → `projet-hors-kueue.sh` | 9 | D5, D2 | les workloads du projet basculent | labels et LocalQueue conformes au bout de 60 s |
| 13 | Retrait ou restriction des anciens profils GPU | — | D6 | les utilisateurs ne les voient plus | `oc get hardwareprofiles -A` |
| 14 | `BatchJob` dans le CR Kueue | — | E1 | redémarrage du contrôleur Kueue | frameworks contiennent `BatchJob`, et la valeur tient |

Pourquoi l'ordre 🔒 :
- **4 avant 8** : les flavors ciblent `nvidia.com/mig.config`. Des nœuds non labellisés ne seraient jamais choisis.
- **6 avant 8** : les CRDs ResourceFlavor et ClusterQueue n'existent qu'après le passage de la DSC en `Unmanaged`.
- **8 avant 9** : sans l'annotation `opendatahub.io/managed: "false"` déjà en place, RHOAI remplace la ClusterQueue `default` par la sienne.
- **7 et 9 avant d'ouvrir aux utilisateurs** : sans eux, les projets n'ont ni label ni LocalQueue, et les profils Queue sont inutilisables.

---

## 5. La dégustation (vérifier que c'est bon)

| Ordre | Test | Commande / geste | Résultat attendu |
|---|---|---|---|
| T1 | Contrôle automatique | `scripts/verifier-deploiement.sh $NS_GPU` | `RÉSULTAT : OK` |
| T2 | Nouveau projet (compte **non admin**, dashboard) | `oc get ns <p> --show-labels` ; `oc -n <p> get localqueue default` | labels Kueue, LocalQueue `default → default` |
| T3 | Workbench avec un profil tranche (par exemple `h200-mig-2g35`) | `oc -n <p> get workloads -o wide` ; nœud du pod | admis sur `rf-h200-mig`, pod sur un nœud `all-balanced` |
| T4 | Workbench avec `h200-full` | idem | admis sur `rf-h200`, pod sur un nœud `all-disabled` |
| T5 | Saturation (demander plus que le quota) | `oc get clusterqueue default -o wide` | workload en attente (« insufficient unused quota »), puis admis quand un workload se libère |
| T6 | Projet L4 (sorti de Kueue) + profil `l4` | nœud du pod, `oc -n <p> get workloads` | pod sur un nœud L4, aucun Workload Kueue |
| T7 | Profil L4 refusé dans un projet H200 | dashboard | `l4` non proposé ou refusé |
| T8 | Quota réservé (si D3) | `oc get clusterqueue cq-<équipe> -o jsonpath='{.status.flavorsUsage}'` | l'usage apparaît sur la ClusterQueue de l'équipe |
| T9 | Au bout de 24 h | `oc get ns -l kueue.openshift.io/managed=true` ; LocalQueues ; spec de la ClusterQueue | rien n'a été modifié par RHOAI |

---

## 6. Le service (ouverture aux utilisateurs)

1. **Communiquer** aux équipes :
   - la liste des profils : à quoi sert chacun, et où il est proposé ;
   - le fonctionnement en file d'attente : un workbench peut attendre qu'un quota se libère ;
   - la règle « un projet = H200 **ou** L4 ».
2. **Documenter pour l'exploitation** : qui lance `projet-hors-kueue.sh` et `projet-dans-kueue.sh`, et qui crée les queues dédiées (F1).
3. **Suivre** les ClusterQueues et les workloads en attente (F3). Revoir les quotas après quelques semaines d'usage.
4. **Conserver** la sauvegarde de l'étape 0 et `RETOUR-ARRIERE.md`.

## 7. Si ça rate (diagnostic rapide)

| Symptôme | Cause probable | Remède |
|---|---|---|
| Aucun profil H200 proposé dans un projet | Projet sans label Kueue, ou sans LocalQueue `default` | Ordres 7 et 9 ; `scripts/projet-dans-kueue.sh` |
| « recherche une local queue » dans le dashboard | `autoCreateQueues` non activé | Ordre 9 |
| Workload admis mais pod `Pending` | Toleration ou nodeLabels de la flavor ≠ nœuds réels, ou quota > capacité, ou PVC local à un autre nœud | Vérifier P3, P4, P7, E2 |
| Pod `SchedulingGated` alors que le workload est admis | Pod créé par un admin avec la SCC `anyuid` | E3 |
| Workload jamais admis | Quota à 0 pour cette ressource dans toutes les flavors, ou ressource absente de `coveredResources` | P7, P8 |
| Le projet L4 repasse sous Kueue | Un seul des deux labels a été retiré | `scripts/projet-hors-kueue.sh` (retire les deux) |
| `mig.config.state=failed` | Profil absent de la config MIG montée, ou GPU non vidé | Étape 2 (profils présents ?), vider le nœud, relabelliser |
| La ClusterQueue `default` a changé | Annotation `managed=false` absente au moment de `autoCreateQueues` | Réappliquer `clusterqueue-default.yaml`, vérifier l'annotation |
