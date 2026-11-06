"""Persistent Roomcast configuration (~/.config/roomcast/config.json).

The GUI edits this file; the background service watches it and makes the
PipeWire graph match.

    pairs:   [{id, name, left, right}]            left/right are speaker keys
    outputs: [{id, name, members: [{type, ref}]}] type is "speaker" or "pair"
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "roomcast"
CONFIG_FILE = CONFIG_DIR / "config.json"


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def load() -> dict:
    try:
        data = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("pairs", [])
    data.setdefault("outputs", [])
    return data


def save(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(CONFIG_FILE)


def mtime() -> float:
    try:
        return CONFIG_FILE.stat().st_mtime
    except OSError:
        return 0.0


def find(items: list, item_id: str) -> dict | None:
    return next((i for i in items if i.get("id") == item_id), None)


PAIR_OUTPUT_PREFIX = "pair-"


def all_outputs(cfg: dict) -> list[dict]:
    """User outputs plus one implicit output per stereo pair.

    Every pair gets its own sound device, so it can be picked directly in
    Sound Settings or the Apps tab without first adding it to an output.
    """
    implicit = [
        {
            "id": PAIR_OUTPUT_PREFIX + p["id"],
            "name": p["name"],
            "members": [{"type": "pair", "ref": p["id"]}],
        }
        for p in cfg["pairs"]
    ]
    return list(cfg["outputs"]) + implicit


def flatten_members(cfg: dict, output: dict) -> list[tuple[str, str]]:
    """Expand an output's members into (speaker key, role) tuples.

    role is "stereo", "left" or "right". A speaker that appears twice keeps
    its first role so it never receives two copies of the audio.
    """
    result: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(key, role):
        if key and key not in seen:
            seen.add(key)
            result.append((key, role))

    for m in output.get("members", []):
        if m.get("type") == "speaker":
            add(m.get("ref"), "stereo")
        elif m.get("type") == "pair":
            pair = find(cfg["pairs"], m.get("ref"))
            if pair:
                add(pair.get("left"), "left")
                add(pair.get("right"), "right")
    return result
