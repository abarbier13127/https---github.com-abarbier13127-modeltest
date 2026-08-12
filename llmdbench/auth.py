#!/usr/bin/env python3
"""
Obtention d'un jeton Bearer : soit fourni tel quel, soit dérivé d'un couple
utilisateur / mot de passe via le serveur OAuth d'OpenShift.

Pourquoi ne pas simplement appeler `oc login` ?
  - `oc login` **écrit dans le KUBECONFIG courant** et remplace le contexte
    actif. Sur ce projet, cela ferait perdre l'accès `system:admin` du fichier
    `kubeconfig-noingress` — un effet de bord inacceptable pour un test.
  - la suite est stdlib-only ; `oc` reste une dépendance facultative et
    hors-processus, jamais requise pour un test purement HTTP.

On implémente donc directement le flux dit *challenging client*, exactement
celui que `oc login -u/-p` utilise :

    GET <authorize_endpoint>?client_id=openshift-challenging-client
                            &response_type=token
    Authorization: Basic base64(user:password)
    X-CSRF-Token: 1
    → 302 Location: .../#access_token=sha256~…&expires_in=86400

Le jeton est dans le **fragment** de l'URL de redirection (après `#`), donc
jamais envoyé à un serveur : il faut lire l'en-tête `Location` sans suivre la
redirection.

L'`authorize_endpoint` est découvert sans authentification sur l'API server :

    GET https://api.<cluster>:6443/.well-known/oauth-authorization-server

Sécurité : un mot de passe passé en argument de ligne de commande est visible
dans `ps` par tout utilisateur de la machine. Par ordre de préférence :
saisie interactive (défaut si le terminal le permet), `--password-stdin`,
variable `LLMD_PASSWORD`, puis `--password` en dernier recours.
"""

from __future__ import annotations

import base64
import getpass
import http.client
import json
import os
import ssl
import sys
import urllib.parse

OAUTH_CLIENT_ID = "openshift-challenging-client"
WELL_KNOWN = "/.well-known/oauth-authorization-server"


class AuthError(RuntimeError):
    """Échec d'obtention du jeton, avec un message directement actionnable."""


def _ctx(insecure: bool):
    if not insecure:
        return ssl.create_default_context()
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def _get(url: str, headers=None, insecure=True, timeout=30):
    """GET sans suivre les redirections. Retourne (status, headers, body)."""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https"):
        raise AuthError(f"URL invalide (schéma manquant ?) : {url!r}")
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    if p.scheme == "https":
        conn = http.client.HTTPSConnection(p.hostname, p.port or 443,
                                           timeout=timeout, context=_ctx(insecure))
    else:
        conn = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=timeout)
    try:
        conn.request("GET", path, headers=headers or {})
        r = conn.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        conn.close()


# --- découverte ---------------------------------------------------------------
def discover_authorize_endpoint(api_server: str, insecure=True, timeout=30) -> str:
    """Lit l'`authorization_endpoint` publié par l'API server (non authentifié)."""
    url = api_server.rstrip("/") + WELL_KNOWN
    try:
        status, _h, body = _get(url, insecure=insecure, timeout=timeout)
    except OSError as e:
        raise AuthError(f"API server injoignable pour la découverte OAuth "
                        f"({url}) : {e}") from e
    if status != 200:
        raise AuthError(f"découverte OAuth impossible : HTTP {status} sur {url}")
    try:
        ep = json.loads(body).get("authorization_endpoint")
    except json.JSONDecodeError as e:
        raise AuthError(f"réponse de découverte OAuth illisible sur {url}") from e
    if not ep:
        raise AuthError(f"aucun `authorization_endpoint` publié sur {url}")
    return ep


def api_server_from_oc() -> str:
    """
    Déduit l'URL de l'API server via `oc whoami --show-server`.

    Import différé : `auth` doit rester utilisable sans `oc` installé.
    """
    from . import cluster
    try:
        return cluster.oc("whoami", "--show-server", timeout=20).strip()
    except Exception as e:  # noqa: BLE001
        raise AuthError(f"impossible de déduire l'API server via `oc` ({e}) — "
                        "fournir --api-server ou --oauth-url") from e


def resolve_authorize_endpoint(args, insecure=True) -> str:
    """--oauth-url > --api-server (découverte) > `oc whoami --show-server`."""
    if getattr(args, "oauth_url", None):
        return args.oauth_url
    api = getattr(args, "api_server", None) or api_server_from_oc()
    return discover_authorize_endpoint(api, insecure=insecure,
                                       timeout=getattr(args, "timeout", 30))


# --- obtention du jeton ------------------------------------------------------
def oauth_token(user: str, password: str, authorize_endpoint: str,
                insecure=True, timeout=30) -> tuple[str, int]:
    """
    Échange un couple utilisateur/mot de passe contre un jeton d'accès OAuth.

    Retourne (jeton, expires_in_secondes). `expires_in` vaut 0 si non publié.
    """
    sep = "&" if "?" in authorize_endpoint else "?"
    url = (f"{authorize_endpoint}{sep}client_id={OAUTH_CLIENT_ID}"
           f"&response_type=token")
    basic = base64.b64encode(f"{user}:{password}".encode()).decode()
    try:
        status, headers, body = _get(url, headers={
            "Authorization": f"Basic {basic}",
            # Obligatoire pour ce client : sans lui le serveur refuse de
            # délivrer un jeton hors navigateur.
            "X-CSRF-Token": "1",
        }, insecure=insecure, timeout=timeout)
    except OSError as e:
        raise AuthError(f"serveur OAuth injoignable ({url}) : {e}") from e

    if status == 401:
        raise AuthError(f"identifiants refusés pour « {user} » (HTTP 401) — "
                        "vérifier le mot de passe et le fournisseur d'identité")
    loc = headers.get("location", "")
    if status not in (302, 303) or not loc:
        snippet = body[:200].decode("utf-8", errors="replace").strip()
        raise AuthError(f"réponse OAuth inattendue : HTTP {status}"
                        + (f" — {snippet}" if snippet else ""))

    frag = urllib.parse.urlparse(loc).fragment
    params = urllib.parse.parse_qs(frag)
    token = (params.get("access_token") or [""])[0]
    if not token:
        # Un `error=` dans le fragment est le cas le plus fréquent ici.
        err = (params.get("error_description") or params.get("error") or [""])[0]
        raise AuthError("aucun jeton dans la redirection OAuth"
                        + (f" : {err}" if err else f" ({loc[:160]})"))
    try:
        expires = int((params.get("expires_in") or ["0"])[0])
    except ValueError:
        expires = 0
    return token, expires


# --- mot de passe ------------------------------------------------------------
def read_password(args) -> str:
    """
    --password-stdin > --password > $LLMD_PASSWORD > saisie interactive.

    L'ordre place volontairement `--password` après stdin : un mot de passe en
    ligne de commande est lisible dans `ps` par les autres utilisateurs.
    """
    if getattr(args, "password_stdin", False):
        pw = sys.stdin.readline().strip()
        if not pw:
            raise AuthError("--password-stdin : rien reçu sur l'entrée standard")
        return pw
    if getattr(args, "password", None):
        return args.password
    env = os.environ.get("LLMD_PASSWORD")
    if env:
        return env
    if sys.stdin.isatty():
        return getpass.getpass(f"Mot de passe pour « {args.user} » : ")
    raise AuthError("aucun mot de passe fourni et pas de terminal pour le "
                    "demander : utiliser --password-stdin, $LLMD_PASSWORD "
                    "ou --password")


# --- point d'entrée unique ---------------------------------------------------
def resolve(args) -> tuple[str, str]:
    """
    Détermine le jeton à utiliser. Retourne (jeton, description_de_la_source).

    Priorité : jeton explicite > utilisateur/mot de passe > $LLMD_API_KEY >
    aucun (appel anonyme, légitime si l'endpoint n'est pas protégé).

    Le jeton explicite gagne pour que `--api-key` reste un moyen de forcer une
    valeur précise, y compris pour tester un jeton volontairement invalide.
    """
    explicit = getattr(args, "api_key", None)
    if explicit:
        return explicit.strip(), "--api-key"

    user = getattr(args, "user", None)
    if user:
        insecure = getattr(args, "insecure", True)
        endpoint = resolve_authorize_endpoint(args, insecure=insecure)
        pw = read_password(args)
        token, expires = oauth_token(user, pw, endpoint, insecure=insecure,
                                     timeout=getattr(args, "timeout", 30))
        src = f"OAuth, utilisateur « {user} »"
        if expires:
            src += f" (valide {expires // 3600} h)"
        return token, src

    from . import config
    if config.DEFAULT_API_KEY:
        return config.DEFAULT_API_KEY.strip(), "$LLMD_API_KEY"
    return "", ""


def resolve_into_args(args) -> str:
    """
    Comme `resolve`, mais écrit le résultat dans `args.api_key`.

    Permet à `httpclient.session_from_args` de rester inchangé : tous les
    scripts continuent de lire `args.api_key`, quelle que soit l'origine réelle
    du jeton.
    """
    token, source = resolve(args)
    args.api_key = token or None
    return source
