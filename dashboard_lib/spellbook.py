"""
Commander Spellbook integration — automatic combo detection per deck.

Replaces the old hand-typed `decks.combos` free-text field (removed from
the Deck Editor and the Decks page's Turn 0 grid — see git history/
PROJECT_STATE.md) with real combo data from https://commanderspellbook.com,
the community combo database. `decks.combos` itself is left in the schema
untouched per this project's non-destructive policy (same treatment as
`cards.edhrec_salt`): any values entered before the retirement aren't
lost, there's just no read/write/display path left for them. Answers two
questions for a deck:

  1. Which known combos are FULLY present in the mainboard right now
     ("included") — including ones you didn't know you'd assembled.
  2. Which combos are exactly ONE card short ("almost") — and, since this
     dashboard already tracks your collection, whether that one missing
     card is something you already own.

Data source / API notes (verified against the live API, not assumed):
  - Endpoint: POST https://backend.commanderspellbook.com/find-my-combos
  - No authentication, no API key, no rate-limit headers observed. One
    request per deck refresh (~0.6-1.6s for a 100-card deck), so this is
    a deliberately light dependency. Still sends a descriptive User-Agent.
  - Request body is `DeckRequest` from the published OpenAPI schema at
    https://backend.commanderspellbook.com/schema/ :
      {"commanders": [{"card": name, "quantity": n}, ...],   # max 12
       "main":       [{"card": name, "quantity": n}, ...]}   # max 600
  - Cards are matched BY NAME. Spellbook accepts a double-faced card's
    front-face name, which is what gets sent (this database stores MDFCs
    as "Front // Back", same as Scryfall) — but note Spellbook echoes the
    FULL "Front // Back" name back in its responses, so both sides are
    normalized through front_face() before any comparison.
  - The response wraps a `FindMyCombosResponse` in a pagination envelope:
    results.included / almostIncluded / includedByChangingCommanders /
    almostIncludedByAddingColors / ...ByChangingCommanders /
    ...ByAddingColorsAndChangingCommanders, plus results.identity.
    Only `included` and `almostIncluded` are used here — the others
    describe decks you don't have (a different commander, or a color
    identity this deck can't legally play), so they aren't actionable for
    a built deck.
  - "almostIncluded" means exactly one card short. That was verified
    empirically, not just taken from the docs: across a 94-card deck's 26
    almost-combos, every single one diffed to exactly 1 missing card.
    `missing` is still computed locally rather than trusted blindly, since
    that's what drives the "do I already own it?" lookup.
  - Combos can also require a TEMPLATE rather than a specific card (e.g.
    'Permanent with "You don\\'t lose the game due to having 0 or less
    life"'). Those come back under `requires` and are surfaced as-is —
    they can't be matched against a card name, so they're shown as a
    caveat rather than silently dropped.
  - Per-variant `salt` / `saltVoteCount` fields exist on the API but came
    back empty for all 416 variants across every deck in this collection,
    so they're ignored here (and salt tracking generally is out of scope —
    see "EDHREC Salt Score (retired)" in the README).

This module holds no Streamlit imports (same rule as queries.py/writes.py)
and `requests` is imported lazily inside the client, so importing it can
never break a page on a machine without `requests` installed — callers
catch ImportError the same way dashboard_lib/refresh.py does.
"""
import time

SPELLBOOK_API = "https://backend.commanderspellbook.com"
FIND_MY_COMBOS_URL = f"{SPELLBOOK_API}/find-my-combos"
SPELLBOOK_SITE = "https://commanderspellbook.com"
REQUEST_TIMEOUT = 30


def split_list(text):
    """Read-side counterpart to writes._join_list(): turn one of
    deck_combos' newline-separated list columns back into a list of
    strings, dropping blanks. Lives here rather than in a page because
    both the Decks page's combo panel and the printable deck sheet decode
    the same stored convention, and spellbook.py is what defines it."""
    return [line.strip() for line in (text or "").split("\n") if line.strip()]


def combo_url(combo_id):
    """Public permalink for one combo, for the "read the full writeup"
    links in the Decks page's combo list. Verified live: the trailing
    slash is what the site canonicalizes to."""
    return f"{SPELLBOOK_SITE}/combo/{combo_id}/"

# Spellbook's own "how spicy is this combo" tag (BracketTagEnum in their
# OpenAPI schema), decoded to the labels their site shows. Deliberately NOT
# mapped onto this dashboard's own 0-5 `decks.bracket` score: these are
# Spellbook's editorial categories for a COMBO, not WotC bracket numbers
# for a DECK, and inventing an equivalence would be making up data. Shown
# as-is so a "Ruthless" combo sitting in a deck you've marked bracket 2 is
# visible as something to think about, without the tool pretending to
# know your bracket is wrong.
BRACKET_TAGS = {
    "C": "Core",
    "P": "Powerful",
    "S": "Spicy",
    "O": "Oddball",
    "R": "Ruthless",
    "E": "Exhibition",
    "B": "Banned",
}

# The two response buckets worth caching, mapped to the `category` value
# stored in deck_combos. See the module docstring for why the other four
# buckets are skipped.
CATEGORY_INCLUDED = "included"
CATEGORY_ALMOST = "almost"

_RESPONSE_BUCKETS = [("included", CATEGORY_INCLUDED), ("almostIncluded", CATEGORY_ALMOST)]


def clean_name(name):
    """Return a usable card-name string, or "" for anything that isn't one.

    Every name entering this module goes through here, because the two
    callers hand over different flavours of "no value": queries.deck_meta()
    comes from a sqlite3.Row, so a missing partner is None, while the Card
    Database page's bulk path iterates a pandas DataFrame
    (loaders.load_decks_df), where a column that's NULL for every row is
    typed float64 and each missing value is NaN — a float, not None.

    `bool(float('nan'))` is True, so a plain `if name:` check lets NaN
    straight through and the next `.strip()` raises AttributeError. Testing
    for an actual str (same convention as formatting.resolve_local_image()
    and split_multi_value()) catches None, NaN, and any other unexpected
    type in one place."""
    return name.strip() if isinstance(name, str) else ""


def _quantity(value):
    """Coerce a deck_cards.quantity into a positive int for the payload.
    Same NaN-not-None concern as clean_name(), plus the API's schema wants
    an integer — a NaN or a float would serialize into invalid JSON for the
    `quantity` field. NULL/unparseable means "one copy"."""
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        return 1
    return quantity if quantity > 0 else 1


def front_face(name):
    """Normalize a card name for cross-source comparison: front face only,
    stripped, lowercased. This database stores double-faced cards as
    "Front // Back" (Scryfall's convention) and so does Spellbook, but the
    two don't always agree on which printing's full name to use, and the
    payload sends front faces only — so every comparison goes through here
    on BOTH sides rather than trusting the full strings to match."""
    return clean_name(name).split(" // ")[0].strip().lower()


def build_deck_payload(card_rows, commander=None, partner=None):
    """Build the find-my-combos request body for one deck.

    `card_rows` is an iterable of (card_name, quantity) for the deck's
    mainboard — exactly the shape queries.deck_combo_card_rows() returns.
    The commander/partner are pulled OUT of the mainboard and sent in the
    `commanders` list instead: Spellbook uses that split to decide which
    combos are reachable for this deck's color identity and which would
    need a different commander, so sending the commander as a plain
    mainboard card would produce subtly wrong buckets.

    Quantities are passed through (Spellbook's schema takes a per-card
    quantity) but barely matter for singleton Commander — they exist for
    the basic lands, which are the only cards here with quantity > 1.
    """
    # clean_name() rather than a truthiness check: a partnerless deck's
    # `partner` arrives as None from sqlite3 but as NaN from pandas, and NaN
    # is truthy. See clean_name().
    commanders = [c for c in (clean_name(commander), clean_name(partner)) if c]
    commander_keys = {front_face(c) for c in commanders}

    main = []
    for name, quantity in card_rows:
        name = clean_name(name)
        if not name:
            continue
        if front_face(name) in commander_keys:
            continue  # already going out in `commanders`
        main.append({"card": name.split(" // ")[0].strip(), "quantity": _quantity(quantity)})

    return {
        "commanders": [
            {"card": c.split(" // ")[0].strip(), "quantity": 1} for c in commanders
        ],
        "main": main,
    }


def _joined_prerequisites(variant):
    """Spellbook splits a combo's setup requirements across two fields —
    `notablePrerequisites` (the ones that actually constrain you, e.g.
    "Your life total is at least 2") and `easyPrerequisites` (trivia like
    "All permanents are untapped"). Both are newline-separated free text.
    They're concatenated here, notable first, since the display treats
    them as one "before this works, you need…" block."""
    parts = [
        (variant.get("notablePrerequisites") or "").strip(),
        (variant.get("easyPrerequisites") or "").strip(),
    ]
    return "\n".join(p for p in parts if p)


def _variant_to_combo(variant, deck_keys):
    """Flatten one API `Variant` into the dict shape this dashboard stores
    and renders. `deck_keys` is the set of front_face() keys for every card
    actually in the deck, used to compute `missing` locally."""
    uses = [u["card"]["name"] for u in variant.get("uses", []) if u.get("card")]
    requires = [
        t["template"]["name"] for t in variant.get("requires", []) if t.get("template")
    ]
    legalities = variant.get("legalities") or {}

    return {
        # Spellbook's variant id is a composite string like "1561-2384"
        # (the card ids that make up the combo), not an integer.
        "combo_id": str(variant.get("id") or ""),
        "uses": uses,
        "requires": requires,
        "missing": [c for c in uses if front_face(c) not in deck_keys],
        "produces": [
            p["feature"]["name"] for p in variant.get("produces", []) if p.get("feature")
        ],
        "description": (variant.get("description") or "").strip(),
        "prerequisites": _joined_prerequisites(variant),
        "mana_needed": (variant.get("manaNeeded") or "").strip(),
        # How many decks on Spellbook run this combo — the only sensible
        # ranking signal they expose, and the one their own site sorts by.
        "popularity": variant.get("popularity"),
        "bracket_tag": BRACKET_TAGS.get(variant.get("bracketTag"), variant.get("bracketTag")),
        "commander_legal": bool(legalities.get("commander")),
    }


def normalize_response(data, deck_card_names):
    """Turn a raw find-my-combos response into
    {"identity": str, "included": [combo, ...], "almost": [combo, ...]},
    each list sorted most-popular-first (unranked combos last).

    `deck_card_names` is every card name in the deck (commander included)
    so `missing` can be computed locally — see the module docstring.
    """
    results = (data or {}).get("results") or {}
    # front_face() absorbs None/NaN/non-str; the truthiness filter is on the
    # RESULT so an unusable name can't contribute an empty key that would
    # make a blank-named card look like it's in the deck.
    deck_keys = {k for k in (front_face(n) for n in deck_card_names) if k}

    out = {"identity": results.get("identity") or "", "included": [], "almost": []}
    for response_key, category in _RESPONSE_BUCKETS:
        combos = [
            _variant_to_combo(v, deck_keys) for v in (results.get(response_key) or [])
        ]
        # -1 sorts unranked (None popularity) combos to the bottom rather
        # than crashing the comparison on None.
        combos.sort(key=lambda c: c["popularity"] if c["popularity"] is not None else -1, reverse=True)
        out[category] = combos
    return out


def combo_list_text(combos, limit=4):
    """One-line, human-readable rundown of a list of combo dicts (the
    normalize_response() shape — uses/produces as python lists, not yet
    flattened to the newline-joined strings deck_combos stores), e.g.
    "Sol Ring + Mana Vault → Infinite mana; Thassa's Oracle + Demonic
    Consultation → Win the game (+2 more)". Empty list -> "".

    This exists so the RESULT of a combo check can name the actual combos
    right away — a bare count ("found 4 combos") isn't an answer to "what
    are they", and this is what both the Decks page's post-check message
    and the Card Database bulk-refresh summary build their text from."""
    if not combos:
        return ""
    lines = []
    for combo in combos[:limit]:
        uses = " + ".join(combo.get("uses") or [])
        produces = ", ".join((combo.get("produces") or [])[:2])
        lines.append(f"{uses} → {produces}" if produces else uses)
    text = "; ".join(lines)
    remaining = len(combos) - limit
    if remaining > 0:
        text += f" (+{remaining} more)"
    return text


def combo_check_summary(result, limit=4):
    """Flash-message text for right after a "Check Spellbook" fetch —
    names the combos found instead of just counting them. `result` is a
    normalize_response() dict ({"included": [...], "almost": [...]}).
    Returns "" only if given an empty/falsy result."""
    if not result:
        return ""
    included = result.get("included") or []
    almost = result.get("almost") or []

    if not included and not almost:
        return "Spellbook doesn't know of any combos for this deck yet — a real answer, not a gap (it only tracks submitted combos)."

    parts = []
    if included:
        parts.append(f"{len(included)} combo(s) in the deck: {combo_list_text(included, limit)}")
    else:
        parts.append("No complete combos in the deck yet")
    if almost:
        parts.append(f"{len(almost)} one card away")
    return " · ".join(parts)


class SpellbookClient:
    """Minimal POST client for find-my-combos. Mirrors the shape of
    scripts/scryfall_lookup.py's ScryfallClient (session reuse, descriptive
    User-Agent, a miss log instead of raising) so the two network clients in
    this project behave the same way."""

    def __init__(self, session=None):
        import requests  # lazy: see module docstring

        self._requests = requests
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "MTGCollectionDashboard/1.0 (personal use)",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        self._miss_log = []

    def find_my_combos(self, payload):
        """POST one deck payload. Returns the parsed JSON response, or None
        on any network/HTTP/decode failure (the reason is appended to
        self._miss_log — callers show it rather than crashing the page)."""
        try:
            resp = self.session.post(
                FIND_MY_COMBOS_URL, json=payload, timeout=REQUEST_TIMEOUT
            )
        except self._requests.RequestException as e:
            self._miss_log.append(f"network error: {e}")
            return None

        if resp.status_code != 200:
            self._miss_log.append(f"http {resp.status_code}: {resp.text[:200]}")
            return None

        try:
            return resp.json()
        except ValueError as e:
            self._miss_log.append(f"bad JSON response: {e}")
            return None

    @property
    def misses(self):
        return list(self._miss_log)


def fetch_deck_combos(card_rows, commander=None, partner=None, client=None):
    """One-call convenience path: build the payload, POST it, normalize the
    response. Returns (result_dict, error_message) — exactly one of the two
    is None, so callers can branch without inspecting the client.

    `client` is injectable so the offline test harness
    (scripts/_test_spellbook_offline.py) can exercise this without network
    access."""
    card_rows = list(card_rows)
    payload = build_deck_payload(card_rows, commander, partner)
    if not payload["main"] and not payload["commanders"]:
        return None, "This deck has no cards to look up yet."

    client = client or SpellbookClient()
    data = client.find_my_combos(payload)
    if data is None:
        reason = client.misses[-1] if client.misses else "unknown error"
        return None, f"Couldn't reach Commander Spellbook ({reason})."

    # Commander/partner are included so an "included" combo that uses the
    # commander isn't reported as missing it. clean_name() again, for the
    # None-vs-NaN reason in its docstring.
    all_names = [name for name, _ in card_rows]
    all_names += [c for c in (clean_name(commander), clean_name(partner)) if c]
    return normalize_response(data, all_names), None


def fetch_many(decks, progress_callback=None, pause=0.3, client=None):
    """Refresh several decks in one pass, for the Card Database page's
    bulk action. `decks` is an iterable of
    (deck_id, deck_name, commander, partner, card_rows).

    Yields (deck_id, deck_name, result, error) per deck. One client is
    reused across all of them so the HTTP connection is kept alive. A short
    pause between requests keeps this polite to a free community API even
    though no documented rate limit exists — 12 decks is ~12 requests, so
    the added wall time is negligible.

    `client` is injectable for the same reason as in fetch_deck_combos():
    the offline harness drives this whole loop without network access."""
    client = client or SpellbookClient()
    decks = list(decks)
    for i, (deck_id, deck_name, commander, partner, card_rows) in enumerate(decks, start=1):
        result, error = fetch_deck_combos(card_rows, commander, partner, client=client)
        if progress_callback:
            progress_callback(i, len(decks), deck_name)
        yield deck_id, deck_name, result, error
        if i < len(decks) and pause:
            time.sleep(pause)
