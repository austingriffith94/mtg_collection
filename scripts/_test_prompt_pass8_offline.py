"""
Offline test for Prompt Pass 8 changes: no Streamlit, no live network.

Prompt Pass 8 ("prompt1.txt" — nav caps + UI text cleanup):
  1. Sidebar nav labels now render ALL CAPS via a small CSS injection
     (card_view.inject_nav_caps_css(), called once per page right after
     st.set_page_config()) rather than by renaming pages/*.py — see
     PROJECT_STATE.md for why renaming (and especially renaming
     dashboard.py, the entry script every launcher hardcodes) was
     rejected in favor of a display-only CSS approach.
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


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        raise SystemExit(f"Test failed: {label}")


PAGE_FILES = [
    "dashboard.py",
    "pages/1_Collection.py",
    "pages/2_Decks.py",
    "pages/3_Land_Probability.py",
    "pages/4_Editor.py",
    "pages/5_Commander_Game_Tracking.py",
]


def main():
    # ------------------------------------------------------------------
    # 1. inject_nav_caps_css() itself: the markdown it emits must carry
    #    a <style> block, passed with unsafe_allow_html=True (required
    #    for the <style> tag to take effect at all), that actually
    #    applies text-transform: uppercase against the sidebar nav's
    #    data-testid hooks.
    # ------------------------------------------------------------------
    captured = {}

    def _fake_markdown(body, **kwargs):
        captured["body"] = body
        captured["kwargs"] = kwargs

    sys.modules["streamlit"].markdown = _fake_markdown
    cv.inject_nav_caps_css()

    check("inject_nav_caps_css calls st.markdown", "body" in captured)
    check("...with unsafe_allow_html=True (required for <style> to apply)", captured["kwargs"].get("unsafe_allow_html") is True)
    css = captured["body"]
    check("...output contains a <style> block", "<style>" in css and "</style>" in css)
    check("...targets the stSidebarNav data-testid hook", 'data-testid="stSidebarNav"' in css)
    check("...targets the stSidebarNavItems data-testid hook", 'data-testid="stSidebarNavItems"' in css)
    check("...actually sets text-transform: uppercase", "text-transform: uppercase" in css)

    # ------------------------------------------------------------------
    # 2. Every page must import card_view and call
    #    cv.inject_nav_caps_css() AFTER its own st.set_page_config() —
    #    a source-text check, since none of these page scripts can be
    #    executed for real in this sandbox (no streamlit install).
    # ------------------------------------------------------------------
    for rel_path in PAGE_FILES:
        path = os.path.join(PROJECT_ROOT, rel_path)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        check(f"{rel_path}: imports card_view", "card_view" in text)
        check(f"{rel_path}: calls inject_nav_caps_css()", "cv.inject_nav_caps_css()" in text)
        config_pos = text.find("st.set_page_config(")
        caps_pos = text.find("cv.inject_nav_caps_css()")
        check(
            f"{rel_path}: inject_nav_caps_css() runs after set_page_config(), not before",
            config_pos != -1 and caps_pos != -1 and caps_pos > config_pos,
        )

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
