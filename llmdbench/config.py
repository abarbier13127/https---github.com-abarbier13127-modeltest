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
    g.add_argument("--api-key", default=DEFAULT_API_KEY,
                   help="jeton Bearer si le Gateway exige une auth")
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
