"""
Generators skill — low-stakes fun toys for texture without pressure.

Silly generators that a companion and the user can spin up when the brain
wants something to chew on but not something to accomplish. These are toys,
not tools.

Generator tables come from config, so each household can stock its own:

  generators:
    tables:
      weird_customer:
        - "Someone who says 'I'll come back later!' They will not come back later."
        - "Someone who picks up every item and puts them all back slightly wrong."
      tiny_quest:
        - "Find the oldest object in the room and give it a backstory."
        - "Name three sounds you can hear right now."

With no tables configured, the skill offers its tool but says nothing is
stocked yet.
"""

import random
import logging
from skills.base import BaseSkill

logger = logging.getLogger(__name__)

MAX_COUNT = 5


class GeneratorsSkill(BaseSkill):
    name = "generators"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get("generators", {}) or {}
        raw = cfg.get("tables", {}) or {}
        self.tables: dict[str, list[str]] = {}
        if isinstance(raw, dict):
            for key, entries in raw.items():
                if isinstance(entries, list):
                    items = [str(e) for e in entries if str(e).strip()]
                    if items:
                        self.tables[str(key)] = items
        else:
            logger.warning("generators.tables should be a mapping of name -> list")

    def get_tools(self) -> list[dict]:
        names = ", ".join(sorted(self.tables)) or "none configured yet"
        return [
            {
                "type": "function",
                "function": {
                    "name": "spin_generator",
                    "description": (
                        "Spin one of the household's silly generators for a random, "
                        "low-stakes bit of texture — for play, or when the brain "
                        "wants something to chew on but not something to accomplish. "
                        f"Available generators: {names}."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "generator": {
                                "type": "string",
                                "description": "Which generator to spin. Omit to pick one at random.",
                            },
                            "count": {
                                "type": "integer",
                                "description": f"How many results (default 1, max {MAX_COUNT}).",
                            },
                        },
                        "required": [],
                    },
                },
            },
        ]

    def execute(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "spin_generator":
            return self._spin(arguments)
        return f"Unknown tool: {tool_name}"

    def _spin(self, args: dict) -> str:
        if not self.tables:
            return (
                "No generators are stocked yet. Add some under generators.tables "
                "in config (a name mapped to a list of entries)."
            )
        name = (args.get("generator") or "").strip()
        if not name:
            name = random.choice(list(self.tables))
        table = self.tables.get(name)
        if table is None:
            # Forgiving match: case-insensitive, spaces vs underscores.
            key = name.lower().replace(" ", "_")
            for k, v in self.tables.items():
                if k.lower().replace(" ", "_") == key:
                    name, table = k, v
                    break
        if table is None:
            return (
                f"No generator called '{name}'. Available: "
                + ", ".join(sorted(self.tables))
            )
        try:
            count = int(args.get("count", 1) or 1)
        except (TypeError, ValueError):
            count = 1
        count = max(1, min(count, MAX_COUNT, len(table)))
        picks = random.sample(table, count)
        header = f"{name.replace('_', ' ').upper()}:\n"
        return header + "\n\n".join(f"  {i+1}. {e}" for i, e in enumerate(picks))
