# Journal des opérations — Guardrails sur ds.alf.corp

Opérations **réellement appliquées** sur le SNO `ds.alf.corp` (OCP 4.20.28, RHOAI 3.4.2), dans
l'ordre. Chaque action a été faite par l'opérateur humain ; les vérifications en lecture seule
par l'assistant. Correspondance avec `../RUNBOOK.md` indiquée entre crochets.

## Étape 0 — Relevé et choix (2026-09-28)

- Relevé en lecture seule : `trustyai: Removed`, `kserve.rawDeploymentServiceConfig: Headless`,
  RHCL 1.4.1, Service Mesh 3.1, cert-manager 1.20, LWS 1.0 ; nœud à 80 % CPU / 86 % RAM en
  *requests* ; GPU 8 Go occupé à 87 % par le LLM.
- Étude de la doc 3.4 et du code (`rhoai-3.4`) de l'orchestrateur **FMS** et de **NeMo**.
- **Décision** : NeMo Guardrails en socle (GA, sans bascule `Headed`), FMS en option ; projet
  dédié `guardrails-demo`.

## Étape 1 — Libérer des ressources (2026-09-29) [§26]

| | |
|---|---|
| Action | console → *Installed Operators* → `openshift-lws-operator` → *LeaderWorkerSetOperator* → supprimer `cluster` |
| Pourquoi | `lws-controller-manager` : 2 CPU / 2 Gi réservés pour 0 `LeaderWorkerSet` |
| Résultat | 72 % CPU / 82 % RAM (≈ 6,6 CPU / 9,6 Gi libres) |
| Retour arrière | *Import YAML* de `backups/leaderworkersetoperator-cluster.RESTORE.yaml` |

## Étape 2 — Activer TrustyAI (2026-09-29) [§27]

```bash
oc patch dsc default-dsc --type merge -p '{"spec":{"components":{"trustyai":{"managementState":"Managed"}}}}'
```

Résultat : DSC Ready en ~1 min ; opérateur TrustyAI Running ; CRD `nemoguardrails`,
`guardrailsorchestrators`… créées. Sauvegarde préalable : `backups/datasciencecluster-default-dsc.20260929.yaml`.

## Étape 3 — Projet et accès au LLM (2026-09-29) [§28]

- Dashboard → *Projects* → *Create project* → `guardrails-demo`.
- `oc apply -f build/ds-alf-corp/00-access-llm.yaml`
- Vérifié : le jeton de `nemo-guardrails` obtient `GET /v1/models` sur `qwen-gpu`.

## Étape 4 — Serveur NeMo (2026-09-30) [§29]

- `oc apply -f build/ds-alf-corp/01-nemo-config.yaml -f build/ds-alf-corp/02-nemo-guardrails.yaml`
- Image de 7,3 Go tirée en ~9 min (un échec réseau réessayé par kubelet) ; pod 2/2 ; 401 sans
  jeton.

## Étape 4 bis — Clients autorisés (2026-09-30) [§30]

- `oc apply -f build/ds-alf-corp/03-client-access.yaml`
- Recette manuelle 9/9 ; filtrage en sortie prouvé par le journal des rails.

## Étape 5 — Préparation d'Open WebUI (2026-09-30) [§31]

- `MAIN_MODEL_BASE_URL` ajouté au CR (sinon `/v1/models` en erreur).
- Streaming des rails de sortie **essayé puis retiré** : bug de sécurité découvert (GUIDE §10) ;
  pod redémarré, non-régression vérifiée.

## Étape 6 — Open WebUI côte à côte (2026-09-30) — en cours

- Manifeste AiDocs `manifests/01-open-webui.yaml` : 9 variables présentes sur le cluster mais
  absentes du fichier réintégrées (dérive), puis bloc LLM à deux connexions
  (`04-open-webui-env.snippet.yaml`) ; `oc apply`.
- Interface : *Stream Chat Response = Off* sur `guardrails.qwen-gpu` ✅.
- Constat : comparaison côte à côte OK sur la question « e-mail inventé ».
- Problème : outils intégrés d'Open WebUI (GUIDE §11) → *Builtin Tools* à décocher sur les deux
  modèles — **à confirmer**.

## Recette automatisée (2026-09-30)

`python3 tests/recette.py params/ds-alf-corp.env --direct` → **15/15**.
