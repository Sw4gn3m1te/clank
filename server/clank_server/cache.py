"""SQLite cache of verified proofs.

The key is a hash of everything that determines whether a proof checks: the goal as the server
restates it (hypotheses with their names, target, universes, imports, opens), the Lean version,
and the versions of the dependencies of the Lake project used for checking (e.g. Mathlib's
commit). Only proofs the server has verified are stored.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from .protocol import ProveRequest


def environment_id(lean_project: Path | None) -> str:
    """Identifies the checking environment: the dependency revisions in `lake-manifest.json`."""
    if lean_project is None:
        return "core"
    try:
        manifest = json.loads((lean_project / "lake-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown"
    packages = sorted(f"{p.get('name')}@{p.get('rev')}" for p in manifest.get("packages", []))
    return ",".join(packages) or "core"


def cache_key(req: ProveRequest, env_id: str) -> str:
    data = {
        "hypotheses": [[h.name, h.type, h.value] for h in req.hypotheses],
        "target": req.target,
        "universes": req.universes,
        "imports": req.imports,
        "opens": req.opens,
        "lean_version": req.lean_version,
        "env": env_id,
    }
    # Only when present, so that existing entries for prefix-less requests stay valid.
    if req.prefix:
        data["prefix"] = req.prefix
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class ProofCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS proofs (key TEXT PRIMARY KEY, proofs TEXT NOT NULL, created REAL NOT NULL)"
        )
        self._db.commit()

    def get(self, key: str) -> list[str] | None:
        row = self._db.execute("SELECT proofs FROM proofs WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, proofs: list[str]) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO proofs (key, proofs, created) VALUES (?, ?, ?)",
            (key, json.dumps(proofs), time.time()),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()
