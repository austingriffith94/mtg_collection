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
"""
import html
from datetime import date

import pandas as pd

from . import formatting as fmt

# Highest mana value drawn as its own bar; everything above lands in the
# last "7+" bucket so one 10-drop doesn't stretch the chart.
CURVE_MAX_CMC = 7


def _esc(value):
    return html.escape(str(value)) if value is not None else ""


def _stat_tile(label, value):
    return (
        f'<div class="tile"><div class="tile-value">{_esc(value)}</div>'
        f'<div class="tile-label">{_esc(label)}</div></div>'
    )


def _curve_svg(nonland_df):
    """Stacked-by-color mana curve as inline SVG (Vega/Streamlit charts
    don't print crisply). Returns (svg, legend_html)."""
    width, height = 480, 118
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
        items = "".join(f"<li>{_esc(r['label'])}</li>" for r in rows)
    else:
        items = '<li class="muted">None recorded</li>'
    return f'<div class="panel"><h3>{_esc(title)}</h3><ol>{items}</ol></div>'


def _decklist(main_df, commander_names):
    """The card list grouped by type, commander(s) first. Each line is
    qty, name, and mana value in gray."""
    sections = []
    cmd_rows = main_df[main_df["name"].isin(commander_names)] if commander_names else main_df.iloc[0:0]
    rest = main_df.drop(cmd_rows.index)

    def line(row):
        cmc = row.get("cmc")
        cmc_txt = "" if pd.isna(cmc) or row.get("is_land") else f'<span class="cmc">{int(cmc)}</span>'
        return (
            f'<div class="card-line"><span class="qty">{int(row["quantity"])}</span>'
            f'<span class="cname">{_esc(row["name"])}</span>{cmc_txt}</div>'
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
.hero-art { width: 1.3in; height: 1.82in; flex: none; border-radius: 0.09in; object-fit: cover;
  box-shadow: 0 3px 10px rgba(0,0,0,.6); border: 2px solid #000; background: #333; }
.hero-text { flex: 1; min-width: 0; display: flex; flex-direction: column; justify-content: center; }
.hero h1 { font-size: 25pt; line-height: 1.05; letter-spacing: .3px; }
.tagline { margin-top: 6px; font-size: 9.5pt; font-style: italic; color: #cfcfdc; line-height: 1.3; }
.pips { margin-top: 10px; display: flex; gap: 5px; align-items: center; }
.pips img { width: 0.27in; height: 0.27in; }
.meta { margin-top: 10px; font-size: 8.5pt; color: #cfcfdc; line-height: 1.5; }
.meta b { color: #fff; }
.showcase { flex: none; width: 2.0in; position: relative; }
.showcase-title { font-size: 7pt; text-transform: uppercase; letter-spacing: 1px; color: #aaa; margin-bottom: 4px; }
.showcase img { position: absolute; width: 0.95in; height: 1.33in; border-radius: 0.06in; object-fit: cover;
  box-shadow: 0 2px 8px rgba(0,0,0,.6); border: 1px solid #000; background: #333; }
.showcase img:nth-of-type(1) { left: 0; top: 0.2in; transform: rotate(-6deg); }
.showcase img:nth-of-type(2) { left: 0.45in; top: 0.45in; transform: rotate(0deg); z-index: 1; }
.showcase img:nth-of-type(3) { left: 0.92in; top: 0.7in; transform: rotate(6deg); z-index: 2; }
.body { flex: 1; min-height: 0; padding: 0.16in 0.35in 0.2in; display: flex; flex-direction: column; gap: 0.12in; }
.tiles { display: grid; grid-template-columns: repeat(6, 1fr); gap: 0.08in; flex: none; }
.tile { border: 1px solid #ddd; border-top: 3px solid var(--accent); border-radius: 4px; padding: 5px 4px; text-align: center; }
.tile-value { font-size: 15pt; font-weight: 700; line-height: 1.1; }
.tile-label { font-size: 6.5pt; text-transform: uppercase; letter-spacing: .8px; color: #777; margin-top: 2px; }
.row { display: grid; gap: 0.14in; flex: none; }
.row.charts { grid-template-columns: 1.5fr 1fr; }
.row.lists { grid-template-columns: repeat(4, 1fr); }
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
.share-bar { display: flex; height: 10px; margin-top: 8px; border: 1px solid #888; border-radius: 3px; overflow: hidden; }
.share-bar span { display: block; height: 100%; }
ol { padding-left: 15px; font-size: 8pt; line-height: 1.45; }
.muted { color: #999; list-style: none; margin-left: -15px; }
.themes { font-size: 8pt; line-height: 1.5; }
.chip { display: inline-block; border: 1px solid var(--accent); border-radius: 9px; padding: 0 6px; margin: 0 3px 3px 0; font-size: 7pt; }
.chip.main { background: var(--accent); color: #fff; mix-blend-mode: normal; }
.decklist { flex: 1; min-height: 0; overflow: hidden; display: flex; flex-direction: column; }
.cards { flex: 1; min-height: 0; column-count: 4; column-gap: 0.16in; column-rule: 1px solid #e4e4e4;
  column-fill: auto; overflow: hidden; }
.section { break-inside: auto; margin-bottom: 4px; }
.section-head { break-after: avoid; font-size: 6.5pt; font-weight: 700; text-transform: uppercase;
  letter-spacing: .8px; color: #fff; background: var(--accent-dark); padding: 1px 4px; border-radius: 2px; margin-bottom: 1px; }
.section-head b { float: right; }
.card-line { display: flex; gap: 4px; font-size: 6.6pt; line-height: 1.32; border-bottom: 1px dotted #e6e6e6; break-inside: avoid; }
.qty { width: 9px; text-align: right; color: #777; flex: none; }
.cname { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cmc { color: #999; flex: none; }
.footer { flex: none; font-size: 6pt; color: #999; text-align: right; margin-top: 3px; }
@media print { body { background: #fff; } .toolbar { display: none; } .page { margin: 0; box-shadow: none; } }
"""


def build_deck_printout_html(
    meta, stats, value, main_df, image_src, symbol_urls, accent_gradient, accent_hex,
    win_conditions, strengths, weaknesses, themes, showcase,
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
        _stat_tile("Bracket", bracket if bracket is not None else "—"),
        _stat_tile("Record (W-L)", record + (f" · {win_rate * 100:.0f}%" if win_rate is not None else "")),
        _stat_tile("Value", fmt.format_money(value.get("total_value"))),
    ])

    curve_svg, curve_legend = _curve_svg(nonland)

    meta_bits = []
    if commanders:
        meta_bits.append(f"<b>Commander</b> {_esc(' & '.join(commanders))}")
    for label, key in (("Interaction", "interaction"), ("Combos", "combos"), ("Tutors", "tutors"), ("Built", "initially_built")):
        v = meta.get(key)
        if v is not None and v != "":
            meta_bits.append(f"<b>{label}</b> {_esc(v)}")

    hero_img = f'<img class="hero-art" src="{_esc(image_src)}" alt="">' if image_src else ""
    pips = "".join(f'<img src="{_esc(u)}" alt="">' for u in symbol_urls)
    showcase_html = ""
    if showcase:
        imgs = "".join(f'<img src="{_esc(c["src"])}" alt="{_esc(c["name"])}">' for c in showcase[:3] if c.get("src"))
        if imgs:
            showcase_html = f'<div class="showcase"><div class="showcase-title">Priciest pickups</div>{imgs}</div>'

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
      <div class="meta">{'<br>'.join(meta_bits)}</div>
    </div>
    {showcase_html}
  </div>
  <div class="body">
    <div class="tiles">{tiles}</div>
    <div class="row charts">
      <div class="panel"><h3>Mana curve (non-land)</h3>{curve_svg}<div class="legend">{curve_legend}</div></div>
      <div class="panel"><h3>Card types</h3>{_type_bars(main_df)}<h3 style="margin-top:8px">Color balance</h3>{_color_share_bar(nonland)}</div>
    </div>
    <div class="row lists">
      {_rank_list("Win conditions", win_conditions)}
      {_rank_list("Strengths", strengths)}
      {_rank_list("Weaknesses", weaknesses)}
      <div class="panel"><h3>Themes</h3><div class="themes">{theme_html}</div></div>
    </div>
    <div class="decklist">
      <div class="section-title">Decklist</div>
      <div class="cards">{_decklist(main_df, commanders)}</div>
      <div class="footer">Generated {date.today().isoformat()} · MTG Collection Dashboard</div>
    </div>
  </div>
</div>
</body></html>"""
