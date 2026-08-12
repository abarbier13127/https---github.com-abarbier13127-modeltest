#!/usr/bin/env python3
"""
Générateurs de prompts.

Trois familles, qui servent des objectifs de test différents :

  unique   chaque prompt a un préfixe distinct → **aucun** cache de préfixe
           exploitable. C'est le pire cas pour le prefill ; c'est ce qu'il faut
           pour mesurer un débit honnête et pour tester l'équilibrage pur
           (le routeur n'a aucune raison d'affinité).

  shared   K préfixes longs réutilisés en boucle → c'est le cas que llm-d
           optimise avec son routage *prefix-cache aware* : les requêtes
           partageant un préfixe devraient converger vers le **même** pod,
           qui a déjà les blocs KV en cache. Un TTFT qui s'effondre au 2ᵉ
           passage = cache hit ; une distribution aléatoire des pods par
           groupe = routage non affine.

  chat     prompts courts et variés, proches d'un trafic réel, pour les tests
           de charge « métier » et les smoke tests.

Tout est déterministe (seed) : deux exécutions comparables produisent
exactement les mêmes prompts, condition nécessaire pour comparer deux runs.
"""

from __future__ import annotations

import hashlib
import random

# Vocabulaire volontairement banal : on veut du texte tokenisable, pas du sens.
_WORDS = (
    "cluster nœud pod conteneur image registre volume stockage réseau service "
    "route ingress passerelle certificat quota limite mémoire processeur carte "
    "graphique inférence modèle jeton contexte fenêtre attention couche poids "
    "quantification latence débit saturation file attente ordonnanceur réplica "
    "sonde redémarrage journal métrique tableau bord alerte seuil incident "
    "sauvegarde restauration migration déploiement version correctif sécurité "
    "authentification autorisation secret chiffrement audit conformité "
    "orchestration automatisation pipeline artefact dépendance compilation"
).split()

CHAT_PROMPTS = [
    "Explique le concept de conteneur en deux phrases.",
    "Donne trois avantages de Kubernetes.",
    "Qu'est-ce que le GPU time-slicing ?",
    "Résume ce qu'est vLLM.",
    "Cite deux cas d'usage de l'inférence LLM en entreprise.",
    "Quelle est la différence entre CPU et GPU ?",
    "Explique le batching continu en une phrase.",
    "Qu'est-ce qu'un InferenceService dans KServe ?",
    "À quoi sert un cache de préfixe KV ?",
    "Explique la désagrégation prefill/decode en deux phrases.",
    "Pourquoi un LLM a-t-il besoin de mémoire GPU ?",
    "Donne un exemple de prompt système utile.",
]

# ~1.3 token par mot en français comme en anglais sur les tokenizers BPE usuels.
_TOKENS_PER_WORD = 1.3

# Facteur de correction entre les tokens *demandés* et les tokens *réellement*
# comptés par le serveur. Mesuré le 2026-08-12 sur qwen-gpu : 64 → 144,
# 2048 → 2928, 8192 → 11632, 20000 → 28392, soit ~1.4×. L'estimation de
# 1.3 token/mot ci-dessus est donc trop optimiste pour du français ; on majore
# volontairement pour que le contrôle de fenêtre de contexte se trompe du bon
# côté (mieux vaut refuser un prompt limite que de le laisser produire un 400).
TOKEN_ESTIMATE_FACTOR = 1.45

# Surcoût fixe : ligne d'identifiant, consigne finale, balisage de rôle.
_WRAPPER_TOKENS = 40


def _filler(rng: random.Random, n_tokens: int) -> str:
    n_words = max(1, int(n_tokens / _TOKENS_PER_WORD))
    return " ".join(rng.choice(_WORDS) for _ in range(n_words))


class Workload:
    """Itérateur de prompts. `next(i)` renvoie `(prompt, groupe)`."""

    def __init__(self, kind="chat", prompt_tokens=64, prefixes=4,
                 prefix_tokens=512, seed=1234):
        if kind not in ("unique", "shared", "chat"):
            raise ValueError(f"workload inconnu: {kind}")
        self.kind = kind
        self.prompt_tokens = prompt_tokens
        self.n_prefixes = max(1, prefixes)
        self.prefix_tokens = prefix_tokens
        self.seed = seed
        rng = random.Random(seed)
        # Préfixes longs, figés pour toute la durée du run.
        self._prefixes = [
            "Contexte de référence n°%d à mémoriser :\n%s\n---\n"
            % (k, _filler(rng, prefix_tokens))
            for k in range(self.n_prefixes)
        ]

    # -- description ----------------------------------------------------------
    @property
    def groups(self) -> list[str]:
        if self.kind == "shared":
            return [f"pfx{k}" for k in range(self.n_prefixes)]
        return ["-"]

    def describe(self) -> str:
        if self.kind == "shared":
            return (f"shared: {self.n_prefixes} préfixes × ~{self.prefix_tokens} tok "
                    f"+ ~{self.prompt_tokens} tok uniques")
        if self.kind == "unique":
            return f"unique: ~{self.prompt_tokens} tok, aucun préfixe commun"
        return "chat: prompts courts variés"

    # -- génération -----------------------------------------------------------
    def next(self, i: int) -> tuple[str, str]:
        if self.kind == "chat":
            return CHAT_PROMPTS[i % len(CHAT_PROMPTS)], "-"

        # Sous-graine dérivée de l'index : reproductible et sans état partagé,
        # donc utilisable depuis plusieurs threads sans verrou.
        rng = random.Random(f"{self.seed}:{i}")
        if self.kind == "unique":
            body = _filler(rng, self.prompt_tokens)
            return (f"Identifiant unique {i}-{rng.randrange(10**9)}.\n{body}\n"
                    "Résume le texte ci-dessus en une phrase."), "-"

        k = i % self.n_prefixes
        tail = _filler(rng, max(8, self.prompt_tokens // 4))
        return (self._prefixes[k] + f"Question {i} : {tail}\n"
                "Réponds brièvement en t'appuyant sur le contexte."), f"pfx{k}"

    def estimated_prompt_tokens(self) -> int:
        """
        Estimation **haute** du nombre de tokens réels du plus long prompt produit.

        Sert au contrôle a priori de la fenêtre de contexte : sans lui, un prompt
        trop long fait échouer chaque requête en HTTP 400, donc 100 % d'erreurs,
        et le test est rouge sans jamais dire pourquoi.
        """
        if self.kind == "chat":
            return 32                       # prompts fixes, tous très courts
        if self.kind == "unique":
            base = self.prompt_tokens
        else:                               # shared : préfixe + queue
            base = self.prefix_tokens + max(8, self.prompt_tokens // 4)
        return int(base * TOKEN_ESTIMATE_FACTOR) + _WRAPPER_TOKENS

    def limiting_option(self) -> str:
        """Nom de l'option à réduire si le contexte ne tient pas."""
        if self.kind == "shared":
            return "--prefix-tokens"
        return "--prompt-tokens"

    def max_setting_for(self, window: int, max_tokens: int) -> int:
        """
        Plus grande valeur de `limiting_option()` qui tienne dans `window`.

        Inverse de `estimated_prompt_tokens` : on retire le budget de génération
        et le surcoût d'encadrement, puis on divise par le facteur de correction.
        """
        budget = window - max_tokens - _WRAPPER_TOKENS
        if budget <= 0:
            return 0
        base = int(budget / TOKEN_ESTIMATE_FACTOR)
        if self.kind == "shared":
            base -= max(8, self.prompt_tokens // 4)
        return max(0, base)

    def fingerprint(self) -> str:
        """Empreinte courte du workload, à inscrire dans les rapports."""
        h = hashlib.sha256(
            f"{self.kind}|{self.prompt_tokens}|{self.n_prefixes}|"
            f"{self.prefix_tokens}|{self.seed}".encode()).hexdigest()
        return h[:12]


def add_workload_args(ap):
    g = ap.add_argument_group("workload")
    g.add_argument("--workload", choices=("chat", "unique", "shared"),
                   default="chat", help="famille de prompts (défaut: chat)")
    g.add_argument("--prompt-tokens", type=int, default=64,
                   help="taille approximative de la partie variable")
    g.add_argument("--prefixes", type=int, default=4,
                   help="nombre de préfixes distincts (workload shared)")
    g.add_argument("--prefix-tokens", type=int, default=512,
                   help="taille approximative de chaque préfixe partagé")
    g.add_argument("--max-tokens", type=int, default=64,
                   help="tokens générés par réponse")
    g.add_argument("--seed", type=int, default=1234, help="graine déterministe")
    return ap


def from_args(args) -> Workload:
    return Workload(kind=args.workload, prompt_tokens=args.prompt_tokens,
                    prefixes=args.prefixes, prefix_tokens=args.prefix_tokens,
                    seed=args.seed)
