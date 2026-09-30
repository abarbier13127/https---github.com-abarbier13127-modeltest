# Guardrails sur OpenShift AI 3.4 — kit d'installation et de démonstration

Tout ce qu'il faut pour installer **NeMo Guardrails** (GA en RHOAI 3.4) devant un LLM servi par
vLLM, le vérifier, et le démontrer — sur la maquette `ds.alf.corp` ou sur **un cluster vierge**.

## 1. Contenu

```
guardrails/
├── README.md                  ← ce fichier : point d'entrée, paramètres à adapter
├── GUIDE.md                   ← guide complet : architecture, prérequis, pas-à-pas, démo,
│                                 bugs et limites connus
├── JOURNAL.md                 ← opérations réellement menées sur ds.alf.corp, dans l'ordre
├── render.sh                  ← rend les modèles pour un environnement → build/<env>/
├── params/
│   ├── exemple.env            ← gabarit commenté à copier pour un nouveau cluster
│   └── ds-alf-corp.env        ← valeurs de la maquette (réellement appliquées)
├── manifests/                 ← modèles paramétrés (${VARIABLE})
│   ├── 00-access-llm.yaml
│   ├── 01-nemo-config.yaml
│   ├── 02-nemo-guardrails.yaml
│   ├── 03-client-access.yaml
│   └── 04-open-webui-env.snippet.yaml
├── tests/
│   └── recette.py             ← recette automatisée (15 tests)
└── build/                     ← manifestes rendus (généré, ignoré par git)
```

## 2. Les manifestes

| Fichier | Objets créés | Rôle | Projet |
|---|---|---|---|
| `00-access-llm.yaml` | ServiceAccount `nemo-guardrails`, Secret `nemo-guardrails-token`, RoleBinding `nemo-guardrails-inference` | identité de NeMo et **droit d'appeler le LLM** (le proxy d'auth KServe vérifie `get` sur l'InferenceService) | `GR_NAMESPACE` + `LLM_NAMESPACE` |
| `01-nemo-config.yaml` | ConfigMap `nemo-config` (`config.yaml`, `rails.co`) | **la politique** : LLM cible, rails Presidio (e-mail, carte, IBAN) en entrée/sortie, regex injection EN/FR et n° de sécu en entrée, regex injection sur les extraits RAG | `GR_NAMESPACE` |
| `02-nemo-guardrails.yaml` | CR `NemoGuardrails` `nemo-guardrails` | l'opérateur TrustyAI en tire Deployment, Service, Route ; auth kube-rbac-proxy ; jeton en `OPENAI_API_KEY`, `MAIN_MODEL_BASE_URL` pour `/v1/models` | `GR_NAMESPACE` |
| `03-client-access.yaml` | ServiceAccount `guardrails-client` + jeton, Role `nemo-guardrails-caller`, RoleBinding `nemo-guardrails-callers` | **qui peut appeler NeMo** : le compte de test et le client de démo (Open WebUI) | `GR_NAMESPACE` |
| `04-open-webui-env.snippet.yaml` | *(extrait, pas un manifeste)* | variables à placer dans le Deployment Open WebUI pour la démo côte à côte (direct / filtré) | projet d'Open WebUI |

Ordre d'application : 00 → 01 + 02 → 03 → (04). Détail et vérifications : `GUIDE.md` §4 et §6.

## 3. Ce qui change selon l'environnement cible

### 3.1 Paramètres (`params/<env>.env`, substitués par `render.sh`)

| Variable | Sens | Où la trouver | ds.alf.corp |
|---|---|---|---|
| `GR_NAMESPACE` | projet qui héberge NeMo | au choix ; le créer **depuis le dashboard** | `guardrails-demo` |
| `LLM_NAMESPACE` | projet du LLM | `oc get isvc -A` | `alf-test` |
| `LLM_ISVC_NAME` | nom de l'InferenceService (recette `--direct`) | `oc get isvc -n <projet>` | `qwen-gpu` |
| `LLM_MODEL_NAME` | nom de modèle servi par vLLM (`id` de `/v1/models`) | `--served-model-name` du runtime, ou `GET /v1/models` | `qwen-gpu` |
| `LLM_BASE_URL` | URL **interne** OpenAI du prédicteur, terminée par `/v1` | `https://<isvc>-predictor.<projet>.svc.cluster.local:<port>/v1` ; port : `oc get svc <isvc>-predictor` | `…qwen-gpu-predictor.alf-test…:8443/v1` |
| `LLM_CALLER_ROLE` | Role autorisant l'appel du LLM | créé par le dashboard avec *Require token authentication* : `<isvc>-view-role` | `qwen-gpu-inference-caller` |
| `OWUI_NAMESPACE`, `OWUI_SA`, `OWUI_TOKEN_SECRET` | client de démo autorisé sur NeMo | Deployment Open WebUI | `aidocs`, `open-webui`, `open-webui-token` |
| `ROUTE_IP` | *(recette seulement)* IP d'entrée des Routes si le poste ne résout pas `*.apps` | `getent hosts console-openshift-console.apps.<domaine>` | `192.168.100.101` |

```bash
cp params/exemple.env params/<mon-cluster>.env && vi params/<mon-cluster>.env
./render.sh params/<mon-cluster>.env
```

### 3.2 À adapter à la main, selon le cas d'usage

| Élément | Fichier | Remarque |
|---|---|---|
| Entités Presidio détectées | `01-nemo-config.yaml` → `sensitive_data_detection` | liste : <https://microsoft.github.io/presidio/supported_entities/> ; reconnaissance **anglaise** uniquement en 3.4 (noms de personnes) |
| Motifs regex (injection, identifiants nationaux) | `01-nemo-config.yaml` → `regex_detection` | syntaxe Python `re`, entre apostrophes YAML |
| Suppression d'Open WebUI | `03-client-access.yaml` | retirer le 2ᵉ sujet du RoleBinding |
| LLM sans authentification | `00` + `02` | pas de RoleBinding ; `OPENAI_API_KEY` quelconque ; URL souvent en `http://…:8080/v1` |
| LLM hors du cluster (API externe) | `01` + `02` | `openai_api_base` et `MAIN_MODEL_BASE_URL` = URL externe ; clé d'API dans un Secret à la place de `nemo-guardrails-token` |

### 3.3 Prérequis cluster (hors manifestes)

OpenShift 4.19/4.20 ; RHOAI 3.4 avec `kserve` et **`trustyai` en `Managed`** ; cert-manager ; accès
à `registry.redhat.io` (image NeMo **7,3 Go**). Détail : `GUIDE.md` §3 et §4.1–4.2.

## 4. Démarrage rapide

```bash
./render.sh params/<env>.env && B=build/<env>
oc patch dsc default-dsc --type merge -p '{"spec":{"components":{"trustyai":{"managementState":"Managed"}}}}'
# créer le projet GR_NAMESPACE depuis le dashboard OpenShift AI, puis :
oc apply -f $B/00-access-llm.yaml
oc apply -f $B/01-nemo-config.yaml -f $B/02-nemo-guardrails.yaml
oc apply -f $B/03-client-access.yaml
python3 tests/recette.py params/<env>.env --direct
```

## 5. Points d'attention (détail dans `GUIDE.md`)

- ⚠️ **Ne pas activer le streaming des rails de sortie** : bug NeMo 3.4 qui neutralise ensuite
  le rail de sortie pour toutes les requêtes (§10). Les clients appellent NeMo sans streaming.
- ⚠️ **Open WebUI** : désactiver *Builtin Tools* sur les modèles et le streaming sur le modèle
  filtré (§6.1, §11).
- Presidio valide la somme de Luhn : tester avec une carte valide (`4111 1111 1111 1111`).
- Refus renvoyés en anglais (messages de la bibliothèque NeMo).
