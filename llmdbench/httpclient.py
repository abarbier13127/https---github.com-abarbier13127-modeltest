#!/usr/bin/env python3
"""
Client HTTP bas niveau, stdlib uniquement (`http.client`).

Pourquoi pas `urllib` comme dans `tests/qwen_client.py` ?
  - keep-alive : `urllib` rouvre une connexion TCP+TLS par requête, ce qui pollue
    la mesure de latence (handshake ≈ 10-30 ms) et fausse les tests de routage
    (chaque nouvelle connexion peut être ré-équilibrée vers un autre pod).
  - streaming SSE : indispensable pour mesurer le **TTFT** (time-to-first-token)
    et l'**ITL** (inter-token latency), les deux métriques qui distinguent un
    problème de prefill d'un problème de decode — donc un problème de routage
    llm-d d'un problème de saturation GPU.

Une `Session` = une connexion persistante = un « client » du point de vue du
Gateway. Les moteurs de charge créent une Session par worker.
"""
import http.client
import json
import ssl
import time
import urllib.parse
from dataclasses import dataclass, field, asdict

from . import config


@dataclass
class Result:
    """Résultat d'une requête d'inférence, avec sa décomposition temporelle."""
    ok: bool = False
    status: int = 0
    error: str | None = None

    t_start: float = 0.0          # horodatage absolu du départ (time.time)
    latency: float = 0.0          # end-to-end, secondes
    sched_delay: float = 0.0      # retard sur l'heure d'arrivée prévue (boucle ouverte)
    ttft: float | None = None     # time-to-first-token (streaming seulement)
    itls: list[float] = field(default_factory=list)  # deltas entre tokens

    prompt_tokens: int = 0
    completion_tokens: int = 0

    pod: str | None = None        # pod servant, si un en-tête le révèle
    group: str | None = None      # étiquette de workload (cf. workload.py)
    text: str = ""                # contenu généré (tronqué)

    @property
    def tpot(self) -> float | None:
        """Time-per-output-token en phase decode (hors prefill)."""
        if self.ttft is None or self.completion_tokens < 2:
            return None
        return (self.latency - self.ttft) / (self.completion_tokens - 1)

    def as_row(self) -> dict:
        d = asdict(self)
        d.pop("itls", None)
        d.pop("text", None)
        d["tpot"] = self.tpot
        return d


class HTTPError(Exception):
    pass


class Session:
    """Connexion persistante vers un endpoint OpenAI-compatible."""

    def __init__(self, url=None, model=None, insecure=None, api_key=None,
                 timeout=120.0):
        self.base = (url or config.DEFAULT_URL).rstrip("/")
        self.model = model or config.DEFAULT_MODEL
        self.insecure = config.DEFAULT_INSECURE if insecure is None else insecure
        self.api_key = api_key if api_key is not None else config.DEFAULT_API_KEY
        self.timeout = timeout
        p = urllib.parse.urlparse(self.base)
        self._https = p.scheme == "https"
        self._host = p.hostname
        self._port = p.port or (443 if self._https else 80)
        self._path_prefix = p.path.rstrip("/")
        self._conn = None

    # -- plomberie ------------------------------------------------------------
    def _ctx(self):
        if not self.insecure:
            return ssl.create_default_context()
        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        return c

    def _connect(self):
        if self._https:
            self._conn = http.client.HTTPSConnection(
                self._host, self._port, timeout=self.timeout, context=self._ctx())
        else:
            self._conn = http.client.HTTPConnection(
                self._host, self._port, timeout=self.timeout)
        return self._conn

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def _headers(self, extra=None):
        """Un jeton de session, s'il y en a un, part en `Authorization: Bearer`."""
        h = {"Content-Type": "application/json", "Accept": "application/json",
             "Connection": "keep-alive"}
        if self.api_key:
            tok = str(self.api_key)
            h["Authorization"] = tok if tok.lower().startswith("bearer ") \
                else f"Bearer {tok}"
        h.update(extra or {})
        return h

    def _request(self, method, path, body=None, extra_headers=None, retry=True):
        """
        Envoie la requête ; réouvre la connexion une fois si elle était morte.

        Le retry est volontairement restreint aux cas où il est **certain** que
        le serveur n'a pas traité la requête : échec pendant l'envoi, ou
        `RemoteDisconnected` (keep-alive fermé côté serveur avant toute réponse).
        Rejouer une requête dont la réponse a commencé créerait un doublon —
        invisible côté client, mais qui fausserait tous les comptages par pod.
        """
        if self._conn is None:
            self._connect()
        payload = json.dumps(body).encode() if body is not None else None
        try:
            self._conn.request(method, self._path_prefix + path, payload,
                               self._headers(extra_headers))
        except (http.client.HTTPException, OSError):
            self.close()
            if not retry:
                raise
            return self._request(method, path, body, extra_headers, retry=False)
        try:
            return self._conn.getresponse()
        except http.client.RemoteDisconnected:
            self.close()
            if not retry:
                raise
            return self._request(method, path, body, extra_headers, retry=False)
        except (http.client.HTTPException, OSError):
            self.close()
            raise

    @staticmethod
    def _pod_from(resp) -> str | None:
        for h in config.ENDPOINT_HEADERS:
            v = resp.getheader(h)
            if v:
                return v
        return None

    # -- API ------------------------------------------------------------------
    def raw(self, method: str, path: str, body=None, headers=None,
            max_body=2000) -> dict:
        """
        Requête brute qui **ne lève jamais** : renvoie le code, les en-têtes et
        le début du corps. Utile quand un 401 est une information à afficher et
        non une exception à rattraper.

        Retour : {status, headers (clés en minuscules), body, latency, error}
        """
        t0 = time.perf_counter()
        try:
            resp = self._request(method, path, body, headers)
            data = resp.read()
            return {
                "status": resp.status,
                "reason": resp.reason,
                "headers": {k.lower(): v for k, v in resp.getheaders()},
                "body": data[:max_body].decode("utf-8", errors="replace"),
                "latency": time.perf_counter() - t0,
                "error": None,
            }
        except Exception as e:  # noqa: BLE001
            self.close()
            return {"status": 0, "reason": "", "headers": {}, "body": "",
                    "latency": time.perf_counter() - t0,
                    "error": f"{type(e).__name__}: {e}"}

    def get_json(self, path: str):
        resp = self._request("GET", path)
        raw = resp.read()
        if resp.status >= 400:
            raise HTTPError(f"{resp.status} sur {path}: {raw[:200]!r}")
        return json.loads(raw or b"{}")

    def list_models(self) -> list[str]:
        return [m["id"] for m in self.get_json("/v1/models").get("data", [])]

    def health(self) -> tuple[bool, int]:
        """Sonde /health (vLLM) ; retombe sur /v1/models si absente."""
        try:
            resp = self._request("GET", "/health")
            resp.read()
            if resp.status < 400:
                return True, resp.status
        except Exception:  # noqa: BLE001 — sonde best-effort
            self.close()
        try:
            self.list_models()
            return True, 200
        except Exception as e:  # noqa: BLE001
            return False, getattr(e, "status", 0)

    def chat(self, prompt: str, max_tokens: int = 64, temperature: float = 0.0,
             stream: bool = True, group: str | None = None,
             system: str | None = None, extra_body: dict | None = None) -> Result:
        """Une complétion chat. `stream=True` renseigne TTFT et ITL."""
        msgs = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
        payload = {
            "model": self.model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if stream:
            # vLLM renvoie alors un dernier chunk contenant `usage`.
            payload["stream_options"] = {"include_usage": True}
        payload.update(extra_body or {})

        r = Result(group=group, t_start=time.time())
        t0 = time.perf_counter()
        try:
            resp = self._request("POST", "/v1/chat/completions", payload)
            r.status = resp.status
            r.pod = self._pod_from(resp)
            if resp.status >= 400:
                r.error = f"HTTP {resp.status}: {resp.read()[:200].decode(errors='replace')}"
                r.latency = time.perf_counter() - t0
                return r
            if stream:
                self._read_stream(resp, r, t0)
            else:
                body = json.loads(resp.read())
                r.latency = time.perf_counter() - t0
                ch = (body.get("choices") or [{}])[0]
                r.text = (ch.get("message") or {}).get("content", "")[:500]
                u = body.get("usage") or {}
                r.prompt_tokens = u.get("prompt_tokens", 0)
                r.completion_tokens = u.get("completion_tokens", 0)
            r.ok = True
        except Exception as e:  # noqa: BLE001 — toute erreur = requête échouée
            r.error = f"{type(e).__name__}: {e}"
            r.latency = time.perf_counter() - t0
            self.close()
        return r

    def _read_stream(self, resp, r: Result, t0: float) -> None:
        """Consomme le flux SSE et horodate chaque token reçu."""
        chunks, last = [], None
        n_chunks = 0
        while True:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line or not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                # Drainer la fin du corps : sans ça la connexion keep-alive reste
                # « occupée », la requête suivante échoue et le retry la renvoie
                # une seconde fois — soit un doublon invisible côté client mais
                # bien réel côté pods (fausse tous les comptages de répartition).
                resp.read()
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            now = time.perf_counter()
            usage = obj.get("usage")
            if usage:  # dernier chunk (stream_options.include_usage)
                r.prompt_tokens = usage.get("prompt_tokens", 0)
                r.completion_tokens = usage.get("completion_tokens", 0)
            for ch in obj.get("choices") or []:
                piece = (ch.get("delta") or {}).get("content")
                if not piece:
                    continue
                if r.ttft is None:
                    r.ttft = now - t0
                else:
                    r.itls.append(now - last)
                last = now
                n_chunks += 1
                if len(chunks) < 200:
                    chunks.append(piece)
        r.latency = time.perf_counter() - t0
        r.text = "".join(chunks)[:500]
        if not r.completion_tokens:
            # Pas de bloc usage : 1 chunk ≈ 1 token, approximation suffisante
            # pour le débit relatif (les comparaisons restent homogènes).
            r.completion_tokens = n_chunks

    # -- confort --------------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def session_from_args(args) -> Session:
    return Session(url=args.url, model=args.model, insecure=args.insecure,
                   api_key=getattr(args, "api_key", None),
                   timeout=getattr(args, "timeout", 120.0))
