#!/usr/bin/env python3
"""stop_watch.py — a per-persona log of how Anthropic replies end.

Records every Anthropic API completion's stop_reason so you can see
classifier hard-stops (stop_reason == "refusal"), API errors / dropped
replies, and ordinary flakiness, per persona, over time.

Lives in core/ next to llm_anthropic.py; logs to data/stop_watch.jsonl in the
Pulse folder. Nothing is stored but the last 40 chars of the reply (for the
mid-sentence check), the stop_reason, flags, model, persona, and a timestamp.
No other message content.

Integration — one line inside AnthropicClient._track (core/llm_anthropic.py),
which already runs at every completion, plus one in each except block:

    from core.stop_watch import record
    record(self.model_name, response=response, usage_tracker=self._usage)   # success
    record(self.model_name, error=str(e), usage_tracker=self._usage)        # failure

record() NEVER raises. A failure to log (bad disk, bad response object,
anything) can never break a companion's reply: it watches, it does not block.

Read the log with `py stop_watch_report.py` from the Pulse folder.
"""
import json
import re
import time
from pathlib import Path

# pulse/data/stop_watch.jsonl — anchored to the pulse root via THIS file's
# location (core/ -> its parent is pulse/), so the recorder and the reporter
# agree on one absolute path no matter what the working directory is. If this
# file ever moves out of core/, fix the .parent.parent below.
_PULSE_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = _PULSE_ROOT / "data" / "stop_watch.jsonl"

# Stop reasons the API returns on a normal, healthy completion.
NORMAL = {"end_turn", "stop_sequence", "tool_use", "pause_turn"}
# ...of those, the ones that END a turn mid-thought ON PURPOSE. A reply that
# "ends mid-sentence" on one of these is expected, not a clip, so it must not
# raise an eyebrow. (tool_use: handing off to a tool. pause_turn: the model
# paused to continue. stop_sequence: hit a configured stop string.)
_EXPECTED_CUT = {"tool_use", "pause_turn", "stop_sequence"}

# A reply that ends "properly": terminal punctuation, a closing bracket or
# quote, a markdown delimiter (* _ ~ `), or an emoji / dingbat (replies often
# end on one). Checked against the final few chars only.
_SENT_END = re.compile(
    r'[.!?…)\]}"\'»*_~`]\s*$'
    r'|[\U0001F300-\U0001FAFF☀-➿←-⇿️‍]\s*$'
)


def _mid_sentence(text: str) -> bool:
    """True if the reply looks cut off mid-thought. Deliberately lenient:
    this only drives the quiet 'soft' flag, never the 'hard' one."""
    t = (text or "").rstrip()
    if len(t) < 4:
        # Very short replies ("Ok", "No", "Yep") read as unfinished to the
        # regex but almost never are. Don't cry wolf on them.
        return False
    return not _SENT_END.search(t[-6:])


def _extract_text(response) -> str:
    """Pull the text from an Anthropic response's content blocks. Never raises."""
    try:
        parts = []
        for block in getattr(response, "content", None) or []:
            if getattr(block, "type", None) == "text":
                parts.append(getattr(block, "text", "") or "")
        return "".join(parts)
    except Exception:
        return ""


def _resolve_persona(usage_tracker, fallback: str = "") -> str:
    """Best-effort name of the persona this call belongs to. Never raises.

    The Anthropic client doesn't carry a persona string, but the usage tracker
    it holds is built per-persona, so its config dict (and, failing that, its
    per-persona DB path) is the surest local source of identity. If nothing
    resolves, we return the fallback and the report simply groups by model.
    """
    if usage_tracker is None:
        return fallback
    cfg = None
    try:
        c = getattr(usage_tracker, "config", None)
        cfg = c if isinstance(c, dict) else None
    except Exception:
        cfg = None

    if cfg is not None:
        # 1) a human-readable name at the TOP level, if any persona has one.
        #    (NB: never user_name / user_nickname — that's the user, not the persona.)
        try:
            for key in ("display_name", "name", "persona", "persona_name"):
                val = cfg.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        except Exception:
            pass
        # 2) the display name a persona config may carry, nested at
        #    channels.lor.author_name.
        try:
            lor = (cfg.get("channels") or {}).get("lor") or {}
            val = lor.get("author_name") if isinstance(lor, dict) else None
            if isinstance(val, str) and val.strip():
                return val.strip()
        except Exception:
            pass
        # 3) the persona folder name pulse.py records on the merged config.
        try:
            val = cfg.get("_persona_name")
            if isinstance(val, str) and val.strip():
                return val.strip()
        except Exception:
            pass

    # 4) fall back to the slug in the per-persona DB path
    #    ("personas/<name>/data/<name>.db" -> "<name>"). Use only the
    #    private per-persona _db, never the _shared_db. Skip generic stems.
    _GENERIC = {"data", "", ".", "pulse", "personas", "db", "memory",
                "usage", "legacy", "database", "main", "store"}
    try:
        db = getattr(usage_tracker, "_db", None)
        dbp = getattr(db, "db_path", None)
        if dbp is not None:
            p = Path(str(dbp))
            parts = list(p.parts)
            low = [x.lower() for x in parts]
            # Most reliable: the directory directly under "personas/".
            if "personas" in low:
                i = low.index("personas")
                if i + 1 < len(parts) and low[i + 1] not in _GENERIC:
                    return parts[i + 1]
            # Otherwise walk up from the file, first non-generic component wins.
            for cand in [p.stem] + [par.name for par in p.parents]:
                if cand and cand.lower() not in _GENERIC:
                    return cand
    except Exception:
        pass
    return fallback


def record(model: str = "", response=None, stop_reason: str = "",
           text: str = "", persona: str = "", error: str = "",
           usage_tracker=None) -> dict:
    """Append one completion's vital signs to the log. NEVER raises.

    Pass either a `response` (stop_reason + text are read off it) or explicit
    `stop_reason`/`error`. Returns the row dict (empty {} only if even the
    backstop had to catch something)."""
    try:
        if response is not None:
            stop_reason = stop_reason or (getattr(response, "stop_reason", None) or "")
            if not text:
                text = _extract_text(response)
            if not model:
                model = getattr(response, "model", "") or ""
        if not persona:
            persona = _resolve_persona(usage_tracker, fallback="")

        stop = stop_reason or ("error" if error else "unknown")
        mid = _mid_sentence(text)

        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "persona": persona or "?",
            "model": model or "?",
            "stop_reason": stop,
            "mid_sentence": mid,
            "tail": (text or "")[-40:],
            "error": (error or "")[:200],
        }
        # HARD = a real clip you'd want to know about tonight: the classifier
        # refused, or the call errored / dropped mid-reply.
        row["hard"] = (
            stop == "refusal"
            or stop in ("error", "unknown")
            or bool(error)
        )
        # SOFT = a reply that merely didn't end on punctuation, on an otherwise
        # normal stop. Mostly stylistic trailing-off; tracked, but quiet.
        row["soft"] = (not row["hard"]) and (
            (stop in NORMAL and stop not in _EXPECTED_CUT and mid)
            or (stop == "max_tokens" and mid)
        )
        row["flag"] = bool(row["hard"] or row["soft"])

        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            # Disk full, permissions, a path race — give up silently. Logging
            # must never be the thing that breaks a reply.
            pass
        return row
    except Exception:
        # Absolute backstop: whatever happened, do not propagate to the caller.
        return {}
