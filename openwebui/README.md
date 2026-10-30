# Open WebUI — SNO ds.alf.corp

Source de vérité de la configuration d'Open WebUI sur le cluster.

| | |
|---|---|
| Namespace | `aidocs` |
| URL | <https://open-webui-aidocs.apps.ds.alf.corp> (entrée `/etc/hosts` → 192.168.100.101) |
| Image | `ghcr.io/open-webui/open-webui:v0.12.5` (officielle, depuis le 2026-10-30) |
| Données | PVC `openweb-data` 20Gi `lvms-vg1` → `/app/backend/data` (SQLite `webui.db`) |
| Modèles | `granite-3-3-2b` (granite-llmd en direct) et `guardrails.granite-3-3-2b` (via NeMo) |

## Contenu

```
manifests/
  10-storage-ca.yaml       PVC openweb-data + ConfigMap service-ca (CA injecté)
  20-deployment.yaml       le Deployment : image, TOUTES les variables d'env
  30-service-route.yaml    Service + Route edge
scripts/
  backup.sh                sauvegarde base (cohérente) + reste du répertoire data
  create-secret-key.sh     crée le Secret WEBUI_SECRET_KEY s'il manque
snapshots/<date>-<version>/  export brut du cluster avant une modification
backups/<date>-<étiquette>/  sauvegardes des données (webui.db + data-sans-db.tgz)
```

Les fichiers de `manifests/` sont identiques au cluster (`oc diff` vide au 2026-10-30).

## Où se règle quoi

1. **Variables d'environnement du Deployment** (`20-deployment.yaml`) : connexions LLM
   (`OPENAI_API_BASE_URLS`, `OPENAI_API_KEYS`, `OPENAI_API_CONFIGS`), modèle par défaut,
   RAG, fonctions désactivées. Avec `ENABLE_PERSISTENT_CONFIG=false`, **elles font foi à
   chaque démarrage** : les réglages équivalents faits dans l'Admin Panel sont écrasés.
2. **Base de données** (non versionnée, d'où `backup.sh`) : utilisateurs, chats et
   **réglages par modèle** (*Admin Panel → Settings → Models*), rattachés à l'**id** du
   modèle. À refaire si l'id change :
   - les deux modèles : *Capabilities* → décocher **Builtin Tools** ;
   - `guardrails.granite-3-3-2b` : *Advanced Params* → **Stream Chat Response = Off**.

   Sans ces réglages, NeMo répond « The 'tools', 'tool_choice', and 'parallel_tool_calls'
   parameters are only supported… » (voir `sim-sno-llmd/bench/guardrails/GUIDE.md` §10–11).
3. **Secrets** (non versionnés) :
   - `open-webui-secret-key` : `WEBUI_SECRET_KEY` (signe les sessions). Le changer
     déconnecte tout le monde.
   - `open-webui-token` : jeton du SA `open-webui`, accepté par NeMo (bench guardrails).

## Appliquer une modification

```bash
export KUBECONFIG=/home/alf/Documents/_dev/SNO/ds/kubeconfig-noingress
oc diff  -f manifests/20-deployment.yaml     # relire
oc apply -f manifests/20-deployment.yaml     # redémarre le pod (Recreate)
```

Ne pas utiliser `oc set env` sans reporter le changement ici, sinon le prochain
`oc apply` l'annule.

## Montée de version

1. Lire les notes : <https://github.com/open-webui/open-webui/releases> (migrations de base ?).
2. `./scripts/backup.sh avant-vX.Y.Z` et `oc get … -o yaml` dans `snapshots/`.
3. Changer `image:` dans `20-deployment.yaml`, `oc apply`, puis vérifier :
   `curl -sk https://open-webui-aidocs.apps.ds.alf.corp/api/version` et les logs
   (`oc -n aidocs logs deploy/open-webui | grep -iE 'alembic|error'`).
4. Retour arrière : remettre l'image précédente **et** restaurer `webui.db` de la
   sauvegarde (une base migrée n'est pas relisible par une version plus ancienne) :
   ```bash
   oc -n aidocs scale deploy/open-webui --replicas=0
   # pod temporaire montant openweb-data, puis : oc cp backups/<…>/webui.db <pod>:/data/webui.db
   #   et supprimer webui.db-wal / webui.db-shm
   oc -n aidocs scale deploy/open-webui --replicas=1
   ```

## Pièges OpenShift (image officielle)

- Le conteneur tourne avec un UID aléatoire (SCC `restricted-v2`) et `/app/backend` n'est
  **pas inscriptible** : sans `WEBUI_SECRET_KEY`, `start.sh` ne peut pas écrire
  `.webui_secret_key` et le pod ne démarre pas. L'ancienne image `quay.io/castawayegr`
  rendait ce répertoire inscriptible, mais elle régénérait la clé à chaque redémarrage :
  toutes les sessions étaient perdues.
- `Recreate` et pas `RollingUpdate` : base SQLite sur un PVC RWO, jamais deux pods en même
  temps.
- Services internes en TLS service-ca : `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` pointent sur
  le ConfigMap `service-ca`.

## Points ouverts

- `ENABLE_IMAGE_GENERATION=true` pointe sur `stable-diffusion-webui.aidocs`, qui n'existe
  pas : la génération d'images ne fonctionne pas.
- Il reste en base un réglage pour l'ancien modèle `guardrails.qwen-gpu` (sans effet).
- Le pod tourne sous le SA `default` ; le SA `open-webui` ne sert qu'à fournir le jeton.

## Historique

| Date | Changement |
|---|---|
| 2026-09-23 | Déploiement initial, image `quay.io/castawayegr/open-webui:v0.11.4` |
| 2026-10-29 | LLM rebranché de qwen-gpu (arrêté) sur granite-llmd ; NeMo idem |
| 2026-10-30 | Image officielle `ghcr.io/open-webui/open-webui:v0.12.5`, Secret `WEBUI_SECRET_KEY` (clé en cours conservée), `Recreate`, readinessProbe. Migrations alembic → `0bd252b206fb`. Sauvegarde : `backups/2026-10-30-v0.11.4/` |
