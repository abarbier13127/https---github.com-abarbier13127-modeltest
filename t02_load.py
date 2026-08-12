#!/usr/bin/env python3
"""
t02 — Capacité et point de rupture.

Répond à deux questions distinctes, avec deux moteurs différents :

1. **Jusqu'où ça monte ?** Balayage en boucle fermée (concurrence croissante).
   On cherche le *genou* : le niveau au-delà duquel le débit cesse de croître
   alors que la latence, elle, continue de monter. Au-delà du genou, ajouter des
   clients n'ajoute plus de travail utile, ça ne fait qu'allonger les files.

2. **Le débit cible est-il soutenable ?** Boucle ouverte à λ imposé (`--rate`).
   C'est le seul mode honnête pour valider un SLO : en boucle fermée, un serveur
   qui ralentit reçoit spontanément moins de trafic et ne peut donc jamais
   montrer un effondrement. En boucle ouverte, les arrivées ne ralentissent pas,
   la file grossit, et `sched_delay` mesure le retard accumulé — un retard qui
   croît est la signature d'un débit non soutenable, même si les latences
   individuelles restent belles.

Le verdict ne porte que sur ce qui est vérifiable sans connaître le matériel :
taux d'erreur, existence d'un genou, tenue du débit cible, et SLO explicitement
demandés par l'utilisateur (`--slo-*`). Le débit absolu n'est jamais jugé : sans
référence, « 12 req/s » n'est ni bon ni mauvais.

Usage :
    python3 t02_load.py --levels 1,2,4,8 --duration 20
    python3 t02_load.py --rate 8 --duration 60 --slo-ttft 1.0 --slo-p95 8.0
    python3 t02_load.py --url http://127.0.0.1:8000 --no-cluster --quick
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llmdbench import (attrib, cluster, config, httpclient, loadgen,  # noqa: E402
                       metrics, report, workload)


# --- collecte ----------------------------------------------------------------
def _pods_ready(args):
    """Pods de serving Ready, ou [] si l'introspection est coupée/indisponible."""
    if args.no_cluster:
        return []
    try:
        return [p for p in cluster.list_pods(args.namespace, args.selector) if p.ready]
    except cluster.ClusterError:
        return []


class _SaturationSampler(threading.Thread):
    """
    Échantillonne les jauges vLLM **pendant** la rafale, et retient les maxima.

    `num_requests_running`, `num_requests_waiting` et `kv_cache_usage_perc` sont
    des jauges : elles décrivent l'instant de la lecture. Les relever après la
    rafale ne montre qu'un pod au repos — c'est le défaut qu'avait la première
    version, qui rapportait systématiquement `waiting=0` alors que la file avait
    bel et bien existé. Seul le maximum observé pendant la charge dit quelque
    chose.
    """

    def __init__(self, namespace, pods, period=3.0):
        super().__init__(daemon=True)
        self.namespace, self.pods, self.period = namespace, pods, period
        # NE PAS nommer cet attribut `_stop` : `threading.Thread._stop()` est une
        # méthode interne, l'écraser casse `join()` par un « 'Event' object is
        # not callable » au moment de la terminaison.
        self._stop_evt = threading.Event()
        self.peak = {}
        self.samples = 0

    def run(self):
        while not self._stop_evt.is_set():
            snap = cluster.snapshot(self.namespace, self.pods)
            self.samples += 1
            for pod, m in snap.items():
                if pod == "_t" or not m:
                    continue
                cur = self.peak.setdefault(pod, {})
                for k in ("running", "waiting", "kv_usage"):
                    if k in m:
                        cur[k] = max(cur.get(k, 0.0), m[k])
            self._stop_evt.wait(self.period)

    def stop(self):
        self._stop_evt.set()
        self.join(timeout=10)


def _run_level(args, wl, make_session, level, start_index, pods):
    """Un palier de concurrence, encadré par deux photos des compteurs pods."""
    before = cluster.snapshot(args.namespace, pods) if pods else None
    sampler = _SaturationSampler(args.namespace, pods) if pods else None
    if sampler:
        sampler.start()
    res, wall = loadgen.run_closed(
        make_session, wl, concurrency=level, duration=args.duration,
        max_tokens=args.max_tokens, stream=True, warmup=args.warmup,
        progress=not args.quiet, start_index=start_index)
    if sampler:
        sampler.stop()
    after = cluster.snapshot(args.namespace, pods) if pods else None

    s = metrics.summarize(res, wall)
    s["concurrency"] = level
    # Débit par client : c'est lui qui s'effondre au passage du genou.
    s["rps_per_client"] = s["throughput_rps"] / level if level else 0.0
    if before and after:
        counts, src = attrib.choose(res, before, after)
        s["pod_counts"], s["pod_source"] = counts, src
        s["saturation"] = _saturation(before, after, sampler)
    return s, res


def latency_drift(results, fraction=0.3):
    """
    Dérive de la latence entre le début et la fin du run.

    C'est LE signal d'un débit non soutenable en boucle ouverte : si le serveur
    n'absorbe pas λ, le travail en cours s'accumule et chaque requête attend un
    peu plus que la précédente. La latence moyenne peut rester présentable, mais
    sa *tendance* monte — un régime stationnaire, lui, a une tendance plate.

    On compare la médiane du premier tiers à celle du dernier tiers, en ordonnant
    par heure de départ (et non par ordre d'arrivée des réponses, qui est déjà
    biaisé par la latence elle-même).

    Retourne {early, late, ratio} en secondes, ou {} si trop peu de points.
    """
    ok = sorted((r for r in results if r.ok), key=lambda r: r.t_start)
    n = int(len(ok) * fraction)
    if n < 5:
        return {}
    early = metrics.pct(sorted(r.latency for r in ok[:n]), 50)
    late = metrics.pct(sorted(r.latency for r in ok[-n:]), 50)
    return {"early": early, "late": late,
            "ratio": (late / early) if early > 0 else float("nan"),
            "n_per_side": n}


def _saturation(before, after, sampler):
    """
    Indices de saturation côté serveur, par pod.

    Deux natures de métriques, deux traitements :

    - **jauges** (`running`, `waiting`, `kv_usage`) → maximum observé *pendant* la
      rafale par `_SaturationSampler`. Un `waiting_peak > 0` signifie que des
      requêtes ont attendu un slot de batch : le GPU est le goulot. Un
      `kv_usage_peak` proche de 1 annonce les préemptions.
    - **compteur** (`preemptions`) → différence entre les deux photos, ce qui
      donne le nombre de préemptions *imputables à ce palier*.
    """
    out = {}
    peaks = sampler.peak if sampler else {}
    for pod, m in after.items():
        if pod == "_t" or not m:
            continue
        d = {}
        for k, v in (peaks.get(pod) or {}).items():
            d[f"{k}_peak"] = v
        b = (before.get(pod) or {}).get("preemptions")
        a = m.get("preemptions")
        if a is not None and b is not None:
            d["preemptions_delta"] = a - b
        out[pod] = d
    if sampler:
        out["_samples"] = sampler.samples
    return out


# --- analyse -----------------------------------------------------------------
def degradation(levels_summary):
    """
    Dégradation des latences entre le premier et le dernier palier.

    Le point de comparaison n'est pas un seuil absolu mais **l'augmentation de la
    concurrence elle-même**. Si la concurrence est multipliée par 8 et le TPOT
    par 5,5, la dégradation est *sous-linéaire* : c'est le comportement normal du
    batching continu, chaque requête ralentit moins vite que la charge ne monte,
    et le débit agrégé progresse quand même.

    En revanche, un TPOT qui se dégrade **plus vite** que la concurrence ne croît
    signifie que le débit agrégé de tokens a *baissé* : ajouter des clients
    détruit du travail utile. C'est la signature d'un emballement (préemptions en
    cascade, pression sur le KV-cache), et non d'une simple mise en file.

    Retourne {} si moins de deux paliers exploitables.
    """
    usable = [s for s in levels_summary
              if s["tpot"].get("n") and s["ttft"].get("n")]
    if len(usable) < 2:
        return {}
    first, last = usable[0], usable[-1]
    conc_ratio = last["concurrency"] / first["concurrency"]

    def ratio(key):
        a, b = first[key]["p50"], last[key]["p50"]
        return (b / a) if a > 0 else float("nan")

    return {
        "from_concurrency": first["concurrency"],
        "to_concurrency": last["concurrency"],
        "concurrency_ratio": conc_ratio,
        "tpot_ratio": ratio("tpot"),
        "ttft_ratio": ratio("ttft"),
        "latency_ratio": ratio("latency"),
        "tpot_from": first["tpot"]["p50"], "tpot_to": last["tpot"]["p50"],
        "ttft_from": first["ttft"]["p50"], "ttft_to": last["ttft"]["p50"],
        "n_first": first["requests_ok"], "n_last": last["requests_ok"],
        # Au-delà de ce point, le débit agrégé de tokens régresse.
        "superlinear": ratio("tpot") > conc_ratio,
    }


def check_degradation(d, ck, max_degradation, min_samples=20):
    """
    Verdicts sur la dégradation. Sans ce contrôle, un modèle devenu 17× plus lent
    passe en vert : le taux d'erreur reste à 0 et le débit continue de croître.
    """
    if not d:
        ck.skip("dégradation des latences", "moins de deux paliers exploitables")
        return
    print(f"\n  dégradation entre concurrence {d['from_concurrency']} et "
          f"{d['to_concurrency']} (×{d['concurrency_ratio']:.0f} de charge) :")
    print(f"    TTFT P50   {1000*d['ttft_from']:.0f} ms → "
          f"{1000*d['ttft_to']:.0f} ms   (×{d['ttft_ratio']:.1f})")
    print(f"    TPOT P50   {1000*d['tpot_from']:.1f} ms → "
          f"{1000*d['tpot_to']:.1f} ms   (×{d['tpot_ratio']:.1f})")

    enough = min(d["n_first"], d["n_last"]) >= min_samples
    if d["superlinear"] and enough:
        ck.fail("dégradation proportionnée à la charge",
                f"TPOT ×{d['tpot_ratio']:.1f} pour une charge ×"
                f"{d['concurrency_ratio']:.0f} : la dégradation est "
                "SUR-linéaire, donc le débit agrégé de tokens a baissé. "
                "Ajouter des clients détruit du travail utile — chercher des "
                "préemptions et la pression sur le KV-cache dans `saturation`")
    elif d["superlinear"]:
        ck.warn("dégradation proportionnée à la charge",
                f"TPOT ×{d['tpot_ratio']:.1f} pour une charge ×"
                f"{d['concurrency_ratio']:.0f} (sur-linéaire), mais seulement "
                f"{min(d['n_first'], d['n_last'])} requêtes sur le palier le "
                f"plus maigre : trop peu pour conclure, refaire avec "
                "--duration plus long")
    else:
        ck.ok("dégradation proportionnée à la charge",
              f"TPOT ×{d['tpot_ratio']:.1f} pour une charge ×"
              f"{d['concurrency_ratio']:.0f} : sous-linéaire, le batching "
              "absorbe la montée")

    # Seuil de confort, indépendant du caractère (sous-)linéaire : même
    # proportionnée, une dégradation de cette ampleur rend le service pénible.
    worst = max(d["tpot_ratio"], d["ttft_ratio"])
    if worst > max_degradation:
        ck.warn("amplitude de la dégradation",
                f"la pire latence est multipliée par {worst:.1f} "
                f"(seuil --max-degradation {max_degradation:.1f}) : "
                f"TTFT ×{d['ttft_ratio']:.1f}, TPOT ×{d['tpot_ratio']:.1f}. "
                "Le service répond toujours, mais beaucoup plus lentement — "
                "c'est ce que le taux d'erreur ne montre pas")


def check_empty(s, ck, max_empty_rate, label=""):
    """Réponses HTTP 200 sans aucun token : succès pour le transport, échec réel."""
    n_ok = s["requests_ok"]
    if not n_ok:
        return
    suffix = f" ({label})" if label else ""
    if s["empty_rate"] > max_empty_rate:
        ck.fail(f"réponses non vides{suffix}",
                f"{s['requests_empty']}/{n_ok} réponse(s) sans aucun token "
                f"({100*s['empty_rate']:.1f} %) alors que le serveur a répondu "
                "HTTP 200 : le modèle accepte les requêtes mais ne génère rien")
    else:
        ct = s.get("completion_tokens") or {}
        ck.ok(f"réponses non vides{suffix}",
              f"{n_ok - s['requests_empty']}/{n_ok} réponses avec du contenu"
              + (f", P50 {ct['p50']:.0f} tokens" if ct.get("n") else ""))


def find_knee(levels_summary, tolerance=0.7, max_error_rate=0.02):
    """
    Localise le genou de saturation.

    Critère : on compare le débit par client de chaque palier à celui du palier
    de référence (le plus bas). Tant que le serveur absorbe la charge, ce ratio
    reste proche de 1 ; il chute dès que les requêtes commencent à s'attendre
    les unes les autres. Le genou est le dernier palier au-dessus du seuil.

    Un palier qui dépasse `max_error_rate` ne peut pas être le genou, même si son
    débit par client tient : un débit obtenu en jetant des requêtes n'est pas une
    capacité. Ce seuil doit rester **le même** que celui de l'arrêt anticipé du
    balayage (`--max-error-rate`), sinon un palier pourrait être toléré par l'un
    et refusé par l'autre.

    Retourne (palier_genou, dict des efficacités par palier).
    """
    if not levels_summary:
        return None, {}
    base = levels_summary[0]
    ref = base["rps_per_client"] or float("nan")
    eff = {s["concurrency"]: (s["rps_per_client"] / ref if ref else float("nan"))
           for s in levels_summary}
    knee = base["concurrency"]
    for s in levels_summary:
        if eff[s["concurrency"]] >= tolerance and s["error_rate"] <= max_error_rate:
            knee = s["concurrency"]
    return knee, eff


def print_table(levels_summary, eff):
    rows = []
    for s in levels_summary:
        t, l = s["ttft"], s["latency"]
        rows.append((
            s["concurrency"],
            f"{s['throughput_rps']:.2f}",
            f"{s['output_tok_per_s']:.0f}",
            f"{1000*t['p50']:.0f}" if t.get("n") else "-",
            f"{1000*t['p95']:.0f}" if t.get("n") else "-",
            f"{l['p50']:.2f}" if l.get("n") else "-",
            f"{l['p95']:.2f}" if l.get("n") else "-",
            f"{100*s['error_rate']:.1f}",
            f"{eff.get(s['concurrency'], float('nan')):.2f}",
        ))
    hdr = ("conc", "req/s", "tok/s", "TTFT P50", "TTFT P95",
           "lat P50", "lat P95", "err %", "effic.")
    w = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(hdr)]
    print("  " + "  ".join(f"{h:>{w[i]}}" for i, h in enumerate(hdr)))
    print("  " + "  ".join("-" * w[i] for i in range(len(hdr))))
    for r in rows:
        print("  " + "  ".join(f"{str(c):>{w[i]}}" for i, c in enumerate(r)))
    print("\n  (effic. = débit par client rapporté au palier le plus bas ; "
          "il chute au passage du genou)")


def write_levels_csv(path, per_level_results):
    """
    Une ligne par requête, avec sa concurrence — pour rejouer l'analyse ailleurs.

    Écrit ici plutôt que via `report.write_csv` parce qu'il faut injecter la
    colonne `concurrency`, qui n'appartient pas au `Result` d'une requête.
    """
    path = os.path.expanduser(path)
    rows = [dict(concurrency=lvl, **r.as_row())
            for lvl, res in per_level_results for r in res]
    if not rows:
        return path
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"  → CSV écrit  : {path} ({len(rows)} lignes)")
    return path


# --- phases ------------------------------------------------------------------
def phase_sweep(args, wl, make_session, ck, pods, out):
    """Balayage en boucle fermée : capacité et genou."""
    print("-- boucle fermée : balayage de concurrence --")
    print(f"  workload : {wl.describe()}  (empreinte {wl.fingerprint()})")
    summaries, per_level, idx = [], [], 0
    for level in args.levels:
        print(f"\n  == concurrence {level} ==")
        s, res = _run_level(args, wl, make_session, level, idx, pods)
        idx += len(res) + args.warmup
        metrics.print_summary(s, prefix="    ")
        if s.get("pod_counts"):
            print(f"    répartition ({s['pod_source']}) : "
                  + ", ".join(f"{k}={v}" for k, v in sorted(s["pod_counts"].items())))
        summaries.append(s)
        per_level.append((level, res))
        if s["error_rate"] > args.max_error_rate:
            print(f"    → taux d'erreur {100*s['error_rate']:.1f} % au-dessus du "
                  f"seuil : arrêt du balayage (inutile de monter plus haut)")
            break

    knee, eff = find_knee(summaries, args.knee_tolerance, args.max_error_rate)
    print()
    print_table(summaries, eff)
    out["levels"] = summaries
    out["knee"] = knee
    out["efficiency"] = eff

    best = max(summaries, key=lambda s: s["throughput_rps"])
    print(f"\n  débit maximal observé : {best['throughput_rps']:.2f} req/s "
          f"({best['output_tok_per_s']:.0f} tok/s) à concurrence {best['concurrency']}")
    print(f"  genou de saturation   : concurrence {knee}")

    # -- contrôles --
    worst = max(summaries, key=lambda s: s["error_rate"])
    ck.expect(worst["error_rate"] <= args.max_error_rate,
              f"taux d'erreur ≤ {100*args.max_error_rate:.1f} % à tous les paliers",
              f"pire palier : concurrence {worst['concurrency']} → "
              f"{100*worst['error_rate']:.1f} %"
              + (f" ({worst['errors'][0]['error']})" if worst["errors"] else ""))

    if len(summaries) < 2:
        ck.skip("détection du genou", "un seul palier exécuté")
    elif best["concurrency"] == summaries[0]["concurrency"]:
        # Le débit n'augmente pas du tout. Trois causes possibles, dont une qui
        # n'est pas un défaut du déploiement : un prompt si volumineux qu'une
        # seule requête sature déjà le GPU. Le signaler évite d'envoyer chercher
        # un goulot réseau qui n'existe pas.
        est = wl.estimated_prompt_tokens()
        cause = ("vérifier un éventuel goulot en amont (Route/Gateway) ou un "
                 "batching désactivé côté vLLM")
        if est > 4000:
            cause = (f"le prompt fait ~{est} tokens : une seule requête suffit "
                     "probablement à saturer le GPU, la concurrence ne peut "
                     "alors rien apporter. Refaire la mesure avec un prompt "
                     "plus court avant de suspecter la plateforme")
        ck.fail("le débit croît avec la concurrence",
                f"débit maximal atteint dès le palier le plus bas "
                f"({summaries[0]['concurrency']}) : concurrence non exploitée — "
                + cause)
    else:
        gain = best["throughput_rps"] / (summaries[0]["throughput_rps"] or float("nan"))
        ck.ok("le débit croît avec la concurrence",
              f"×{gain:.1f} entre concurrence {summaries[0]['concurrency']} "
              f"et {best['concurrency']}")

    # Agrégat des réponses vides sur tout le balayage : un palier isolé peut
    # n'en avoir aucune alors que le problème apparaît sous charge.
    agg = {
        "requests_ok": sum(s["requests_ok"] for s in summaries),
        "requests_empty": sum(s.get("requests_empty", 0) for s in summaries),
        "completion_tokens": metrics.dist(
            [s["completion_tokens"]["p50"] for s in summaries
             if s.get("completion_tokens", {}).get("n")]),
    }
    agg["empty_rate"] = agg["requests_empty"] / max(agg["requests_ok"], 1)
    out["empty"] = agg
    check_empty(agg, ck, args.max_empty_rate, "tous paliers")

    d = degradation(summaries)
    out["degradation"] = d
    check_degradation(d, ck, args.max_degradation)

    if knee == args.levels[-1] and len(summaries) == len(args.levels):
        ck.warn("genou non atteint",
                f"le débit par client tient encore à concurrence {knee} : la "
                "capacité réelle est au-delà, relancer avec des paliers plus hauts")
    return summaries, per_level


def phase_open(args, wl, make_session, ck, pods, out):
    """Boucle ouverte au débit cible : le seul mode qui valide un SLO."""
    print(f"\n-- boucle ouverte : {args.rate} req/s pendant {args.duration:.0f} s --")
    before = cluster.snapshot(args.namespace, pods) if pods else None
    res, wall = loadgen.run_open(
        make_session, wl, rate=args.rate, duration=args.duration,
        max_tokens=args.max_tokens, stream=True, warmup=args.warmup,
        progress=not args.quiet, seed=args.seed,
        max_inflight=args.max_inflight or None)
    after = cluster.snapshot(args.namespace, pods) if pods else None

    s = metrics.summarize(res, wall)
    ok = [r for r in res if r.ok]
    s["sched_delay"] = metrics.dist(r.sched_delay for r in res)
    s["rate_target"] = args.rate
    s["rate_achieved"] = s["throughput_rps"]
    s["drift"] = latency_drift(res)
    if before and after:
        counts, src = attrib.choose(res, before, after)
        s["pod_counts"], s["pod_source"] = counts, src
    out["open_loop"] = s

    metrics.print_summary(s, prefix="  ")
    d = s["sched_delay"]
    if d.get("n"):
        print(f"  retard d'ordonnancement : P50={d['p50']*1000:.0f} "
              f"P95={d['p95']*1000:.0f} P99={d['p99']*1000:.0f} ms "
              f"(max {d['max']:.2f} s)")
    if ok:
        print(f"  latence dans le temps   : {metrics.sparkline([r.latency for r in ok])}")
    dr = s["drift"]
    if dr:
        print(f"  dérive de la latence    : P50 début {dr['early']:.3f} s → "
              f"fin {dr['late']:.3f} s  (×{dr['ratio']:.2f})")

    # Débit tenu : on tolère l'écart d'échantillonnage d'une fenêtre courte.
    ratio = s["rate_achieved"] / args.rate if args.rate else float("nan")
    ck.expect(ratio >= 0.95, f"débit cible {args.rate} req/s tenu",
              f"réalisé {s['rate_achieved']:.2f} req/s ({100*ratio:.0f} %)")

    # Un débit refusé se voit aussi en erreurs (resets, 429, 503) : sans ce
    # contrôle, un serveur qui jette la moitié du trafic « tient » son débit.
    ck.expect(s["error_rate"] <= args.max_error_rate,
              f"taux d'erreur ≤ {100*args.max_error_rate:.1f} % au débit cible",
              f"{100*s['error_rate']:.1f} % ({s['requests_err']} requêtes)"
              + (f" — {s['errors'][0]['error']}" if s["errors"] else ""))
    check_empty(s, ck, args.max_empty_rate, "boucle ouverte")

    # Régime stationnaire : la latence ne doit pas dériver au fil du run.
    if dr:
        ck.expect(dr["ratio"] <= args.max_drift,
                  f"régime stationnaire (dérive de latence ≤ ×{args.max_drift:.1f})",
                  f"P50 passée de {dr['early']:.3f} s à {dr['late']:.3f} s "
                  f"(×{dr['ratio']:.2f}) sur {dr['n_per_side']} requêtes de "
                  "chaque côté — une dérive signifie que le travail s'accumule "
                  "et que le débit demandé dépasse la capacité")
    else:
        ck.skip("régime stationnaire", "trop peu de requêtes pour juger la tendance")

    # Le retard d'ordonnancement n'est informatif que si le nombre de requêtes
    # simultanées est borné (--max-inflight) : sinon un worker est toujours
    # libre, le retard reste nul, et l'attente se déplace dans la latence.
    if d.get("n") and args.max_inflight:
        ck.expect(d["p99"] <= args.max_sched_delay,
                  f"pas d'attente d'admission (retard P99 ≤ {args.max_sched_delay:.1f} s)",
                  f"retard P99 = {d['p99']:.2f} s avec au plus "
                  f"{args.max_inflight} requêtes simultanées")
    elif d.get("n"):
        ck.skip("attente d'admission",
                "non borné (--max-inflight absent) : le retard reste nul par "
                "construction, la saturation se lit dans la dérive de latence")

    if args.slo_ttft and s["ttft"].get("n"):
        v = s["ttft"]["p95"]
        ck.expect(v <= args.slo_ttft, f"SLO TTFT P95 ≤ {args.slo_ttft:.2f} s",
                  f"mesuré {v:.3f} s")
    if args.slo_p95 and s["latency"].get("n"):
        v = s["latency"]["p95"]
        ck.expect(v <= args.slo_p95, f"SLO latence P95 ≤ {args.slo_p95:.2f} s",
                  f"mesuré {v:.3f} s")
    return res


# --- main --------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Capacité et point de rupture (boucle fermée + boucle ouverte)")
    config.add_common_args(ap)
    workload.add_workload_args(ap)
    g = ap.add_argument_group("charge")
    g.add_argument("--levels", default="1,2,4,8",
                   help="paliers de concurrence, séparés par des virgules")
    g.add_argument("--duration", type=float, default=20.0,
                   help="durée de chaque palier, en secondes")
    g.add_argument("--warmup", type=int, default=2,
                   help="requêtes de chauffe non comptées, par palier")
    g.add_argument("--rate", type=float, default=0.0,
                   help="débit cible en req/s pour la phase boucle ouverte "
                        "(0 = phase ignorée)")
    g.add_argument("--max-inflight", type=int, default=0,
                   help="borne le nombre de requêtes simultanées en boucle "
                        "ouverte (0 = non borné, soit 10×rate)")
    g.add_argument("--no-sweep", action="store_true",
                   help="ignorer le balayage et ne faire que la boucle ouverte")
    g.add_argument("--quick", action="store_true",
                   help="raccourci : paliers 1,4 et 8 s par palier")
    s = ap.add_argument_group("seuils et SLO")
    s.add_argument("--max-error-rate", type=float, default=0.02,
                   help="taux d'erreur toléré par palier (défaut 0.02)")
    s.add_argument("--knee-tolerance", type=float, default=0.7,
                   help="seuil d'efficacité définissant le genou (défaut 0.7)")
    s.add_argument("--max-sched-delay", type=float, default=2.0,
                   help="retard d'admission P99 toléré, s (avec --max-inflight)")
    s.add_argument("--max-drift", type=float, default=1.5,
                   help="dérive de latence tolérée entre début et fin de run "
                        "(défaut ×1.5)")
    s.add_argument("--max-degradation", type=float, default=3.0,
                   help="facteur de dégradation des latences toléré entre le "
                        "premier et le dernier palier (défaut ×3)")
    s.add_argument("--max-empty-rate", type=float, default=0.01,
                   help="part de réponses HTTP 200 sans aucun token tolérée "
                        "(défaut 0.01)")
    s.add_argument("--slo-ttft", type=float, default=0.0,
                   help="SLO sur le TTFT P95, en secondes (0 = non vérifié)")
    s.add_argument("--slo-p95", type=float, default=0.0,
                   help="SLO sur la latence P95, en secondes (0 = non vérifié)")
    args = ap.parse_args()

    if args.quick:
        args.levels, args.duration = "1,4", 8.0
    try:
        args.levels = [int(x) for x in str(args.levels).replace(" ", "").split(",") if x]
    except ValueError:
        print("  --levels attend une liste d'entiers, ex. « 1,2,4,8 »")
        return report.EXIT_INCONCLUSIVE
    if not args.levels:
        print("  --levels vide")
        return report.EXIT_INCONCLUSIVE
    args.levels.sort()
    if args.no_sweep and args.rate <= 0:
        print("  --no-sweep sans --rate : il ne reste rien à exécuter")
        return report.EXIT_INCONCLUSIVE

    auth_source = config.resolve_auth(args)
    config.banner("t02 — Capacité et point de rupture", args, {
        "auth": auth_source or "aucune (appel anonyme)",
        "paliers": ",".join(map(str, args.levels)) if not args.no_sweep else "(ignorés)",
        "durée": f"{args.duration:.0f} s par palier",
        "débit": f"{args.rate} req/s (boucle ouverte)" if args.rate else "(boucle ouverte ignorée)",
    })

    ck = report.Checks("t02 capacité")
    out = {"args": vars(args)}
    wl = workload.from_args(args)
    out["workload"] = {"kind": wl.kind, "describe": wl.describe(),
                       "fingerprint": wl.fingerprint()}

    # Prérequis : sans endpoint joignable, tout le reste est du bruit.
    probe = httpclient.session_from_args(args)
    try:
        alive, status = probe.health()
        window = probe.context_window() if alive else None
    finally:
        probe.close()
    if not ck.expect(alive, "endpoint exploitable",
                     f"HTTP {status} — {httpclient.explain_auth(status, args.api_key, getattr(args, 'auth_error', None))}"):
        return report.EXIT_INCONCLUSIVE

    # Paramètres incohérents : autant le dire tout de suite plutôt que de
    # collecter des centaines de HTTP 400.
    out["context_window"] = window
    if not config.check_context_window(wl, args.max_tokens, window, ck):
        return ck.conclude(inconclusive=True)

    pods = _pods_ready(args)
    if pods:
        print(f"  {len(pods)} pod(s) de serving Ready — répartition et saturation "
              "seront relevées par palier\n")
    elif not args.no_cluster:
        print("  (aucun pod de serving détecté : mesures HTTP seules)\n")

    make_session = lambda: httpclient.session_from_args(args)  # noqa: E731

    per_level = []
    if not args.no_sweep:
        _summaries, per_level = phase_sweep(args, wl, make_session, ck, pods, out)
    if args.rate > 0:
        res = phase_open(args, wl, make_session, ck, pods, out)
        per_level.append((0, res))  # concurrence 0 = phase boucle ouverte

    if args.json:
        out["checks"] = ck.summary()
        report.write_json(args.json, out)
    if args.csv and per_level:
        write_levels_csv(args.csv, per_level)
    return ck.conclude()


if __name__ == "__main__":
    sys.exit(main())
