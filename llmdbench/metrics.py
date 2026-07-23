#!/usr/bin/env python3
"""
Statistiques : percentiles, agrégats de run, histogrammes texte, et mesures
d'équilibrage (déséquilibre max/min, Gini, test du χ² d'uniformité).

Le χ² permet de trancher objectivement « ce déséquilibre entre pods est-il du
bruit ou une vraie asymétrie de routage ? » sans dépendre de scipy : la fonction
de survie du χ² est implémentée ici (gamma incomplète régularisée, ~40 lignes).
"""
import math
import statistics


# --- percentiles -------------------------------------------------------------
def pct(sorted_vals, p: float) -> float:
    """Percentile par interpolation linéaire. `sorted_vals` trié croissant."""
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def dist(values) -> dict:
    """Résumé d'une distribution : n, min/moy/max, P50/P90/P95/P99."""
    s = sorted(v for v in values if v is not None and not math.isnan(v))
    if not s:
        return {"n": 0}
    return {
        "n": len(s), "min": s[0], "mean": statistics.fmean(s), "max": s[-1],
        "p50": pct(s, 50), "p90": pct(s, 90), "p95": pct(s, 95), "p99": pct(s, 99),
        "stdev": statistics.pstdev(s) if len(s) > 1 else 0.0,
    }


def fmt_dist(d: dict, unit="s", scale=1.0) -> str:
    if not d.get("n"):
        return "aucune donnée"
    f = lambda k: d[k] * scale  # noqa: E731
    return (f"min={f('min'):.3f} moy={f('mean'):.3f} P50={f('p50'):.3f} "
            f"P90={f('p90'):.3f} P95={f('p95'):.3f} P99={f('p99'):.3f} "
            f"max={f('max'):.3f} {unit}")


# --- agrégat d'un run --------------------------------------------------------
def summarize(results, wall: float) -> dict:
    """Agrège une liste de `httpclient.Result` sur une fenêtre de `wall` s."""
    ok = [r for r in results if r.ok]
    ko = [r for r in results if not r.ok]
    out_tok = sum(r.completion_tokens for r in ok)
    in_tok = sum(r.prompt_tokens for r in ok)
    per_tok = [r.latency / max(r.completion_tokens, 1) for r in ok]
    itl_all = [x for r in ok for x in r.itls]

    s = {
        "requests_ok": len(ok),
        "requests_err": len(ko),
        "error_rate": len(ko) / max(len(results), 1),
        "wall_s": wall,
        "throughput_rps": len(ok) / wall if wall > 0 else 0.0,
        "output_tok": out_tok,
        "input_tok": in_tok,
        "output_tok_per_s": out_tok / wall if wall > 0 else 0.0,
        "latency": dist(r.latency for r in ok),
        "ttft": dist(r.ttft for r in ok),
        "tpot": dist(r.tpot for r in ok),
        "itl": dist(itl_all),
        "latency_per_tok": dist(per_tok),
        "errors": _top_errors(ko),
    }
    return s


def _top_errors(ko, limit=5) -> list:
    counts = {}
    for r in ko:
        key = (r.error or "?").split("\n")[0][:120]
        counts[key] = counts.get(key, 0) + 1
    return sorted(({"error": k, "count": v} for k, v in counts.items()),
                  key=lambda d: -d["count"])[:limit]


def print_summary(s: dict, prefix="  ") -> None:
    n_tot = s["requests_ok"] + s["requests_err"]
    print(f"{prefix}requêtes OK    : {s['requests_ok']} / {n_tot}"
          f"   erreurs : {s['requests_err']} ({100*s['error_rate']:.1f} %)")
    print(f"{prefix}débit          : {s['throughput_rps']:.2f} req/s"
          f"   |  {s['output_tok_per_s']:.0f} tok/s générés")
    if s["latency"].get("n"):
        print(f"{prefix}latence e2e    : {fmt_dist(s['latency'])}")
    if s["ttft"].get("n"):
        d = s["ttft"]
        print(f"{prefix}TTFT           : P50={d['p50']*1000:.0f} P90={d['p90']*1000:.0f} "
              f"P99={d['p99']*1000:.0f} ms   (moy {d['mean']*1000:.0f})")
    if s["tpot"].get("n"):
        d = s["tpot"]
        print(f"{prefix}TPOT (decode)  : P50={d['p50']*1000:.1f} P99={d['p99']*1000:.1f} ms/tok")
    if s["itl"].get("n"):
        d = s["itl"]
        print(f"{prefix}ITL inter-tok  : P50={d['p50']*1000:.1f} P99={d['p99']*1000:.1f} ms")
    for e in s["errors"]:
        print(f"{prefix}  ! {e['count']}× {e['error']}")


# --- histogramme texte -------------------------------------------------------
_BLOCKS = " ▁▂▃▄▅▆▇█"


def histogram(values, bins=20, width=44, unit="s") -> str:
    v = sorted(x for x in values if x is not None)
    if len(v) < 2:
        return "  (trop peu de points)"
    lo, hi = v[0], v[-1]
    if hi <= lo:
        hi = lo + 1e-9
    counts = [0] * bins
    for x in v:
        counts[min(bins - 1, int((x - lo) / (hi - lo) * bins))] += 1
    top = max(counts) or 1
    lines = []
    for i, c in enumerate(counts):
        edge = lo + (hi - lo) * i / bins
        bar = "█" * int(width * c / top)
        lines.append(f"  {edge:8.3f}{unit} | {bar}{'' if c else ''} {c}")
    return "\n".join(lines)


def sparkline(values, buckets=40) -> str:
    """Mini-courbe temporelle (une ligne) — utile pour repérer un décrochage."""
    v = [x for x in values if x is not None]
    if not v:
        return ""
    per = max(1, len(v) // buckets)
    means = [statistics.fmean(v[i:i + per]) for i in range(0, len(v), per)][:buckets]
    lo, hi = min(means), max(means)
    rng = (hi - lo) or 1e-9
    return "".join(_BLOCKS[min(8, int((m - lo) / rng * 8))] for m in means)


# --- équilibrage entre pods --------------------------------------------------
def balance_stats(counts: dict) -> dict:
    """
    counts : {pod: nb_requêtes}. Renvoie les indicateurs de répartition.

    - imbalance : max/min (1.0 = parfait). Lisible mais sensible aux petits n.
    - gini      : 0 = parfait, 1 = tout sur un pod.
    - chi2/p    : test d'uniformité. p < 0.01 = déséquilibre statistiquement
                  significatif (pas du bruit d'échantillonnage).
    """
    vals = [max(0, int(v)) for v in counts.values()]
    n, k = sum(vals), len(vals)
    out = {"pods": k, "total": n, "counts": dict(counts)}
    if k == 0 or n == 0:
        return out | {"imbalance": float("nan"), "gini": float("nan"),
                      "chi2": float("nan"), "p_value": float("nan")}
    exp = n / k
    out["expected_per_pod"] = exp
    out["imbalance"] = (max(vals) / min(vals)) if min(vals) > 0 else float("inf")
    out["max_dev_pct"] = 100 * (max(vals) - exp) / exp
    # Gini
    sv = sorted(vals)
    cum = sum((2 * (i + 1) - k - 1) * x for i, x in enumerate(sv))
    out["gini"] = cum / (k * n) if n else 0.0
    # χ² d'ajustement à l'uniforme, k-1 degrés de liberté
    chi2 = sum((x - exp) ** 2 / exp for x in vals)
    out["chi2"] = chi2
    out["p_value"] = chi2_sf(chi2, k - 1) if k > 1 else 1.0
    return out


def chi2_sf(x: float, df: int) -> float:
    """P(X > x) pour X ~ χ²(df). Gamma incomplète régularisée Q(df/2, x/2)."""
    if df <= 0:
        return float("nan")
    if x <= 0:
        return 1.0
    return _gammq(df / 2.0, x / 2.0)


def _gammq(a: float, x: float) -> float:
    if x < a + 1.0:
        return 1.0 - _gser(a, x)
    return _gcf(a, x)


def _gser(a, x, itmax=300, eps=3e-12):
    """Série pour P(a,x), convergente pour x < a+1."""
    ap, s, delta = a, 1.0 / a, 1.0 / a
    for _ in range(itmax):
        ap += 1.0
        delta *= x / ap
        s += delta
        if abs(delta) < abs(s) * eps:
            break
    return s * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gcf(a, x, itmax=300, eps=3e-12, fpmin=1e-300):
    """Fraction continue (Lentz) pour Q(a,x), convergente pour x >= a+1."""
    b, c, d = x + 1.0 - a, 1.0 / fpmin, 1.0 / (x + 1.0 - a)
    h = d
    for i in range(1, itmax + 1):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < fpmin:
            d = fpmin
        c = b + an / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h * math.exp(-x + a * math.log(x) - math.lgamma(a))


def print_balance(b: dict, prefix="  ") -> None:
    if not b.get("total"):
        print(f"{prefix}aucune attribution pod/requête disponible.")
        return
    print(f"{prefix}répartition sur {b['pods']} pod(s), {b['total']} requêtes "
          f"(attendu {b['expected_per_pod']:.1f}/pod) :")
    width = max((len(k) for k in b["counts"]), default=10)
    top = max(b["counts"].values()) or 1
    for podname, c in sorted(b["counts"].items(), key=lambda kv: -kv[1]):
        share = 100 * c / b["total"]
        bar = "█" * int(28 * c / top)
        print(f"{prefix}  {podname:<{width}}  {c:>6}  {share:5.1f} %  {bar}")
    print(f"{prefix}déséquilibre max/min = {b['imbalance']:.2f}   "
          f"Gini = {b['gini']:.3f}   χ²({b['pods']-1}) = {b['chi2']:.1f}  "
          f"p = {b['p_value']:.4f}")
