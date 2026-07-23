#!/usr/bin/env python3
"""
Attribution requête → pod : « qui a réellement servi quoi ? »

Deux sources, par ordre de préférence :

1. **Compteurs vLLM** (`vllm:request_success_total`) lus avant/après la rafale
   sur chaque pod via l'API server. Source de vérité : elle voit ce que le pod
   a traité, pas ce que le routeur prétend. Marche avec n'importe quel chemin
   d'accès (Route, Gateway, service interne). Coût : un appel `oc` par pod et
   par borne.

2. **En-tête de réponse** (`x-gateway-destination-endpoint` et variantes), quand
   le Gateway llm-d / Envoy l'expose. Gratuit et par requête — c'est la seule
   source qui permet de corréler *une* requête à *un* pod, donc la seule
   utilisable pour l'affinité de préfixe fine.

Les deux se recoupent : si les totaux divergent nettement, c'est en soi un
signal (trafic parasite, retries côté routeur, ou pod hors du selector).
"""
from . import cluster


def header_counts(results) -> dict:
    counts = {}
    for r in results:
        if r.ok and r.pod:
            counts[r.pod] = counts.get(r.pod, 0) + 1
    return counts


def header_counts_by_group(results) -> dict:
    """{groupe: {pod: n}} — pour l'analyse d'affinité par préfixe."""
    out = {}
    for r in results:
        if r.ok and r.pod:
            out.setdefault(r.group or "-", {})
            out[r.group or "-"][r.pod] = out[r.group or "-"].get(r.pod, 0) + 1
    return out


def metric_counts(before: dict, after: dict) -> dict:
    d = cluster.delta(before, after, "requests_ok")
    return {k: int(round(v)) for k, v in d.items() if v >= 0}


def choose(results, before=None, after=None) -> tuple[dict, str]:
    """
    Renvoie (counts, source). Les compteurs pods priment s'ils sont exploitables.
    """
    hc = header_counts(results)
    mc = metric_counts(before, after) if (before and after) else {}
    n_ok = sum(1 for r in results if r.ok)
    if mc and sum(mc.values()) > 0:
        total = sum(mc.values())
        # Tolérance : le pod peut compter des sondes/health en plus.
        if n_ok and abs(total - n_ok) / n_ok <= 0.25:
            return mc, "compteurs vLLM par pod"
        if hc:
            return hc, (f"en-têtes Gateway (compteurs pods écartés : "
                        f"{total} vs {n_ok} requêtes)")
        return mc, f"compteurs vLLM par pod (écart au client : {total} vs {n_ok})"
    if hc:
        return hc, "en-têtes Gateway"
    return {}, "aucune (ni en-tête de routage, ni métriques pod)"


def affinity_score(by_group: dict) -> dict:
    """
    Mesure l'affinité : pour chaque groupe de prompts, quelle fraction des
    requêtes a atterri sur le pod majoritaire de ce groupe ?

    - `mean_affinity` ≈ 1.0  → routage affine (chaque préfixe a « son » pod)
    - `mean_affinity` ≈ 1/N  → routage indifférent au préfixe (N pods)
    - `distinct_owners`      → nb de pods majoritaires distincts : si tous les
      groupes convergent vers le même pod, il n'y a pas affinité mais
      déséquilibre.
    """
    per, owners = {}, {}
    for grp, counts in by_group.items():
        tot = sum(counts.values())
        if not tot:
            continue
        pod, n = max(counts.items(), key=lambda kv: kv[1])
        per[grp] = {"owner": pod, "share": n / tot, "total": tot,
                    "pods_touched": len(counts)}
        owners[pod] = owners.get(pod, 0) + 1
    mean = sum(v["share"] for v in per.values()) / len(per) if per else float("nan")
    return {"per_group": per, "mean_affinity": mean,
            "distinct_owners": len(owners), "groups": len(per)}
