"""
House Projects — what a relay costs.

Each step already records the tokens its companion used during their turn
(core/workbench.start_usage / finish_usage). This turns those into dollars with
the same price list as scripts/cost_report.py, using the companion's own model.

Budget caps are gentle: a turn in progress always finishes; the relay just
won't START the next person once the project is at its budget — it pauses and
pings the user instead (core/house_projects.step_done).
"""

import importlib.util
from datetime import date, timedelta
from pathlib import Path

PULSE_ROOT = Path(__file__).resolve().parent.parent
_cr = None


def _cost_report():
    global _cr
    if _cr is None:
        spec = importlib.util.spec_from_file_location("cost_report", PULSE_ROOT / "scripts" / "cost_report.py")
        _cr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_cr)
    return _cr


def _model_of(who: str) -> tuple[str, str, str] | None:
    """(folder, provider type, model) for a companion, from their config (root config fills gaps)."""
    from core import workbench
    found = workbench._persona_config(who)
    if not found:
        return None
    folder, cfg = found
    prov = cfg.get("provider") or {}
    try:
        import yaml
        root = (yaml.safe_load((PULSE_ROOT / "config.yaml").read_text(encoding="utf-8")) or {}).get("provider") or {}
    except Exception:
        root = {}
    ptype = (prov.get("type") or root.get("type") or "").lower()
    model = prov.get("model") or root.get("model") or ""
    return folder, ptype, model


def dollars(who: str, used: dict) -> float | None:
    """Dollar cost of a token record {prompt, completion, cache_read, cache_write[, cache_write_5m/1h]},
    or None if unknown."""
    info = _model_of(who)
    if not info or not used:
        return None
    folder, ptype, model = info
    cr = _cost_report()
    row = {"provider": ptype, "model": model, "prompt": used.get("prompt", 0), "output": used.get("completion", 0),
           "cache_read": used.get("cache_read", 0), "cache_write": used.get("cache_write", 0),
           "cache_write_5m": used.get("cache_write_5m", 0), "cache_write_1h": used.get("cache_write_1h", 0)}
    try:
        cost, _ = cr.compute_cost(row, cr.persona_cache_ttl(folder))
    except Exception:
        return None
    return cost


def _live_used(who: str, rec: dict) -> dict | None:
    """Tokens used so far in a turn that hasn't finished yet."""
    from core import workbench
    info = _model_of(who)
    start = (rec or {}).get("start")
    if not info or not start:
        return None
    db_path = PULSE_ROOT / "personas" / info[0] / "data" / f"{info[0]}.db"
    if not db_path.exists():
        return None
    try:
        from core.db import PulseDatabase
        db = PulseDatabase(db_path)
        return workbench.turn_used(rec, db)          # skips time they spent away from the project
    except Exception:
        return None


def step_cost(step: dict, live: bool = False) -> float | None:
    """Cost of one step (all of its token records). live=True includes a turn still in progress."""
    total, known = 0.0, False
    for who, rec in (step.get("tokens") or {}).items():
        used = rec.get("used") or (_live_used(who, rec) if live else None)
        c = dollars(who, used) if used else None
        if c is not None:
            total += c
            known = True
    return total if known else None


def project_cost(proj: dict, live: bool = False) -> float:
    """Finished turns, plus (live=True) the turn happening right now. A turn that never clocked
    out (e.g. skipped before this fix) isn't counted live, or it would keep growing forever."""
    return sum(step_cost(s, live and s.get("status") == "active") or 0.0 for s in proj.get("steps", []))


def clock_out(step: dict):
    """Freeze the tokens of a turn that ended without the companion calling done (the user moved on)."""
    for who, rec in (step.get("tokens") or {}).items():
        if rec.get("used") is None:
            used = _live_used(who, rec)
            if used is not None:
                rec["used"] = used


def money(x: float | None) -> str:
    if x is None:
        return "?"
    return f"${x:,.2f}" if x >= 0.01 or x == 0 else "<$0.01"
