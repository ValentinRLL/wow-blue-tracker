"""État persistant : identifiants des posts déjà traités. Aucune donnée sensible."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

MAX_SEEN = 1000
MAX_ATTEMPTS = 3


@dataclass
class State:
    initialized: bool = False
    seen: list[int] = field(default_factory=list)
    attempts: dict[str, int] = field(default_factory=dict)
    # Plus ancien post retenu mais pas encore traité (rafale, limite de temps, panne).
    pending_floor: int | None = None

    def seen_set(self) -> set[int]:
        return set(self.seen)

    def mark_seen(self, post_id: int) -> None:
        if post_id not in self.seen:
            self.seen.append(post_id)
        self.attempts.pop(str(post_id), None)
        if len(self.seen) > MAX_SEEN:
            self.seen = sorted(self.seen)[-MAX_SEEN:]

    def record_failure(self, post_id: int) -> bool:
        """Compte un échec. Renvoie True si le post doit être abandonné."""
        n = self.attempts.get(str(post_id), 0) + 1
        self.attempts[str(post_id)] = n
        if n >= MAX_ATTEMPTS:
            self.mark_seen(post_id)
            return True
        return False


def load(path: Path) -> State:
    if not path.exists():
        return State()
    data = json.loads(path.read_text(encoding="utf-8"))
    return State(
        initialized=bool(data.get("initialized")),
        seen=[int(x) for x in data.get("seen", [])],
        attempts={str(k): int(v) for k, v in data.get("attempts", {}).items()},
        pending_floor=int(data["pending_floor"]) if data.get("pending_floor") else None,
    )


def save(path: Path, state: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "initialized": state.initialized,
        "seen": sorted(state.seen),
        "attempts": dict(sorted(state.attempts.items())),
        "pending_floor": state.pending_floor,
    }
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)
