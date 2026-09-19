"""
Reusable card-browsing UI shared by the Collection page and the
Decks page:

  - add_derived_columns()   adds card_type / color_display / scryfall_url /
                             is_land (Phase 3, MDFC-aware)
  - render_filter_panel()   sidebar filters (search, facets, colors, price, CMC)
  - render_browser()        the View toggle (Table / Image Grid) + "group by"
                             breakout controls + sort, then dispatches to...
  - render_table()          st.dataframe with a clickable Scryfall link column
  - render_grid()           paginated image grid, each card image itself
                             links out to its Scryfall page

Grouping ("layering of toggles") is implemented with ONE st.expander per
top-level group (Streamlit does not allow nesting expanders inside each
other) and plain markdown sub-headers for any additional grouping levels
selected underneath it.
"""
import base64
import html
import math
import os

import pandas as pd
import streamlit as st

from . import formatting as fmt

PAGE_SIZE_OPTIONS = [12, 24, 48, 96]
SORT_FIELDS = {
    "Name": "name",
    "Mana Value": "cmc",
    "Price": "current_price_usd",
    "Rarity": "rarity",
    "Set": "set_code",
}

# ------------------------------------------------------------------
# Sidebar nav-label styling (Prompt Pass 8) — every page calls this
# once, right after st.set_page_config(), so Streamlit's own
# auto-generated sidebar page links (the pages/ directory nav this app
# doesn't build itself) render in ALL CAPS. Pure CSS (text-transform),
# so it's display-only: page filenames, URLs/query params, and every
# hardcoded route string elsewhere in this codebase (e.g.
# render_deck_landing_grid()'s deck_page_path default below) are
# untouched. Streamlit doesn't expose a supported way to relabel the
# auto-generated nav items themselves (only the separate, newer
# st.navigation() API does, which this app doesn't use), so this
# targets the nav's internal data-testid hooks directly — those aren't
# a stable public API and have changed across Streamlit versions
# before, so if a future Streamlit upgrade ever stops showing caps
# here, this selector list is the first place to check.
# ------------------------------------------------------------------
def inject_nav_caps_css():
    st.markdown(
        """
        <style>
        [data-testid="stSidebarNav"] a,
        [data-testid="stSidebarNav"] a *,
        [data-testid="stSidebarNavItems"] a,
        [data-testid="stSidebarNavItems"] a * {
            text-transform: uppercase !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ------------------------------------------------------------------
# Sidebar filter reset (Prompt Pass 4) — every widget key created by
# render_filter_panel() (and any bespoke filter control a page adds
# outside it, via register_filter_key()) is tracked here so
# db.refresh_data_button() can clear them all on click, across every
# page/deck/board scope at once.
# ------------------------------------------------------------------
_FILTER_KEYS_STATE = "_active_filter_widget_keys"


def register_filter_key(key):
    """Track a sidebar-filter widget's session_state key so a "Refresh
    data" click can reset it. Called automatically by
    render_filter_panel() for every filter widget it builds; a page can
    also call this directly for a bespoke filter control that lives
    outside render_filter_panel (e.g. the Collection page's deck-status
    toggle) so it's reset the same way."""
    st.session_state.setdefault(_FILTER_KEYS_STATE, set())
    st.session_state[_FILTER_KEYS_STATE].add(key)


def reset_filters():
    """Delete every tracked filter widget's session_state entry so the
    next script run re-renders them at their defaults (no selection, no
    search text, full price/CMC range). Must be called BEFORE
    st.rerun() — Streamlit forbids setting/clearing a widget's
    session_state value after it has already been instantiated in the
    current run, but doing it here, inside the button-click handler and
    before the widgets are re-created on the next run, is the standard,
    safe pattern for a reset control."""
    for key in st.session_state.get(_FILTER_KEYS_STATE, set()):
        st.session_state.pop(key, None)
    st.session_state.pop(_FILTER_KEYS_STATE, None)


# ------------------------------------------------------------------
# Derived columns shared by every card-bearing dataframe
# ------------------------------------------------------------------
def add_derived_columns(df):
    df = df.copy()
    df["card_type"] = df["type_line"].fillna("").apply(fmt.derive_card_type)
    df["color_display"] = df["color_identity"].fillna("").apply(fmt.color_identity_display)
    df["scryfall_url"] = [
        fmt.scryfall_card_url(s, n)
        for s, n in zip(df.get("set_code"), df.get("collector_number"))
    ]
    # Phase 3: MDFC-aware land flag — True if EITHER face is a Land, unlike
    # card_type above which only looks at the front face for display
    # bucketing. Used by the mana curve and Land Probability page so a
    # card like "Instant // Land" is correctly treated as a land for
    # curve-exclusion, land-ratio, and draw-probability math.
    df["is_land"] = df["type_line"].fillna("").apply(fmt.has_land_face)
    return df


# ------------------------------------------------------------------
# Filters
# ------------------------------------------------------------------
def _multi_value_options(series, colorless_label=None):
    vals = set()
    for cell in series.dropna():
        for part in fmt.split_multi_value(cell):
            vals.add(part)
    options = sorted(vals)
    if colorless_label:
        options = options + [colorless_label]
    return options


def _multi_value_filter(df, col, selected, colorless_label=None):
    if not selected:
        return df
    target = {s for s in selected if s != colorless_label}
    want_colorless = colorless_label is not None and colorless_label in selected

    def matches(cell):
        parts = set(fmt.split_multi_value(cell))
        if not parts:
            return want_colorless
        return bool(parts & target) if target else False

    return df[df[col].apply(matches)]


def render_filter_panel(
    df,
    key_prefix,
    container=None,
    single_facets=None,
    multi_facets=None,
    bool_toggles=None,
    show_price_slider=True,
    show_cmc_slider=True,
):
    """
    single_facets: [(col, label), ...] -> plain "is in selection" multiselect
    multi_facets:  [(col, label, colorless_label_or_None), ...] -> comma-
                   separated values, "any selected value present" matching
                   (used for color_identity and strategy_tags)
    bool_toggles:  [(col, label), ...] -> All / Yes / No selectbox on a
                   0/1 column
    Returns the filtered dataframe. All widget keys are namespaced with
    key_prefix so the Collection and Decks pages don't collide.
    """
    c = container if container is not None else st.sidebar
    filtered = df

    search_key = f"{key_prefix}_search"
    register_filter_key(search_key)
    search = c.text_input("Search card name", key=search_key, placeholder="e.g. Sol Ring")
    if search:
        filtered = filtered[filtered["name"].str.contains(search, case=False, na=False, regex=False)]

    for col, label, colorless_label in multi_facets or []:
        if col not in df.columns:
            continue
        options = _multi_value_options(df[col], colorless_label)
        if not options:
            continue
        multi_key = f"{key_prefix}_{col}_multi"
        register_filter_key(multi_key)
        selected = c.multiselect(label, options, key=multi_key)
        filtered = _multi_value_filter(filtered, col, selected, colorless_label)

    for col, label in single_facets or []:
        if col not in df.columns:
            continue
        options = sorted(v for v in df[col].dropna().unique() if str(v).strip())
        if not options:
            continue
        single_key = f"{key_prefix}_{col}_single"
        register_filter_key(single_key)
        selected = c.multiselect(label, options, key=single_key)
        if selected:
            filtered = filtered[filtered[col].isin(selected)]

    for col, label in bool_toggles or []:
        if col not in df.columns:
            continue
        bool_key = f"{key_prefix}_{col}_bool"
        register_filter_key(bool_key)
        choice = c.selectbox(label, ["All", "Yes", "No"], key=bool_key)
        if choice == "Yes":
            filtered = filtered[filtered[col] == 1]
        elif choice == "No":
            filtered = filtered[filtered[col] == 0]

    if show_price_slider and "current_price_usd" in df.columns and df["current_price_usd"].notna().any():
        max_price = float(df["current_price_usd"].max(skipna=True) or 0)
        if max_price > 0:
            price_key = f"{key_prefix}_price"
            register_filter_key(price_key)
            lo, hi = c.slider(
                "Price (USD)", 0.0, round(max_price + 0.5, 2), (0.0, round(max_price + 0.5, 2)),
                key=price_key,
            )
            filtered = filtered[
                filtered["current_price_usd"].isna()
                | filtered["current_price_usd"].between(lo, hi)
            ]

    if show_cmc_slider and "cmc" in df.columns and df["cmc"].notna().any():
        max_cmc = int(df["cmc"].max(skipna=True) or 0)
        if max_cmc > 0:
            cmc_key = f"{key_prefix}_cmc"
            register_filter_key(cmc_key)
            lo, hi = c.slider("Mana Value", 0, max_cmc, (0, max_cmc), key=cmc_key)
            filtered = filtered[filtered["cmc"].isna() | filtered["cmc"].between(lo, hi)]

    return filtered


# ------------------------------------------------------------------
# View toggle + group-by breakout + sort, then dispatch to table/grid
# ------------------------------------------------------------------
def render_browser(df, key_prefix, group_options, column_config, extra_table_cols=None, columns_per_row=5):
    """
    group_options: [(col, label), ...] offered in the "Group / breakout by"
                   multiselect, applied in the order the user picks them
                   (layered breakouts).
    column_config: ordered dict {col: st.column_config...} defining the
                   Table view's columns (order + rendering, incl. the
                   clickable Scryfall LinkColumn).
    """
    if df.empty:
        st.info("No cards match the current filters.")
        return

    ctrl1, ctrl2, ctrl3, ctrl4 = st.columns([1.3, 2, 1.3, 1])
    view = ctrl1.radio("View", ["Table", "Image Grid"], horizontal=True, key=f"{key_prefix}_view")

    label_by_col = dict(group_options)
    col_by_label = {label: col for col, label in group_options}
    chosen_labels = ctrl2.multiselect(
        "Group / breakout by", list(col_by_label.keys()), key=f"{key_prefix}_groupby",
        help="Layer multiple breakouts — each pick nests inside the previous one.",
    )
    group_cols = [col_by_label[lbl] for lbl in chosen_labels]

    sort_label = ctrl3.selectbox("Sort by", list(SORT_FIELDS.keys()), key=f"{key_prefix}_sort")
    descending = ctrl4.toggle("Desc.", key=f"{key_prefix}_desc")

    sort_col = SORT_FIELDS[sort_label]
    if sort_col in df.columns:
        df = df.sort_values(by=sort_col, ascending=not descending, na_position="last", kind="stable")

    st.caption(f"{len(df)} card row(s) shown")

    def leaf_renderer(sub_df):
        if view == "Table":
            render_table(sub_df, column_config, extra_table_cols)
        else:
            # sub_df.index values are unique in the parent dataframe and
            # partitioned disjointly across groups, so the first index value
            # is a stable, collision-free widget-key suffix per leaf group.
            leaf_key = f"{key_prefix}_g{sub_df.index[0]}"
            render_grid(sub_df, key_prefix=leaf_key, columns_per_row=columns_per_row)

    if not group_cols:
        leaf_renderer(df)
    else:
        _render_grouped(df, group_cols, label_by_col, leaf_renderer, depth=0)


def _render_grouped(df, group_cols, label_by_col, leaf_renderer, depth):
    if not group_cols:
        leaf_renderer(df)
        return

    col = group_cols[0]
    label = label_by_col.get(col, col)
    working = df.copy()
    working["_group_val"] = working[col].fillna("—").astype(str)
    working.loc[working["_group_val"].str.strip() == "", "_group_val"] = "—"

    for val in sorted(working["_group_val"].unique()):
        sub = working[working["_group_val"] == val].drop(columns=["_group_val"])
        if sub.empty:
            continue
        header = f"{label}: {val}  ·  {len(sub)} card{'s' if len(sub) != 1 else ''}"
        if depth == 0:
            # Only ONE level of st.expander is ever used per branch —
            # Streamlit does not allow nesting an expander inside another.
            with st.expander(header, expanded=(working["_group_val"].nunique() <= 4)):
                _render_grouped(sub, group_cols[1:], label_by_col, leaf_renderer, depth + 1)
        else:
            heading = "####" if depth == 1 else "#####"
            st.markdown(f"{heading} {header}")
            _render_grouped(sub, group_cols[1:], label_by_col, leaf_renderer, depth + 1)
            st.divider()


# ------------------------------------------------------------------
# Table view
# ------------------------------------------------------------------
def render_table(df, column_config, extra_cols=None):
    cols = [c for c in column_config.keys() if c in df.columns]
    for c in extra_cols or []:
        if c in df.columns and c not in cols:
            cols.append(c)
    st.dataframe(
        df[cols],
        column_config=column_config,
        hide_index=True,
        use_container_width=True,
    )


def base_column_config(price=True):
    cfg = {
        "image_uri": st.column_config.ImageColumn("Art", width="small"),
        "name": st.column_config.TextColumn("Name", width="medium"),
        "set_code": st.column_config.TextColumn("Set", width="small"),
        "collector_number": st.column_config.TextColumn("#", width="small"),
        "rarity": st.column_config.TextColumn("Rarity", width="small"),
        "type_line": st.column_config.TextColumn("Type", width="large"),
        "mana_cost": st.column_config.TextColumn("Cost", width="small"),
        "cmc": st.column_config.NumberColumn("MV", width="small", format="%.0f"),
        "color_display": st.column_config.TextColumn("Colors", width="small"),
        "scryfall_url": st.column_config.LinkColumn("Scryfall", display_text="View ↗", width="small"),
    }
    if price:
        cfg["current_price_usd"] = st.column_config.NumberColumn("Price", format="$%.2f", width="small")
    return cfg


# ------------------------------------------------------------------
# Image grid view — each card image itself links out to Scryfall
# ------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def _encode_local_image_b64(abs_path, mtime):
    with open(abs_path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def _image_src(row):
    local_path = fmt.resolve_local_image(row.get("local_image_path"))
    if local_path:
        try:
            mtime = os.path.getmtime(local_path)
            b64 = _encode_local_image_b64(local_path, mtime)
            ext = os.path.splitext(local_path)[1].lstrip(".").lower() or "jpg"
            ext = "jpeg" if ext == "jpg" else ext
            return f"data:image/{ext};base64,{b64}"
        except OSError:
            pass
    image_uri = row.get("image_uri")
    if isinstance(image_uri, str) and image_uri.startswith("http"):
        return image_uri
    return None


def _render_card_tile(row):
    name = html.escape(str(row.get("name") or "Unknown card"))
    url = row.get("scryfall_url")
    src = _image_src(row)
    qty = row.get("quantity")
    qty_badge = f" ×{int(qty)}" if pd.notna(qty) else ""

    if src:
        img_html = f'<img src="{src}" style="width:100%;border-radius:8px;display:block;box-shadow:0 1px 4px rgba(0,0,0,0.35);" alt="{name}"/>'
    else:
        img_html = (
            '<div style="width:100%;aspect-ratio:5/7;background:#2b2b2b;border-radius:8px;'
            'display:flex;align-items:center;justify-content:center;color:#999;'
            'font-size:0.75rem;text-align:center;padding:8px;">No cached image</div>'
        )

    if url:
        tile = (
            f'<a href="{url}" target="_blank" rel="noopener" style="text-decoration:none;color:inherit;">'
            f"{img_html}"
            f'<div style="text-align:center;font-size:0.8rem;margin-top:4px;line-height:1.2;">{name}{qty_badge}</div>'
            f'<div style="text-align:center;font-size:0.72rem;color:#4da3ff;">🔗 Scryfall</div>'
            f"</a>"
        )
    else:
        tile = (
            f"{img_html}"
            f'<div style="text-align:center;font-size:0.8rem;margin-top:4px;line-height:1.2;">{name}{qty_badge}</div>'
        )

    st.markdown(tile, unsafe_allow_html=True)


def render_grid(df, key_prefix, columns_per_row=5):
    if df.empty:
        st.info("No cards match the current filters.")
        return

    total = len(df)
    ctrl1, ctrl2 = st.columns([1, 1])
    # Default is 48 (index 2 of [12, 24, 48, 96]), per Phase 2.
    page_size = ctrl1.selectbox("Cards per page", PAGE_SIZE_OPTIONS, index=2, key=f"{key_prefix}_pagesize")
    n_pages = max(1, math.ceil(total / page_size))
    page = ctrl2.number_input("Page", min_value=1, max_value=n_pages, value=1, step=1, key=f"{key_prefix}_page")

    start = (page - 1) * page_size
    page_df = df.iloc[start : start + page_size]
    st.caption(f"Showing {start + 1}–{min(start + page_size, total)} of {total}")

    cols = st.columns(columns_per_row)
    for i, (_, row) in enumerate(page_df.iterrows()):
        with cols[i % columns_per_row]:
            _render_card_tile(row)


# ------------------------------------------------------------------
# Home-page deck landing grid (Prompt Pass 4) — image-backed tiles that
# navigate straight to the Decks page with a deck pre-selected.
# ------------------------------------------------------------------
def deck_image_src(row):
    """Resolve the best available thumbnail for a deck: a user-set cover
    image first (Editor -> Deck Info -> Cover image), then the
    commander's own card art (local cache, else Scryfall's remote URL),
    else None so the caller renders a placeholder (or, on the Decks
    page's header as of Prompt Pass 10, simply omits the image entirely
    — "if available" per that prompt, not a placeholder box). Public
    (no leading underscore) since Prompt Pass 10 made this a second
    caller alongside render_deck_landing_grid() below: the Decks page's
    own header now reuses this exact fallback chain instead of
    duplicating it a third time — was previously private/single-caller.
    `row` needs the same shape queries.list_decks_with_covers() returns
    (cover_image_path, commander_local_image_path,
    commander_image_uri) — a dict or a single pandas Series row both
    work, since both support .get()."""
    cover = fmt.resolve_local_image(row.get("cover_image_path"))
    if cover:
        try:
            mtime = os.path.getmtime(cover)
            b64 = _encode_local_image_b64(cover, mtime)
            ext = os.path.splitext(cover)[1].lstrip(".").lower() or "png"
            ext = "jpeg" if ext == "jpg" else ext
            return f"data:image/{ext};base64,{b64}"
        except OSError:
            pass

    commander_local = fmt.resolve_local_image(row.get("commander_local_image_path"))
    if commander_local:
        try:
            mtime = os.path.getmtime(commander_local)
            b64 = _encode_local_image_b64(commander_local, mtime)
            ext = os.path.splitext(commander_local)[1].lstrip(".").lower() or "jpg"
            ext = "jpeg" if ext == "jpg" else ext
            return f"data:image/{ext};base64,{b64}"
        except OSError:
            pass

    remote = row.get("commander_image_uri")
    if isinstance(remote, str) and remote.startswith("http"):
        return remote
    return None


def render_deck_landing_grid(decks_df, deck_page_path="Decks", columns_per_row=4):
    """Home-page landing grid: one image-backed tile per deck. Clicking
    a tile navigates (plain <a> href, same tab) straight to the Decks
    page with `?deck_id=<id>` in the URL, which that page reads on load
    to pre-select the deck — see pages/2_Decks.py. Mirrors the existing
    Scryfall-link tile pattern in render_grid()/_render_card_tile()
    above, just pointed at an internal page instead of an external URL."""
    if decks_df is None or decks_df.empty:
        st.info("No decks found yet.")
        return

    cols = st.columns(columns_per_row)
    for i, (_, row) in enumerate(decks_df.iterrows()):
        name = html.escape(str(row.get("name") or "Deck"))
        deck_id = row.get("deck_id")
        href = f"{deck_page_path}?deck_id={deck_id}"
        src = deck_image_src(row)

        if src:
            img_html = (
                f'<img src="{src}" style="width:100%;border-radius:10px;display:block;'
                f'box-shadow:0 1px 4px rgba(0,0,0,0.35);" alt="{name}"/>'
            )
        else:
            img_html = (
                '<div style="width:100%;aspect-ratio:5/7;background:#2b2b2b;border-radius:10px;'
                'display:flex;align-items:center;justify-content:center;color:#999;'
                'font-size:0.85rem;text-align:center;padding:8px;">No cover image</div>'
            )

        tile = (
            f'<a href="{href}" target="_self" style="text-decoration:none;color:inherit;">'
            f"{img_html}"
            f'<div style="text-align:center;font-size:0.95rem;margin-top:6px;line-height:1.3;'
            f'font-weight:600;">{name}</div>'
            f"</a>"
        )
        with cols[i % columns_per_row]:
            st.markdown(tile, unsafe_allow_html=True)


# ------------------------------------------------------------------
# Deck page header + themed accents (Prompt Pass 10 / prompt3.txt) —
# replaces the Decks page's old generic "🃏 Decks" page title and its
# separate st.header(representative)/st.caption(description) blocks
# with one branded header, plus page-wide color-identity CSS accents.
# ------------------------------------------------------------------
def render_deck_header(deck_name, tagline, image_src, color_identity_str):
    """The Decks page's branded header (Prompt Pass 10): the deck's own
    NAME (decks.name) as the actual page title — not the separate,
    user-editable "representative" field the old header showed instead
    — next to its resolved thumbnail (image_src — see deck_image_src()
    above; "if available" per the prompt, so no image at all rather than
    a placeholder box when nothing resolves, unlike the home-page grid
    which always needs SOME tile art to keep its layout from looking
    broken), the deck's short descriptive tagline directly beneath the
    name (moved here in this pass from its old spot below the
    Commander/Partner/.../Built stat grid), and a row of the deck's own
    official Scryfall mana-symbol badges for its color identity."""
    name_html = html.escape(str(deck_name or "Deck"))
    tagline_html = (
        f'<div style="opacity:0.75;font-size:1.05rem;margin-top:2px;">{html.escape(str(tagline))}</div>'
        if tagline else ""
    )
    symbol_urls = fmt.deck_color_identity_symbol_urls(color_identity_str)
    symbols_html = "".join(
        f'<img src="{u}" width="26" height="26" style="margin-right:4px;vertical-align:middle;" '
        f'alt="mana symbol"/>'
        for u in symbol_urls
    )
    symbols_row = f'<div style="margin-top:8px;">{symbols_html}</div>' if symbols_html else ""

    name_block = (
        f'<div style="font-size:2.1rem;font-weight:700;line-height:1.2;">{name_html}</div>'
        f"{tagline_html}{symbols_row}"
    )

    if image_src:
        img_html = (
            f'<img src="{image_src}" style="width:96px;border-radius:10px;display:block;'
            f'box-shadow:0 1px 4px rgba(0,0,0,0.35);" alt="{name_html}"/>'
        )
        block = (
            '<div style="display:flex;align-items:center;gap:18px;">'
            f"{img_html}<div>{name_block}</div>"
            "</div>"
        )
    else:
        block = name_block

    st.markdown(block, unsafe_allow_html=True)


def inject_deck_accent_css(gradient_css, accent_hex):
    """Subtle per-deck themed accents (Prompt Pass 10 / prompt3.txt) so
    the Decks page isn't flat black-and-white: recolors this page's
    st.divider() rules (plain <hr> elements) with the deck's own
    color-identity gradient (fmt.deck_accent_gradient()), and gives this
    page's expander (the Moxfield-export panel) and the sidebar a
    colored accent edge in the deck's dominant color
    (fmt.deck_accent_hex()). Call once per page render, AFTER the
    current deck (and therefore its accent colors) is known — unlike
    inject_nav_caps_css() (Prompt Pass 8), which is identical on every
    page and safe to call before anything else, this one is deck-
    specific and must be (re-)called every rerun once the deck picker
    has settled, so switching decks in the sidebar re-themes the page.
    Same caveat as inject_nav_caps_css(): `hr`/stExpander/stSidebar are
    Streamlit's own rendered/internal DOM structure, not all guaranteed
    stable public API across versions — this hasn't been checked against
    a real running Streamlit (no live Streamlit in this project's build
    sandbox, per every prior phase's own testing caveat), so a future
    Streamlit upgrade is the first place to check if these accents ever
    stop appearing."""
    st.markdown(
        f"""
        <style>
        hr {{
            height: 4px;
            border: none;
            border-radius: 2px;
            background: {gradient_css};
            opacity: 0.9;
        }}
        div[data-testid="stExpander"] details {{
            border-left: 4px solid {accent_hex};
            border-radius: 4px;
        }}
        section[data-testid="stSidebar"] {{
            border-right: 3px solid {accent_hex};
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


# ------------------------------------------------------------------
# Win-rate conditional formatting (Prompt Pass 9 / prompt2.txt) — shared
# by the Commander Game Tracking page's Win-by-Deck summary, Win-by-
# Player summary, and Head-to-Head matrix, so all three read the same
# color scale off one function rather than three separate st.dataframe
# call sites each rolling their own. This is the one spot in the app
# that hands st.dataframe() a pandas Styler instead of a plain
# DataFrame — everywhere else (render_table(), the two summary/matrix
# call sites before this pass) just passes a DataFrame straight through.
# ------------------------------------------------------------------
def style_win_rate_percentages(df, columns=None, baseline=fmt.WIN_RATE_BASELINE, na_rep="\u2014"):
    """Build a pandas Styler over `df` that renders each cell in
    `columns` (win-rate floats, 0.0-1.0) as a whole-percent string and
    colors its background on fmt.win_rate_background_color()'s diverging
    red/white/blue scale, anchored at `baseline`. `columns=None` (the
    default) styles every column — the Head-to-Head matrix, where every
    cell is itself a win rate; pass an explicit list (e.g. ["Win %"]) for
    a table like the Win-by-Deck/Win-by-Player summaries, which also
    carry non-win-rate columns (deck/player name, games played, ...)
    that must pass through untouched. NaN cells (e.g. the Head-to-Head
    diagonal, or a deck/player with no recorded result) render as
    `na_rep` and stay unstyled, same as fmt.win_rate_background_color()
    returning None for them.

    Pandas 2.1 renamed Styler.applymap() to Styler.map() and pandas 3.0
    removed applymap() outright; this project's floor is pandas>=2.0
    (which only has applymap()), so this picks whichever the installed
    pandas actually offers rather than hardcoding one."""
    cols = list(columns) if columns is not None else list(df.columns)
    styler = df.style.format(
        lambda v: f"{v * 100:.0f}%" if pd.notna(v) else na_rep, subset=cols
    )
    elementwise = styler.map if hasattr(styler, "map") else styler.applymap
    return elementwise(lambda v: fmt.win_rate_cell_style(v, baseline), subset=cols)
