"""
Sanitizer tests for the draw skill — verifies the MVP
hardening rules. Run:

    py -3.14 scripts/test_draw_sanitize.py

Prints pass/fail per case, exits 0 if all pass, 1 if any fail.

Standalone script (no pytest dependency) so anyone can just run it.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

# Console safety — Windows cp1252 chokes on ✓/✗ otherwise
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

# Add pulse root to sys.path so `from skills.draw import ...` works
_HERE = Path(__file__).resolve().parent
_PULSE_ROOT = _HERE.parent
sys.path.insert(0, str(_PULSE_ROOT))

from skills.draw import (
    sanitize_svg,
    SVGRejected,
    MAX_SVG_BYTES,
    MAX_ELEMENT_COUNT,
    MAX_PATH_D_BYTES,
)


# ── Test cases (hardening spec) ──────────────────────

def test_valid_svg_survives():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<circle cx="50" cy="50" r="40" fill="red"/></svg>'
    )
    out, report = sanitize_svg(svg)
    assert "circle" in out, f"circle should survive: {out!r}"
    assert "red" in out, f"fill should survive: {out!r}"
    assert not report["stripped_elements"], report
    assert not report["stripped_attrs"], report


def test_internal_href_survives():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<defs><linearGradient id="grad"><stop offset="0" stop-color="red"/></linearGradient></defs>'
        '<rect width="100" height="100" fill="url(#grad)"/>'
        '<a href="#top"><text>go</text></a></svg>'
    )
    out, report = sanitize_svg(svg)
    assert "#top" in out, f"internal href='#top' should survive: {out!r}"
    assert not any("external-href" in a for a in report["stripped_attrs"])


def test_script_stripped():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<script>alert(1)</script><circle r="10"/></svg>'
    )
    out, report = sanitize_svg(svg)
    assert "script" not in out.lower(), f"<script> should be gone: {out!r}"
    assert "alert" not in out.lower(), f"script contents should be gone: {out!r}"
    assert "script" in report["stripped_elements"], report


def test_foreignobject_stripped():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<foreignObject><div xmlns="http://www.w3.org/1999/xhtml">evil</div></foreignObject>'
        '<circle r="5"/></svg>'
    )
    out, report = sanitize_svg(svg)
    assert "foreignObject" not in out, f"<foreignObject> should be gone: {out!r}"
    assert "evil" not in out.lower(), f"contents should be gone: {out!r}"
    assert "foreignObject" in report["stripped_elements"], report


def test_event_attrs_stripped():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<rect onclick="alert(1)" onload="steal()" width="10" height="10"/></svg>'
    )
    out, report = sanitize_svg(svg)
    assert "onclick" not in out.lower()
    assert "onload" not in out.lower()
    assert "alert" not in out.lower()
    assert "steal" not in out.lower()
    assert any("event:onclick" in a for a in report["stripped_attrs"]), report


def test_external_href_stripped():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<a href="https://evil.example"><text>click</text></a></svg>'
    )
    out, report = sanitize_svg(svg)
    assert "evil.example" not in out
    assert any("external-href" in a for a in report["stripped_attrs"]), report


def test_external_xlink_href_stripped():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
        '<image xlink:href="http://tracker.example/pixel.gif" width="1" height="1"/></svg>'
    )
    out, report = sanitize_svg(svg)
    assert "tracker.example" not in out
    assert any("external-href" in a for a in report["stripped_attrs"]), report


def test_malformed_rejected():
    try:
        sanitize_svg('<svg xmlns="http://www.w3.org/2000/svg"><circle unclosed>')
    except SVGRejected as e:
        assert "malformed" in str(e).lower(), f"expected 'malformed', got: {e}"
        return
    raise AssertionError("expected SVGRejected for malformed XML")


def test_oversized_source_rejected():
    # 20,000 <circle/>s is > 128 KB
    body = "<circle/>" * 20_000
    svg = f'<svg xmlns="http://www.w3.org/2000/svg">{body}</svg>'
    try:
        sanitize_svg(svg)
    except SVGRejected:
        return
    raise AssertionError("expected SVGRejected for oversized source")


def test_oversized_element_count_rejected():
    # Try to trip element count independent of byte cap. Very small elements,
    # count over cap. Even if bytes trip first, either rejection is correct.
    body = "<g/>" * (MAX_ELEMENT_COUNT + 100)
    svg = f'<svg xmlns="http://www.w3.org/2000/svg">{body}</svg>'
    try:
        sanitize_svg(svg)
    except SVGRejected:
        return
    raise AssertionError("expected SVGRejected for oversized element count")


def test_oversized_path_data_rejected():
    # Single <path> with a d= larger than MAX_PATH_D_BYTES.
    # Careful: total source can trip byte cap first, that's still a valid rejection.
    d_repeat = "L1,1 "
    d = "M0,0 " + d_repeat * ((MAX_PATH_D_BYTES // len(d_repeat)) + 100)
    svg = f'<svg xmlns="http://www.w3.org/2000/svg"><path d="{d}"/></svg>'
    try:
        sanitize_svg(svg)
    except SVGRejected:
        return
    raise AssertionError("expected SVGRejected for oversized path data")


def test_namespace_preserved():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<rect width="50" height="50" fill="blue"/></svg>'
    )
    out, _ = sanitize_svg(svg)
    assert "<svg" in out or "<ns0:svg" in out, f"root svg tag should survive: {out!r}"
    # Either default-namespace form OR ns-prefix form is acceptable — both parse as SVG
    assert "svg" in out.lower()
    assert "http://www.w3.org/2000/svg" in out, f"xmlns should survive: {out!r}"


def test_report_logs_categories_not_payloads():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<script>super.secret.payload("dont log me")</script></svg>'
    )
    _, report = sanitize_svg(svg)
    # Report should say 'script' stripped but never leak the payload
    all_report_strs = " ".join(
        report["stripped_elements"] + report["stripped_attrs"]
    )
    assert "script" in all_report_strs
    assert "super.secret" not in all_report_strs
    assert "dont log me" not in all_report_strs


# ── Runner ──────────────────────────────────────────────────────

TESTS = [
    ("valid ordinary SVG survives",             test_valid_svg_survives),
    ("internal href='#fragment' survives",      test_internal_href_survives),
    ("<script> removed",                        test_script_stripped),
    ("<foreignObject> removed",                 test_foreignobject_stripped),
    ("on* event attrs removed",                 test_event_attrs_stripped),
    ("external href removed",                   test_external_href_stripped),
    ("external xlink:href removed",             test_external_xlink_href_stripped),
    ("malformed XML rejected cleanly",          test_malformed_rejected),
    ("oversized source rejected",               test_oversized_source_rejected),
    ("oversized element count rejected",        test_oversized_element_count_rejected),
    ("oversized path data rejected",            test_oversized_path_data_rejected),
    ("namespace preserved after sanitize",      test_namespace_preserved),
    ("report logs categories, not payloads",    test_report_logs_categories_not_payloads),
]


def main():
    passed = 0
    failed = []
    print(f"Running {len(TESTS)} draw-sanitizer tests...\n")
    for desc, fn in TESTS:
        try:
            fn()
            print(f"  ✓ {desc}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {desc}: {e}")
            failed.append((desc, str(e)))
        except Exception as e:
            print(f"  ✗ {desc}: unexpected {type(e).__name__}: {e}")
            failed.append((desc, f"{type(e).__name__}: {e}"))

    print(f"\n{passed}/{len(TESTS)} tests passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
