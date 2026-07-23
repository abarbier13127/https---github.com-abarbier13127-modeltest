"""
llmdbench — bibliothèque de test de charge et de validation llm-d / KServe.

Objectif : vérifier qu'un modèle servi par vLLM sous OpenShift AI se comporte
correctement quand il est réparti sur plusieurs GPU (time-slicing ou cartes
physiques) et plusieurs pods (replicas KServe ou rôles prefill/decode llm-d).

**Stdlib Python uniquement** — aucun `pip install`, rejouable tel quel.
Les seules dépendances externes sont facultatives et hors-processus :
  - la CLI `oc` (déjà présente sur le poste) pour l'introspection cluster ;
  - rien d'autre.

Modules :
  config      constantes, résolution d'endpoint, arguments CLI communs
  httpclient  client HTTP keep-alive + streaming SSE (TTFT / ITL)
  workload    générateurs de prompts (unique, préfixe partagé, conversation)
  loadgen     moteurs de charge boucle fermée (workers) et ouverte (Poisson)
  metrics     percentiles, agrégats, histogrammes texte
  cluster     introspection `oc` : pods, GPU, rôles llm-d, métriques par pod
  report      export JSON / CSV / Markdown
"""

__version__ = "1.0.0"
__all__ = [
    "config",
    "httpclient",
    "workload",
    "loadgen",
    "metrics",
    "cluster",
    "report",
]
