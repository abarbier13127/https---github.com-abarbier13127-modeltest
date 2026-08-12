#!/usr/bin/env python3
"""
t03 — Répartition de la charge entre pods.

Question : « mes N replicas travaillent-ils vraiment tous, et à parts égales ? »

Le workload par défaut est `unique` : chaque prompt a un préfixe distinct, donc
le routeur n'a **aucune raison légitime** de préférer un pod à un autre. Tout
déséquilibre observé est alors imputable au routage lui-même, pas au workload.
(L'affinité de préfixe, elle, est *souhaitable* et se teste dans t04 — ne pas
confondre les deux : un routage prefix-aware sur un workload `shared`
déséquilibre volontairement, et c'est correct.)

Deux régimes sont mesurés, parce qu'ils ne disent pas la même chose :

  A. **Plusieurs connexions** (défaut) — le cas d'un vrai parc de clients.
     C'est ici qu'on exige l'uniformité.

  B. **Une seule connexion keep-alive**, requêtes séquentielles — révèle la
     granularité de l'équilibrage. Si toutes les requêtes d'une même connexion
     atterrissent sur le même pod, l'équilibrage est *par connexion* et non par
     requête : un client qui garde sa connexion ouverte reste collé à son pod.
     Ce n'est pas un bug en soi, mais c'est une propriété opérationnelle
     déterminante (un client unique et bavard ne bénéficiera pas des replicas).

Le verdict s'appuie sur un test du χ² d'uniformité et non sur un simple
max/min : avec peu de requêtes, un écart de 20 % entre pods est du bruit
d'échantillonnage, et faire échouer un test là-dessus le rendrait inutilisable.

Usage :
    python3 t03_balance.py -n alf-test
    python3 t03_balance.py --url http://127.0.0.1:8000 --no-cluster --requests 300
    python3 t03_balance.py --expect-pods 4 --max-imbalance 1.3
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llmdbench import (attrib, cluster, config, httpclient, loadgen,  # noqa: E402
                       metrics, report, workload)


# --- phase A : plusieurs connexions ------------------------------------------
def phase_multi(args, wl, make_session, ck, pods, out):
    n_clients = args.clients or max(8, 4 * max(len(pods), 2))
    print(f"-- phase A : {n_clients} connexions concurrentes, "
          f"{args.requests} requêtes --")
    print(f"  workload : {wl.describe()}")

    before = cluster.snapshot(args.namespace, pods) if pods else None
    res, wall = loadgen.run_closed(
        make_session, wl, concurrency=n_clients, duration=0,
        requests=args.requests, max_tokens=args.max_tokens, stream=True,
        warmup=args.warmup, progress=not args.quiet)
    after = cluster.snapshot(args.namespace, pods) if pods else None

    s = metrics.summarize(res, wall)
    metrics.print_summary(s, prefix="  ")
    out["phase_a"] = {"summary": s, "clients": n_clients}

    if not ck.expect(s["requests_ok"] > 0, "requêtes abouties",
                     f"{s['requests_ok']} OK sur {len(res)}"):
        return res, None

    # Un HTTP 200 sans aucun token est un succès pour le transport et un échec
    # réel : sans ce contrôle, un modèle qui répond du vide passerait en vert.
    if s["empty_rate"] > args.max_empty_rate:
        ck.fail("réponses non vides",
                f"{s['requests_empty']}/{s['requests_ok']} réponse(s) sans "
                f"aucun token ({100*s['empty_rate']:.1f} %) malgré un HTTP 200")
    else:
        ct = s.get("completion_tokens") or {}
        ck.ok("réponses non vides",
              f"{s['requests_ok'] - s['requests_empty']}/{s['requests_ok']} "
              "réponses avec du contenu"
              + (f", P50 {ct['p50']:.0f} tokens" if ct.get("n") else ""))

    counts, source = attrib.choose(res, before, after)
    print(f"\n  source d'attribution : {source}")
    if not counts:
        ck.skip("répartition entre pods",
                "ni en-tête de routage, ni compteurs vLLM lisibles — "
                "impossible de savoir qui a servi quoi")
        out["phase_a"]["attribution"] = None
        return res, None

    bal = metrics.balance_stats(counts)
    metrics.print_balance(bal)
    out["phase_a"]["attribution"] = {"source": source, "balance": bal}

    # -- contrôles --
    n_pods = bal["pods"]
    expected = args.expect_pods or (len(pods) if pods else None)
    if expected:
        ck.expect(n_pods >= expected,
                  f"les {expected} pod(s) attendus reçoivent du trafic",
                  f"{n_pods} pod(s) vus dans l'attribution")

    if n_pods < 2:
        ck.skip("uniformité de la répartition",
                f"un seul pod servant ({n_pods}) : rien à équilibrer")
    else:
        starved = [p for p, c in bal["counts"].items() if c == 0]
        ck.expect(not starved, "aucun pod laissé sans trafic",
                  f"pods à 0 requête : {', '.join(starved)}" if starved
                  else f"{n_pods} pods actifs")

        # χ² : le déséquilibre est-il significatif, ou du bruit ?
        p = bal["p_value"]
        ck.expect(p >= args.alpha,
                  f"répartition compatible avec l'uniforme (χ², p ≥ {args.alpha})",
                  f"p = {p:.4f}, χ²({n_pods-1}) = {bal['chi2']:.1f}, "
                  f"écart max {bal['max_dev_pct']:+.0f} % — "
                  + ("déséquilibre statistiquement significatif"
                     if p < args.alpha else "compatible avec du bruit"))

        # Garde-fou lisible, en complément du χ² (qui devient très sévère quand
        # le nombre de requêtes est grand : 5 % d'écart peut être « significatif »
        # sans avoir la moindre conséquence pratique).
        imb = bal["imbalance"]
        ck.expect(imb <= args.max_imbalance,
                  f"déséquilibre max/min ≤ {args.max_imbalance:.2f}",
                  f"mesuré {imb:.2f}")

    # Un pod plus lent que les autres se voit dans les latences par pod, pas
    # dans les compteurs : c'est la signature d'un GPU partagé ou d'un voisin
    # bruyant, invisible pour un test de répartition pur.
    per_pod = _latency_per_pod(res)
    if len(per_pod) >= 2:
        print("\n  latence par pod (attribution par en-tête) :")
        for pod, d in sorted(per_pod.items()):
            print(f"    {pod:<28} n={d['n']:<5} P50={d['p50']:.3f}s  P95={d['p95']:.3f}s")
        p50s = {k: v["p50"] for k, v in per_pod.items()}
        slow, fast = max(p50s.values()), min(p50s.values())
        ratio = slow / fast if fast > 0 else float("nan")
        out["phase_a"]["latency_per_pod"] = per_pod
        if ratio > args.max_latency_spread:
            ck.warn("un pod est nettement plus lent que les autres",
                    f"P50 la plus lente ×{ratio:.2f} la plus rapide "
                    f"({slow:.3f}s vs {fast:.3f}s) — GPU partagé, time-slicing "
                    "inégal, ou pod colocalisé avec une autre charge")
        else:
            ck.ok("latences homogènes entre pods",
                  f"écart P50 max ×{ratio:.2f}")
    return res, counts


def _latency_per_pod(results) -> dict:
    """{pod: distribution de latence} — nécessite l'attribution par en-tête."""
    by = {}
    for r in results:
        if r.ok and r.pod:
            by.setdefault(r.pod, []).append(r.latency)
    return {p: metrics.dist(v) for p, v in by.items() if len(v) >= 3}


# --- phase B : une seule connexion -------------------------------------------
def phase_single(args, wl, make_session, ck, pods, out):
    """
    Séquentiel sur UNE connexion : mesure la granularité de l'équilibrage.

    Informational par nature — les deux comportements existent et sont
    défendables. On le signale, on ne le sanctionne pas.
    """
    n = max(12, args.single_requests)
    print(f"\n-- phase B : 1 seule connexion keep-alive, {n} requêtes séquentielles --")
    before = cluster.snapshot(args.namespace, pods) if pods else None
    s = make_session()
    res = []
    try:
        for i in range(n):
            prompt, grp = wl.next(10_000 + i)
            res.append(s.chat(prompt, max_tokens=args.max_tokens, group=grp,
                              stream=True))
    finally:
        s.close()
    after = cluster.snapshot(args.namespace, pods) if pods else None

    counts, source = attrib.choose(res, before, after)
    ok = sum(1 for r in res if r.ok)
    print(f"  {ok}/{n} requêtes OK — attribution : {source}")
    if not counts:
        ck.skip("granularité de l'équilibrage", "attribution indisponible")
        return
    bal = metrics.balance_stats(counts)
    metrics.print_balance(bal)
    out["phase_b"] = {"attribution": {"source": source, "balance": bal}}

    if bal["pods"] <= 1 and (out.get("phase_a", {}).get("attribution") or {}) \
            .get("balance", {}).get("pods", 0) > 1:
        pod = next(iter(bal["counts"]))
        ck.warn("équilibrage par connexion, pas par requête",
                f"les {ok} requêtes d'une même connexion keep-alive sont toutes "
                f"allées sur {pod} — un client unique et persistant n'exploitera "
                "qu'un seul replica ; prévoir plusieurs connexions côté "
                "application, ou un équilibrage par requête côté Gateway")
    elif bal["pods"] > 1:
        ck.ok("équilibrage par requête",
              f"une même connexion a été répartie sur {bal['pods']} pods")
    else:
        ck.skip("granularité de l'équilibrage",
                "un seul pod servant : les deux régimes sont indistinguables")


# --- main --------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Répartition de la charge entre pods")
    config.add_common_args(ap)
    workload.add_workload_args(ap)
    ap.set_defaults(workload="unique")  # pas d'affinité légitime : cf. docstring
    g = ap.add_argument_group("répartition")
    g.add_argument("--requests", type=int, default=200,
                   help="nombre de requêtes en phase A (défaut 200)")
    g.add_argument("--clients", type=int, default=0,
                   help="connexions concurrentes en phase A (0 = 4× le nb de pods)")
    g.add_argument("--single-requests", type=int, default=24,
                   help="requêtes de la phase B, sur une seule connexion")
    g.add_argument("--no-single", action="store_true",
                   help="ignorer la phase B (une seule connexion)")
    g.add_argument("--warmup", type=int, default=2,
                   help="requêtes de chauffe non comptées, par phase")
    s = ap.add_argument_group("seuils")
    s.add_argument("--expect-pods", type=int, default=0,
                   help="nombre de pods qui doivent recevoir du trafic "
                        "(0 = tous les pods Ready détectés)")
    s.add_argument("--alpha", type=float, default=0.01,
                   help="seuil de p-value du χ² (défaut 0.01)")
    s.add_argument("--max-imbalance", type=float, default=1.5,
                   help="rapport max/min toléré entre pods (défaut 1.5)")
    s.add_argument("--max-empty-rate", type=float, default=0.01,
                   help="part de réponses HTTP 200 sans aucun token tolérée "
                        "(défaut 0.01)")
    s.add_argument("--max-latency-spread", type=float, default=1.5,
                   help="écart de latence P50 toléré entre pods (défaut ×1.5)")
    args = ap.parse_args()

    auth_source = config.resolve_auth(args)
    config.banner("t03 — Répartition de la charge", args, {
        "auth": auth_source or "aucune (appel anonyme)",
        "requêtes": args.requests,
        "workload": args.workload,
    })
    ck = report.Checks("t03 répartition")
    out = {"args": vars(args)}
    wl = workload.from_args(args)
    out["workload"] = {"kind": wl.kind, "describe": wl.describe(),
                       "fingerprint": wl.fingerprint()}

    probe = httpclient.session_from_args(args)
    try:
        alive, status = probe.health()
        window = probe.context_window() if alive else None
    finally:
        probe.close()
    if not ck.expect(alive, "endpoint exploitable",
                     f"HTTP {status} — {httpclient.explain_auth(status, args.api_key, getattr(args, 'auth_error', None))}"):
        return report.EXIT_INCONCLUSIVE

    out["context_window"] = window
    if not config.check_context_window(wl, args.max_tokens, window, ck):
        return ck.conclude(inconclusive=True)

    pods = []
    if not args.no_cluster:
        try:
            pods = [p for p in cluster.list_pods(args.namespace, args.selector)
                    if p.ready]
            cluster.print_pods(pods)
        except cluster.ClusterError as e:
            print(f"  (introspection cluster indisponible : {e})")
    if len(pods) == 1:
        print("\n  Un seul pod de serving : ce test ne peut rien conclure sur la "
              "répartition.\n  Il vérifiera seulement que le trafic aboutit.")
    print()

    make_session = lambda: httpclient.session_from_args(args)  # noqa: E731

    res, _counts = phase_multi(args, wl, make_session, ck, pods, out)
    if not args.no_single:
        phase_single(args, wl, make_session, ck, pods, out)

    if args.json:
        out["checks"] = ck.summary()
        report.write_json(args.json, out)
    if args.csv and res:
        report.write_csv(args.csv, res)
    return ck.conclude()


if __name__ == "__main__":
    sys.exit(main())
