# Runbook — lancer la suite contre le modèle déployé sur le SNO `ds`

Comment exécuter les tests de charge contre `qwen-gpu` (namespace `alf-test`),
par opposition au mock local documenté dans l'en-tête de `tools/mock_vllm.py`.

Établi et vérifié le **2026-08-12**.

> 📊 **[Annexe — lecture des tableaux de sortie](#annexe--lecture-des-tableaux-de-sortie)**
> — signification de chaque colonne (`conc`, `TTFT`, `TPOT`, `effic.`, `tok/s`…)
> pour les deux tableaux produits par la suite : le balayage de `t02_load.py` et
> le mini-bench de `call_model.py`.

## Contexte du déploiement

| Élément | Valeur |
|---|---|
| InferenceService | `qwen-gpu` dans `alf-test` (l'autre, `qwen`, est `READY=False`) |
| URL | `https://qwen-gpu-alf-test.apps.ds.alf.corp` (Route `reencrypt`/Redirect) |
| Nom du modèle (champ `model`) | **`qwen-gpu`** — et non `qwen`, qui est le défaut du code |
| `max_model_len` | 32768 |
| Pods de serving | **1 seul** predictor, 3 containers (vLLM + `kube-rbac-proxy` + sidecar) |
| GPU | **1 seul allocatable** sur le nœud (RTX A2000 8GB Laptop, pas de time-slicing) |
| Auth | activée : `Bearer` obligatoire, sinon 401 |

L'endpoint est protégé par un sidecar `kube-rbac-proxy` qui exige le droit
`get inferenceservices/qwen-gpu` dans `alf-test`. Le ServiceAccount
`qwen-gpu-sa` possède déjà ce droit via le Role `qwen-gpu-view-role` : inutile
de créer un RoleBinding, il suffit de lui demander un jeton.

## 1. Préparation

Une fois par session de travail :

```bash
cd /home/alf/Documents/_dev/SNO/ds/sim-sno-llmd/bench
export KUBECONFIG=/home/alf/Documents/_dev/SNO/ds/kubeconfig-noingress

# Endpoint et modèle, pour ne pas les répéter à chaque commande
export LLMD_URL=https://qwen-gpu-alf-test.apps.ds.alf.corp
export LLMD_MODEL=qwen-gpu
export LLMD_NS=alf-test

# Accès à l'inférence : jeton du ServiceAccount qui a déjà le droit
export LLMD_API_KEY=$(oc create token qwen-gpu-sa -n alf-test --duration=120m)

mkdir -p out
```

Le jeton expire au bout de 2 h. **Si un run se met à échouer en 401, c'est la
première chose à refaire.**

Séparer la génération du jeton de son usage n'est pas du confort : c'est ce qui
rend un échec visible immédiatement, au lieu de le transformer en 401 obscur.

```bash
echo "longueur du jeton : ${#LLMD_API_KEY}"   # ~1250 attendu ; 0 = échec
```

Vérification en une ligne avant de lancer quoi que ce soit de long :

```bash
curl -sk -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer $LLMD_API_KEY" "$LLMD_URL/v1/models"   # attendu : 200
```

## 2. Authentification : les deux mécanismes

⚠️ **L'authentification n'est pas optionnelle.** L'endpoint est protégé : sans
jeton, `/health` comme `/v1/models` répondent 401 et tous les tests s'arrêtent
immédiatement. Passer uniquement `--url` et `--model` **ne suffit pas**.

Deux mécanismes au choix, avec cette priorité :

```
--api-key  >  --user/--password  >  $LLMD_API_KEY  >  appel anonyme
```

### a. Un jeton déjà obtenu

```bash
.venv39/bin/python t01_smoke.py \
  --url https://qwen-gpu-alf-test.apps.ds.alf.corp --model qwen-gpu \
  --api-key "$(oc create token qwen-gpu-sa -n alf-test --duration=60m)"
```

Noter les guillemets autour de `"$(...)"` : sans eux, une valeur vide fait
disparaître l'argument et une valeur contenant un espace le découpe.

⚠️ **`oc create token` ne fonctionne QUE pour un ServiceAccount.** C'est l'API
TokenRequest, qui n'existe pas pour un utilisateur humain. C'est le piège
classique : l'erreur part sur `stderr` pendant que la substitution renvoie une
chaîne vide, et on obtient un 401 sans comprendre pourquoi.

```
$ oc create token user -n alf-test
error: failed to create token: serviceaccounts "user" not found
```

`--api-key` attend un **jeton**, jamais un nom d'utilisateur : la valeur part
telle quelle dans `Authorization: Bearer <valeur>`, et c'est le jeton qui porte
l'identité. L'usurpation par en-tête ne marche pas non plus, le sidecar
n'honore pas `Impersonate-User`.

### b. Un couple utilisateur / mot de passe

Pour un compte htpasswd, les scripts obtiennent le jeton OAuth eux-mêmes
(ajouté le 2026-08-12), **sans jamais toucher au KUBECONFIG** :

```bash
# mot de passe demandé interactivement — à préférer, rien ne traîne dans `ps`
.venv39/bin/python t01_smoke.py --user user

# sans terminal interactif
echo '<motdepasse>' | .venv39/bin/python t01_smoke.py --user user --password-stdin

# ou par variable d'environnement
export LLMD_PASSWORD='<motdepasse>'
.venv39/bin/python t01_smoke.py --user user
```

Sources du mot de passe, par ordre de priorité : `--password-stdin`, puis
`--password`, puis `$LLMD_PASSWORD`, puis saisie interactive. `--password` est
volontairement placé après stdin : un mot de passe en ligne de commande est
lisible dans `ps` par les autres utilisateurs de la machine.

Sous le capot, c'est le flux OAuth *challenging client* — celui-là même
qu'utilise `oc login -u/-p` — implémenté en stdlib. L'endpoint est découvert
automatiquement :

```
API server  : https://api.ds.alf.corp:6443      (via `oc whoami --show-server`)
authorize   : https://oauth-openshift.apps.ds.alf.corp/oauth/authorize
              (via /.well-known/oauth-authorization-server)
```

Si `oc` n'est pas disponible, imposer la valeur avec `--api-server` ou
`--oauth-url` (ou les variables `$LLMD_API_SERVER` / `$LLMD_OAUTH_URL`).

**Pourquoi ne pas simplement appeler `oc login`** : il écrit dans le KUBECONFIG
courant et remplacerait le contexte `system:admin` de `kubeconfig-noingress`.
La méthode manuelle reste possible, mais alors dans un fichier séparé :

```bash
KUBECONFIG=/tmp/kc-user oc login https://api.ds.alf.corp:6443 \
  -u user -p '<motdepasse>' --insecure-skip-tls-verify=true
export LLMD_API_KEY=$(KUBECONFIG=/tmp/kc-user oc whoami -t)
```

### c. Qui a le droit

État au 2026-08-12, vérifié par `oc auth can-i --as=…`, qui prédit exactement le
code HTTP sans passer par la Route :

| Identité | Comment obtenir le jeton | Droit | Endpoint |
|---|---|---|---|
| SA `qwen-gpu-sa` | `oc create token qwen-gpu-sa -n alf-test` | oui (RoleBinding `qwen-gpu-view`) | 200 |
| SA `default` | `oc create token default -n alf-test` | non | **403** |
| user `user` | `--user user` | oui (RoleBinding `qwen-gpu-view-user`) | 200 |
| user `admin` | `--user admin` | oui (cluster-admin) | 200 |
| user `user2` | `--user user2` | non | **403** |

Pour accorder l'accès à une identité qui ne l'a pas :

```bash
oc create rolebinding qwen-gpu-view-user2 -n alf-test \
  --role=qwen-gpu-view-role --user=user2
```

La propagation est immédiate, le sidecar ne cache pas ses décisions.

### d. 401 contre 403 : lire la différence

- **401** = aucune identité reconnue (jeton absent, invalide, expiré, tronqué).
  Le proxy n'a pas pu authentifier.
- **403** = identité parfaitement reconnue mais autorisation refusée, avec un
  corps explicite qui nomme le sujet et le droit manquant :

  ```
  Forbidden (user=system:serviceaccount:alf-test:default, verb=get,
             resource=inferenceservices, subresource=)
  ```

  Un 403 prouve donc que l'authentification fonctionne — il ne manque qu'un
  RoleBinding.

Rappel : le verbe du SubjectAccessReview est `get` quelle que soit la méthode
HTTP. Un `POST /v1/chat/completions` déclenche le même `get`, donc accorder
`get` accorde l'inférence complète.

### e. Le parcours navigateur complet (`oauth_web_flow.py`)

Les points b et c empruntent le flux *challenging client* : un seul GET avec un
en-tête `Authorization: Basic`. C'est rapide, mais cela court-circuite tout ce
qui fait l'authentification d'un vrai utilisateur — pas de page de login, pas de
cookie de session, pas de jeton CSRF, pas d'écran de consentement.

`oauth_web_flow.py` rejoue le parcours **navigateur**, en HTTP pur (l'équivalent
d'une série de `curl -c/-b -L`), et rend la page `/oauth/token/display` telle
que l'utilisateur l'aurait vue :

```
GET  /oauth/authorize?client_id=openshift-browser-client
                     &response_type=code&redirect_uri=…/oauth/token/display
  → écran de choix de l'IdP, puis 302 vers sa page de login
POST <page de login>       identifiant + mot de passe + csrf (+ cookie de session)
  → 302 /oauth/authorize?…            retour au flux
  → 302 /oauth/token/display?code=…   le serveur échange le code lui-même
GET  /oauth/token/display             ← la page rendue en sortie
```

**`oc` n'est jamais appelé** et le KUBECONFIG n'est jamais touché.

#### Sur le SNO `ds`

Le cluster déclare deux fournisseurs, donc `--idp` est obligatoire — sans lui le
script s'arrête en listant les noms exacts :

```bash
# ce que le cluster propose (aucun identifiant demandé)
.venv39/bin/python oauth_web_flow.py --list-idp
#   --idp kube:admin               (libellé affiché : kube:admin)
#   --idp htpasswd_provider        (libellé affiché : htpasswd_provider)

.venv39/bin/python oauth_web_flow.py -u user --idp htpasswd_provider

# sans terminal, et en vérifiant que le jeton obtenu est réellement utilisable
echo '<motdepasse>' | .venv39/bin/python oauth_web_flow.py \
  -u user --idp htpasswd_provider --password-stdin \
  --api-server https://api.ds.alf.corp:6443

# trace de chaque requête et redirection + page HTML brute conservée
.venv39/bin/python oauth_web_flow.py -u user --idp htpasswd_provider \
  --verbose --html out/token-display.html --json out/oauth-flow.json
```

#### Contre un autre cluster ou un autre IdP

Rien n'est codé en dur pour htpasswd ni pour `ds`. `--idp` prend le nom déclaré
dans `oauth/cluster`, quel que soit son type (LDAP, OIDC/Keycloak, GitHub,
ADFS…), et le formulaire est analysé plutôt que supposé :

- les noms des champs sont **détectés** (`username`, `email`, `UserName`,
  `uid`, `login`…), et les champs cachés du formulaire (CSRF, `then`, domaine
  par défaut) sont réémis tels quels ;
- les IdP **en deux temps** (identifiant sur un premier écran, mot de passe sur
  le suivant) sont gérés ;
- les redirections sont suivies **même vers un autre domaine** : un IdP externe
  qui héberge sa propre page de login fonctionne, cookies compris ;
- l'écran de **consentement** est validé automatiquement s'il apparaît.

```bash
# autre cluster, IdP LDAP
python3 oauth_web_flow.py --oauth-url https://oauth-openshift.apps.autre.corp \
  -u jdoe --idp ldap_provider

# ou en laissant découvrir l'endpoint depuis l'API server
python3 oauth_web_flow.py --api-server https://api.autre.corp:6443 -u jdoe --idp mon-oidc

# formulaire non reconnu : forcer les champs, en ajouter un
python3 oauth_web_flow.py -u jdoe --idp adfs \
  --user-field UserName --password-field Password --field Domain=CORP
```

En cas d'arrêt sur une page inconnue, le script **rend cette page** et
l'enregistre si `--html` est passé : c'est ce qui permet de trouver les noms de
champs à donner à `--user-field` / `--password-field` / `--field`.

La base du serveur OAuth est déduite de `--url` (`*.apps.<domaine>` →
`oauth-openshift.apps.<domaine>`), ou découverte via `--api-server`
(`/.well-known/oauth-authorization-server`), ou imposée par `--oauth-url`.
Le login et le mot de passe sont demandés interactivement s'ils manquent ; les
sources du mot de passe sont les mêmes qu'en b. `$LLMD_IDP`, `$LLMD_OAUTH_URL`
et `$LLMD_API_SERVER` évitent de les répéter.

Codes retour : `0` jeton affiché, `1` parcours cassé (identifiants refusés,
page inattendue, serveur injoignable), `2` impossible de démarrer (pas d'URL
OAuth, pas d'utilisateur).

Ce que ce test couvre et que les autres ne voient pas : la Route
`oauth-openshift` et son certificat, le template de login servi par l'IdP, la
session par cookie, le CSRF, la sélection d'IdP et l'écran de consentement.
Utile après une rotation de certificats ou un changement d'IdP, où le flux
challenging peut continuer de marcher alors que la connexion via la console est
cassée.

## 3. Contrôle préalable

```bash
.venv39/bin/python t01_smoke.py --json out/t01-sno.json
```

Établit que l'endpoint répond, que le streaming SSE fonctionne (donc que le TTFT
est mesurable et non bufferisé par la Route), et dresse l'inventaire côté
cluster. Environ 30 s. Si ce test ne passe pas, rien d'autre n'est
interprétable.

## 4. Capacité et genou de saturation

```bash
# Référence basse (~40 s)
.venv39/bin/python t02_load.py --levels 1,2,4 --duration 12 --max-tokens 32 \
  --quiet --json out/t02-sno.json

# Recherche du genou (~2 min) — le run intéressant
.venv39/bin/python t02_load.py --levels 4,8,16,32 --duration 15 --max-tokens 64 \
  --json out/t02-sno-high.json --csv out/t02-sno-high.csv
```

Retirer `--quiet` pour suivre la progression en direct (une ligne toutes les 2 s
avec le débit instantané).

### Lire le résultat

- **`effic.`** — débit par client rapporté au palier le plus bas. Vaut 1,00 à la
  référence et chute quand le GPU sature. Le genou est le dernier palier
  au-dessus de 0,70 (réglable par `--knee-tolerance`).
- **TTFT P95** — s'il décroche brutalement alors que le TPOT bouge peu, c'est le
  *prefill* qui fait la queue : signature classique de la saturation d'un GPU
  unique. Si au contraire c'est le TPOT qui monte, le goulot est le *decode*.
- **`saturation`** dans le JSON — jauges relevées **pendant** la rafale, en
  maxima : `running_peak`, `waiting_peak`, `kv_usage_peak`, plus
  `preemptions_delta` imputable au palier. Un `waiting_peak > 0` confirme que des
  requêtes ont attendu un slot de batch ; un `kv_usage_peak` proche de 1 annonce
  les préemptions. Un `running_peak` égal à la concurrence signifie que vLLM les
  traite toutes en batch continu, sans file.
- **`degradation`** dans le JSON — dégradation du TTFT et du TPOT entre le
  premier et le dernier palier, rapportée à l'augmentation de la charge. C'est ce
  qui rend visible un modèle devenu très lent, que le taux d'erreur ne montre
  jamais (voir ci-dessous).

### Le taux d'erreur ne dit pas que le service est utilisable

C'est le piège le plus contre-intuitif de ce test, et il a été rencontré le
2026-08-12. Le taux d'erreur répond à « le serveur a-t-il refusé ou coupé ? ».
Un modèle qui répond à tout, mais dix-sept fois plus lentement, affiche **0 %
d'erreur**. Trois contrôles couvrent ce que le taux d'erreur laisse passer :

- **réponses non vides** — une réponse HTTP 200 sans aucun token est un succès
  pour le transport et un échec réel. Seuil `--max-empty-rate` (1 % par défaut).
- **dégradation proportionnée à la charge** — le TPOT est comparé à
  l'augmentation de concurrence. Une dégradation *sous-linéaire* (TPOT ×5,5 pour
  une charge ×8) est le comportement normal du batching continu : chaque requête
  ralentit moins vite que la charge ne monte. Une dégradation **sur-linéaire**
  signifie que le débit agrégé de tokens a *baissé* — ajouter des clients détruit
  du travail utile, signature d'un emballement (préemptions en cascade, pression
  KV) et non d'une simple mise en file. C'est un FAIL, sauf si l'échantillon est
  trop maigre (< 20 requêtes sur un palier), auquel cas c'est un WARN.
- **amplitude de la dégradation** — WARN au-delà de `--max-degradation` (×3 par
  défaut), indépendamment du caractère proportionné : même « normale », une
  latence multipliée par 17 rend le service pénible, et c'est cela qu'il faut
  voir.

Extraction rapide :

```bash
.venv39/bin/python -c "
import json; d=json.load(open('out/t02-sno-high.json'))
for l in d['levels']:
    print(l['concurrency'], round(l['throughput_rps'],2), 'req/s',
          round(1000*l['ttft']['p95']), 'ms TTFT P95', l.get('saturation'))
print('genou:', d['knee'])
"
```

### Mesure de référence du 2026-08-12

Obtenue avec `--levels 1,2,4 --duration 12 --max-tokens 32`, à comparer aux runs
futurs (mêmes paramètres, sinon rien n'est comparable) :

| conc | req/s | tok/s | TTFT P50 | lat P95 | err % | effic. |
|---|---|---|---|---|---|---|
| 1 | 4,26 | 136 | 20 ms | 0,24 s | 0,0 | 1,00 |
| 2 | 7,81 | 250 | 26 ms | 0,26 s | 0,0 | 0,92 |
| 4 | 14,75 | 472 | 30 ms | 0,27 s | 0,0 | 0,87 |

Le genou n'est pas atteint à 4 : la capacité réelle est au-delà, d'où le run en
`4,8,16,32`.

Avec `--levels 4,32 --duration 12 --max-tokens 64`, le déploiement encaisse
32 clients simultanés sans broncher — 6 PASS / 0 FAIL / 0 WARN :

| | conc 4 | conc 32 | rapport |
|---|---|---|---|
| débit | — | — | **×5,0** |
| TTFT P50 | 31 ms | 58 ms | ×1,9 |
| TPOT P50 | 7,5 ms | 11,6 ms | ×1,5 |
| `running_peak` | 4 | 32 | batch continu, `waiting_peak` = 0 |

### Contre-exemple : un run mal paramétré

Le même déploiement, avec `--workload unique --prompt-tokens 4096
--max-tokens 4096 --levels 4,8,16,32 --duration 12` :

| conc | TTFT P50 | TPOT P50 | latence P50 | `wall` réel |
|---|---|---|---|---|
| 4 | 414 ms | 11,8 ms | 1,78 s | 48 s |
| 8 | 767 ms | 30,5 ms | 3,35 s | 45 s |
| 16 | 1387 ms | 59,2 ms | 8,99 s | 59 s |
| 32 | **6887 ms** | **64,8 ms** | **16,54 s** | **117 s** |

Le service *répond* — 0 % d'erreur, ~1400 tokens par réponse — mais il faut 7 s
pour le premier token. Ce run **ne mesure pas une capacité** : `--max-tokens 4096`
donne des requêtes de plus d'une minute, alors que `--duration` vaut 12 s. La
durée ne contrôle que le moment où l'on **cesse d'émettre** ; les requêtes en vol
sont menées à terme, d'où un `wall` de 48 à 117 s dominé par la traîne. Les req/s
en deviennent non monotones (0,25 / 0,55 / 0,47 / 0,27) et le genou annoncé est un
artefact.

**Règle** : la durée d'un palier doit être grande devant la latence d'une requête.
`--max-tokens 64` avec `--duration 15`, ou `--max-tokens 512` avec
`--duration 120`.

L'attribution par pod fonctionne sur ce déploiement via les compteurs vLLM lus
par `oc get --raw …/proxy/metrics` (et non par en-tête, la Route KServe n'en
expose pas). L'écart de 2 requêtes entre le compte client et le compte pod est
normal : ce sont les requêtes de chauffe.

### Caractérisation prefill / decode du 2026-08-12

Deux mesures de référence qui séparent les deux phases, utiles pour interpréter
tout run futur sur ce déploiement — et pour ne pas confondre les deux paramètres
qui, à la lecture, se ressemblent.

**`--max-tokens` agit sur le decode.** C'est la limite de tokens *générés*, pas la
taille du prompt. Prompt strictement identique dans les trois cas :

| `max_tokens` | `prompt_tokens` | tokens générés | TTFT | latence | TPOT |
|---|---|---|---|---|---|
| 16 | 151 | 16 | 94 ms | 0,20 s | 7,0 ms |
| 64 | 151 | 64 | 22 ms | 0,46 s | 6,9 ms |
| 256 | 151 | **120** | 23 ms | 0,84 s | 6,9 ms |

Trois enseignements :

- le `prompt_tokens` ne bouge pas — ce paramètre ne touche **pas** au prompt ;
- c'est un **plafond, pas une longueur imposée** : à 256, le modèle s'est arrêté
  de lui-même à 120 tokens. Au-delà d'un certain point, augmenter ne change plus
  rien et le débit en tok/s plafonne (140 puis 142) ;
- le TPOT est constant, donc la latence suit une droite :
  `latence ≈ TTFT + (tokens_générés − 1) × TPOT`, soit
  `23 ms + 119 × 6,9 ms = 844 ms` contre 840 ms mesurés.

Conséquences pratiques : monter `--max-tokens` fait **baisser le débit en req/s**
alors que le **tok/s reste constant** (le decode est le goulot et travaille au
même rythme), augmente la pression sur le cache KV, et rapproche du `--timeout`
— un timeout étant compté comme une **erreur**. Deux runs avec des
`--max-tokens` différents ne sont donc **pas comparables** en req/s : c'est le
premier paramètre à figer.

**`--prompt-tokens` agit sur le prefill.** `max_tokens` figé à 32, prompts tous
différents :

| `--prompt-tokens` | `prompt_tokens` réel | TTFT | latence |
|---|---|---|---|
| 64 | 144 | 34 ms | 0,25 s |
| 512 | 774 | 50 ms | 0,26 s |
| 2048 | 2928 | **173 ms** | 0,39 s |
| 8192 | 11632 | **862 ms** | 1,10 s |

Le TTFT est multiplié par 25 pendant que la partie decode ne bouge pas. C'est
cette séparation qui rend le diagnostic possible : **un TTFT qui explose désigne
le prefill** (taille de contexte, cache, routage), **un TPOT qui se dégrade
désigne le decode** (contention GPU).

À noter que `--prompt-tokens` est une **approximation** : le générateur estime
~1,3 token par mot, le vrai tokenizer en compte davantage, plus le texte
d'encadrement de la consigne — d'où 144 tokens réels pour 64 demandés. Le
rapport se stabilise autour de **×1,4** aux grandes tailles. Se fier au
`prompt_tokens` présent dans le JSON et le CSV. Pour le workload `shared`, la
taille effective du contexte est dominée par `--prefix-tokens`.

⚠️ **`--prompt-tokens` n'a aucun effet avec le workload `chat`**, qui est le
défaut de t02 : ses 12 prompts sont fixes. Il faut `--workload unique` (effet
total) ou `shared` (effet partiel, le préfixe dominant).

### Plafond de contexte

Le script ne borne **rien** : il construira un prompt de 100 000 tokens sans
broncher. La limite est côté serveur, `max_model_len = 32768`, et c'est
`prompt + tokens générés` qui doit y tenir — la génération consomme la même
fenêtre.

| `--prompt-tokens` | tokens réels | avec `--max-tokens 64` | résultat |
|---|---|---|---|
| 20000 | 28392 | 28456 | OK, TTFT 3249 ms |
| 22526 | 31571 | 31635 | OK — plafond conseillé |
| 23500 | 32705 | 32769 | **HTTP 400** |

Le plafond pratique est donc `(32768 − max_tokens) / 1,45`, soit **~22 500** avec
un `--max-tokens` modeste.

Depuis le 2026-08-12, **t02 et t03 vérifient ce plafond avant d'envoyer quoi que
ce soit** (`config.check_context_window`), en lisant `max_model_len` sur
`/v1/models`. Sans ce contrôle, un prompt trop long produit un HTTP 400 *par
requête* : 100 % d'erreurs, verdict rouge, et rien qui indique que la cause est
un paramètre mal choisi. Le contrôle donne directement la valeur à ne pas
dépasser :

```
[FAIL] ❌ fenêtre de contexte — prompt estimé à ~43540 tokens + 64 générés
       = ~43604, or la fenêtre du modèle est de 32768 : chaque requête serait
       refusée en HTTP 400. Réduire --prompt-tokens à 22526 au plus, ou
       baisser --max-tokens
```

Le run s'arrête alors immédiatement, en code retour **2** (non concluant), sans
avoir chargé le GPU. Entre 90 % et 100 % de la fenêtre, le contrôle passe en WARN
et laisse le run se dérouler : l'estimation étant approximative, un basculement
en 400 reste possible selon les prompts tirés. Si `max_model_len` n'est pas
publié (cas du mock), le contrôle passe en SKIP sans influencer le verdict.

⚠️ **Un gros prompt fausse la lecture du balayage.** À `--prompt-tokens 21000`,
une seule requête sature déjà le GPU : le débit *baisse* quand la concurrence
monte (0,51 → 0,44 req/s, efficacité 0,22 ; TTFT P50 de 202 ms à 2444 ms). Le
contrôle « le débit croît avec la concurrence » échoue alors légitimement, mais
**ce n'est pas un défaut de la plateforme** — son message le signale
explicitement au-delà de 4000 tokens estimés. Pour mesurer une capacité, rester
sur des prompts réalistes.

⚠️ **Piège de protocole, rencontré en produisant ce tableau.** Un premier essai
donnait un TTFT plat (29 / 32 / 37 ms), contredisant la théorie : la requête de
chauffe utilisait **le même prompt**, le cache de préfixe était donc chaud et le
prefill servi presque gratuitement. Pour mesurer un coût de prefill honnête, il
faut des prompts que le serveur n'a **jamais** vus. C'est la raison pour laquelle
t03 utilise `--workload unique` par défaut et t02 fait avancer les index de
prompts entre les paliers.

## 5. Valider un SLO au débit cible

Une fois la capacité connue, la boucle ouverte est le seul mode honnête (en
boucle fermée, un serveur qui ralentit reçoit spontanément moins de trafic et ne
peut jamais montrer un effondrement).

### D'où vient la valeur de `--rate`

**Du débit au genou, pas du débit maximal.** Prendre dans la sortie de l'étape 4
la ligne `genou de saturation : concurrence N`, puis le `req/s` de ce palier N
dans le tableau — c'est-à-dire `levels[].throughput_rps` pour `concurrency == knee`
dans le JSON. Mettre **70 % de cette valeur** dans `--rate`.

```bash
# Extraction directe depuis le JSON de l'étape 4
.venv39/bin/python -c "
import json; d=json.load(open('out/t02-sno-high.json'))
k=[s for s in d['levels'] if s['concurrency']==d['knee']][0]
print('genou conc=%d  %.2f req/s  -> --rate %.0f' % (
    d['knee'], k['throughput_rps'], 0.7*k['throughput_rps']))"
```

⚠️ **Ne pas se baser sur le « débit maximal observé ».** Deux raisons, l'une
logique et l'autre méthodologique :

1. Le maximum est atteint **au-delà** du genou, dans un régime où le débit par
   client s'est déjà effondré. Sur le run du 2026-08-12, 70 % du maximum
   (39,36 req/s) donne 27,6 req/s, soit **plus** que le débit du genou lui-même
   (25,06 req/s) : on demanderait au serveur de tenir en permanence un régime
   qu'il ne soutient déjà plus. Ce n'est pas une marge, c'est un déficit.
2. Le maximum n'est un plafond que si le balayage a trouvé le mur. Sur ce même
   run, il n'y avait **aucune erreur** au dernier palier et le débit montait
   encore ×1,57 entre conc 16 et 32 : `39,36` ne dit pas où sature le serveur,
   il dit où on a cessé de mesurer. Relancer avec `--levels 32,64,128` change la
   valeur sans que la plateforme ait bougé.

Le genou, lui, est un point de bascule réellement mesuré : le dernier palier où
chaque client reçoit encore au moins `--knee-tolerance` (0,70 par défaut) du
débit par client de la référence.

### Le run

```bash
# --rate 17 = 70 % des 25,06 req/s du genou (concurrence 16), run du 2026-08-12
.venv39/bin/python t02_load.py --no-sweep --rate 17 --duration 60 --max-tokens 64 \
  --max-inflight 32 --slo-ttft 0.5 --slo-p95 1.5 \
  --json out/t02-sno-slo.json
```

**Calibrer les SLO sur les latences déjà observées**, sinon le contrôle ne teste
rien. Les valeurs ci-dessus (TTFT P95 ≤ 0,5 s, latence P95 ≤ 1,5 s) encadrent les
pires mesures de l'étape 4 (164 ms et 0,87 s) avec une marge d'environ ×2. Un
`--slo-p95 5.0` sur ce déploiement passerait quoi qu'il arrive et ne signalerait
aucune régression.

⚠️ **`--max-inflight` n'est pas cosmétique.** Sans borne, `run_open` alloue
`10×rate` workers : un worker est donc toujours libre, le retard d'admission
reste nul par construction et le contrôle correspondant passe en SKIP. Avec la
borne, on obtient en plus la mesure du retard d'admission, et la saturation se
lit sur trois signaux complémentaires : débit réalisé/demandé, taux d'erreur, et
dérive de la latence entre le début et la fin du run.

## 6. Ce qui ne sert à rien ici

**`t03_balance.py` ne peut rien conclure sur ce déploiement.** Un seul GPU
allocatable, donc un seul pod predictor : les contrôles de répartition passent en
SKIP faute de quoi que ce soit à équilibrer. Le test ne redeviendra pertinent
qu'avec plusieurs pods de serving — donc en rétablissant le time-slicing GPU, ou
avec un second nœud.

## Dépannage

Les messages ci-dessous sont ceux affichés par les scripts ; ils distinguent
volontairement les causes, un même 401 pouvant venir de trois situations
différentes.

| Message / symptôme | Cause | Correctif |
|---|---|---|
| `HTTP 401 … aucun jeton n'a été transmis (valeur vide)` | rien n'a été fourni, ou une substitution `$(...)` a échoué silencieusement | vérifier `${#LLMD_API_KEY}` ; se souvenir que `oc create token` ne marche que pour un SA |
| `HTTP 401 … l'obtention du jeton a échoué en amont : …` | l'échange OAuth a échoué (mot de passe, découverte) | lire la cause citée dans le message |
| `HTTP 401` avec un jeton bien transmis | jeton invalide ou **expiré** | régénérer le jeton (durée de vie 2 h) |
| `HTTP 403` + corps `Forbidden (user=…, verb=get…)` | identité reconnue, SAR refusé | ajouter un RoleBinding sur `qwen-gpu-view-role`, ou utiliser `qwen-gpu-sa` |
| `HTTP 0 — aucune réponse du serveur` | rien n'a répondu | **là seulement**, vérifier `--url`, le DNS (`/etc/hosts`), la Route et l'état du pod |
| `aucun mot de passe fourni et pas de terminal` | `--user` sans mot de passe dans un script | utiliser `--password-stdin` ou `$LLMD_PASSWORD` |
| `impossible de déduire l'API server via oc` | `oc` absent ou KUBECONFIG non exporté | exporter `KUBECONFIG`, ou passer `--api-server` / `--oauth-url` |
| `FAIL fenêtre de contexte … réduire --prompt-tokens à N au plus` | prompt + génération dépassent `max_model_len` | appliquer la valeur `N` donnée par le message, ou baisser `--max-tokens` ; le run s'est arrêté **avant** d'envoyer (code 2) |
| `WARN fenêtre de contexte … marge de X %` | on est entre 90 et 100 % de la fenêtre | rien d'obligatoire, mais un basculement en 400 reste possible selon les prompts tirés |
| `FAIL le débit croît avec la concurrence … le prompt fait ~N tokens` | un prompt énorme sature le GPU à lui seul | **pas un défaut de la plateforme** : refaire la mesure avec un prompt réaliste |
| `FAIL réponses non vides … sans aucun token` | le serveur répond 200 mais ne génère rien | vérifier les logs du pod vLLM ; ce n'est pas un problème de réseau ni d'auth |
| `FAIL dégradation proportionnée … SUR-linéaire` | le débit agrégé de tokens a baissé quand la charge a monté | regarder `preemptions_delta` et `kv_usage_peak` dans `saturation` : pression KV ou préemptions en cascade |
| `WARN amplitude de la dégradation … ×N` | le service répond mais bien plus lentement | c'est une information, pas une panne ; ajuster `--max-degradation` si le seuil ne correspond pas à ton exigence |
| req/s non monotones, `wall` très supérieur à `--duration` | requêtes plus longues que le palier (`--max-tokens` trop grand) | réduire `--max-tokens` ou allonger `--duration` (voir le contre-exemple en section 4) |
| `--prompt-tokens` sans effet visible | workload `chat` (défaut de t02), dont les 12 prompts sont fixes | ajouter `--workload unique` |
| `modèle « qwen » servi` en FAIL sur t01 | `LLMD_MODEL` non exporté | le modèle s'appelle `qwen-gpu`, le défaut du code est `qwen` |
| TTFT ≈ latence totale, WARN « streaming bufferisé » | un intermédiaire agrège le flux SSE | les mesures TTFT/ITL sont alors ininterprétables : vérifier la Route et le sidecar |
| pod `Pending` sur `Insufficient nvidia.com/gpu` après modification de l'ISVC | rollout `RollingUpdate` avec 1 seul GPU | l'ISVC est déjà en `Recreate` ; sinon la repasser (implique une coupure d'inférence) |

## État de validation

Vérifié contre le déploiement réel le 2026-08-12 : `--api-key`, `$LLMD_API_KEY`,
l'appel anonyme (401 correctement expliqué), `--user` avec mauvais mot de passe,
`--user` sans mot de passe hors terminal, `--password-stdin`, `$LLMD_PASSWORD`,
la découverte OAuth, ainsi que t01 et t02 de bout en bout.

Contrôles ajoutés le 2026-08-12 après analyse d'un run réel : réponses vides (FAIL provoqué contre le mock avec 1396/1396 réponses vides, PASS en contre-épreuve) et dégradation des latences (rejeu du run réel : TTFT ×16,6 → WARN d'amplitude, TPOT ×5,5 pour une charge ×8 → sous-linéaire donc PASS).

Contrôle de fenêtre de contexte : les quatre branches testées (FAIL au-delà de la
fenêtre, PASS au nominal, WARN entre 90 et 100 %, SKIP quand `max_model_len` n'est
pas publié). La valeur suggérée par le message d'erreur a été **vérifiée
utilisable** — 22526 passe avec 31635 tokens au total, 23500 échoue en 400 : le
facteur d'estimation majore d'environ 4 %, donc il se trompe du bon côté.

**Non vérifié** : un échange OAuth *réussi* — faute de disposer des mots de passe
des comptes htpasswd. Le reste du chemin l'est (un mauvais mot de passe produit
un 401 propre et non une erreur de protocole, ce qui valide la forme de la
requête).

## Prudence

Le nœud est un SNO qui héberge aussi la console, RHOAI et MaaS. Les runs
ci-dessus sont volontairement courts et bornés en tokens. Avant de monter les
paliers très haut ou d'allonger les durées, garder en tête que le GPU est unique
et que la charge est réelle — ce n'est pas un banc dédié.

---

# Annexe — référence des paramètres

Chaque script affiche sa propre aide avec `--help`. Ce qui suit ajoute ce que
l'aide en ligne ne peut pas dire : à quoi sert le paramètre, quand y toucher, et
les pièges.

## Paramètres communs à tous les scripts

Ordre de résolution, valable pour chacun : **option en ligne de commande >
variable d'environnement > défaut codé**.

### Endpoint

| Paramètre | Env | Défaut | Rôle et pièges |
|---|---|---|---|
| `--url` | `LLMD_URL` | `https://qwen-alf-test.apps.ds.alf.corp` | Base URL de l'API OpenAI-compatible. ⚠️ Le défaut pointe sur l'ISVC `qwen`, qui est `READY=False` sur ce cluster : **toujours passer l'URL de `qwen-gpu`** ou exporter `LLMD_URL`. |
| `--model` | `LLMD_MODEL` | `qwen` | Valeur du champ `model` des requêtes. ⚠️ Ici c'est **`qwen-gpu`**. Un mauvais nom donne un FAIL sur « modèle servi » et des 404/400 côté vLLM. |
| `--api-key` | `LLMD_API_KEY` | (vide) | Jeton Bearer déjà obtenu. Attend un **jeton**, jamais un nom d'utilisateur. |
| `--secure` | `LLMD_INSECURE=0` | désactivé | Vérifie le certificat TLS. Par défaut la vérification est **ignorée**, parce que les certificats du cluster sont signés par une CA interne. Ne l'activer que si la CA est installée sur le poste. |
| `--timeout` | — | `120.0` | Timeout HTTP par requête, en secondes. À monter si tu génères beaucoup de tokens (`--max-tokens 2048` sur un petit GPU peut dépasser 120 s) : un timeout est compté comme une **erreur** et fera échouer le contrôle de taux d'erreur. |

### Authentification par identifiants

Alternative à `--api-key`, décrite en détail en section 2b.

| Paramètre | Env | Rôle et pièges |
|---|---|---|
| `-u`, `--user` | — | Utilisateur OpenShift. Déclenche l'échange OAuth automatique. |
| `--password` | `LLMD_PASSWORD` | ⚠️ Visible dans `ps` par les autres utilisateurs. Préférer `--password-stdin` ou la saisie interactive. |
| `--password-stdin` | — | Lit le mot de passe sur l'entrée standard. La bonne option dans un script. |
| `--api-server` | `LLMD_API_SERVER` | URL de l'API server pour la découverte OAuth. Par défaut déduite de `oc whoami --show-server` — donc `oc` et `KUBECONFIG` sont nécessaires, sauf si tu passes cette option. |
| `--oauth-url` | `LLMD_OAUTH_URL` | Endpoint `/oauth/authorize`, pour court-circuiter la découverte. Utile sans `oc`. |

### Introspection cluster

| Paramètre | Env | Défaut | Rôle et pièges |
|---|---|---|---|
| `-n`, `--namespace` | `LLMD_NS` | `alf-test` | Namespace des pods de serving. Sert à lire les métriques par pod via `oc get --raw`. |
| `--selector` | `LLMD_SELECTOR` | (vide) | Label selector des pods. Vide = auto-détection : sont retenus les pods qui réservent un GPU, ou dont le nom contient `predictor`, ou portant un label `llm-d`. À renseigner si l'auto-détection ramène trop ou trop peu de pods. |
| `--no-cluster` | — | désactivé | Coupe **toute** introspection `oc`. Obligatoire contre le mock local. Conséquence : plus de répartition par pod ni d'indices de saturation ; l'attribution retombe sur les en-têtes du Gateway, absents sur une Route KServe. |

### Sortie

| Paramètre | Rôle et pièges |
|---|---|
| `--json FICHIER` | Résultat brut complet, y compris les distributions, la saturation par pod et le détail des contrôles. C'est le format à archiver pour comparer deux runs. |
| `--csv FICHIER` | Une ligne **par requête**. Dans t02 la colonne `concurrency` est ajoutée, et la phase boucle ouverte y apparaît avec `concurrency=0`. |
| `--quiet` | Supprime l'affichage de progression (une ligne toutes les 2 s). Les verdicts et les tableaux restent. À utiliser dans un enchaînement non interactif. |

## Paramètres de workload (t02 et t03)

Ces options décrivent **ce qu'on envoie**, indépendamment de la façon de le
faire. Tout est déterministe : même `--seed` et mêmes paramètres ⇒ mêmes prompts,
condition nécessaire pour comparer deux runs.

| Paramètre | Défaut | Rôle et pièges |
|---|---|---|
| `--workload` | `chat` | Trois familles, à choisir selon ce qu'on veut prouver (voir ci-dessous). |
| `--prompt-tokens` | `64` | Taille **approximative** de la partie variable du prompt : agit sur le **prefill**, donc sur le TTFT. Compter ~1,4× la valeur demandée en tokens réels. ⚠️ **Sans effet avec `--workload chat`** (défaut de t02). Plafond ~22 500 sur ce modèle, vérifié avant envoi (voir section 4). |
| `--prefixes` | `4` | Nombre de préfixes distincts, workload `shared` uniquement. |
| `--prefix-tokens` | `512` | Taille de chaque préfixe partagé. Plus il est long, plus le gain d'un hit de cache est spectaculaire — et donc mesurable. |
| `--max-tokens` | `64` | **Plafond** de tokens générés par réponse — ne touche pas au prompt, et le modèle peut s'arrêter avant. Agit sur le **decode** : la latence croît linéairement, le débit req/s baisse, le tok/s reste constant. Consomme la fenêtre de contexte au même titre que le prompt. À figer en premier, deux runs de valeurs différentes n'étant pas comparables (voir section 4). |
| `--seed` | `1234` | Graine déterministe. Ne la changer que pour vérifier qu'un résultat ne dépend pas d'un jeu de prompts particulier. |

Le choix de `--workload` n'est pas cosmétique :

- **`chat`** — 12 prompts courts qui tournent en boucle. Trafic réaliste, mais le
  cache de préfixe de vLLM les sert presque tous en *hit* : le TTFT mesuré est
  celui du régime « cache chaud ». C'est le défaut de t02.
- **`unique`** — chaque prompt a un préfixe distinct, donc **aucun** cache
  exploitable. Pire cas pour le prefill, et le seul honnête pour mesurer une
  capacité pessimiste ou tester un équilibrage pur (le routeur n'a aucune raison
  d'affinité). C'est le défaut de t03, délibérément.
- **`shared`** — K préfixes longs réutilisés. C'est le cas que llm-d optimise
  avec son routage *prefix-cache aware* ; réservé à t04.

## `t02_load.py` — capacité et point de rupture

### Charge

| Paramètre | Défaut | Rôle et pièges |
|---|---|---|
| `--levels` | `1,2,4,8` | Paliers de **concurrence** (clients simultanés), pas des débits. Triés automatiquement par ordre croissant, car le calcul d'efficacité prend le palier le plus bas comme référence. Progression géométrique conseillée. |
| `--duration` | `20.0` | Durée de **chaque** palier, en secondes. Le nombre de requêtes n'est donc pas fixe : c'est ce qui rentre dans la durée. Durée totale ≈ `nb_paliers × duration` + chauffe + 2 appels `oc` par pod et par palier. |
| `--warmup` | `2` | Requêtes de chauffe par palier, non comptées, exécutées sur une seule connexion. Absorbent le premier chargement et l'établissement TLS. Mettre `0` fausse le premier palier. |
| `--rate` | `0` | Débit cible en req/s de la phase **boucle ouverte**. `0` = phase ignorée. C'est le seul mode qui valide un SLO. |
| `--max-inflight` | `0` | Borne les requêtes simultanées en boucle ouverte. `0` = `10×rate`. ⚠️ **Sans borne, le contrôle d'attente d'admission passe en SKIP par construction** : un worker est toujours libre, donc le retard reste nul et l'attente part dans la latence. |
| `--no-sweep` | désactivé | Ne faire que la boucle ouverte. Exige `--rate`, sinon il ne reste rien à exécuter. |
| `--quick` | désactivé | Raccourci : force `--levels 1,4` et `--duration 8`. Pour un test de non-régression rapide, pas pour une mesure. |

### Seuils et SLO

| Paramètre | Défaut | Rôle et pièges |
|---|---|---|
| `--max-error-rate` | `0.02` | Taux d'erreur toléré. Sert à **trois** choses : le verdict, l'arrêt anticipé du balayage (inutile de monter plus haut quand ça casse), et l'exclusion d'un palier du genou — un débit obtenu en jetant des requêtes n'est pas une capacité. |
| `--knee-tolerance` | `0.7` | Seuil d'efficacité définissant le genou. Le genou est le dernier palier dont le débit par client vaut encore 70 % de celui du palier de référence. Abaisser ce seuil déplace le genou vers la droite : c'est un choix de tolérance, pas une mesure. |
| `--max-sched-delay` | `2.0` s | Retard d'admission P99 toléré. **N'a d'effet qu'avec `--max-inflight`** ; sinon le contrôle est en SKIP. |
| `--max-drift` | `1.5` | Dérive de latence tolérée entre le début et la fin du run (médiane du premier tiers contre le dernier tiers, ordonnée par heure de départ). Une dérive signifie que le travail s'accumule : c'est le signal de saturation quand l'admission n'est pas bornée. |
| `--max-degradation` | `3.0` | Amplitude de dégradation des latences tolérée entre le premier et le dernier palier (WARN au-delà). Indépendant du caractère proportionné : c'est le confort d'usage qui est jugé, pas la scalabilité. |
| `--max-empty-rate` | `0.01` | Part de réponses HTTP 200 **sans aucun token** tolérée. Au-delà : FAIL. Sans ce contrôle, un modèle répondant du vide afficherait 0 % d'erreur. |
| `--slo-ttft` | `0` | SLO sur le TTFT P95, en secondes. `0` = non vérifié. |
| `--slo-p95` | `0` | SLO sur la latence totale P95, en secondes. `0` = non vérifié. |

Les SLO sont les **seuls** jugements en valeur absolue de tout t02, et ils ne
sont évalués que si tu les demandes explicitement : sans référence matérielle,
« 12 req/s » n'est ni bon ni mauvais.

## `t03_balance.py` — répartition entre pods

### Répartition

| Paramètre | Défaut | Rôle et pièges |
|---|---|---|
| `--requests` | `200` | Nombre de requêtes de la phase A. Trop peu et le χ² ne peut rien conclure ; viser au moins ~30 requêtes par pod. |
| `--clients` | `0` | Connexions concurrentes en phase A. `0` = `4 × nb de pods`, plancher 8. Doit rester **≥ au nombre de pods**, sinon un équilibrage par connexion affamerait mécaniquement certains pods et le déséquilibre serait un artefact du test. |
| `--single-requests` | `24` | Requêtes de la phase B, sur une seule connexion keep-alive. Plancher interne de 12. |
| `--no-single` | désactivé | Ignore la phase B. |
| `--warmup` | `2` | Requêtes de chauffe par phase, non comptées. |

### Seuils

| Paramètre | Défaut | Rôle et pièges |
|---|---|---|
| `--expect-pods` | `0` | Nombre de pods qui **doivent** recevoir du trafic. `0` = tous les pods Ready détectés. À forcer si l'auto-détection se trompe. |
| `--alpha` | `0.01` | Seuil de p-value du χ² d'uniformité. `p < alpha` = déséquilibre statistiquement significatif, pas du bruit d'échantillonnage. Abaisser rend le test plus permissif. |
| `--max-imbalance` | `1.5` | Rapport max/min toléré entre pods. Garde-fou lisible en complément du χ², qui devient très sévère sur de gros volumes : 5 % d'écart peut y être « significatif » sans aucune conséquence pratique. |
| `--max-empty-rate` | `0.01` | Part de réponses HTTP 200 sans aucun token tolérée (FAIL au-delà). |
| `--max-latency-spread` | `1.5` | Écart de latence P50 toléré entre pods (WARN, pas FAIL). Détecte un pod plus lent que les autres — GPU partagé, time-slicing inégal, voisin bruyant. Nécessite l'attribution **par en-tête**, donc inopérant derrière une Route KServe simple. |

## `call_model.py` — appel unitaire et mini-bench

| Paramètre | Défaut | Rôle et pièges |
|---|---|---|
| `--token` | — | Jeton en clair. Propre à ce script et **prioritaire** sur `--api-key`. |
| `--token-file` | — | Jeton lu dans un fichier. À préférer à `--token` (rien dans `ps`). |
| `--prompt` | — | Prompt unique : **désactive** le mini-bench. |
| `--max-tokens` | `48` | Tokens générés, pour `--prompt` seulement. Les niveaux du mini-bench ont leurs propres valeurs (32 / 160 / 512). |
| `--level` | `tous` | `simple`, `moyen`, `complexe` ou `tous`. Comparer les trois vaut mieux qu'un chiffre unique : un TTFT qui ne gonfle qu'au niveau complexe désigne le prefill, un TPOT dégradé partout désigne le decode. |
| `--repeat` | `1` | Répétitions de chaque prompt. À monter pour dégrossir la variance. |

## `t01_smoke.py` — smoke test

| Paramètre | Défaut | Rôle |
|---|---|---|
| `--prompt` | « Explique le GPU time-slicing… » | Prompt des deux appels de démonstration (un non-streaming, un en streaming). |
| `--max-tokens` | `48` | Tokens générés par appel. |

## Codes de retour

Identiques dans tous les scripts, pour un enchaînement en shell :

| Code | Signification |
|---|---|
| `0` | tous les contrôles passent |
| `1` | au moins un contrôle échoue |
| `2` | non concluant — endpoint injoignable ou inexploitable, paramètres incohérents |

`SKIP` ne fait jamais échouer : un cluster à un seul pod ne « rate » pas un test
d'équilibrage, il n'a simplement rien à équilibrer. `WARN` non plus : c'est une
observation à regarder, pas un échec.

---

# Annexe — lecture des tableaux de sortie

La suite produit **deux tableaux différents**, qu'il est facile de confondre.
Ils ne répondent pas à la même question et ne se lisent pas de la même façon.

| | `call_model.py` | `t02_load.py` |
|---|---|---|
| Colonnes | `niveau · appels · prompt→gén. · latence · TTFT · TPOT · tok/s` | `conc · req/s · tok/s · TTFT P50/P95 · lat P50/P95 · err % · effic.` |
| Axe du tableau | la **difficulté du prompt** | la **concurrence** |
| Concurrence | 1 (appels séquentiels) | 1, 2, 4, 8… |
| Statistiques | **moyennes** | percentiles P50/P90/P95/P99 |
| Question posée | « le modèle répond-il, et à quel coût selon le type de requête ? » | « jusqu'où ça monte, où est le genou ? » |

## Tableau 1 — `t02_load.py`, balayage de concurrence

Produit par `print_table()` (`t02_load.py:318`), une ligne par palier, phase
boucle fermée uniquement.

| Colonne | Source | Unité | Sens | Bon signe |
|---|---|---|---|---|
| `conc` | `--levels` | clients | Nombre de clients qui bouclent en permanence (boucle fermée : dès qu'une réponse arrive, la suivante part) | — c'est la variable qu'on fait monter |
| `req/s` | `throughput_rps` = requêtes OK / durée | req/s | Débit **utile** ; les erreurs ne comptent pas | croît avec `conc`, puis plafonne |
| `tok/s` | `output_tok_per_s` | tokens/s | Tokens **générés** par seconde, agrégé tous clients. La vraie mesure du travail GPU | croît puis plafonne ; s'il **baisse** = emballement |
| `TTFT P50` | `ttft.p50` ×1000 | ms | *Time To First Token* : délai avant le 1er token. Mesure le **prefill** + l'attente en file | monte doucement, explose au passage du genou |
| `TTFT P95` | `ttft.p95` | ms | Le même au 95ᵉ percentile : ce que vivent les 5 % les moins chanceux | l'écart P95/P50 dit la variabilité de la file |
| `lat P50` | `latency.p50` | **s** (pas ms) | Latence end-to-end médiane, prefill + decode | à lire avec `tokens/réponse` |
| `lat P95` | `latency.p95` | s | Idem P95 | c'est cette colonne que vise `--slo-p95` |
| `err %` | `error_rate` ×100 | % | Requêtes en échec (HTTP ≠ 200, timeout, reset) | ≤ `--max-error-rate` (défaut 2 %) ; au-delà le balayage **s'arrête** |
| `effic.` | `rps_per_client` du palier ÷ celui du palier le plus bas | ratio 0→1 | **Efficacité** : chaque client obtient-il encore le même service qu'en solo ? | 1.0 = parfait. Le **genou** est le dernier palier ≥ `--knee-tolerance` (0.7) |

Les deux lignes sous le tableau : **débit maximal observé** (palier où `req/s`
culmine) et **genou de saturation** (capacité exploitable ; au-delà, ajouter des
clients allonge les files sans produire plus de travail).

⚠️ Un palier qui dépasse `--max-error-rate` ne peut pas être le genou
(`find_knee:288`) : un débit obtenu en jetant des requêtes n'est pas une capacité.

### Le résumé affiché avant chaque ligne du tableau

`metrics.print_summary()` (`llmdbench/metrics.py:97`), pour chaque palier :

| Ligne | Signification |
|---|---|
| `requêtes OK : n / total   erreurs : k (x %)` | Volume et taux d'échec |
| `débit : X req/s \| Y tok/s générés` | Les deux faces du débit |
| `tokens/réponse : P50 / min / max` | Longueur des réponses. **Indispensable** : la latence e2e n'est comparable entre paliers que si cette valeur est stable. `⚠️ n réponse(s) VIDE(s)` = des HTTP 200 sans aucun token — succès pour le transport, échec réel (`empty_rate`) |
| `latence e2e : min/moy/P50/P90/P95/P99/max` | Distribution complète, en secondes |
| `TTFT : P50 / P90 / P99 ms` | Prefill + attente |
| `TPOT (decode) : P50 / P99 ms/tok` | *Time Per Output Token* = latence ÷ tokens générés. Coût moyen d'un token en decode |
| `ITL inter-tok : P50 / P99 ms` | *Inter-Token Latency* : l'intervalle **réel mesuré** entre deux tokens du flux SSE. TPOT est une moyenne par requête (le TTFT y est dilué), ITL est la mesure directe de la fluidité perçue. Un ITL P99 élevé = à-coups visibles |
| `! k× <erreur>` | Top 5 des messages d'erreur distincts |

**TPOT vs ITL** — c'est la confusion classique : TPOT lisse, ITL montre les
pauses (préemption, changement de batch).

### Le bloc dégradation

`degradation()` / `check_degradation()` (`t02_load.py:179`), entre le premier et
le dernier palier exploitable :

| Champ | Sens |
|---|---|
| `concurrency_ratio` | Facteur d'augmentation de la charge (ex. ×8) |
| `ttft_ratio` / `tpot_ratio` / `latency_ratio` | Facteur de dégradation du P50 correspondant |
| `superlinear` | `tpot_ratio > concurrency_ratio`. **Le verdict clé** |

Le point de comparaison n'est pas un seuil absolu, c'est la montée de charge
elle-même :

- **sous-linéaire** (TPOT ×2.9 pour charge ×8) → normal, le batching continu absorbe → `OK`
- **sur-linéaire** (TPOT ×9 pour charge ×8) → le débit agrégé de tokens a **baissé** : ajouter des clients détruit du travail utile (préemptions, pression KV-cache) → `FAIL`, ou `WARN` si moins de 20 requêtes sur le palier le plus maigre
- indépendamment, si `max(ttft_ratio, tpot_ratio) > --max-degradation` : `WARN` d'amplitude — le service répond toujours mais devient pénible, ce que `err %` ne montre jamais

### Sorties propres à la boucle ouverte (`--rate`)

`phase_open()` (`t02_load.py:454`). Les arrivées suivent un λ imposé et **ne
ralentissent pas** quand le serveur ralentit — seul mode honnête pour valider un
SLO.

| Sortie | Sens |
|---|---|
| `rate_target` / `rate_achieved` | Cible vs réalisé. Contrôle : ratio ≥ 0.95 |
| `retard d'ordonnancement P50/P95/P99` (`sched_delay`) | Écart entre heure de départ **prévue** et **réelle**. ⚠️ Informatif **uniquement** avec `--max-inflight` : sans borne, un worker est toujours libre, le retard reste nul par construction et l'attente se déplace dans la latence |
| `latence dans le temps` (sparkline) | Mini-courbe une ligne, repère un décrochage |
| `dérive de la latence` (`drift`) | P50 du premier tiers → P50 du dernier tiers, ordonnés par heure de **départ**. `ratio > --max-drift` (1.5) = le travail s'accumule. **LE signal du débit non soutenable** : la moyenne peut rester belle, c'est la *tendance* qui trahit |
| `SLO TTFT P95 ≤ --slo-ttft` | Vérifié sur `ttft.p95`, en secondes |
| `SLO latence P95 ≤ --slo-p95` | Vérifié sur `latency.p95`, en secondes |

### Sorties cluster (introspection `oc` active)

`saturation` (`_saturation:147`), par pod, deux natures de métriques :

- **jauges** `running_peak` / `waiting_peak` / `kv_usage_peak` : maximum observé
  **pendant** la rafale, échantillonné par un thread — les lire après ne montre
  qu'un pod au repos. `waiting_peak > 0` = des requêtes ont attendu un slot de
  batch, le GPU est le goulot. `kv_usage_peak` proche de 1 annonce les préemptions.
- **compteur** `preemptions_delta` : différence entre les deux photos, donc les
  préemptions imputables à **ce palier**.

`balance_stats` (`metrics.py:161`), répartition entre pods : `imbalance` = max/min
(1.0 parfait, sensible aux petits n) · `gini` = 0 parfait, 1 = tout sur un pod ·
`chi2` / `p_value` : **`p < 0.01` = déséquilibre statistiquement significatif**,
pas du bruit d'échantillonnage.

## Tableau 2 — `call_model.py`, mini-bench 3 niveaux

Produit par `print_table()` (`call_model.py:199`). Appels **séquentiels**, pas de
concurrence : ce tableau caractérise le coût selon le type de requête.

| Colonne | Source | Sens |
|---|---|---|
| `niveau` | clé de `LEVELS` (`call_model.py:64`) | Le **type de charge**, pas un palier de concurrence. Trois niveaux figés : `simple` (max_tokens 32 — capitale de la France, 17×23), `moyen` (160 — explication en 3 phrases, liste de 5 puces), `complexe` (512 — analyse d'incident rendue en JSON strict, procédure en 8 étapes) |
| `appels` | `ok/n` | Réussis / tentés. 2 prompts par niveau × `--repeat` (défaut 1), donc `2/2` par défaut |
| `prompt→gén.` | `prompt_tok` → `gen_tok`, **moyennes** | Tokens d'entrée → tokens produits. Le contexte qui rend les autres colonnes comparables : une latence de 8 s ne veut rien dire sans savoir si 30 ou 500 tokens ont été générés |
| `latence` | `fmean(r.latency)` | Latence end-to-end **moyenne**, en secondes, prefill + decode |
| `TTFT` | `fmean(r.ttft)` | *Time To First Token*, en ms. Coût du **prefill** : ingérer le prompt avant de sortir le 1er token. Monte avec `prompt_tok` |
| `TPOT` | `fmean(r.tpot)` | *Time Per Output Token*, en ms/token. Coût du **decode**, un token à la fois. Inverse de la vitesse de frappe perçue |
| `tok/s` | `fmean(completion_tokens / latency)` | Débit apparent **par requête**. ⚠️ Piège : c'est `tokens ÷ latence totale`, donc le TTFT est dilué dedans — sur le niveau `simple` (32 tokens) le prefill pèse lourd et ce chiffre est artificiellement bas. Ce n'est **pas** le `tok/s` de `t02`, qui est un débit agrégé serveur |

Les marqueurs `✓` / `✗` / `·` affichés pendant l'exécution sont le **contrôle de
contenu** (`content_ok:142`) : `paris` attendu dans la réponse, `391`, ou du JSON
valide parsable. Un `✗` est signalé en `WARN`, pas en `FAIL` — c'est la qualité du
modèle, pas celle de la plateforme.

Les deux notes automatiques sous le tableau (`print_table:211`) :

- `TTFT ×N entre le niveau le plus léger et le plus lourd` — si ratio > 3.
  Diagnostic : coût de prefill dominant (contexte long, KV-cache, ou routage)
- `TPOT ×N` — si ratio > 1.5. Diagnostic : le decode se dégrade avec la
  longueur → contention GPU ou batching saturé

La lecture clé : **TPOT devrait être à peu près constant entre les trois
niveaux**. Le decode coûte le même prix par token qu'on en génère 32 ou 512. S'il
grimpe, quelque chose se dégrade avec la longueur de séquence.

## Deux réflexes de lecture

1. `err %` à 0 ne veut pas dire que tout va bien — regarder `tokens/réponse`
   (réponses vides) et `tpot_ratio` (dégradation).
2. Le débit absolu n'est jamais jugé par les scripts : sans référence matérielle,
   « 12 req/s » n'est ni bon ni mauvais. Les verdicts portent uniquement sur les
   erreurs, l'existence d'un genou, la tenue du débit cible, et les SLO
   explicitement demandés via `--slo-*`.
