#!/usr/bin/env python3
"""
Appel du modèle, avec ou sans authentification, + mini-bench embarqué.

Règle d'auth, unique : **si un jeton est fourni, il part dans l'en-tête
`Authorization: Bearer <jeton>` ; sinon la requête part sans en-tête
d'authentification.** Le script fonctionne dans les deux cas et dit lequel il a
utilisé.

Par défaut il déroule un **mini-bench en trois niveaux de complexité** :

  simple    prompt court, réponse de quelques tokens. Domine le TTFT : c'est la
            latence « à vide » du chemin Route → Gateway → pod → prefill.
  moyen     explication de ~150 tokens. Régime mixte, le plus proche d'un usage
            interactif réel.
  complexe  contexte long + génération longue et contrainte (JSON). Charge le
            prefill *et* le decode : c'est là que se voient la pression sur le
            KV-cache et les écarts entre pods.

Comparer les trois niveaux vaut mieux qu'un chiffre unique : un TTFT qui gonfle
seulement au niveau complexe désigne le prefill (donc le GPU ou le routage), un
TPOT qui se dégrade partout désigne le decode (donc la contention entre pods).

Stdlib uniquement, aucun `pip install`.

Sources du jeton, dans l'ordre :
    --token <valeur>          jeton en clair
    --token-file <fichier>    jeton lu dans un fichier
    $LLMD_API_KEY             variable d'environnement
    (rien)                    appel anonyme

Usage :
    python3 call_model.py                                   # bench, sans auth
    python3 call_model.py --token "$(oc whoami -t)"         # bench, avec auth
    python3 call_model.py --token-file ~/.llmd-token
    python3 call_model.py --level simple --repeat 3
    python3 call_model.py --prompt "Bonjour"                # appel unique
    python3 call_model.py --url http://127.0.0.1:8000 --secure --no-cluster

Codes retour : 0 = OK, 1 = échec, 2 = non concluant (endpoint injoignable).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

# `llmdbench` est le paquet local livré à côté de ce script — que des modules de
# la stdlib, rien à installer. Python ajoute normalement le dossier du script au
# sys.path, sauf en mode isolé (-I / -P) : on le fait explicitement pour que le
# script tourne depuis n'importe où et quelle que soit l'invocation.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llmdbench import config, httpclient, report  # noqa: E402

JSON_EXPECT = "__json__"

# Trois niveaux, deux prompts chacun. `attendu` = contrôle de contenu :
# une chaîne à retrouver, JSON_EXPECT pour « la réponse doit être du JSON
# valide », ou None quand seule la forme importe.
LEVELS = {
    "simple": {
        "max_tokens": 32,
        "prompts": [
            ("Réponds uniquement par le nom de la ville, sans phrase. "
             "Quelle est la capitale de la France ?", "paris"),
            ("Calcule 17 × 23. Réponds uniquement par le nombre.", "391"),
        ],
    },
    "moyen": {
        "max_tokens": 160,
        "prompts": [
            ("Explique en trois phrases ce qu'est le time-slicing GPU sur "
             "Kubernetes, et à quoi il sert pour servir un LLM.", None),
            ("Donne cinq avantages de vLLM pour le serving de LLM en "
             "production, sous forme de liste à puces.", None),
        ],
    },
    "complexe": {
        "max_tokens": 512,
        "prompts": [
            ("Tu es un ingénieur plateforme. Contexte : un cluster OpenShift "
             "sert un LLM via KServe et vLLM ; une carte GPU est partagée en "
             "quatre via le time-slicing ; deux pods predictor tournent sur le "
             "même nœud ; les utilisateurs se plaignent d'une latence qui "
             "double aux heures de pointe.\n"
             "Analyse la situation, puis réponds UNIQUEMENT par un objet JSON "
             "valide, sans texte autour et sans balises de code, de la forme :\n"
             '{"causes": ["…", "…"], "verifications": ["…"], '
             '"actions": ["…"], "risque": "faible|moyen|eleve"}',
             JSON_EXPECT),
            ("Rédige une procédure numérotée en 8 étapes pour diagnostiquer un "
             "InferenceService KServe qui reste en READY=False sur OpenShift "
             "AI, en citant à chaque étape la commande `oc` à lancer et ce "
             "qu'il faut y lire.", None),
        ],
    },
}


# --- jeton -------------------------------------------------------------------
def resolve_token(args) -> tuple[str, str]:
    """Renvoie (jeton, source). Jeton vide = appel anonyme assumé."""
    if args.token:
        return args.token.strip(), "--token"
    if args.token_file:
        path = os.path.expanduser(args.token_file)
        with open(path, encoding="utf-8") as f:
            return f.read().strip(), f"fichier {path}"
    if config.DEFAULT_API_KEY:
        return config.DEFAULT_API_KEY, "$LLMD_API_KEY"
    return "", ""


# --- contrôle de contenu -----------------------------------------------------
def _extract_json(text: str):
    """Tolère les balises de code et le bavardage autour de l'objet JSON."""
    t = text.strip()
    if "```" in t:
        parts = t.split("```")
        t = max(parts, key=len).removeprefix("json").strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(t[i:j + 1])
    except json.JSONDecodeError:
        return None


def content_ok(text: str, expect) -> bool | None:
    """True/False si un contrôle s'applique, None sinon."""
    if expect is None:
        return None
    if expect == JSON_EXPECT:
        return _extract_json(text) is not None
    return expect.lower() in text.lower()


# --- bench -------------------------------------------------------------------
def run_level(s, level: str, spec: dict, repeat: int, ck, quiet: bool) -> dict:
    results, contenu_ko = [], 0
    for prompt, expect in spec["prompts"]:
        for _ in range(repeat):
            r = s.chat(prompt, max_tokens=spec["max_tokens"])
            results.append(r)
            verdict = content_ok(r.text, expect) if r.ok else None
            if verdict is False:
                contenu_ko += 1
            if not quiet:
                head = prompt.split("\n")[0][:58]
                if r.ok:
                    mark = {True: "✓", False: "✗", None: "·"}[verdict]
                    print(f"    {mark} {head:<58} {r.latency:6.2f}s  "
                          f"TTFT {1000*(r.ttft or 0):5.0f} ms  "
                          f"{r.completion_tokens:4d} tok")
                else:
                    print(f"    ! {head:<58} ÉCHEC — {r.error}")

    ok = [r for r in results if r.ok]
    ck.expect(len(ok) == len(results), f"niveau {level} — {len(results)} appel(s)",
              f"{len(ok)}/{len(results)} réussi(s)"
              + (f" — {results[0].error}" if not ok and results else ""))
    if contenu_ko:
        ck.warn(f"niveau {level} — contenu attendu",
                f"{contenu_ko}/{len(results)} réponse(s) hors format ou hors sujet "
                "(qualité du modèle, pas de la plateforme)")
    if not ok:
        return {"level": level, "n": len(results), "ok": 0}

    tps = [r.completion_tokens / r.latency for r in ok if r.latency > 0]
    tpots = [r.tpot for r in ok if r.tpot is not None]
    return {
        "level": level,
        "n": len(results),
        "ok": len(ok),
        "max_tokens": spec["max_tokens"],
        "prompt_tok": statistics.fmean(r.prompt_tokens for r in ok),
        "gen_tok": statistics.fmean(r.completion_tokens for r in ok),
        "latency": statistics.fmean(r.latency for r in ok),
        "ttft": statistics.fmean(r.ttft for r in ok if r.ttft is not None) or 0.0,
        "tpot": statistics.fmean(tpots) if tpots else 0.0,
        "tok_s": statistics.fmean(tps) if tps else 0.0,
        "content_ko": contenu_ko,
    }


def print_table(rows: list) -> None:
    print("\n  niveau     appels  prompt→gén.   latence     TTFT     TPOT    tok/s")
    print("  " + "-" * 68)
    for r in rows:
        if not r.get("ok"):
            print(f"  {r['level']:<10} {r['n']:>4}    — aucun appel réussi —")
            continue
        print(f"  {r['level']:<10} {r['ok']:>3}/{r['n']:<3} "
              f"{r['prompt_tok']:>5.0f}→{r['gen_tok']:<5.0f} "
              f"{r['latency']:>8.2f}s {1000*r['ttft']:>7.0f}ms "
              f"{1000*r['tpot']:>6.1f}ms {r['tok_s']:>7.1f}")

    good = [r for r in rows if r.get("ok")]
    if len(good) >= 2:
        ttfts = [r["ttft"] for r in good]
        tpots = [r["tpot"] for r in good if r["tpot"]]
        note = []
        if max(ttfts) > 3 * max(min(ttfts), 1e-3):
            note.append(f"TTFT ×{max(ttfts)/max(min(ttfts),1e-3):.1f} entre le "
                        "niveau le plus léger et le plus lourd → coût de prefill "
                        "dominant (contexte long, KV-cache, ou routage)")
        if tpots and max(tpots) > 1.5 * min(tpots):
            note.append(f"TPOT ×{max(tpots)/min(tpots):.1f} → le decode se dégrade "
                        "avec la longueur : contention GPU ou batching saturé")
        for n in note:
            print(f"  → {n}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Appel du modèle (jeton Bearer optionnel) + mini-bench 3 niveaux")
    config.add_common_args(ap)
    ap.add_argument("--token", help="jeton Bearer en clair")
    ap.add_argument("--token-file", help="fichier contenant le jeton")
    ap.add_argument("--prompt", help="prompt unique : désactive le mini-bench")
    ap.add_argument("--max-tokens", type=int, default=48,
                    help="tokens générés, pour --prompt seulement")
    ap.add_argument("--level", choices=("tous", *LEVELS), default="tous",
                    help="niveau(x) du mini-bench (défaut: tous)")
    ap.add_argument("--repeat", type=int, default=1,
                    help="répétitions de chaque prompt (défaut 1)")
    args = ap.parse_args()

    token, source = resolve_token(args)
    auth = f"Bearer, source {source}" if token else "aucune (appel anonyme)"
    mode = "prompt unique" if args.prompt else f"mini-bench ({args.level})"
    config.banner("Appel du modèle", args, {"auth": auth, "mode": mode})

    ck = report.Checks("appel modèle")
    s = httpclient.Session(url=args.url, model=args.model, insecure=args.insecure,
                           api_key=token or None, timeout=args.timeout)
    out = {"url": args.url, "model": args.model, "authenticated": bool(token)}
    try:
        # 1. L'endpoint répond-il, et l'auth passe-t-elle ?
        r = s.raw("GET", "/v1/models")
        out["models_status"] = r["status"]
        if r["error"]:
            ck.fail("GET /v1/models", r["error"])
            print("\n  Endpoint injoignable : vérifier --url, le DNS/Route et que "
                  "le modèle est bien déployé.")
            return report.EXIT_INCONCLUSIVE

        if r["status"] in (401, 403):
            reason = r["headers"].get("x-ext-auth-reason", "") or r["body"][:120]
            ck.fail(f"authentification refusée (HTTP {r['status']})", reason.strip())
            print("\n  " + (
                "L'endpoint exige un jeton : relancer avec --token / --token-file."
                if not token else
                "Le jeton fourni est refusé : expiré, mauvaise audience, ou droits "
                "insuffisants côté AuthPolicy."))
            return report.EXIT_FAIL

        if not ck.expect(r["status"] == 200, "GET /v1/models",
                         f"HTTP {r['status']} en {1000*r['latency']:.0f} ms"):
            return ck.conclude()

        models = s.list_models()
        out["models"] = models
        ck.expect(args.model in models, f"modèle « {args.model} » exposé",
                  f"modèles : {models}")

        # 2a. Prompt unique fourni : on ne déroule pas le bench.
        if args.prompt:
            rc = s.chat(args.prompt, max_tokens=args.max_tokens)
            out["result"] = rc.as_row()
            ck.expect(rc.ok and rc.completion_tokens > 0, "complétion",
                      f"{rc.latency:.2f}s, {rc.completion_tokens} tok, "
                      f"TTFT={1000*(rc.ttft or 0):.0f} ms"
                      + (f" — {rc.error}" if rc.error else ""))
            if rc.ok:
                if rc.pod:
                    print(f"\n  pod servant : {rc.pod}")
                print(f"\n  Réponse : {rc.text.strip()[:400]}"
                      + ("…" if len(rc.text) > 400 else ""))
        # 2b. Mini-bench trois niveaux.
        else:
            rows = []
            for level, spec in LEVELS.items():
                if args.level not in ("tous", level):
                    continue
                print(f"\n  -- niveau {level} "
                      f"(max_tokens={spec['max_tokens']}, ×{args.repeat}) --")
                rows.append(run_level(s, level, spec, args.repeat, ck, args.quiet))
            out["bench"] = rows
            print_table(rows)
    finally:
        s.close()

    if args.json:
        out["checks"] = ck.summary()
        report.write_json(args.json, out)
    return ck.conclude()


if __name__ == "__main__":
    sys.exit(main())
