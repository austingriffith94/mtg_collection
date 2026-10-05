"""
Printable one-page deck sheet.

build_deck_printout_html() returns a complete, self-contained HTML document
(inline CSS, inline SVG charts, no JavaScript beyond one "Print" button)
sized to a single US Letter page (8.5 x 11 in). The Decks page offers it
as a download; the user opens the file in a browser and prints / saves as
PDF. HTML rather than a real PDF library on purpose: WeasyPrint needs GTK
on Windows and ReportLab means hand-positioning everything, while every
browser already prints CSS `@page` layouts faithfully.

This module is deliberately Streamlit-free and database-free — it takes
plain dicts / DataFrames the Decks page has already loaded, so it can be
unit-tested and reused (e.g. a future bulk "print every deck" script)
without a running app. The only dashboard_lib dependency is formatting.py
for the shared WUBRG colors, so the sheet's palette always matches the
on-screen mana curve and deck accent.

Images: anything passed in as an `src` is dropped into an <img> as-is — a
data: URI for locally cached art (fully offline-safe) or a Scryfall https
URL (needs a network connection when the file is opened, same trade-off
the dashboard's own image grid already makes). Mana symbols are hotlinked
from Scryfall's CDN for the same reason formatting.mana_symbol_svg_url()
documents.

Layout is a fixed-size page with overflow hidden, so a very large deck
list is clipped rather than spilling onto a second sheet; the card list
uses a small font and four columns, which comfortably fits a 100-card
Commander deck.

Every block above the decklist is `flex: none` and the decklist is the
only `flex: 1` element, so anything added to the sheet comes straight out
of the card list's height. That's why the Turn 0 combo panel carries both
a row cap (COMBO_ROW_CAP) and a CSS max-height: a row's height depends on
how many cards and features it names, so a row count alone can't bound it.
Measured across this collection the panel costs 0.36-1.02in and no deck
clips a single card line.
"""
import html
from datetime import date

import pandas as pd

from . import formatting as fmt

# Highest mana value drawn as its own bar; everything above lands in the
# last "7+" bucket so one 10-drop doesn't stretch the chart.
CURVE_MAX_CMC = 7

# Row caps for the three "special cards" panels. A panel past its cap ends
# in "+N more" instead of growing, so a Reserved-List- or Game-Changer-heavy
# deck can't squeeze the decklist off the page. The panel headings still
# show the true total.
SPECIAL_ROW_CAP = 8
MANA_TAG_CAP = 6          # optimized-mana tags shown
MANA_TAG_CARD_CAP = 6     # cards listed per tag

# Combos printed in the Turn 0 panel. Only "included" combos (every card
# already in the mainboard) are printed — the "almost" bucket runs to 148
# rows on one of these decks and is a deck-building tool, not something
# you read out at the table, so it's reduced to a count in the heading.
# Six rows covers every deck in this collection (max observed: 5) and the
# panel still degrades to "+N more" past the cap.
COMBO_ROW_CAP = 6
# Features one combo lists before the rest become "…" — a single combo can
# produce six ("Infinite death triggers, Infinite creature ETB, …"), which
# would wrap the line three times for no extra value at the table.
COMBO_PRODUCES_CAP = 2

# Fit-to-page: if the decklist columns still overflow (e.g. a very long
# list), shrink the decklist font a step at a time down to a legibility
# floor. Runs when the page loads, so the browser's print layout (same
# fixed-size page) matches. The page is a fixed 8.5x11in box, so screen and
# print layouts agree.
_FIT_SCRIPT = """
<script>
(function () {
  function fit() {
    var cards = document.querySelector('.cards');
    if (!cards) return;
    var size = 6.4;
    // Overflowing columns spill sideways, so compare scrollWidth to width.
    while (cards.scrollWidth > cards.clientWidth + 2 && size > 5.2) {
      size -= 0.2;
      cards.style.setProperty('--card-font', size + 'pt');
    }
  }
  window.addEventListener('load', fit);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(fit);
})();
</script>
"""


def _capped(rows, cap):
    """First `cap` rows, plus how many were left out."""
    rows = list(rows)
    return rows[:cap], max(0, len(rows) - cap)


def _more(n):
    return f'<div class="muted more">+{n} more</div>' if n else ""


def _esc(value):
    return html.escape(str(value)) if value is not None else ""


def _stat_tile(label, value):
    return (
        f'<div class="tile"><div class="tile-value">{_esc(value)}</div>'
        f'<div class="tile-label">{label}</div></div>'
    )


def _curve_svg(nonland_df):
    """Stacked-by-color mana curve as inline SVG (Vega/Streamlit charts
    don't print crisply). Returns (svg, legend_html)."""
    width, height = 480, 100
    left, right, top, bottom = 8, 8, 16, 22
    if nonland_df.empty:
        return "", ""

    bucketed = nonland_df.assign(
        bucket=nonland_df["color_display"].apply(fmt.mana_curve_color_bucket),
        cmc_bin=nonland_df["cmc"].fillna(0).clip(upper=CURVE_MAX_CMC).astype(int),
    )
    pivot = (
        bucketed.groupby(["cmc_bin", "bucket"])["quantity"].sum().unstack("bucket").fillna(0)
        .reindex(range(CURVE_MAX_CMC + 1), fill_value=0)
    )
    present = [b for b in fmt.MANA_CURVE_COLOR_ORDER if b in pivot.columns]
    totals = pivot.sum(axis=1)
    y_max = max(float(totals.max()), 1.0)

    slot = (width - left - right) / (CURVE_MAX_CMC + 1)
    bar_w = slot * 0.7
    plot_h = height - top - bottom
    parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" class="chart">']
    # Baseline.
    parts.append(
        f'<line x1="{left}" y1="{height - bottom}" x2="{width - right}" y2="{height - bottom}" '
        'stroke="#bbb" stroke-width="1"/>'
    )
    for cmc in range(CURVE_MAX_CMC + 1):
        x = left + slot * cmc + (slot - bar_w) / 2
        y_cursor = height - bottom
        for bucket in present:
            qty = float(pivot.loc[cmc, bucket])
            if qty <= 0:
                continue
            seg_h = plot_h * qty / y_max
            y_cursor -= seg_h
            # Thin stroke so the near-white "W" bars stay visible on paper.
            parts.append(
                f'<rect x="{x:.1f}" y="{y_cursor:.1f}" width="{bar_w:.1f}" height="{seg_h:.1f}" '
                f'fill="{fmt.MANA_CURVE_COLOR_HEX[bucket]}" stroke="#8a8a8a" stroke-width="0.4"/>'
            )
        total = int(totals[cmc])
        if total:
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{y_cursor - 3:.1f}" text-anchor="middle" '
                f'font-size="9" font-weight="700" fill="#222">{total}</text>'
            )
        label = f"{cmc}+" if cmc == CURVE_MAX_CMC else str(cmc)
        parts.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{height - 8}" text-anchor="middle" '
            f'font-size="9" fill="#555">{label}</text>'
        )
    parts.append("</svg>")

    legend = "".join(
        f'<span class="legend-item"><i style="background:{fmt.MANA_CURVE_COLOR_HEX[b]}"></i>{_esc(b)}</span>'
        for b in present
    )
    return "".join(parts), legend


def _type_bars(main_df):
    """Horizontal bar per card type (non-empty types only), pure CSS."""
    counts = main_df.groupby("card_type")["quantity"].sum()
    rows = [(t, int(counts.get(t, 0))) for t in fmt.ALL_CARD_TYPES if counts.get(t, 0) > 0]
    if not rows:
        return ""
    biggest = max(n for _, n in rows)
    return "".join(
        f'<div class="bar-row"><span class="bar-name">{_esc(t)}</span>'
        f'<span class="bar-track"><span class="bar-fill" style="width:{100 * n / biggest:.0f}%"></span></span>'
        f'<span class="bar-num">{n}</span></div>'
        for t, n in rows
    )


def _color_share_bar(nonland_df):
    """One stacked bar of how the non-land cards split across colors."""
    if nonland_df.empty:
        return ""
    buckets = nonland_df["color_display"].apply(fmt.mana_curve_color_bucket)
    shares = nonland_df["quantity"].groupby(buckets).sum()
    total = float(shares.sum()) or 1.0
    segs = "".join(
        f'<span style="width:{100 * shares[b] / total:.1f}%;background:{fmt.MANA_CURVE_COLOR_HEX[b]}" '
        f'title="{_esc(b)}: {int(shares[b])}"></span>'
        for b in fmt.MANA_CURVE_COLOR_ORDER if b in shares.index
    )
    labels = "".join(
        f'<span class="legend-item"><i style="background:{fmt.MANA_CURVE_COLOR_HEX[b]}"></i>'
        f'{_esc(b)} {100 * shares[b] / total:.0f}%</span>'
        for b in fmt.MANA_CURVE_COLOR_ORDER if b in shares.index
    )
    return f'<div class="share-bar">{segs}</div><div class="legend">{labels}</div>'


def _rank_list(title, rows):
    if rows:
        # Label in bold when there's a description beneath it; otherwise the
        # label is the whole entry (strengths/weaknesses are often just a
        # sentence stored as the label).
        items = "".join(
            f"<li><b>{_esc(r['label'])}</b><span class=\"desc\">{_esc(r['description'])}</span></li>"
            if r.get("description") else f"<li>{_esc(r['label'])}</li>"
            for r in rows
        )
    else:
        items = '<li class="muted">None recorded</li>'
    return f'<div class="panel"><h3>{_esc(title)}</h3><ol>{items}</ol></div>'


def _special_panels(game_changers, mana_tags, reserved):
    """Game Changers / optimized mana / Reserved List panels."""
    def panel(title, rows_html, empty):
        body = rows_html or f'<div class="muted">{empty}</div>'
        return f'<div class="panel"><h3>{title}</h3>{body}</div>'

    gc_rows, gc_extra = _capped(game_changers, SPECIAL_ROW_CAP)
    gc = "".join(
        f'<div class="kv"><span>{_esc(g["name"])}</span><em>{_esc(g.get("tag") or "")}</em></div>'
        for g in gc_rows
    ) + _more(gc_extra)

    def mana_row(m):
        # `cards` is a comma-joined list of card names; split it so a long
        # list can be capped rather than wrapping over many lines.
        names = [n.strip() for n in str(m["cards"]).split(",") if n.strip()]
        shown, extra = _capped(names, MANA_TAG_CARD_CAP)
        tail = f" +{extra} more" if extra else ""
        return f'<div class="mana-tag"><b>{_esc(m["tag"])}</b> {_esc(", ".join(shown))}{tail}</div>'

    tag_rows, tag_extra = _capped(mana_tags, MANA_TAG_CAP)
    mana = "".join(mana_row(m) for m in tag_rows) + (
        f'<div class="muted more">+{tag_extra} more tags</div>' if tag_extra else ""
    )

    res_rows, res_extra = _capped(reserved, SPECIAL_ROW_CAP)
    res = "".join(
        f'<div class="kv"><span>{_esc(r["name"])}</span><em>{fmt.format_money(r.get("price"))}</em></div>'
        for r in res_rows
    ) + _more(res_extra)
    return (
        panel(f"Game Changers ({len(game_changers)})", gc, "None in this deck")
        + panel("Optimized mana", mana, "No optimized-mana tags")
        + panel(f"Reserved List ({len(reserved)})", res, "None in this deck")
    )


def _combo_panel(combos):
    """Turn 0 combo panel: the combos whose every card is already in the
    mainboard, most-popular-first (the order queries.deck_combos() returns).
    Only "included" combos are shown — the "almost" bucket (one card away)
    is a deck-building tool, not something to read out at the table, and
    runs long enough (148 rows on one deck in this collection) that even a
    count would be noise here; the Decks page is where that bucket lives.

    Each row is "A + B + C", the Spellbook bracket tag as a chip, and what
    it produces in gray underneath — the three things a turn 0 rundown
    actually needs. Combos hinging on a template ("Creature with Persist
    or Undying") get that spelled out, because the combo is only live if
    something in the deck fills the slot and the sheet shouldn't imply
    otherwise.

    `combos` rows are plain dicts: uses / produces / requires are already
    lists (the caller splits the stored newline columns), bracket_tag and
    mana_needed are strings.
    """
    rows, extra = _capped(combos, COMBO_ROW_CAP)

    if not rows:
        body = (
            '<div class="muted">No known combos fully assembled — Spellbook '
            "only tracks submitted combos, so your own synergies will not appear here.</div>"
        )
    else:
        items = []
        for c in rows:
            uses = " + ".join(c.get("uses") or [])
            produces = (c.get("produces") or [])[:COMBO_PRODUCES_CAP]
            produced = ", ".join(produces)
            if len(c.get("produces") or []) > COMBO_PRODUCES_CAP:
                produced += " …"
            tag = c.get("bracket_tag")
            mana = c.get("mana_needed")
            # Template slots are a caveat, not a card, so they read as
            # "also needs" rather than joining the card list above.
            requires = "; ".join(c.get("requires") or [])
            bits = "".join([
                f'<span class="combo-tag">{_esc(tag)}</span>' if tag else "",
                f'<span class="combo-mana">{_esc(mana)}</span>' if mana else "",
            ])
            sub = _esc(produced)
            if requires:
                sub += f'<span class="combo-req"> · also needs {_esc(requires)}</span>'
            items.append(
                f'<div class="combo"><div class="combo-head">'
                f'<span class="combo-uses">{_esc(uses)}</span>{bits}</div>'
                f'<div class="combo-prod">{sub}</div></div>'
            )
        body = f'<div class="combo-cols">{"".join(items)}</div>' + _more(extra)

    return (
        f'<div class="panel combo-panel"><h3>Turn 0 — combos in this deck '
        f'({len(combos)})</h3>{body}</div>'
    )


def _decklist(main_df, commander_names):
    """The card list grouped by type, commander(s) first. Each line is
    qty, name, and mana value in gray."""
    sections = []
    cmd_rows = main_df[main_df["name"].isin(commander_names)] if commander_names else main_df.iloc[0:0]
    rest = main_df.drop(cmd_rows.index)

    def line(row):
        cmc = row.get("cmc")
        # ★ Game Changer, ◆ Reserved List (key printed beside the title).
        marks = (" ★" if row.get("is_game_changer") == 1 else "") + (" ◆" if row.get("is_reserved") == 1 else "")
        cmc_txt = "" if pd.isna(cmc) or row.get("is_land") else f'<span class="cmc">{int(cmc)}</span>'
        return (
            f'<div class="card-line"><span class="qty">{int(row["quantity"])}</span>'
            f'<span class="cname">{_esc(row["name"])}{marks}</span>{cmc_txt}</div>'
        )

    if not cmd_rows.empty:
        sections.append(("Commander", cmd_rows))
    for card_type in fmt.ALL_CARD_TYPES:
        grp = rest[rest["card_type"] == card_type]
        if not grp.empty:
            sections.append((card_type, grp.sort_values(["cmc", "name"])))

    out = []
    for title, grp in sections:
        n = int(grp["quantity"].sum())
        out.append(
            f'<div class="section"><div class="section-head">{_esc(title)} <b>{n}</b></div>'
            + "".join(line(r) for _, r in grp.iterrows())
            + "</div>"
        )
    return "".join(out)


_CSS = """
@page { size: letter; margin: 0; }
* { box-sizing: border-box; margin: 0; padding: 0; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { background: #d9d9d9; font-family: "Segoe UI", Helvetica, Arial, sans-serif; color: #1c1c1c; }
.toolbar { text-align: center; padding: 10px; }
.toolbar button { font: 600 14px "Segoe UI", sans-serif; padding: 8px 18px; border-radius: 6px;
  border: 0; background: #222; color: #fff; cursor: pointer; }
.page { width: 8.5in; height: 11in; margin: 0 auto 20px; background: #fff; overflow: hidden;
  box-shadow: 0 2px 14px rgba(0,0,0,.35); display: flex; flex-direction: column; }
.accent { height: 0.14in; flex: none; background: var(--gradient); }
.hero { flex: none; display: flex; gap: 0.22in; padding: 0.16in 0.35in; color: #fff;
  background: radial-gradient(circle at 80% 0%, rgba(255,255,255,.12), transparent 55%), #14141c; }
.hero-art { width: 1.15in; height: 1.61in; flex: none; border-radius: 0.09in; object-fit: cover;
  box-shadow: 0 3px 10px rgba(0,0,0,.6); border: 2px solid #000; background: #333; }
.hero-text { flex: 1; min-width: 0; display: flex; flex-direction: column; justify-content: center; }
.hero h1 { font-size: 23pt; line-height: 1.05; letter-spacing: .3px; }
.tagline { margin-top: 6px; font-size: 9.5pt; font-style: italic; color: #cfcfdc; line-height: 1.3; }
.hero-themes { margin-top: 8px; }
.hero-themes .chip { border-color: #8f8fa3; color: #dcdce8; }
.hero-themes .chip.main { background: #fff; color: #14141c; border-color: #fff; font-weight: 600; }
.pips { margin-top: 8px; display: flex; gap: 5px; align-items: center; }
.pips img { width: 0.27in; height: 0.27in; }
.meta { margin-top: 8px; font-size: 8pt; color: #cfcfdc; line-height: 1.4; }
.meta b { color: #fff; }
.showcase { flex: none; width: 2.0in; position: relative; }
.showcase-title { font-size: 7pt; text-transform: uppercase; letter-spacing: 1px; color: #aaa; margin-bottom: 4px; }
.showcase img { position: absolute; width: 0.9in; height: 1.26in; border-radius: 0.06in; object-fit: cover;
  box-shadow: 0 2px 8px rgba(0,0,0,.6); border: 1px solid #000; background: #333; }
.showcase img:nth-of-type(1) { left: 0; top: 0.2in; transform: rotate(-6deg); }
.showcase img:nth-of-type(2) { left: 0.45in; top: 0.4in; transform: rotate(0deg); z-index: 1; }
.showcase img:nth-of-type(3) { left: 0.92in; top: 0.6in; transform: rotate(6deg); z-index: 2; }
.body { flex: 1; min-height: 0; padding: 0.16in 0.35in 0.2in; display: flex; flex-direction: column; gap: 0.12in; }
.tiles { display: grid; grid-template-columns: repeat(6, 1fr); gap: 0.08in; flex: none; }
.tile { border: 1px solid #ddd; border-top: 3px solid var(--accent); border-radius: 4px; padding: 5px 4px; text-align: center; }
.tile-value { font-size: 15pt; font-weight: 700; line-height: 1.1; }
.tile-label { font-size: 6.5pt; text-transform: uppercase; letter-spacing: .8px; color: #777; margin-top: 2px; }
.row { display: grid; gap: 0.14in; flex: none; }
.row.charts { grid-template-columns: 1.5fr 1fr; }
.row.lists { grid-template-columns: repeat(3, 1fr); }
.panel h3, .section-title { font-size: 7.5pt; text-transform: uppercase; letter-spacing: 1px; color: #555;
  border-bottom: 2px solid var(--accent); padding-bottom: 2px; margin-bottom: 5px; }
.chart { width: 100%; height: auto; display: block; }
.legend { display: flex; flex-wrap: wrap; gap: 2px 9px; margin-top: 3px; font-size: 6.5pt; color: #555; }
.legend-item i { display: inline-block; width: 8px; height: 8px; margin-right: 3px; border: 1px solid #888; vertical-align: -1px; }
.bar-row { display: flex; align-items: center; gap: 5px; font-size: 7.5pt; margin-bottom: 3px; }
.bar-name { width: 0.8in; flex: none; }
.bar-track { flex: 1; height: 8px; background: #eee; border-radius: 4px; overflow: hidden; }
.bar-fill { display: block; height: 100%; background: var(--gradient); }
.bar-num { width: 18px; text-align: right; font-weight: 700; }
.share-bar { display: flex; height: 9px; margin-top: 4px; border: 1px solid #888; border-radius: 3px; overflow: hidden; }
.share-bar span { display: block; height: 100%; }
ol { padding-left: 15px; font-size: 7.5pt; line-height: 1.35; }
li { margin-bottom: 2px; }
.desc { display: block; font-size: 6.6pt; color: #666; line-height: 1.3; }
.kv { display: flex; justify-content: space-between; gap: 6px; font-size: 7.2pt; line-height: 1.4;
  border-bottom: 1px dotted #e6e6e6; }
.mana-tag { font-size: 6.8pt; line-height: 1.3; margin-bottom: 2px; color: #555; }
.mana-tag b { color: #1c1c1c; }
.kv em { font-style: normal; color: #666; flex: none; }
.row.specials { grid-template-columns: repeat(3, 1fr); }
/* Turn 0 combos: full width, two text columns, so a 5-combo deck costs
   ~0.5in of height instead of the ~1in a single column would. flex:none
   keeps the decklist below as the only element that absorbs slack. */
/* Hard height bound, not just a row cap: a row's height varies with how
   many cards and features it names, so COMBO_ROW_CAP alone can't promise a
   ceiling. Measured worst case across this collection is 1.02in (5 combos,
   3 cards each); 1.12in clears that with room to spare and clips anything
   pathological rather than letting it eat the decklist — the same
   clip-don't-spill contract .page and .decklist already use. */
.combo-panel { flex: none; max-height: 1.12in; overflow: hidden; }
.combo-panel > .muted { margin-left: 0; }
.combo-cols { column-count: 2; column-gap: 0.18in; column-rule: 1px solid #e4e4e4; }
.combo { break-inside: avoid; margin-bottom: 3px; }
.combo-head { font-size: 7.2pt; line-height: 1.3; }
.combo-uses { font-weight: 700; }
.combo-tag { font-size: 6pt; text-transform: uppercase; letter-spacing: .5px; color: #fff;
  background: var(--accent-dark); border-radius: 6px; padding: 0 4px; margin-left: 4px;
  vertical-align: 1px; white-space: nowrap; }
.combo-mana { font-size: 6.4pt; color: #666; margin-left: 4px; white-space: nowrap; }
.combo-prod { font-size: 6.4pt; color: #666; line-height: 1.25; }
.combo-req { color: #999; font-style: italic; }
.hint { text-transform: none; letter-spacing: 0; color: #888; font-size: 6.5pt; float: right; }
.tile-label small { text-transform: none; letter-spacing: 0; color: #999; }
.muted { color: #999; list-style: none; margin-left: -15px; font-size: 7pt; }
.kv + .muted, .panel > .muted { margin-left: 0; }
.themes { font-size: 8pt; line-height: 1.5; }
.chip { display: inline-block; border: 1px solid var(--accent); border-radius: 9px; padding: 0 6px; margin: 0 3px 3px 0; font-size: 7pt; }
.chip.main { background: var(--accent); color: #fff; }
.decklist { flex: 1; min-height: 0; overflow: hidden; display: flex; flex-direction: column; }
.cards { flex: 1; min-height: 0; column-count: 4; column-gap: 0.16in; column-rule: 1px solid #e4e4e4;
  column-fill: auto; overflow: hidden; }
.section { break-inside: auto; margin-bottom: 4px; }
.section-head { break-after: avoid; font-size: 6.5pt; font-weight: 700; text-transform: uppercase;
  letter-spacing: .8px; color: #fff; background: var(--accent-dark); padding: 1px 4px; border-radius: 2px; margin-bottom: 1px; }
.section-head b { float: right; }
.muted.more { margin-left: 0; font-style: italic; }
.card-line { display: flex; gap: 4px; font-size: var(--card-font, 6.4pt); line-height: 1.27; border-bottom: 1px dotted #e6e6e6; break-inside: avoid; }
.qty { width: 9px; text-align: right; color: #777; flex: none; }
.cname { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cmc { color: #999; flex: none; }
.footer { flex: none; font-size: 6pt; color: #999; text-align: right; margin-top: 3px; }
@media print { body { background: #fff; } .toolbar { display: none; } .page { margin: 0; box-shadow: none; } }
"""


def build_deck_printout_html(
    meta, stats, value, main_df, image_src, symbol_urls, accent_gradient, accent_hex,
    win_conditions, strengths, weaknesses, themes, showcase,
    game_changers=(), mana_tags=(), reserved=(), combos=None,
):
    """Assemble the one-page sheet.

    meta / stats / value   dicts from loaders.load_deck_meta/_stats/_value
    main_df                loaders.load_deck_cards_df() (needs the derived
                           card_type / color_display / is_land columns)
    image_src              hero image (card_view.deck_image_src()) or None
    symbol_urls            fmt.deck_color_identity_symbol_urls()
    accent_*               fmt.deck_accent_gradient() / deck_accent_hex()
    win_conditions, strengths, weaknesses
                           loaders.load_deck_rank_list() rows
    themes                 loaders.load_deck_themes() rows
    game_changers          [{name, tag}]; mana_tags [{tag, cards}];
                           reserved [{name, price}] — the three "special
                           cards" panels
    combos                 Spellbook "included" combos for the Turn 0
                           panel: [{uses, produces, requires, bracket_tag,
                           mana_needed}] with the list fields already
                           split (spellbook.split_list). None (the
                           default) omits the panel entirely — pass []
                           for a deck that's been checked and has none.
                           The "almost" (one card away) bucket is never
                           printed here — see _combo_panel().
    showcase               up to 3 dicts {name, src} — the deck's priciest
                           cards, drawn fanned on the right of the banner
    """
    commanders = [n for n in (meta.get("commander"), meta.get("partner")) if n]
    nonland = main_df[~main_df["is_land"]]

    total_cards = int(main_df["quantity"].sum())
    lands = int(main_df.loc[main_df["is_land"], "quantity"].sum())
    nonland_qty = nonland["quantity"].sum()
    avg_mv = (nonland["cmc"].fillna(0) * nonland["quantity"]).sum() / nonland_qty if nonland_qty else None

    win_rate = stats.get("win_rate")
    record = (
        f"{stats.get('wins', 0)}-{stats.get('losses', 0)}"
        if stats.get("games_played") else "—"
    )
    bracket = meta.get("bracket")
    tiles = "".join([
        _stat_tile("Cards", total_cards),
        _stat_tile("Lands", lands),
        _stat_tile("Avg mana value", f"{avg_mv:.2f}" if avg_mv is not None else "—"),
        _stat_tile("Bracket <small>of 5</small>", bracket if bracket is not None else "—"),
        _stat_tile("Record (W-L)", record + (f" · {win_rate * 100:.0f}%" if win_rate is not None else "")),
        _stat_tile("Value", fmt.format_money(value.get("total_value"))),
    ])

    curve_svg, curve_legend = _curve_svg(nonland)

    meta_bits = []
    if commanders:
        meta_bits.append(f"<b>Commander</b> {_esc(' & '.join(commanders))}")
    # The old hand-typed decks.combos field used to render here; removed
    # along with its Deck Editor input and the Decks page's Turn 0 display
    # — see dashboard_lib/spellbook.py for the real combo data that
    # replaced it (not currently wired into this printout; it's a Decks-
    # page panel, not passed to build_deck_printout_html()).
    short = []
    if meta.get("interaction") is not None:
        # The Deck Editor's Interaction field runs 0-5.
        short.append(f"<b>Interaction</b> {_esc(meta['interaction'])} / 5")
    for label, key in (("Tutors", "tutors"), ("Built", "initially_built")):
        if meta.get(key):
            short.append(f"<b>{label}</b> {_esc(meta[key])}")
    if short:
        meta_bits.append(" &nbsp;·&nbsp; ".join(short))

    hero_img = f'<img class="hero-art" src="{_esc(image_src)}" alt="">' if image_src else ""
    pips = "".join(f'<img src="{_esc(u)}" alt="">' for u in symbol_urls)
    showcase_html = ""
    if showcase:
        imgs = "".join(f'<img src="{_esc(c["src"])}" alt="{_esc(c["name"])}">' for c in showcase[:3] if c.get("src"))
        if imgs:
            showcase_html = f'<div class="showcase"><div class="showcase-title">Priciest pickups</div>{imgs}</div>'

    # combos=None (not just empty) means the deck was never checked against
    # Spellbook — print no panel at all rather than an "unknown combos" row
    # claiming a fact we don't have. A checked deck with zero combos still
    # gets the panel, showing its own "none found" message.
    combo_html = _combo_panel(combos) if combos is not None else ""

    mains = [t["theme"] for t in themes if t["role"] == "main"]
    subs = [t["theme"] for t in themes if t["role"] == "sub"]
    theme_html = (
        "".join(f'<span class="chip main">{_esc(t)}</span>' for t in mains)
        + "".join(f'<span class="chip">{_esc(t)}</span>' for t in subs)
    ) or '<span class="muted">None recorded</span>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(meta.get('name') or 'Deck')} — deck sheet</title>
<style>{_CSS}
:root {{ --gradient: {accent_gradient}; --accent: {accent_hex}; --accent-dark: #2a2a35; }}
</style></head><body>
<div class="toolbar"><button onclick="window.print()">Print / Save as PDF</button>
<div style="font:12px sans-serif;color:#555;margin-top:4px">Tip: set paper to Letter, margins to None, and tick "Background graphics".</div></div>
<div class="page">
  <div class="accent"></div>
  <div class="hero">
    {hero_img}
    <div class="hero-text">
      <h1>{_esc(meta.get('name') or 'Deck')}</h1>
      <div class="tagline">{_esc(meta.get('description') or '')}</div>
      <div class="pips">{pips}</div>
      <div class="hero-themes">{theme_html}</div>
      <div class="meta">{'<br>'.join(meta_bits)}</div>
    </div>
    {showcase_html}
  </div>
  <div class="body">
    <div class="tiles">{tiles}</div>
    <div class="row charts">
      <div class="panel"><h3>Mana curve (non-land) &amp; color balance</h3>{curve_svg}{_color_share_bar(nonland)}</div>
      <div class="panel"><h3>Card types</h3>{_type_bars(main_df)}</div>
    </div>
    <div class="row lists">
      {_rank_list("Win conditions", win_conditions)}
      {_rank_list("Strengths", strengths)}
      {_rank_list("Weaknesses", weaknesses)}
    </div>
    <div class="row specials">{_special_panels(game_changers, mana_tags, reserved)}</div>
    {combo_html}
    <div class="decklist">
      <div class="section-title">Decklist <span class="hint">★ Game Changer &nbsp; ◆ Reserved List &nbsp; · left number = copies, right number = mana value</span></div>
      <div class="cards">{_decklist(main_df, commanders)}</div>
      <div class="footer">Generated {date.today().isoformat()} · MTG Collection Dashboard</div>
    </div>
  </div>
</div>
{_FIT_SCRIPT}
</body></html>"""


# ------------------------------------------------------------------
# Printable pull sheet (Phase 6 — brewing a deck)
#
# A sibling of build_deck_printout_html() above rather than a flag on it,
# deliberately: it is a different document with a different ordering (by
# where the card physically is, not by card type) and a different job (a
# checklist you work the box with, not a deck's one-page portrait).
# Overloading the printout builder would tangle both.
#
# Unlike the deck sheet, this is NOT pinned to a single page: a brew can
# need 85 cards and clipping the list would defeat the entire point, so it
# flows across as many sheets as it takes with the group headers repeating.
# ------------------------------------------------------------------
_PULL_CSS = """
@page { size: letter; margin: 0.5in; }
* { box-sizing: border-box; margin: 0; padding: 0; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { background: #d9d9d9; font-family: "Segoe UI", Helvetica, Arial, sans-serif;
  color: #1c1c1c; font-size: 10.5pt; }
.toolbar { text-align: center; padding: 10px; }
.toolbar button { font: 600 14px "Segoe UI", sans-serif; padding: 8px 18px; border-radius: 6px;
  border: 0; background: #222; color: #fff; cursor: pointer; }
.sheet { width: 7.5in; margin: 0 auto 20px; background: #fff; padding: 0.3in 0.35in 0.4in;
  box-shadow: 0 2px 14px rgba(0,0,0,.35); }
h1 { font-size: 19pt; line-height: 1.1; }
.sub { margin-top: 4px; font-size: 10pt; color: #555; }
.bar { height: 0.1in; margin: 10px 0 14px; border-radius: 2px; background: var(--gradient, #333); }
.group { margin-top: 14px; break-inside: avoid-page; }
.group h2 { font-size: 12pt; padding: 5px 8px; background: #1f1f28; color: #fff;
  border-radius: 4px 4px 0 0; display: flex; justify-content: space-between; }
.group h2 .n { font-weight: 400; color: #c9c9d6; }
.group .note { font-size: 8.5pt; color: #666; padding: 4px 8px 0; font-style: italic; }
table { width: 100%; border-collapse: collapse; }
td { padding: 3px 8px; border-bottom: 1px solid #e3e3e8; vertical-align: top; }
tr:last-child td { border-bottom: 0; }
td.box { width: 0.26in; }
td.box span { display: inline-block; width: 0.15in; height: 0.15in; border: 1.5px solid #555;
  border-radius: 2px; }
td.qty { width: 0.4in; color: #555; text-align: right; }
td.where { width: 2.1in; color: #555; font-size: 9pt; }
td.warn { color: #9a2b2b; font-weight: 600; }
.sub-head { font-size: 9.5pt; font-weight: 600; padding: 7px 8px 2px; color: #333;
  background: #f4f4f7; }
.footer { margin-top: 18px; font-size: 8.5pt; color: #777; text-align: center; }
"""

# The group order and headings this sheet prints, kept LOCAL rather than
# imported from queries.SOURCING_BUCKETS so this module stays free of any
# dashboard_lib dependency but formatting.py (see the module docstring).
# The keys must match queries.SOURCING_BUCKETS; the wording deliberately
# differs from queries.bucket_label()'s, because a sheet you hold while
# digging through a box wants an instruction, not a column header. A bucket
# missing from here simply doesn't print.
_PULL_GROUPS = (
    ("box", "From the box"),
    ("unlocated", "Owned — location unknown"),
    ("other_deck", "From another deck"),
    ("not_owned", "Not owned"),
    ("sleeved", "Already sleeved here"),
)

_PULL_GROUP_NOTES = {
    "box": "Work these in location order — that's how they're sorted.",
    "unlocated": "You own these but no box is recorded. Find them, then set the location "
                 "on the Workbench's Reconcile tab so next time they're a box pull.",
    "other_deck": "Each of these is the ONLY copy you own, currently sleeved in another "
                  "deck. Pulling one leaves that deck short.",
    "not_owned": "Not owned — buy or proxy before this deck can be finished.",
    "sleeved": "Already in this deck's sleeves. Nothing to do; listed so the count adds up.",
}


def build_pull_sheet_html(plan, accent_gradient=None, meta=None):
    """A printable checklist for physically assembling a brewing deck.

    `plan` is queries.deck_sourcing_plan()'s result. `meta` is the optional
    loaders.load_deck_meta() dict (only the commander line is used), and
    `accent_gradient` the deck's fmt.deck_accent_gradient(), purely so the
    sheet's rule matches the deck's on-screen accent.

    Cards are grouped by WHERE THEY ARE, in _PULL_GROUPS order, because
    that is the order you actually work: empty the box in one pass, then go
    deck by deck. Within the box group they sort by location then name, so
    one walk through the storage boxes collects everything.

    The "from another deck" group names the donor deck per card and is the
    one group that carries a warning, since every row in it costs another
    deck a card. "Already sleeved here" is listed last and only for the
    count to reconcile against the deck's own list.

    Returns a complete self-contained HTML document — same approach and
    same reasoning as build_deck_printout_html(); see this module's
    docstring. Streamlit-free and database-free: it takes the plan dict and
    nothing else.
    """
    gradient = accent_gradient or "linear-gradient(90deg,#3a3a4a,#6a6a80)"
    commander = (meta or {}).get("representative") or (meta or {}).get("commander")

    counts = plan["counts"]
    to_do = sum(counts[b] for b in ("box", "unlocated", "other_deck"))
    pct = plan["pct"]

    groups = []
    for bucket, label in _PULL_GROUPS:
        rows = plan["buckets"].get(bucket) or []
        if not rows:
            continue
        copies = counts.get(bucket, 0)
        if bucket == "other_deck":
            body = _pull_donor_rows(plan["donors"])
        else:
            body = _pull_rows(rows, show_where=bucket == "box")
        note = _PULL_GROUP_NOTES.get(bucket, "")
        groups.append(
            f'<div class="group"><h2><span>{_esc(label)}</span>'
            f'<span class="n">{copies} card{"s" if copies != 1 else ""}</span></h2>'
            + (f'<div class="note">{_esc(note)}</div>' if note else "")
            + f"<table>{body}</table></div>"
        )

    headline = f"{plan['needed']} non-basics"
    if pct is not None:
        headline += f" · {pct:.0f}% sleeved"
    headline += f" · {to_do} to pull"

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Pull sheet — {_esc(plan['deck_name'])}</title>
<style>{_PULL_CSS}</style></head>
<body>
<div class="toolbar"><button onclick="window.print()">🖨️ Print this pull sheet</button></div>
<div class="sheet" style="--gradient:{gradient}">
  <h1>Pull sheet — {_esc(plan['deck_name'])}</h1>
  <div class="sub">{_esc(headline)}{f" · {_esc(commander)}" if commander else ""}</div>
  <div class="bar"></div>
  {"".join(groups) or '<p class="note">Nothing to pull — this deck is fully sleeved.</p>'}
  <div class="footer">Generated {date.today().isoformat()} · MTG Collection Dashboard ·
    basics excluded</div>
</div>
</body></html>"""


def _pull_rows(rows, show_where=False):
    """Checklist rows for one bucket. Sorted by location then name when the
    location is shown (the box walk), by name otherwise."""
    def sort_key(r):
        where = ", ".join(r["locations"])
        return (where.casefold(), r["card"].casefold()) if show_where else (r["card"].casefold(),)

    out = []
    for r in sorted(rows, key=sort_key):
        where = ", ".join(r["locations"])
        out.append(
            '<tr><td class="box"><span></span></td>'
            f'<td>{_esc(r["card"])}</td>'
            f'<td class="qty">{"x" + str(r["copies"]) if r["copies"] > 1 else ""}</td>'
            + (f'<td class="where">{_esc(where)}</td>' if show_where else "")
            + "</tr>"
        )
    return "".join(out)


def _pull_donor_rows(donors):
    """The "from another deck" group, sub-headed per donor deck so one trip
    to a deck box collects every card it owes — with the donor's own
    shortfall stated once, where the decision actually gets made."""
    out = []
    for d in donors:
        hurt = (
            f' — leaves it {d["shortfall"]} short' if d["shortfall"] else " — leftovers, costs it nothing"
        )
        out.append(
            f'<tr><td class="sub-head" colspan="3">{_esc(d["deck_name"])} '
            f'({d["copies"]})<span class="{"warn" if d["shortfall"] else ""}">'
            f'{_esc(hurt)}</span></td></tr>'
        )
        for c in sorted(d["cards"], key=lambda r: r["card"].casefold()):
            out.append(
                '<tr><td class="box"><span></span></td>'
                f'<td>{_esc(c["card"])}</td>'
                f'<td class="qty">{"x" + str(c["copies"]) if c["copies"] > 1 else ""}</td></tr>'
            )
    return "".join(out)
