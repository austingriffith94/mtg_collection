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
def _deck_image_src(row):
    """Resolve the best available thumbnail for a deck landing-page
    tile: a user-set cover image first (Editor -> Deck Info -> Cover
    image), then the commander's own card art (local cache, else
    Scryfall's remote URL), else None so the caller renders a
    placeholder."""
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
        src = _deck_image_src(row)

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
