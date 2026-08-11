#!/usr/bin/env python3
"""
Introspection du cluster via la CLI `oc` (aucune dépendance Python k8s).

Pourquoi passer par le cluster alors qu'on teste en HTTP ?
Parce que **rien côté client ne dit quel pod a servi la requête**, sauf si le
Gateway ajoute un en-tête (ce que ne fait pas la Route KServe simple). La
méthode fiable et universelle est la *différence de compteurs* : on lit
`vllm:request_success_total` sur chaque pod avant et après une rafale, et le
delta donne la répartition exacte, sans instrumenter quoi que ce soit.

Les métriques sont lues via le proxy de l'API server :
    oc get --raw /api/v1/namespaces/<ns>/pods/<pod>:<port>/proxy/metrics
ce qui ne nécessite ni `curl` dans l'image, ni port-forward, ni Route.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field

OC = os.environ.get("OC_BIN", "oc")

# Métriques vLLM suivies. Les noms varient selon les versions : on accepte
# plusieurs alias et on garde le premier présent.
KEY_METRICS = {
    "requests_ok": ["vllm:request_success_total"],
    "prompt_tokens": ["vllm:prompt_tokens_total"],
    "gen_tokens": ["vllm:generation_tokens_total"],
    "running": ["vllm:num_requests_running"],
    "waiting": ["vllm:num_requests_waiting"],
    "kv_usage": ["vllm:gpu_cache_usage_perc"],
    "preemptions": ["vllm:num_preemptions_total"],
    "prefix_queries": ["vllm:prefix_cache_queries_total", "vllm:gpu_prefix_cache_queries"],
    "prefix_hits": ["vllm:prefix_cache_hits_total", "vllm:gpu_prefix_cache_hits"],
    "prefix_hit_rate": ["vllm:gpu_prefix_cache_hit_rate"],
    "ttft_count": ["vllm:time_to_first_token_seconds_count"],
    "ttft_sum": ["vllm:time_to_first_token_seconds_sum"],
}

# Labels qui trahissent le rôle d'un pod dans une topologie llm-d / LWS.
ROLE_LABELS = (
    "llm-d.ai/role",
    "llm-d.ai/inferenceServing",
    "kserve.io/component",
    "component",
    "app.kubernetes.io/component",
    "leaderworkerset.sigs.k8s.io/worker-index",
)


class ClusterError(RuntimeError):
    pass


@dataclass
class Pod:
    name: str
    node: str = ""
    phase: str = ""
    ready: bool = False
    ip: str = ""
    gpus: float = 0.0
    role: str = ""
    restarts: int = 0
    labels: dict = field(default_factory=dict)
    port: int = 8080

    @property
    def short(self) -> str:
        """Nom raccourci lisible dans les tableaux (suffixe de ReplicaSet ôté)."""
        return self.name if len(self.name) <= 34 else "…" + self.name[-33:]


# --- appel CLI ---------------------------------------------------------------
def oc(*args, timeout=60, check=True) -> str:
    if shutil.which(OC) is None:
        raise ClusterError(f"binaire `{OC}` introuvable dans le PATH")
    p = subprocess.run([OC, *args], capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise ClusterError(f"oc {' '.join(args)} -> rc={p.returncode}: "
                           f"{(p.stderr or p.stdout).strip()[:300]}")
    return p.stdout


def oc_json(*args, **kw):
    return json.loads(oc(*args, "-o", "json", **kw) or "{}")


def available() -> tuple[bool, str]:
    """(utilisable, message). Ne lève pas : les tests HTTP doivent survivre."""
    try:
        who = oc("whoami", timeout=20).strip()
        return True, f"oc OK ({who}, KUBECONFIG={os.environ.get('KUBECONFIG', '~/.kube/config')})"
    except Exception as e:  # noqa: BLE001
        return False, f"oc indisponible : {e}"


# --- découverte des pods -----------------------------------------------------
def _gpu_of(spec) -> float:
    tot = 0.0
    for c in spec.get("containers", []):
        lim = (c.get("resources", {}).get("limits") or {})
        for k in ("nvidia.com/gpu", "amd.com/gpu"):
            if k in lim:
                try:
                    tot += float(lim[k])
                except (TypeError, ValueError):
                    pass
    return tot


def _role_of(labels: dict) -> str:
    for k in ROLE_LABELS:
        if labels.get(k):
            return f"{k.split('/')[-1]}={labels[k]}"
    return ""


def _serving_port(spec) -> int:
    for c in spec.get("containers", []):
        for p in c.get("ports", []) or []:
            if p.get("name") in ("http", "http1", "user-port", "metrics") or \
               p.get("containerPort") in (8080, 8000, 3000):
                return int(p["containerPort"])
    return 8080


def list_pods(namespace: str, selector: str = "", only_serving=True) -> list[Pod]:
    """
    Liste les pods du namespace. Sans selector, on garde par défaut ceux qui
    ressemblent à du serving : GPU réservé, ou nom/label de predictor llm-d.
    """
    args = ["get", "pods", "-n", namespace]
    if selector:
        args += ["-l", selector]
    items = oc_json(*args).get("items", [])
    pods = []
    for it in items:
        md, spec, st = it["metadata"], it["spec"], it.get("status", {})
        conds = {c["type"]: c["status"] for c in st.get("conditions", []) or []}
        p = Pod(
            name=md["name"],
            node=spec.get("nodeName", ""),
            phase=st.get("phase", ""),
            ready=conds.get("Ready") == "True",
            ip=st.get("podIP", ""),
            gpus=_gpu_of(spec),
            role=_role_of(md.get("labels", {})),
            restarts=sum(cs.get("restartCount", 0)
                         for cs in st.get("containerStatuses", []) or []),
            labels=md.get("labels", {}),
            port=_serving_port(spec),
        )
        if only_serving and not selector:
            looks_serving = (
                p.gpus > 0
                or "predictor" in p.name
                or "llm-d" in json.dumps(p.labels)
                or p.labels.get("component") in ("predictor", "prefill", "decode")
            )
            if not looks_serving:
                continue
        pods.append(p)
    return sorted(pods, key=lambda x: x.name)


def group_by_role(pods: list[Pod]) -> dict:
    g = {}
    for p in pods:
        g.setdefault(p.role or "(sans rôle)", []).append(p)
    return g


def node_gpu_capacity(node: str) -> dict:
    n = oc_json("get", "node", node)
    return {
        "allocatable": n["status"]["allocatable"].get("nvidia.com/gpu", "0"),
        "capacity": n["status"]["capacity"].get("nvidia.com/gpu", "0"),
    }


def serving_objects(namespace: str) -> dict:
    """InferenceService / LLMInferenceService / Deployments / LWS du namespace."""
    out = {}
    for kind in ("inferenceservice", "llminferenceservice", "deployment",
                 "leaderworkerset", "inferencepool"):
        try:
            items = oc_json("get", kind, "-n", namespace, timeout=30).get("items", [])
        except Exception:  # noqa: BLE001 — CRD absente = non pertinent ici
            continue
        if items:
            out[kind] = [
                {
                    "name": i["metadata"]["name"],
                    "replicas": (i.get("spec", {}).get("replicas")
                                 or i.get("spec", {}).get("predictor", {}).get("minReplicas")),
                    "ready": _ready_of(i),
                }
                for i in items
            ]
    return out


def _ready_of(obj) -> str:
    st = obj.get("status", {}) or {}
    for c in st.get("conditions", []) or []:
        if c.get("type") in ("Ready", "Available"):
            return f"{c['type']}={c['status']}"
    if "readyReplicas" in st:
        return f"ready={st.get('readyReplicas', 0)}/{st.get('replicas', 0)}"
    return "?"


# --- métriques Prometheus par pod --------------------------------------------
def parse_prom(text: str) -> dict:
    """
    Parse un exposé Prometheus en {nom: [(labels, valeur)]}.
    Suffisant pour des compteurs/gauges ; les histogrammes sont exposés comme
    des séries `_bucket`/`_sum`/`_count` normales, donc capturés aussi.
    """
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] == "#":
            continue
        try:
            if "{" in line:
                name, rest = line.split("{", 1)
                labelstr, valstr = rest.rsplit("}", 1)
                labels = {}
                for part in _split_labels(labelstr):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        labels[k.strip()] = v.strip().strip('"')
            else:
                name, valstr, labels = (*line.split(None, 1), {})
            val = float(valstr.split()[0])
        except (ValueError, IndexError):
            continue
        out.setdefault(name.strip(), []).append((labels, val))
    return out


def _split_labels(s: str) -> list:
    """Découpe `a="x",b="y,z"` en respectant les virgules entre guillemets."""
    parts, cur, q = [], [], False
    for ch in s:
        if ch == '"':
            q = not q
        if ch == "," and not q:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def pod_metrics_raw(namespace: str, pod: str, port: int = 8080, path="/metrics",
                    timeout=30) -> str:
    return oc("get", "--raw",
              f"/api/v1/namespaces/{namespace}/pods/{pod}:{port}/proxy{path}",
              timeout=timeout)


def pod_metrics(namespace: str, pod: str, port: int = 8080) -> dict:
    """{clé_courte: valeur} pour les métriques de KEY_METRICS (somme des séries)."""
    parsed = parse_prom(pod_metrics_raw(namespace, pod, port))
    out = {}
    for key, aliases in KEY_METRICS.items():
        for name in aliases:
            if name in parsed:
                out[key] = sum(v for _lbl, v in parsed[name])
                break
    return out


def snapshot(namespace: str, pods: list[Pod], quiet=True) -> dict:
    """Photo des compteurs de tous les pods. {pod: {clé: valeur}}."""
    snap = {}
    for p in pods:
        try:
            snap[p.name] = pod_metrics(namespace, p.name, p.port)
        except Exception as e:  # noqa: BLE001
            if not quiet:
                print(f"    (métriques indisponibles sur {p.name} : {e})")
            snap[p.name] = {}
    snap["_t"] = time.time()
    return snap


def delta(before: dict, after: dict, key="requests_ok") -> dict:
    """Variation d'un compteur entre deux snapshots. {pod: delta}."""
    out = {}
    for pod, m in after.items():
        if pod == "_t":
            continue
        b = (before.get(pod) or {}).get(key)
        a = m.get(key)
        if a is None:
            continue
        out[pod] = a - (b if b is not None else a)
    return out


def metrics_available(snap: dict) -> bool:
    return any(m for k, m in snap.items() if k != "_t")


# --- actions (tests de résilience) -------------------------------------------
def delete_pod(namespace: str, pod: str, grace: int | None = None) -> str:
    args = ["delete", "pod", pod, "-n", namespace]
    if grace is not None:
        args += [f"--grace-period={grace}"]
    return oc(*args, timeout=120)


def scale(namespace: str, kind: str, name: str, replicas: int) -> str:
    return oc("scale", kind, name, "-n", namespace, f"--replicas={replicas}",
              timeout=60)


def wait_ready(namespace: str, selector: str = "", expect: int | None = None,
               timeout=300, interval=5, verbose=True) -> tuple[bool, list]:
    """Attend que les pods de serving soient Ready (et au nombre attendu)."""
    end = time.time() + timeout
    pods = []
    while time.time() < end:
        pods = list_pods(namespace, selector)
        ready = [p for p in pods if p.ready and p.phase == "Running"]
        if ready and (expect is None or len(ready) >= expect):
            return True, pods
        if verbose:
            print(f"    … {len(ready)}/{expect or '?'} pod(s) Ready "
                  f"({', '.join(f'{p.name}:{p.phase}' for p in pods) or 'aucun pod'})")
        time.sleep(interval)
    return False, pods


def print_pods(pods: list[Pod], prefix="  ") -> None:
    if not pods:
        print(f"{prefix}aucun pod de serving trouvé.")
        return
    w = max(len(p.short) for p in pods)
    print(f"{prefix}{'POD':<{w}}  {'NŒUD':<14} {'ÉTAT':<10} {'GPU':>4} {'RST':>3}  RÔLE")
    for p in pods:
        print(f"{prefix}{p.short:<{w}}  {p.node[:14]:<14} "
              f"{(p.phase + ('/Ready' if p.ready else '')):<10} "
              f"{p.gpus:>4.0f} {p.restarts:>3}  {p.role}")
