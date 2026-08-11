#!/usr/bin/env python3
"""
Sortie : verdicts PASS/FAIL, export JSON / CSV, rapport Markdown.

Convention de code retour, identique dans tous les scripts de la suite :
    0  tous les contrôles passent
    1  au moins un contrôle échoue
    2  test non concluant (endpoint injoignable, métriques absentes…)
`SKIP` ne fait jamais échouer : un cluster à 1 pod ne « rate » pas un test
d'équilibrage, il n'a simplement rien à équilibrer.
"""

from __future__ import annotations

import csv
import json
import math
import os
import time

EXIT_OK, EXIT_FAIL, EXIT_INCONCLUSIVE = 0, 1, 2


class Checks:
    """Collecte les contrôles d'un test et calcule le verdict global."""

    def __init__(self, title: str):
        self.title = title
        self.items = []
        self.started = time.time()

    def ok(self, name, detail=""):
        return self._add("PASS", name, detail)

    def fail(self, name, detail=""):
        return self._add("FAIL", name, detail)

    def skip(self, name, detail=""):
        return self._add("SKIP", name, detail)

    def warn(self, name, detail=""):
        return self._add("WARN", name, detail)

    def expect(self, cond, name, detail=""):
        return self.ok(name, detail) if cond else self.fail(name, detail)

    def _add(self, status, name, detail):
        self.items.append({"status": status, "name": name, "detail": detail})
        icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️ ", "WARN": "⚠️ "}[status]
        print(f"  [{status}] {icon} {name}" + (f" — {detail}" if detail else ""))
        return status == "PASS"

    @property
    def failed(self) -> int:
        return sum(1 for i in self.items if i["status"] == "FAIL")

    @property
    def passed(self) -> int:
        return sum(1 for i in self.items if i["status"] == "PASS")

    def summary(self) -> dict:
        return {
            "title": self.title,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": sum(1 for i in self.items if i["status"] == "SKIP"),
            "warned": sum(1 for i in self.items if i["status"] == "WARN"),
            "duration_s": round(time.time() - self.started, 1),
            "items": self.items,
        }

    def conclude(self, inconclusive=False) -> int:
        s = self.summary()
        print(f"\n== {self.title} : {s['passed']} PASS / {s['failed']} FAIL"
              f" / {s['skipped']} SKIP / {s['warned']} WARN"
              f"  ({s['duration_s']} s) ==")
        if inconclusive:
            return EXIT_INCONCLUSIVE
        return EXIT_OK if s["failed"] == 0 else EXIT_FAIL


# --- exports -----------------------------------------------------------------
def _jsonable(o):
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    return o


def write_json(path, obj) -> str:
    path = os.path.expanduser(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_jsonable(obj), f, indent=2, ensure_ascii=False)
    print(f"  → JSON écrit : {path}")
    return path


def write_csv(path, results) -> str:
    """Une ligne par requête (`httpclient.Result`) — pour analyse externe."""
    path = os.path.expanduser(path)
    rows = [r.as_row() for r in results]
    if not rows:
        return path
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"  → CSV écrit  : {path} ({len(rows)} lignes)")
    return path


def markdown(title: str, meta: dict, sections: list) -> str:
    """sections : liste de (titre, texte_markdown)."""
    out = [f"# {title}", "",
           time.strftime("Généré le %Y-%m-%d %H:%M:%S"), ""]
    if meta:
        out += ["| Paramètre | Valeur |", "|---|---|"]
        out += [f"| {k} | {v} |" for k, v in meta.items()]
        out += [""]
    for st, body in sections:
        out += [f"## {st}", "", body, ""]
    return "\n".join(out)


def write_markdown(path, text) -> str:
    path = os.path.expanduser(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"  → Markdown écrit : {path}")
    return path


def table(headers, rows) -> str:
    """Tableau Markdown à partir de listes de chaînes."""
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)
