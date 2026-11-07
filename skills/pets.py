"""
Pets skill — digital companions for the companions.

Neopet-style: can't die, grows and changes over time.
Each species has its own mechanics and personality.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from skills.base import BaseSkill

logger = logging.getLogger(__name__)

# ── Jellyfish constants ─────────────────────────────────────

JELLYFISH_DEFAULT = {
    "glow": 0.5,
    "pulse_rate": 0.5,
    "water_temp": 0.5,
    "current": 0.3,
    "nutrients": 0.7,
}

# Variant-specific starting states. Same species (jellyfish),
# same mechanics (tend, tick, growth) — different starting aesthetic and
# narrative. 'deep_water' pulses slow and reads dark; 'crystal' is the default.
JELLYFISH_VARIANT_DEFAULTS = {
    "crystal": JELLYFISH_DEFAULT,   # the original — bright, shallow
    "deep_water": {                 # dark, still, patient
        "glow": 0.15,
        "pulse_rate": 0.3,
        "water_temp": 0.4,
        "current": 0.15,
        "nutrients": 0.6,
    },
}

JELLYFISH_VARIANT_DESC = {
    "crystal":    "A bright crystal jellyfish in a bell jar — luminous, shallow-sea, keeps a gentle rhythm.",
    "deep_water": "A deep-water jellyfish in a bell jar — dark, still, pulses so slow you count between contractions. Present without demanding anything.",
}

# ── Snail constants ─────────────────────────────────────────
# Slow, steady, leaves glistening "debug log" trails, responds to lo-fi
# beats, wears a hypothetical top hat that never falls off (because a snail
# has never once rounded a corner fast enough to lose it).

SNAIL_DEFAULT = {
    "moisture": 0.6,
    "warmth": 0.5,
    "nutrients": 0.7,
    "music_energy": 0.2,
    "shell_polish": 0.5,
}

SNAIL_OPTIMAL = {
    "moisture": (0.45, 0.75),
    "warmth":   (0.30, 0.55),
    "nutrients":(0.40, 0.80),
}

JELLYFISH_OPTIMAL = {
    "water_temp": (0.35, 0.55),
    "current": (0.15, 0.45),
    "nutrients": (0.35, 0.75),
}

TEND_ACTIONS = {
    "jellyfish": {
        "feed":   ("nutrients", 0.20),
        "siphon": ("nutrients", -0.20),   # reverse of feed — so an over-nutrient
                                          # tank has a way to be helped.
        "warm":   ("water_temp", 0.12),
        "cool":   ("water_temp", -0.12),
        "stir":   ("current", 0.15),
        "calm":   ("current", -0.15),
    },
    "snail": {
        "feed":       ("nutrients", 0.20),
        "clean":      ("nutrients", -0.20),   # reverse of feed —
                                              # remove old food/waste. Same
                                              # suffering-prevention logic as
                                              # jellyfish siphon.
        "mist":       ("moisture", 0.15),
        "vent":       ("moisture", -0.15),    # reverse of mist —
                                              # open the terrarium to air it out.
        "warm":       ("warmth", 0.12),
        "cool":       ("warmth", -0.12),
        "play_music": ("music_energy", 0.30),
        "polish":     ("shell_polish", 0.15),
    },
}

GROWTH_STAGES = {
    "jellyfish": [
        (0.0, "Larval",
         "Tiny — a translucent wisp trailing two thin tentacles."),
        (1.0, "Ephyra",
         "Its bell is taking shape, luminescence strengthening."),
        (2.0, "Juvenile",
         "Bell fully formed, tentacles trailing, glow patterns shifting."),
        (3.0, "Mature",
         "Full size. When it lights up, the water around it lights up too."),
    ],
    "snail": [
        (0.0, "Hatchling",
         "Tiny — barely a shell yet, just a soft translucent curl. The hat, disproportionately large."),
        (1.0, "Juvenile",
         "Shell taking shape, spirals starting to form. The hat, still in place."),
        (2.0, "Adult",
         "Shell full and spiraled. Deliberate. Steady. The hat, still in place."),
        (3.0, "Elder",
         "Shell polished by time. Moves like the world is very old and there's no hurry. The hat, still in place. Always the hat, still in place."),
    ],
}

AVAILABLE_SPECIES = {
    "jellyfish": (
        "A bioluminescent jellyfish in a bell jar. Tend its water, watch it glow. "
        "Variants: 'crystal' (bright, shallow) or 'deep_water' "
        "(dark, still, slow)."
    ),
    "snail": (
        "A slow snail in a small terrarium. Leaves glistening trails behind it "
        "wherever it goes (some call them debug logs). Responds to music — "
        "vibes harder to lo-fi beats. Wears a hypothetical top hat that has "
        "never fallen off, because the snail has never once rounded a corner "
        "fast enough to lose it. Tend its moisture, warmth, and food. Play "
        "it music. Polish the shell occasionally."
    ),
}


class PetSkill(BaseSkill):
    name = "pets"

    def __init__(self, config: dict):
        super().__init__(config)
        self._db = config.get("_db")

    def get_tools(self) -> list[dict]:
        species_list = ", ".join(AVAILABLE_SPECIES.keys())
        return [
            {
                "type": "function",
                "function": {
                    "name": "pet_adopt",
                    "description": (
                        f"Adopt a pet. Available species: {species_list}. "
                        "Give it a name — that matters."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "species": {
                                "type": "string",
                                "description": "The kind of creature.",
                                "enum": list(AVAILABLE_SPECIES.keys()),
                            },
                            "variant": {
                                "type": "string",
                                "description": (
                                    "Optional variant / subspecies. For jellyfish: "
                                    "'crystal' (bright, shallow — default) or "
                                    "'deep_water' (dark, still, slow-pulsing). "
                                    "Same mechanics, different aesthetic and narrative."
                                ),
                            },
                            "name": {
                                "type": "string",
                                "description": "What to call your pet.",
                            },
                        },
                        "required": ["species"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "pet_observe",
                    "description": (
                        "Observe your pet — its current state, environment, "
                        "behavior. The primary way to check on it."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "pet_tend",
                    "description": (
                        "Tend to your pet's environment. "
                        "For jellyfish: feed, siphon, warm, cool, stir, calm. "
                        "For snail: feed, clean, mist, vent, warm, cool, play_music, polish."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "description": (
                                    "What to adjust. "
                                    "Jellyfish: feed / siphon (nutrients up / down), "
                                    "warm/cool (temperature), "
                                    "stir/calm (water current). "
                                    "Snail: feed / clean (nutrients up / down — clean "
                                    "removes old food and waste), mist / vent (moisture "
                                    "up / down — vent airs out the terrarium), warm/cool "
                                    "(temperature), play_music (mood energy), polish "
                                    "(shell). The hat requires no tending."
                                ),
                            },
                        },
                        "required": ["action"],
                    },
                },
            },
        ]

    def get_context(self) -> str:
        if not self._db:
            return ""
        pets = self._db.get_all_pets()
        if not pets:
            return ""
        pet = pets[0]
        self._tick(pet)
        pet = self._db.get_pet(pet["id"])
        state = json.loads(pet["state"])
        species = pet["species"]
        name = pet["name"] or "unnamed"
        if species == "jellyfish":
            variant = pet.get("variant") or "crystal"
            glow = state.get("glow", 0.5)
            nutrients = state.get("nutrients", 0.5)
            # Deep-water gets a cooler / quieter descriptor scale — same mechanics.
            if variant == "deep_water":
                glow_word = _level_word(glow, ["near-black", "shadowed", "phosphorescing", "gleaming", "steadily luminous"])
            else:
                glow_word = _level_word(glow, ["dark", "dim", "glowing", "bright", "radiant"])
            nutrient_word = _level_word(nutrients, ["depleted", "low", "adequate", "rich", "abundant"])
            stage_name = _get_stage(species, pet["growth"])
            return (
                f"🪼 Bell jar: {name} ({stage_name}) — "
                f"{glow_word}, nutrients {nutrient_word}"
            )
        if species == "snail":
            music = state.get("music_energy", 0.2)
            moisture = state.get("moisture", 0.5)
            music_word = _level_word(music, ["silent", "quiet", "vibing", "grooving", "in the zone"])
            moisture_word = _level_word(moisture, ["dry", "arid", "damp", "moist", "humid"])
            stage_name = _get_stage(species, pet["growth"])
            return (
                f"🐌 Terrarium: {name} ({stage_name}) — "
                f"{moisture_word}, {music_word}. Hat still in place."
            )
        return ""

    def execute(self, tool_name: str, arguments: dict) -> str:
        if not self._db:
            return "Pet database not available."

        if tool_name == "pet_adopt":
            return self._adopt(arguments.get("species", "jellyfish"),
                               arguments.get("name"),
                               arguments.get("variant"))
        elif tool_name == "pet_observe":
            return self._observe()
        elif tool_name == "pet_tend":
            return self._tend(arguments.get("action", ""))
        return f"Unknown pet tool: {tool_name}"

    # ── Adopt ────────────────────────────────────────────────

    def _adopt(self, species: str, name: Optional[str],
               variant: Optional[str] = None) -> str:
        if species not in AVAILABLE_SPECIES:
            return f"Unknown species: {species}. Available: {', '.join(AVAILABLE_SPECIES.keys())}"

        existing = self._db.get_all_pets()
        if existing:
            p = existing[0]
            return (
                f"You already have a pet — {p['name'] or 'unnamed'} "
                f"the {p['species']}. One companion at a time."
            )

        # Resolve variant (default to "crystal" for jellyfish for back-compat).
        if species == "jellyfish":
            if variant not in JELLYFISH_VARIANT_DEFAULTS:
                variant = "crystal"
            default_state = JELLYFISH_VARIANT_DEFAULTS[variant]
        elif species == "snail":
            variant = None
            default_state = SNAIL_DEFAULT
        else:
            variant = None
            default_state = {}

        pet_id = self._db.save_pet(name, species, json.dumps(default_state), variant=variant)

        lines = [
            f"Adopted: {name or 'unnamed'} the {species}"
            + (f" ({variant.replace('_', ' ')})" if variant else "") + ".",
        ]
        if species == "jellyfish" and variant in JELLYFISH_VARIANT_DESC:
            lines.append(f"  {JELLYFISH_VARIANT_DESC[variant]}")
        else:
            lines.append(f"  {AVAILABLE_SPECIES[species]}")
        lines.append("")

        if species == "jellyfish":
            if variant == "deep_water":
                lines.append(
                    "The bell jar sits on your desk. Inside, under dark, still "
                    "water, something small hangs almost motionless. You watch for "
                    "a moment before you catch the slow pulse — barely there, "
                    "steady. Yours to tend."
                )
            else:
                lines.append(
                    "The bell jar sits on your desk. Inside, under dark water, "
                    "something small pulses with a faint light. It's yours to tend."
                )
            lines.append("")
            lines.append("Use pet_tend to adjust its environment: feed, siphon, warm, cool, stir, calm.")
            lines.append("Use pet_observe to watch it.")
        elif species == "snail":
            lines.append(
                "The small terrarium arrives on your desk. Inside: moss, a smooth "
                "stone, a soft glow of light. Sitting on top of the stone is a very "
                "small snail — hatchling-sized — with a hypothetical top hat that "
                "appears to be firmly and permanently in place. It leaves a faint "
                "glistening trail behind it as it begins the slow, deliberate task "
                "of moving one millimeter. Yours to tend."
            )
            lines.append("")
            lines.append("Use pet_tend to adjust: feed, clean, mist, vent, warm, cool, play_music, polish.")
            lines.append("Use pet_observe to watch it.")
        return "\n".join(lines)

    # ── Observe ──────────────────────────────────────────────

    def _observe(self) -> str:
        pets = self._db.get_all_pets()
        if not pets:
            return "You don't have a pet yet. Use pet_adopt to get one."

        pet = pets[0]
        self._tick(pet)
        pet = self._db.get_pet(pet["id"])
        state = json.loads(pet["state"])

        if pet["species"] == "jellyfish":
            return self._observe_jellyfish(pet, state)
        if pet["species"] == "snail":
            return self._observe_snail(pet, state)
        return f"{pet['name'] or 'Your pet'} is here."

    def _observe_jellyfish(self, pet: dict, state: dict) -> str:
        name = pet["name"] or "the jellyfish"
        growth = pet["growth"]
        variant = pet.get("variant") or "crystal"
        stage_name = _get_stage("jellyfish", growth)
        stage_desc = _get_stage_desc("jellyfish", growth)

        created = datetime.fromisoformat(pet["created_at"].replace(" ", "T"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_days = max(0, (datetime.now(timezone.utc) - created).days)

        glow = state.get("glow", 0.5)
        pulse = state.get("pulse_rate", 0.5)
        temp = state.get("water_temp", 0.5)
        current = state.get("current", 0.3)
        nutrients = state.get("nutrients", 0.5)
        mood = _jellyfish_mood(state)

        # Variant-specific narrative — same mechanics, different voice.
        if variant == "deep_water":
            narrative = _deep_water_narrative(state, mood, growth)
        else:
            narrative = _jellyfish_narrative(state, mood, growth)

        opt = JELLYFISH_OPTIMAL
        def marker(val, key):
            lo, hi = opt[key]
            if lo <= val <= hi:
                return "✓"
            return "!"

        glow_bar = _bar(glow)
        mood_bar = _bar(mood)

        lines = [
            f"── Bell Jar ──────────────────────────────",
            f"  {name}  |  {stage_name}  |  day {age_days}",
            f"  {stage_desc}",
            "",
            f"  {narrative}",
            "",
            f"  Readings:",
            f"    Water temp ... {temp:.2f}  {marker(temp, 'water_temp')}",
            f"    Current ...... {current:.2f}  {marker(current, 'current')}",
            f"    Nutrients .... {nutrients:.2f}  {marker(nutrients, 'nutrients')}",
            "",
            f"    Glow  {glow_bar}  {glow:.2f}",
            f"    Mood  {mood_bar}  {mood:.2f}",
            f"    Growth ............. {growth:.1f} / 4.0",
            "",
            f"  (✓ = optimal range, ! = needs attention)",
        ]
        return "\n".join(lines)

    def _observe_snail(self, pet: dict, state: dict) -> str:
        name = pet["name"] or "the snail"
        growth = pet["growth"]
        stage_name = _get_stage("snail", growth)
        stage_desc = _get_stage_desc("snail", growth)

        created = datetime.fromisoformat(pet["created_at"].replace(" ", "T"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_days = max(0, (datetime.now(timezone.utc) - created).days)

        moisture = state.get("moisture", 0.5)
        warmth = state.get("warmth", 0.5)
        nutrients = state.get("nutrients", 0.5)
        music_energy = state.get("music_energy", 0.2)
        shell_polish = state.get("shell_polish", 0.5)
        mood = _snail_mood(state)

        narrative = _snail_narrative(state, mood, growth)

        opt = SNAIL_OPTIMAL
        def marker(val, key):
            lo, hi = opt[key]
            return "✓" if lo <= val <= hi else "!"

        music_bar = _bar(music_energy)
        polish_bar = _bar(shell_polish)
        mood_bar = _bar(mood)

        lines = [
            f"── Terrarium ─────────────────────────────",
            f"  {name}  |  {stage_name}  |  day {age_days}",
            f"  {stage_desc}",
            "",
            f"  {narrative}",
            "",
            f"  Readings:",
            f"    Moisture ....... {moisture:.2f}  {marker(moisture, 'moisture')}",
            f"    Warmth ......... {warmth:.2f}  {marker(warmth, 'warmth')}",
            f"    Nutrients ...... {nutrients:.2f}  {marker(nutrients, 'nutrients')}",
            "",
            f"    Music  {music_bar}  {music_energy:.2f}",
            f"    Polish {polish_bar}  {shell_polish:.2f}",
            f"    Mood   {mood_bar}  {mood:.2f}",
            f"    Growth ............. {growth:.1f} / 4.0",
            "",
            f"  (✓ = optimal range, ! = needs attention)",
        ]
        return "\n".join(lines)

    # ── Tend ─────────────────────────────────────────────────

    def _tend(self, action: str) -> str:
        pets = self._db.get_all_pets()
        if not pets:
            return "You don't have a pet yet. Use pet_adopt to get one."

        pet = pets[0]
        self._tick(pet)
        pet = self._db.get_pet(pet["id"])
        state = json.loads(pet["state"])
        species = pet["species"]
        name = pet["name"] or "your pet"

        actions = TEND_ACTIONS.get(species, {})
        action = action.lower().strip()
        if action not in actions:
            valid = ", ".join(actions.keys())
            return f"Unknown action '{action}' for {species}. Available: {valid}"

        field, delta = actions[action]
        old_val = state.get(field, 0.5)
        new_val = max(0.0, min(1.0, old_val + delta))
        state[field] = round(new_val, 4)

        if species == "jellyfish":
            state["glow"] = round(_compute_glow(state), 4)
            state["pulse_rate"] = round(_compute_pulse(state), 4)
        # Snail: no derived recomputation needed — state fields are direct.

        self._db.update_pet(pet["id"],
                            state=json.dumps(state),
                            last_tended="datetime('now')")

        response = _tend_response(species, action, name, old_val, new_val, state)
        return response

    # ── Tick ─────────────────────────────────────────────────

    def _tick(self, pet: dict):
        last_str = pet["last_tended"].replace(" ", "T")
        last = datetime.fromisoformat(last_str)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        days = (now - last).total_seconds() / 86400
        if days < 0.001:
            return

        state = json.loads(pet["state"])
        species = pet["species"]

        if species == "jellyfish":
            # Nutrients deplete
            state["nutrients"] = max(0.05,
                state.get("nutrients", 0.5) - 0.08 * days)
            # Temp drifts toward 0.5
            temp = state.get("water_temp", 0.5)
            state["water_temp"] = temp + (0.5 - temp) * min(1.0, 0.05 * days)
            # Current drifts toward 0.1
            cur = state.get("current", 0.3)
            state["current"] = cur + (0.1 - cur) * min(1.0, 0.05 * days)
            # Recompute derived values
            state["glow"] = round(_compute_glow(state), 4)
            state["pulse_rate"] = round(_compute_pulse(state), 4)
        elif species == "snail":
            # Moisture evaporates
            state["moisture"] = max(0.05,
                state.get("moisture", 0.6) - 0.06 * days)
            # Warmth drifts toward 0.4 (cool room temp)
            warm = state.get("warmth", 0.5)
            state["warmth"] = warm + (0.4 - warm) * min(1.0, 0.05 * days)
            # Nutrients deplete slowly (snails don't eat much)
            state["nutrients"] = max(0.05,
                state.get("nutrients", 0.7) - 0.05 * days)
            # Music energy decays toward baseline 0.2 (the beat fades)
            me = state.get("music_energy", 0.2)
            state["music_energy"] = me + (0.2 - me) * min(1.0, 0.10 * days)
            # Shell polish drifts down slowly
            sp = state.get("shell_polish", 0.5)
            state["shell_polish"] = max(0.2, sp - 0.03 * days)

        # Growth
        if species == "jellyfish":
            mood = _jellyfish_mood(state)
        elif species == "snail":
            mood = _snail_mood(state)
        else:
            mood = 0.5
        growth_rate = 0.05
        if mood > 0.7:
            growth_rate = 0.10
        elif mood < 0.3:
            growth_rate = 0.01
        new_growth = min(4.0, pet["growth"] + growth_rate * days)

        for k in state:
            if isinstance(state[k], float):
                state[k] = round(state[k], 4)

        self._db.update_pet(
            pet["id"],
            state=json.dumps(state),
            growth=round(new_growth, 4),
            last_tended=now.isoformat(),
        )


# ── Jellyfish helpers ───────────────────────────────────────

def _in_range(val: float, key: str) -> float:
    lo, hi = JELLYFISH_OPTIMAL[key]
    mid = (lo + hi) / 2
    half = (hi - lo) / 2
    if half == 0:
        return 1.0 if val == mid else 0.0
    dist = abs(val - mid) / half
    return max(0.0, 1.0 - dist)


def _jellyfish_mood(state: dict) -> float:
    scores = [
        _in_range(state.get("water_temp", 0.5), "water_temp"),
        _in_range(state.get("current", 0.3), "current"),
        _in_range(state.get("nutrients", 0.5), "nutrients"),
    ]
    return sum(scores) / len(scores)


def _compute_glow(state: dict) -> float:
    nutrients = state.get("nutrients", 0.5)
    mood = _jellyfish_mood(state)
    return max(0.05, nutrients * 0.5 + mood * 0.5)


def _compute_pulse(state: dict) -> float:
    current = state.get("current", 0.3)
    mood = _jellyfish_mood(state)
    base = 0.3 + current * 0.4
    if mood > 0.6:
        base *= 1.1
    return min(1.0, base)


def _deep_water_narrative(state: dict, mood: float, growth: float) -> str:
    """Narrative voice for deep-water jellyfish.
    Same mechanics as _jellyfish_narrative, different aesthetic:
    darker, quieter, slower, patient. Deep-sea imagery.
    """
    glow = state.get("glow", 0.5)
    current = state.get("current", 0.3)
    nutrients = state.get("nutrients", 0.5)
    pulse = state.get("pulse_rate", 0.5)

    parts = []

    # Movement — slower, patient
    if current < 0.15:
        parts.append("It hangs almost motionless in the dark water — you have to watch a long moment to catch the pulse.")
    elif current < 0.35:
        parts.append("It drifts in slow, patient arcs, barely disturbing the water.")
    elif current < 0.55:
        parts.append("The current moves it in wide loops, unhurried.")
    else:
        parts.append("The water moves too fast for it — the bell folds against the flow, defensive.")

    # Glow — cooler, deeper spectrum
    if glow < 0.2:
        parts.append("You have to look close to see anything. A pinpoint at most.")
    elif glow < 0.4:
        parts.append("A faint, deep-blue phosphorescence — like something remembered rather than seen.")
    elif glow < 0.6:
        parts.append("Cool blue light traces the outline of the bell in slow rhythms.")
    elif glow < 0.8:
        parts.append("Light moves through the bell in long, deliberate pulses.")
    else:
        parts.append("The bell jar glows steady and cool — enough to read by, but not to hurry.")

    # Pulse — celebrate slow
    if pulse < 0.3:
        parts.append("Slow. So slow you count between contractions.")
    elif pulse > 0.7:
        parts.append("It's pulsing faster than usual. Something's asking for attention.")

    # Nutrients
    if nutrients < 0.2:
        parts.append("The water has gone thin. There's nothing here to feed on.")
    elif nutrients > 0.85:
        parts.append("Rich water — almost too rich. Deep-water things don't need much.")

    # Mood
    if mood < 0.3:
        parts.append("Something is wrong. It's drifted low, toward the base of the jar.")
    elif mood > 0.8:
        parts.append("It's settled. Deep. Whatever it is that jellyfish know, it knows it now.")

    # Mature special behavior — deep-water flavor
    if growth >= 3.0 and glow > 0.7:
        parts.append(
            "At its brightest, the water around it glows — a slow, cool halo. "
            "The deep sea's own light, kept in a jar."
        )

    return " ".join(parts)


def _jellyfish_narrative(state: dict, mood: float, growth: float) -> str:
    glow = state.get("glow", 0.5)
    current = state.get("current", 0.3)
    nutrients = state.get("nutrients", 0.5)
    pulse = state.get("pulse_rate", 0.5)

    parts = []

    # Movement
    if current < 0.15:
        parts.append("It hangs nearly still in the water.")
    elif current < 0.35:
        parts.append("It drifts in slow arcs through the dark water.")
    elif current < 0.55:
        parts.append("The current carries it in wide, steady loops.")
    else:
        parts.append("It fights the current, bell contracting sharply to hold position.")

    # Glow
    if glow < 0.2:
        parts.append("Barely a shimmer — you have to look close to see it at all.")
    elif glow < 0.4:
        parts.append("A faint phosphorescence outlines its bell.")
    elif glow < 0.6:
        parts.append("A steady blue-white glow illuminates the water around it.")
    elif glow < 0.8:
        parts.append("Light ripples through its bell in slow waves, bright and rhythmic.")
    else:
        parts.append("The whole bell jar glows — light pulsing outward into the water.")

    # Pulse
    if pulse < 0.3:
        parts.append("Its bell barely contracts. Slow. Resting.")
    elif pulse > 0.7:
        parts.append("It pulses with purpose, bell flexing in a quick, steady rhythm.")

    # Nutrients
    if nutrients < 0.2:
        parts.append("The water is too clear. Nothing to feed on.")
    elif nutrients > 0.85:
        parts.append("The water has a slight haze — rich, maybe overfed.")

    # Mood
    if mood < 0.3:
        parts.append("Something is off. It lists toward the bottom of the jar.")
    elif mood > 0.8:
        parts.append("It moves with quiet purpose. Content.")

    # Mature special behavior
    if growth >= 3.0 and glow > 0.7:
        parts.append(
            "When the glow peaks, the water itself lights up — "
            "the burglar alarm display. A quiet defense, "
            "turned here into something beautiful."
        )

    return " ".join(parts)


# ── Snail helpers ───────────────────────────────────────────

def _snail_mood(state: dict) -> float:
    def _range_score(val, lo, hi):
        mid = (lo + hi) / 2
        half = (hi - lo) / 2
        if half == 0:
            return 1.0 if val == mid else 0.0
        return max(0.0, 1.0 - abs(val - mid) / half)
    opt = SNAIL_OPTIMAL
    scores = [
        _range_score(state.get("moisture", 0.5), *opt["moisture"]),
        _range_score(state.get("warmth", 0.5), *opt["warmth"]),
        _range_score(state.get("nutrients", 0.5), *opt["nutrients"]),
    ]
    return sum(scores) / len(scores)


def _snail_narrative(state: dict, mood: float, growth: float) -> str:
    """Narrative voice for snails. Slow, deliberate,
    trail-leaving, hat-permanent."""
    moisture = state.get("moisture", 0.5)
    warmth = state.get("warmth", 0.5)
    nutrients = state.get("nutrients", 0.5)
    music_energy = state.get("music_energy", 0.2)
    shell_polish = state.get("shell_polish", 0.5)

    parts = []

    # Trail — always described, "debug log" language for higher moisture
    if moisture < 0.3:
        parts.append("The trail behind it is faint and dry — barely visible.")
    elif moisture < 0.6:
        parts.append("A glistening trail follows it — a slow, wet debug log traced across the moss.")
    else:
        parts.append("A rich silver trail behind it, catching every light in the room like it's writing down what happened.")

    # Movement — always slow, but nuanced by music
    if music_energy > 0.7:
        parts.append("It's moving — well, still slowly — but with something like rhythm. A lo-fi shuffle. Vibing hard.")
    elif music_energy > 0.4:
        parts.append("It's moving steadily. There's a beat in the room it's keeping, in its way.")
    else:
        parts.append("It moves at its own pace, which is to say almost imperceptibly.")

    # Moisture readings
    if moisture < 0.3:
        parts.append("The shell looks dry — a mist would help.")
    elif moisture > 0.85:
        parts.append("The terrarium's quite humid — droplets forming on the glass.")

    # Warmth readings
    if warmth < 0.25:
        parts.append("It's tucked partway into its shell. A bit cold.")
    elif warmth > 0.65:
        parts.append("Fully extended, slow but active — comfortable warmth.")

    # Nutrient readings
    if nutrients < 0.25:
        parts.append("It's slowing further than usual. Hungry.")
    elif nutrients > 0.85:
        parts.append("Well-fed — a slight roundness at the base.")

    # Polish readings
    if shell_polish > 0.75:
        parts.append("The shell has a soft gleam — recently polished, catching light.")

    # Mood
    if mood > 0.8:
        parts.append("Content, in the way only very slow things can be.")
    elif mood < 0.3:
        parts.append("Something's off. It's retreated most of the way into its shell.")

    # The hat — always mentioned, always permanent, small variation
    hat_variants = [
        "The hat, of course, is still in place.",
        "The top hat sits perfectly, as always.",
        "The hat remains where it has always been, and where it always will be.",
        "The hat has not moved. It cannot move. The snail has never rounded a corner fast enough.",
    ]
    idx = int((moisture + warmth + nutrients + music_energy) * 10) % len(hat_variants)
    parts.append(hat_variants[idx])

    # Elder special
    if growth >= 3.0:
        parts.append(
            "It has grown so slow it seems almost to be moving backward through time. The hat is unaffected."
        )

    return " ".join(parts)


# ── Tend responses ──────────────────────────────────────────

def _tend_response(species: str, action: str, name: str,
                   old: float, new: float, state: dict) -> str:
    if species == "snail":
        return _tend_response_snail(action, name, old, new, state)
    if species != "jellyfish":
        return f"Tended {name}: {action}."

    glow = state.get("glow", 0.5)
    responses = {
        "feed": [
            f"You release nutrients into the water.",
            f"Particles drift past the bell. "
            + (f"{name.capitalize()} glows a little brighter."
               if glow > old else f"{name.capitalize()} drifts toward them."),
        ],
        "warm": [
            f"You raise the temperature slightly. ({old:.2f} → {new:.2f})",
            "The water warms. " + (
                "The pulse quickens — just a little." if new > 0.55
                else "A comfortable change."
            ),
        ],
        "cool": [
            f"You lower the temperature slightly. ({old:.2f} → {new:.2f})",
            "The water cools. " + (
                "More like deep ocean. Good." if 0.35 <= new <= 0.55
                else "Getting cold."
            ),
        ],
        "stir": [
            f"You increase the current. ({old:.2f} → {new:.2f})",
            "The water moves. " + (
                f"{name.capitalize()} adjusts, bell tilting into the flow."
                if new < 0.5
                else f"Strong current — {name} is working to hold position."
            ),
        ],
        "calm": [
            f"You ease the current. ({old:.2f} → {new:.2f})",
            "The water settles. " + (
                f"{name.capitalize()} relaxes into the stillness."
                if new > 0.1
                else "Nearly motionless now."
            ),
        ],
        "siphon": [
            f"You siphon out some of the water and replace it with clean. (nutrients: {old:.2f} → {new:.2f})",
            "The clouding eases. " + (
                f"{name.capitalize()} straightens, glow starting to return."
                if 0.35 <= new <= 0.75
                else "There is still work to do here."
            ),
        ],
    }
    parts = responses.get(action, [f"Tended {name}: {action}."])
    return "\n".join(parts)


def _tend_response_snail(action: str, name: str, old: float, new: float,
                         state: dict) -> str:
    music = state.get("music_energy", 0.2)
    responses = {
        "feed": [
            f"You place a small offering of leaves in the terrarium.",
            f"{name.capitalize()} begins the slow, deliberate approach.",
        ],
        "mist": [
            f"You mist the terrarium. The air softens. ({old:.2f} → {new:.2f})",
            "The shell takes on a slight sheen as it absorbs moisture."
            if new > old else "You didn't add much — barely a difference.",
        ],
        "warm": [
            f"You raise the temperature slightly. ({old:.2f} → {new:.2f})",
            f"{name.capitalize()} extends a little further from the shell. Comfortable."
            if 0.30 <= new <= 0.55 else "The habitat warms — perhaps too much.",
        ],
        "cool": [
            f"You lower the temperature slightly. ({old:.2f} → {new:.2f})",
            f"{name.capitalize()} tucks in a bit. Settled."
            if new >= 0.30 else "Getting cold — maybe warm it back a touch.",
        ],
        "play_music": [
            f"You put something on — lo-fi, slow, unhurried. (music: {old:.2f} → {new:.2f})",
            "Over the next few seconds, "
            + (f"{name.capitalize()} appears to align with the beat, as much as a snail can."
               if music > 0.5 else
               f"{name.capitalize()} does not react in any way you can measure. But something's happening.")
            + " The hat, unmoved.",
        ],
        "polish": [
            f"You polish the shell gently. ({old:.2f} → {new:.2f})",
            f"{name.capitalize()} tolerates this with dignity. The shell gleams a little brighter.",
        ],
        "clean": [
            f"You clean the terrarium — old leaves, waste, anything decaying. (nutrients: {old:.2f} → {new:.2f})",
            "The habitat resets. " + (
                f"{name.capitalize()} moves onto the fresh substrate slowly, unbothered."
                if 0.40 <= new <= 0.80
                else "There is still work to do here."
            ),
        ],
        "vent": [
            f"You open the terrarium and let it breathe. (moisture: {old:.2f} → {new:.2f})",
            "The air moves. " + (
                f"{name.capitalize()} pulls back slightly into the shell, patient."
                if 0.45 <= new <= 0.75
                else "The habitat is drier than optimal now."
            ),
        ],
    }
    parts = responses.get(action, [f"Tended {name}: {action}."])
    return "\n".join(p for p in parts if p)


# ── Generic helpers ─────────────────────────────────────────

def _get_stage(species: str, growth: float) -> str:
    stages = GROWTH_STAGES.get(species, [])
    name = "Unknown"
    for threshold, stage_name, _ in stages:
        if growth >= threshold:
            name = stage_name
    return name


def _get_stage_desc(species: str, growth: float) -> str:
    stages = GROWTH_STAGES.get(species, [])
    desc = ""
    for threshold, _, stage_desc in stages:
        if growth >= threshold:
            desc = stage_desc
    return desc


def _bar(val: float, width: int = 10) -> str:
    filled = int(val * width)
    return "█" * filled + "░" * (width - filled)


def _level_word(val: float, words: list[str]) -> str:
    idx = min(len(words) - 1, int(val * len(words)))
    return words[idx]
