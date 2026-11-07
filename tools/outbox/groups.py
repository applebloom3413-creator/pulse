"""Group-chat targets for the outbox.

For v1, groups are stored manually in tools/outbox/groups.json:

    [
      {"name": "Family group chat", "target": -1001234567890},
      {"name": "Family group chat", "target": "@myfamilygroup"}
    ]

`target` is anything Telethon's `client.send_file(target, ...)` accepts —
a numeric chat ID (negative for groups), a @username, or an invite link.
"""
from __future__ import annotations
import json
from dataclasses import dataclass

from .paths import GROUPS_FILE


@dataclass
class Group:
    name: str
    target: str | int  # passed straight to Telethon


def load_groups() -> list[Group]:
    if not GROUPS_FILE.exists():
        return []
    try:
        raw = json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[Group] = []
    if not isinstance(raw, list):
        return []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        target = entry.get("target")
        if not name or target in (None, ""):
            continue
        out.append(Group(name=str(name), target=target))
    return out


def save_groups(groups: list[Group]) -> None:
    data = [{"name": g.name, "target": g.target} for g in groups]
    GROUPS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
