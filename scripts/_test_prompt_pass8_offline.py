"""
Offline test for Prompt Pass 8 changes: no Streamlit, no live network.

Prompt Pass 8 ("prompt1.txt" — nav caps + UI text cleanup):
  1. Sidebar nav labels render ALL CAPS. Originally a CSS injection
     (card_view.inject_nav_caps_css()); since Phase 1 of UI_ACTION_PLAN.md
     the labels are uppercase st.Page titles in dashboard_lib/nav.py and
     the CSS is gone, so this now checks the registry instead.
  2. A UI-facing "Prompt Pass 5:" / "rather than the old plain
     draw-probability" leak was removed from the Land Probability
     page's "Check a specific card" caption. Every OTHER "Prompt Pass
     N" / "Phase N" mention in the codebase was audited and left in
     place, since they live in code comments/docstrings — this
     project's own established, intentional convention for annotating
     when behavior was introduced (see working-methods notes), not a
     leak into anything a user actually sees.

Run from anywhere:
    python scripts/_test_prompt_pass8_offline.py
"""
import os
import sys
import types

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

if "streamlit" not in sys.modules:
    _fake_streamlit = types.ModuleType("streamlit")

    def _fake_cache_data(*args, **kwargs):
        def _decorator(func):
            func.clear = lambda: None
            return func
        return _decorator

    _fake_streamlit.cache_data = _fake_cache_data
    sys.modules["streamlit"] = _fake_streamlit

from dashboard_lib import card_view as cv
from dashboard_lib import nav


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        raise SystemExit(f"Test failed: {label}")


PAGE_FILES = [p.path for p in nav.PAGES]


def main():
    # ------------------------------------------------------------------
    # 1. Every sidebar label in the page registry is ALL CAPS.
    # ------------------------------------------------------------------
    for spec in nav.PAGES:
        check(f"nav title {spec.title!r} is uppercase", spec.title == spec.title.upper())
    check("inject_nav_caps_css() is gone", not hasattr(cv, "inject_nav_caps_css"))

    # ------------------------------------------------------------------
    # 2. Every page imports card_view and calls cv.setup_page()
    #    (st.set_page_config) — a source-text check.
    # ------------------------------------------------------------------
    for rel_path in PAGE_FILES:
        path = os.path.join(PROJECT_ROOT, rel_path)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        check(f"{rel_path}: imports card_view", "card_view" in text)
        check(f"{rel_path}: calls cv.setup_page()", "cv.setup_page(" in text)

    # ------------------------------------------------------------------
    # 3. The Land Probability page's "Check a specific card" caption no
    #    longer leaks Prompt-Pass/legacy-iteration commentary into the
    #    UI. Checked by exact phrase, not a blanket "no 'Prompt Pass'
    #    anywhere in the file" check — this file's module docstring
    #    still legitimately mentions "Prompt Pass 5" several times as
    #    plain-text developer annotation, which is correct and
    #    unchanged (see file header above).
    # ------------------------------------------------------------------
    land_prob_path = os.path.join(PROJECT_ROOT, "pages", "3_Land_Probability.py")
    with open(land_prob_path, encoding="utf-8") as f:
        land_prob_text = f.read()
    check(
        "Land Probability page: bolded '**Prompt Pass 5:**' UI leak is gone",
        "**Prompt Pass 5:**" not in land_prob_text,
    )
    check(
        "Land Probability page: 'rather than the old plain draw-probability' leak is gone",
        "old plain draw" not in land_prob_text,
    )
    check(
        "Land Probability page: the caption still explains what the tool does",
        "actually **playing**" in land_prob_text and "projected through Turn 8" in land_prob_text,
    )

    # ------------------------------------------------------------------
    # 4. Repo-wide guard: no page's rendered UI text bold-markdowns a
    #    "Prompt Pass"/"Phase N" reference (the pattern the one real
    #    leak above used, "**Prompt Pass 5:**" — code comments/
    #    docstrings never use markdown bold syntax, so this heuristic
    #    only catches genuine UI-string leaks, not the intentional
    #    dev-facing annotations).
    # ------------------------------------------------------------------
    for rel_path in PAGE_FILES:
        path = os.path.join(PROJECT_ROOT, rel_path)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        check(f"{rel_path}: no bolded '**Prompt Pass' UI leak", "**Prompt Pass" not in text)
        check(f"{rel_path}: no bolded '**Phase ' UI leak", "**Phase " not in text)

    print("\nAll Prompt Pass 8 offline checks passed.")


if __name__ == "__main__":
    main()
