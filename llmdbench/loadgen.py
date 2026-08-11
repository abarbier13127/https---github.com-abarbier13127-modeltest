#!/usr/bin/env python3
"""
Moteurs de charge : boucle fermée et boucle ouverte.

**Boucle fermée** (`run_closed`) : N clients envoient une requête, attendent la
réponse, recommencent. C'est le modèle du `load_test_qwen.py` existant. Il
mesure la capacité à concurrence donnée, mais il *auto-régule* : si le serveur
ralentit, la charge offerte baisse toute seule. Il ne peut donc jamais montrer
un effondrement de service.

**Boucle ouverte** (`run_open`) : les requêtes arrivent à un débit imposé λ
(inter-arrivées exponentielles = processus de Poisson), indépendamment de la
vitesse du serveur. Si le serveur ne suit pas, la file grossit et la latence
mesurée l'inclut — c'est la correction de la *coordinated omission*, et c'est
le seul mode qui révèle honnêtement le point de rupture d'un déploiement.

Règle d'usage : boucle fermée pour établir la capacité, boucle ouverte pour
valider un SLO à un débit cible.
"""

from __future__ import annotations

import queue
import random
import threading
import time

from . import httpclient


class _Live:
    """Compteur partagé + affichage périodique de la progression."""

    def __init__(self, enabled=True, every=2.0):
        self.enabled = enabled
        self.every = every
        self.lock = threading.Lock()
        self.results = []
        self._stop = threading.Event()
        self._t = None

    def add(self, r):
        with self.lock:
            self.results.append(r)

    def start(self, t0, horizon):
        if not self.enabled:
            return
        self._t = threading.Thread(target=self._loop, args=(t0, horizon), daemon=True)
        self._t.start()

    def _loop(self, t0, horizon):
        last = 0
        while not self._stop.wait(self.every):
            with self.lock:
                n = len(self.results)
                ok = sum(1 for r in self.results if r.ok)
                lat = [r.latency for r in self.results[-200:] if r.ok]
            el = time.perf_counter() - t0
            rps = (n - last) / self.every
            last = n
            p50 = sorted(lat)[len(lat) // 2] if lat else float("nan")
            bar = f"{el:5.1f}/{horizon:.0f}s" if horizon else f"{el:5.1f}s"
            print(f"   … {bar}  n={n:<6} ok={ok:<6} {rps:6.1f} req/s  "
                  f"lat~{p50:.2f}s", flush=True)

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(timeout=0.1)


def _warmup(make_session, workload, n, max_tokens):
    """Requêtes de chauffe non comptées : chargement des poids, JIT, cache."""
    if n <= 0:
        return
    s = make_session()
    try:
        for i in range(n):
            prompt, grp = workload.next(-1 - i)
            s.chat(prompt, max_tokens=max_tokens, group=grp)
    finally:
        s.close()


def run_closed(make_session, workload, concurrency=8, duration=30.0,
               requests=None, max_tokens=64, stream=True, warmup=2,
               progress=True, start_index=0):
    """
    N workers en boucle. S'arrête après `duration` s ou `requests` requêtes.

    Retourne (results, wall_seconds).
    """
    _warmup(make_session, workload, warmup, max_tokens)
    live = _Live(progress)
    idx = [start_index]
    idx_lock = threading.Lock()
    stop_evt = threading.Event()
    budget = [requests] if requests else None

    def worker():
        s = make_session()
        try:
            while not stop_evt.is_set():
                with idx_lock:
                    if budget is not None:
                        if budget[0] <= 0:
                            return
                        budget[0] -= 1
                    i = idx[0]
                    idx[0] += 1
                prompt, grp = workload.next(i)
                r = s.chat(prompt, max_tokens=max_tokens, group=grp, stream=stream)
                live.add(r)
        finally:
            s.close()

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(concurrency)]
    t0 = time.perf_counter()
    live.start(t0, duration if not requests else 0)
    for t in threads:
        t.start()
    if duration:
        stop_evt.wait(duration)
        stop_evt.set()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0
    live.stop()
    return live.results, wall


def run_open(make_session, workload, rate=5.0, duration=30.0, max_tokens=64,
             stream=True, warmup=2, progress=True, max_inflight=None,
             start_index=0, seed=7):
    """
    Arrivées de Poisson au débit `rate` req/s pendant `duration` s.

    `max_inflight` borne le nombre de requêtes simultanées (défaut :
    10×rate, plancher 16). Une requête qui ne trouve pas de worker libre attend
    — son attente est comptée dans `sched_delay` **et** dans la latence.
    """
    _warmup(make_session, workload, warmup, max_tokens)
    n_workers = int(max_inflight or max(16, rate * 10))
    live = _Live(progress)
    q = queue.Queue()
    rng = random.Random(seed)

    def worker():
        s = make_session()
        try:
            while True:
                item = q.get()
                try:
                    if item is None:
                        return
                    i, due = item
                    now = time.perf_counter()
                    if now < due:            # arrivée future : on patiente
                        time.sleep(due - now)
                        delay = 0.0
                    else:                    # saturation : on est déjà en retard
                        delay = now - due
                    prompt, grp = workload.next(i)
                    r = s.chat(prompt, max_tokens=max_tokens, group=grp, stream=stream)
                    r.sched_delay = delay
                    live.add(r)
                finally:
                    q.task_done()
        finally:
            s.close()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(n_workers)]
    for t in threads:
        t.start()

    t0 = time.perf_counter()
    live.start(t0, duration)
    i, due = start_index, t0
    while True:
        due += rng.expovariate(rate) if rate > 0 else 1e9
        if due - t0 > duration:
            break
        # On ne dort que si l'arrivée est loin : sinon le worker gère l'attente.
        lead = due - time.perf_counter()
        if lead > 0:
            time.sleep(lead)
        q.put((i, due))
        i += 1
    q.join()
    wall = time.perf_counter() - t0
    for _ in threads:
        q.put(None)
    live.stop()
    return live.results, wall


def sweep(make_session, workload, levels, duration=20.0, **kw):
    """
    Balaye plusieurs niveaux de concurrence et renvoie la liste des runs.

    Sert à trouver le *genou* de saturation : le point où le débit cesse de
    croître alors que la latence, elle, continue de monter. Sur un déploiement
    multi-pods correctement équilibré, ce genou doit se déplacer vers la droite
    proportionnellement au nombre de replicas.
    """
    runs = []
    for lvl in levels:
        print(f"\n-- concurrence {lvl} --")
        res, wall = run_closed(make_session, workload, concurrency=lvl,
                               duration=duration, **kw)
        runs.append((lvl, res, wall))
    return runs
