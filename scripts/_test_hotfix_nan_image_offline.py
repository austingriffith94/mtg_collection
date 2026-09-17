"""
Regression test for a crash reported after Prompt Pass 6 delivery:

    TypeError: join() argument must be str, bytes, or os.PathLike
    object, not 'float'
    ... dashboard.py -> card_view.render_deck_landing_grid ->
        _deck_image_src -> fmt.resolve_local_image ->
        os.path.join(BASE_DIR, local_image_path)

Root cause: when a whole column loaded via pandas.read_sql_query is NULL
for EVERY row (e.g. no deck has a custom cover image yet, or none of a
user's cards have a cached local image), pandas types that column as
float64 and represents each missing value as NaN — a float, not None.
`bool(float('nan'))` is True in Python, so the old
`if not local_image_path:` guard in resolve_local_image() (and the
analogous guard in scryfall_card_url()) silently let NaN straight
through to os.path.join()/string formatting.

This predates Prompt Pass 6 (the affected code is Phase 4's
`queries.list_decks_with_covers()` / `card_view.render_deck_landing_grid()`)
but wasn't caught by Phase 4's offline tests because the synthetic/real
test data used at the time always had at least one non-NULL value in
each affected column, so pandas kept those columns as object dtype
(None, not NaN). A real user's database with EVERY deck lacking a cover
image (or every card lacking a cached local image) hits the all-NaN case
in production. Fixed by requiring an actual non-blank `str` in both
functions.

Run from anywhere:
    python scripts/_test_hotfix_nan_image_offline.py
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

import pandas as pd

from dashboard_lib import formatting as fmt
from dashboard_lib import card_view as cv


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        raise SystemExit(f"Test failed: {label}")


def main():
    # ------------------------------------------------------------------
    # 1. Direct unit checks: resolve_local_image() must never raise on
    #    NaN, None, empty string, or any other unexpected type — it
    #    should just return None so callers fall back to the remote URL.
    # ------------------------------------------------------------------
    check("resolve_local_image(NaN) returns None instead of raising", fmt.resolve_local_image(float("nan")) is None)
    check("resolve_local_image(None) returns None", fmt.resolve_local_image(None) is None)
    check("resolve_local_image('') returns None", fmt.resolve_local_image("") is None)
    check("resolve_local_image('   ') returns None", fmt.resolve_local_image("   ") is None)
    check("resolve_local_image(0) returns None (unexpected type, not just falsy)", fmt.resolve_local_image(0) is None)
    check(
        "resolve_local_image(a path that doesn't exist on disk) returns None (unchanged prior behavior)",
        fmt.resolve_local_image("image_cache/definitely_not_a_real_file_12345.jpg") is None,
    )

    check("scryfall_card_url(NaN, NaN) returns None instead of building a bogus URL", fmt.scryfall_card_url(float("nan"), float("nan")) is None)
    check("scryfall_card_url(None, None) returns None", fmt.scryfall_card_url(None, None) is None)
    check("scryfall_card_url('c21', '263') still works normally", fmt.scryfall_card_url("c21", "263") == "https://scryfall.com/card/c21/263")

    # ------------------------------------------------------------------
    # 2. Reproduce the exact real-world trigger. In the reporter's
    #    pandas/environment, a `decks.cover_image_path` column that's
    #    NULL for every row came back from queries.list_decks_with_covers()
    #    (pandas.read_sql_query) typed as float64 NaN rather than object/
    #    None — this exact type-inference quirk is pandas-version-
    #    dependent (this sandbox's pandas 3.0.2 keeps it as object/None
    #    for the same query, so it's NOT reproduced by calling the real
    #    query function here). To test the fix itself rather than
    #    depend on a version-specific quirk reproducing in THIS sandbox,
    #    build the DataFrame by hand with the exact failure signature —
    #    a float64 NaN column — and feed it through the real
    #    card_view.render_deck_landing_grid()/_deck_image_src() call
    #    path, unmodified, the same one that crashed.
    # ------------------------------------------------------------------
    decks_df = pd.DataFrame(
        {
            "deck_id": [1, 2],
            "name": ["Test Deck One", "Test Deck Two"],
            "representative": [None, None],
            "commander": ["Some Commander", None],
            "cover_image_path": [float("nan"), float("nan")],
            "commander_image_uri": [float("nan"), float("nan")],
            "commander_local_image_path": [float("nan"), float("nan")],
        }
    )
    check(
        "test fixture reproduces the real bug precondition: cover_image_path is an all-NaN float64 column",
        pd.api.types.is_float_dtype(decks_df["cover_image_path"]) and decks_df["cover_image_path"].isna().all(),
    )

    # This is the exact call that crashed before the fix.
    for _, row in decks_df.iterrows():
        src = cv._deck_image_src(row)
        check(f"_deck_image_src for deck {row['name']!r} returns None (no crash, falls back to placeholder)", src is None)

    # render_deck_landing_grid() itself must run start-to-finish without
    # raising (st.columns/st.markdown are stubbed no-ops here since
    # streamlit isn't installed offline, matching this project's
    # established fake-streamlit test pattern).
    class _FakeStColumn:
        def markdown(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    sys.modules["streamlit"].columns = lambda n: [_FakeStColumn() for _ in range(n)]
    sys.modules["streamlit"].markdown = lambda *a, **kw: None
    sys.modules["streamlit"].info = lambda *a, **kw: None
    cv.render_deck_landing_grid(decks_df)
    check("render_deck_landing_grid runs to completion without raising", True)

    print("\nAll NaN-image hotfix regression checks passed.")


if __name__ == "__main__":
    main()
