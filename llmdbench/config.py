#!/usr/bin/env python3
"""
Configuration commune : endpoint, modèle, namespace, arguments CLI partagés.

Ordre de priorité : option CLI > variable d'environnement > défaut codé.

Variables d'environnement reconnues :
    LLMD_URL        base URL de l'endpoint OpenAI-compatible
    LLMD_MODEL      nom du modèle (champ "model" des requêtes)
    LLMD_NS         namespace OpenShift du déploiement
    LLMD_SELECTOR   label selector des pods de serving
    LLMD_API_KEY    jeton Bearer si le Gateway impose une auth (Kuadrant)
    LLMD_INSECURE   "0" pour vérifier le certificat TLS (défaut : ignoré)
    KUBECONFIG      utilisé tel quel par la CLI `oc`
"""

from __future__ import annotations

import argparse
import os

# --- Défauts alignés sur le SNO `ds` -----------------------------------------
DEFAULT_URL = os.environ.get("LLMD_URL", "https://qwen-alf-test.apps.ds.alf.corp")
DEFAULT_MODEL = os.environ.get("LLMD_MODEL", "qwen")
DEFAULT_NS = os.environ.get("LLMD_NS", "alf-test")
DEFAULT_SELECTOR = os.environ.get("LLMD_SELECTOR", "")  # vide = auto-détection
DEFAULT_API_KEY = os.environ.get("LLMD_API_KEY", "") or None
DEFAULT_INSECURE = os.environ.get("LLMD_INSECURE", "1") != "0"

# Port du serveur vLLM dans le pod (sert /metrics et l'API OpenAI).
DEFAULT_POD_PORT = int(os.environ.get("LLMD_POD_PORT", "8080"))

# En-têtes que les gateways llm-d / EPP ajoutent parfois à la réponse et qui
# révèlent le pod choisi. On les capture toutes, la première trouvée gagne.
ENDPOINT_HEADERS = (
    "x-gateway-destination-endpoint",
    "x-inference-pod",
    "x-llm-d-endpoint",
    "x-envoy-upstream-host",
    "x-served-by",
)


def add_common_args(ap: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Ajoute les options partagées par tous les scripts de la suite."""
    g = ap.add_argument_group("endpoint")
    g.add_argument("--url", default=DEFAULT_URL,
                   help=f"base URL de l'API (défaut: {DEFAULT_URL})")
    g.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"nom du modèle (défaut: {DEFAULT_MODEL})")
    # Défaut à None (et non DEFAULT_API_KEY) pour pouvoir distinguer « option
    # donnée » de « variable d'environnement » : c'est `auth.resolve` qui
    # applique la priorité, sinon $LLMD_API_KEY masquerait --user/--password.
    g.add_argument("--api-key", default=None,
                   help="jeton Bearer déjà obtenu (sinon : $LLMD_API_KEY, "
                        "ou --user/--password)")

    a = ap.add_argument_group("authentification par identifiants (alternative "
                              "à --api-key)")
    a.add_argument("-u", "--user",
                   help="utilisateur OpenShift ; le jeton OAuth est obtenu "
                        "automatiquement, sans toucher au KUBECONFIG")
    a.add_argument("--password",
                   help="mot de passe (déconseillé : visible dans `ps` ; "
                        "préférer --password-stdin ou $LLMD_PASSWORD)")
    a.add_argument("--password-stdin", action="store_true",
                   help="lit le mot de passe sur l'entrée standard")
    a.add_argument("--api-server", default=os.environ.get("LLMD_API_SERVER") or None,
                   help="URL de l'API server pour la découverte OAuth "
                        "(défaut : `oc whoami --show-server`)")
    a.add_argument("--oauth-url", default=os.environ.get("LLMD_OAUTH_URL") or None,
                   help="endpoint /oauth/authorize, pour court-circuiter la "
                        "découverte")
    g.add_argument("--secure", dest="insecure", action="store_false",
                   default=DEFAULT_INSECURE,
                   help="vérifier le certificat TLS (défaut: ignoré)")
    g.add_argument("--timeout", type=float, default=120.0,
                   help="timeout HTTP par requête, en secondes")

    c = ap.add_argument_group("cluster (introspection oc, facultatif)")
    c.add_argument("-n", "--namespace", default=DEFAULT_NS,
                   help=f"namespace des pods de serving (défaut: {DEFAULT_NS})")
    c.add_argument("--selector", default=DEFAULT_SELECTOR,
                   help="label selector des pods ; vide = auto-détection")
    c.add_argument("--no-cluster", action="store_true",
                   help="désactive toute introspection `oc` (tests HTTP purs)")

    o = ap.add_argument_group("sortie")
    o.add_argument("--json", metavar="FICHIER",
                   help="écrit le résultat brut en JSON")
    o.add_argument("--csv", metavar="FICHIER",
                   help="écrit les requêtes individuelles en CSV")
    o.add_argument("--quiet", action="store_true", help="sortie condensée")
    return ap


def resolve_auth(args, ck=None) -> str:
    """
    Résout le jeton (`--api-key`, ou `--user`/`--password`, ou $LLMD_API_KEY) et
    l'écrit dans `args.api_key`. Retourne une description de la source.

    À appeler juste après `parse_args()`, avant toute requête. En cas d'échec de
    l'échange OAuth on n'interrompt pas brutalement : le message est affiché et
    l'exécution continue en anonyme, ce qui laisse le contrôle « endpoint
    exploitable » produire un diagnostic homogène avec les autres cas.
    """
    from . import auth
    args.auth_error = None
    try:
        return auth.resolve_into_args(args)
    except auth.AuthError as e:
        print(f"  ⚠️  authentification impossible : {e}")
        args.api_key = None
        # Mémorisé pour que le diagnostic en aval renvoie à cette cause au lieu
        # d'en supposer une autre (cf. httpclient.explain_auth).
        args.auth_error = str(e)
        return f"échec ({e})"


def check_context_window(wl, max_tokens: int, window, ck) -> bool:
    """
    Vérifie **avant d'envoyer** que prompt + génération tiennent dans la fenêtre
    de contexte du modèle. Retourne False si le run est voué à l'échec.

    Pourquoi un contrôle a priori : au-delà de la fenêtre, vLLM refuse *chaque*
    requête par un HTTP 400. Le test affiche alors 100 % d'erreurs et un verdict
    rouge, sans que rien n'indique que la cause est un paramètre mal choisi. Le
    message ci-dessous donne directement la valeur à ne pas dépasser.

    C'est bien `prompt + max_tokens` qui doit tenir, et non le prompt seul : les
    tokens générés consomment la même fenêtre.
    """
    est = wl.estimated_prompt_tokens()
    need = est + max_tokens
    if window is None:
        ck.skip("fenêtre de contexte",
                "`max_model_len` non publié par /v1/models — contrôle impossible")
        return True

    opt = wl.limiting_option()
    if need > window:
        ck.fail("fenêtre de contexte",
                f"prompt estimé à ~{est} tokens + {max_tokens} générés = "
                f"~{need}, or la fenêtre du modèle est de {window} : chaque "
                f"requête serait refusée en HTTP 400. Réduire {opt} à "
                f"{wl.max_setting_for(window, max_tokens)} au plus, ou baisser "
                "--max-tokens")
        return False
    if need > 0.9 * window:
        ck.warn("fenêtre de contexte",
                f"~{need} tokens pour une fenêtre de {window} : marge de "
                f"{100*(1 - need/window):.0f} % seulement. L'estimation étant "
                f"approximative, {opt} peut basculer en HTTP 400 selon les "
                "prompts tirés")
    else:
        ck.ok("fenêtre de contexte",
              f"~{need} tokens estimés ({est} de prompt + {max_tokens} générés) "
              f"pour une fenêtre de {window}")
    return True


def banner(title: str, args, extra: dict | None = None) -> None:
    """En-tête uniforme pour tous les tests."""
    print(f"\n=== {title} ===")
    print(f"  endpoint  : {args.url}")
    print(f"  modèle    : {args.model}")
    if not getattr(args, "no_cluster", False):
        print(f"  namespace : {args.namespace}")
    for k, v in (extra or {}).items():
        print(f"  {k:<9} : {v}")
    print()
