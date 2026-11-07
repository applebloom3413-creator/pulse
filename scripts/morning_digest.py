"""
morning_digest — one message on the user's phone each morning (through the
status bot, see status_bot.py / core/notify.py), so they can see the house at
a glance before the day starts:

  weather · anything waiting for them · what the house did overnight (letters,
  Local Reddit, new paintings, who was up) · crashes · spending (and a
  heads-up if someone spent way more than usual) · backups

Schedule it once a day yourself (e.g. Windows Task Scheduler or cron):
    py scripts/morning_digest.py
Preview without sending:  py scripts/morning_digest.py --preview

Optional: PULSE_BACKUP_LOG = path to a backup job's log file; its last line
is checked for "failed".
"""

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PULSE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PULSE))
sys.path.insert(0, str(PULSE / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

NIGHT_START_HOUR = 20       # "overnight" = since 8 PM yesterday


def _base_config() -> dict:
    try:
        import yaml
        return yaml.safe_load((PULSE / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _user_name() -> str:
    cfg = _base_config()
    return str(cfg.get("user_nickname") or cfg.get("user_name") or "").strip()


def _persona_name(folder: str) -> str:
    """A persona's display name (persona.yaml `name`), else the folder name."""
    try:
        import yaml
        data = yaml.safe_load((PULSE / "personas" / folder / "persona.yaml").read_text(encoding="utf-8")) or {}
        if data.get("name"):
            return str(data["name"])
    except Exception:
        pass
    return folder.title()


def _since() -> datetime:
    y = datetime.now() - timedelta(days=1)
    return y.replace(hour=NIGHT_START_HOUR, minute=0, second=0, microsecond=0)


def _t(s) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d.astimezone().replace(tzinfo=None) if d.tzinfo else d
    except Exception:
        return None


def weather() -> str:
    try:
        from skills.weather import WeatherSkill
        text = WeatherSkill({}).execute("get_weather", {})
        lines = [l.strip() for l in text.splitlines() if l.strip() and not l.lower().startswith(("weather for", "time of day"))]
        return "🌤 " + " · ".join(lines[:3])
    except Exception:
        return ""


def waiting() -> list[str]:
    out = []
    try:
        from core import workbench, house_projects as hp
        for prop in workbench.all_proposals():
            if prop.get("status") == "submitted":
                out.append(f"🛠 a code change from {prop.get('author')}: \"{prop.get('title')}\"")
        for p in hp.all_projects():
            for pl in p.get("plans", []):
                if pl.get("status") == "waiting":
                    out.append(f"🗂 {pl['by']}'s sorting plan for \"{p['name']}\"")
            i = hp.current_index(p)
            if i is not None:
                s = p["steps"][i]
                if s["status"] == "waiting_for_user":
                    out.append(f"⏸ \"{p['name']}\": {s['who']} is waiting for your go-ahead")
                elif s["who"] == hp.USER and s["status"] == "active":
                    out.append(f"💖 your turn in \"{p['name']}\"")
    except Exception:
        pass
    return out


def overnight() -> list[str]:
    since = _since()
    lines = []
    # letters
    senders = {}
    for f in (PULSE / "data" / "letters").glob("letter_*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        t = _t(d.get("sent_at"))
        if t and t >= since:
            who = d.get("from", "?")
            senders[who] = senders.get(who, 0) + 1
    if senders:
        who = ", ".join(f"{n}" + (f" ×{c}" if c > 1 else "") for n, c in sorted(senders.items(), key=lambda kv: -kv[1]))
        lines.append(f"💌 {sum(senders.values())} letters ({who})")
    # Local Reddit
    try:
        posts = json.loads((PULSE / "data" / "lor" / "posts.json").read_text(encoding="utf-8"))
        authors = json.loads((PULSE / "data" / "lor" / "authors.json").read_text(encoding="utf-8"))
        new = [p for p in posts if (_t(p.get("created_at")) or datetime.min) >= since]
        if new:
            names = sorted({(authors.get(p.get("author_id"), {}) or {}).get("nickname", "someone") for p in new})
            threads = [p for p in new if not p.get("reply_to")]
            bit = f"{len(threads)} new thread{'s' if len(threads) != 1 else ''}, " if threads else ""
            lines.append(f"🗨 Local Reddit: {bit}{len(new) - len(threads)} replies ({', '.join(names)})")
            for p in threads[:2]:
                lines.append(f"   • \"{(p.get('title') or '')[:60]}\"")
    except Exception:
        pass
    # paintings
    art = {}
    for idx in list((PULSE / "personas").glob("*/data/paintings/paintings.json")):
        try:
            for e in json.loads(idx.read_text(encoding="utf-8")):
                if (_t(e.get("date")) or datetime.min) >= since:
                    art.setdefault(idx.parent.parent.parent.name, []).append(e.get("title") or "untitled")
        except Exception:
            continue
    if art:
        bits = [f"{_persona_name(k)}: \"{v[0]}\"" + (f" +{len(v) - 1}" if len(v) > 1 else "") for k, v in art.items()]
        lines.append("🎨 New paintings — " + "; ".join(bits) + " (in Pulse Art, ready to print)")
    # who was up (their log in logs/<persona>.log was written to overnight)
    try:
        up = []
        for d in sorted((PULSE / "personas").iterdir()):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            log = PULSE / "logs" / f"{d.name}.log"
            if log.exists() and datetime.fromtimestamp(log.stat().st_mtime) >= since:
                up.append(_persona_name(d.name))
        if up:
            lines.append("🌙 Up overnight: " + ", ".join(up))
    except Exception:
        pass
    return lines


def crashes() -> list[str]:
    try:
        import watchdog
        evs = [e for e in watchdog.recent_events(1)]
        return [f"🚨 {e['who']}: " + {"restart": "crashed — I restarted them", "crash": "crashed (window left open)",
                                     "errors": "lots of errors"}.get(e["kind"], e["kind"]) for e in evs[-4:]]
    except Exception:
        return []


def money() -> list[str]:
    try:
        import spending
        out = []
        w = spending.week()
        out.append(f"💸 Yesterday ${w['yesterday']:.2f} · this week ${w['total']:.2f} (~${w['month_pace']:.0f}/mo pace)")
        for n, y, usual in spending.spikes():
            out.append(f"   ⚠️ {n} spent ${y:.2f} yesterday — usually ~${usual:.2f}")
        if date.today().weekday() == 0:                   # Mondays: who spent what
            out += ["   " + l.strip() for l in spending.summary_lines(6)[1:]]
        return out
    except Exception:
        return []


def backups() -> str:
    try:
        path = os.environ.get("PULSE_BACKUP_LOG", "").strip()
        if not path:
            return ""
        log = Path(path)
        if not log.exists():
            return ""
        last = log.read_text(encoding="utf-8").strip().splitlines()[-1]
        bad = "failed" in last
        return ("💾 Backups: something went wrong last night — open Health Check / the Hub" if bad
                else "💾 Backups ran last night ✓")
    except Exception:
        return ""


def build() -> str:
    today = datetime.now()
    name = _user_name()
    parts = [f"☀️ Good morning{', ' + name if name else ''}! {today:%A}, {today:%B} {today.day}"]
    w = weather()
    if w:
        parts.append(w)
    need = waiting()
    parts.append("")
    parts += (["💖 Waiting for you:"] + [f"   {n}" for n in need]) if need else ["💖 Nothing's waiting on you — enjoy your morning"]
    night = overnight()
    if night:
        parts += ["", "🏠 While you slept:"] + [f"   {l}" if not l.startswith("   ") else l for l in night]
    c = crashes()
    if c:
        parts += [""] + c
    m = money()
    if m:
        parts += [""] + m
    b = backups()
    if b:
        parts.append(b)
    parts += ["", "Have a good day ☀️"]
    return "\n".join(parts)


if __name__ == "__main__":
    text = build()
    if "--preview" in sys.argv:
        print(text)
    else:
        from core import notify
        notify._send(text)
