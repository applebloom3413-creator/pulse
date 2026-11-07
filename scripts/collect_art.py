"""
collect_art — gathers every painting the companions make into one easy folder
for printing:

  <art dir>/<Companion>/<date> <Title>.png             big + crisp (2400 px)
  <art dir>/<Companion>/For printing/<date> <Title>.png  letter-size page with a
                                                         border + title, artist,
                                                         date and their caption
  <art dir>/<Companion>/Garden/...                      their garden pictures

<art dir> is $PULSE_ART_DIR if set, otherwise "~/Pulse Art".

Pixel art is scaled with hard edges (no blur), so prints stay sharp.
Only new paintings are made each run, so it's quick. Read-only on Pulse.
Can be scheduled nightly; the Pulse Hub's Art button also runs it.

Run:  py -3.14 scripts/collect_art.py
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame

PULSE = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("PULSE_ART_DIR") or (Path.home() / "Pulse Art"))
BIG = 2400                                   # long side of the crisp copy
PAGE = (2550, 3300)                          # 8.5 x 11 in at 300 dpi


def companion_names() -> dict:
    """persona folder -> display name (LoR author name, else persona name, else folder)"""
    import yaml
    names = {}
    for cfg in (PULSE / "personas").glob("*/config.yaml"):
        if cfg.parent.name.startswith("_"):
            continue
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        n = ((data.get("channels") or {}).get("lor") or {}).get("author_name")
        if not n:
            for ident in ("persona.yaml", "persona.json"):
                ip = cfg.parent / ident
                if ip.exists():
                    try:
                        n = (yaml.safe_load(ip.read_text(encoding="utf-8")) or {}).get("name")
                    except Exception:
                        n = None
                    break
        names[cfg.parent.name] = n or cfg.parent.name.title()
    return names


def safe(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\n\r\t]+', "", s).strip(" .")[:80] or "untitled"


def nice_date(iso: str) -> str:
    try:
        d = datetime.fromisoformat(iso)
        return f"{d:%B} {d.day}, {d.year}"
    except Exception:
        return ""


def crisp(img: pygame.Surface, long_side: int) -> pygame.Surface:
    w, h = img.get_size()
    k = max(1, long_side // max(w, h))                         # whole-number scale = perfectly sharp pixels
    return pygame.transform.scale(img, (w * k, h * k))


def wrap(font, text: str, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        test = (line + " " + word).strip()
        if font.size(test)[0] <= width:
            line = test
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def print_page(art: pygame.Surface, title: str, artist: str, date: str, caption: str) -> pygame.Surface:
    page = pygame.Surface(PAGE)
    page.fill((255, 255, 255))
    pic = crisp(art, 2000)
    if max(pic.get_size()) > 2000:
        pic = pygame.transform.scale(pic, (pic.get_width() * 2000 // max(pic.get_size()),
                                           pic.get_height() * 2000 // max(pic.get_size())))
    x = (PAGE[0] - pic.get_width()) // 2
    y = 260
    pygame.draw.rect(page, (235, 228, 222), (x - 24, y - 24, pic.get_width() + 48, pic.get_height() + 48))
    page.blit(pic, (x, y))
    yy = y + pic.get_height() + 140
    f_title = pygame.font.SysFont("georgia", 96, bold=True)
    f_by = pygame.font.SysFont("georgia", 58)
    f_cap = pygame.font.SysFont("georgia", 50, italic=True)
    for surf_ in (f_title.render(title, True, (60, 45, 40)),
                  f_by.render(f"by {artist}" + (f"  ·  {date}" if date else ""), True, (140, 110, 100))):
        page.blit(surf_, surf_.get_rect(midtop=(PAGE[0] // 2, yy)))
        yy += surf_.get_height() + 30
    if caption:
        yy += 30
        for line in wrap(f_cap, f"“{caption}”", PAGE[0] - 500)[:6]:
            s = f_cap.render(line, True, (110, 90, 85))
            page.blit(s, s.get_rect(midtop=(PAGE[0] // 2, yy)))
            yy += s.get_height() + 12
    return page


def collect(verbose: bool = True) -> int:
    pygame.init()
    if not pygame.display.get_init() or pygame.display.get_surface() is None:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    names = companion_names()
    sources = [(names.get(p.parent.parent.name, p.parent.parent.name.title()), p)
               for p in (PULSE / "personas").glob("*/data/paintings")]
    sources.append(("Pulse", PULSE / "data" / "paintings"))
    made = 0
    for artist, folder in sorted(sources):
        index = folder / "paintings.json"
        if not index.exists():
            continue
        try:
            entries = json.loads(index.read_text(encoding="utf-8"))
        except Exception:
            continue
        dest = OUT / safe(artist)
        for e in entries:
            src = folder / (e.get("true_path") or "")
            if not src.is_file():
                continue
            stem = f"{(e.get('date') or '')[:10]} {safe(e.get('title') or e.get('id') or src.stem)}".strip()
            big, page = dest / f"{stem}.png", dest / "For printing" / f"{stem}.png"
            if big.exists() and page.exists() and big.stat().st_mtime >= src.stat().st_mtime:
                continue
            art = pygame.image.load(str(src))
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "For printing").mkdir(exist_ok=True)
            pygame.image.save(crisp(art, BIG), str(big))
            pygame.image.save(print_page(art, e.get("title") or "Untitled", artist,
                                         nice_date(e.get("date") or ""), e.get("caption") or ""), str(page))
            made += 1
            if verbose:
                print(f"  🎨 {artist}: {e.get('title')}")
    # garden pictures
    for folder in list((PULSE / "personas").glob("*/data/garden")) + [PULSE / "data" / "garden"]:
        artist = "Pulse" if folder.parent.name == "data" and folder.parent.parent == PULSE else \
            names.get(folder.parent.parent.name, folder.parent.parent.name.title())
        for f in folder.glob("*.png"):
            target = OUT / safe(artist) / "Garden" / f.name
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(f.read_bytes())
                made += 1
    (OUT / "README.txt").write_text(
        "Everything the companions have painted, one folder each.\n"
        "The big PNGs are crisp for any size. 'For printing' has letter-size pages\n"
        "with a border, the title, who made it and their caption - just print them.\n"
        "New paintings show up here each time the collector runs.\n", encoding="utf-8")
    return made


if __name__ == "__main__":
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    n = collect()
    print(f"Done - {n} new picture{'s' if n != 1 else ''} in {OUT}")
