"""
Offline test for the Commander Spellbook combo feature: no Streamlit, no
live network.

The feature (see dashboard_lib/spellbook.py and schema.sql's deck_combos
comment) sends a deck to commanderspellbook.com's public find-my-combos
API and caches two things per deck: combos fully present in the mainboard
("included"), and combos exactly one card short ("almost"), the latter
cross-referenced against the collection so you can see whether you already
own the missing card.

The canned API payloads below are trimmed copies of REAL responses captured
from the live API while building this (Vilis / Tuvasa decks), so the shapes
being parsed here are the shapes the API actually returns — not invented
fixtures.

Covers:
  A. schema.sql — a fresh database already has deck_combos +
     deck_combo_sync.
  B. queries.ensure_schema() upgrade path — a database missing both tables
     (simulating a pre-feature database) gets them created, and re-running
     is a no-op.
  C. spellbook.front_face() — MDFC/case/whitespace normalization, the key
     every cross-source name comparison goes through.
  D. spellbook.build_deck_payload() — commander/partner pulled out of the
     mainboard into `commanders`, MDFCs sent as front faces, quantities
     preserved, blank names skipped.
  E. spellbook.normalize_response() — `missing` computed locally, sort by
     popularity (unranked last), bracketTag decoded, prerequisites joined,
     templates captured under `requires`.
  F. spellbook.fetch_deck_combos() with an injected fake client — the
     whole fetch path with no network, including the error branch.
  F2. spellbook.combo_list_text()/combo_check_summary() — the "what are
      the combos, not just a count" flash-message text, including the
      all-empty and included-only/almost-only branches.
  G. writes.save_deck_combos() — round-trip, replace-not-merge on a
     re-sync, duplicate combo_id tolerated, sync stamp counts.
  H. writes.clear_deck_combos() — returns a deck to "never fetched", not
     "fetched and found nothing".
  I. queries.deck_combos/deck_combo_sync/deck_combo_sync_all reads.
  J. queries.owned_card_quantities() — owned vs available (a copy sleeved
     in another deck is owned but not free), NULL quantity counted as 1.
  K. writes.delete_deck() clears the combo cache — without it the
     deck_id foreign key would abort the delete under
     PRAGMA foreign_keys = ON.
  L. Source-text checks that pages/2_Decks.py actually wires the panel in,
     and loaders.py invalidates the combo caches.
  M. Source-text checks that the OLD hand-typed decks.combos field was
     actually removed from the Deck Editor, the Decks page's Turn 0 grid,
     and the printable deck sheet — not just that the new panel exists
     alongside it.

Run from anywhere:
    python scripts/_test_spellbook_offline.py
"""
import os
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dashboard_lib import queries as q
from dashboard_lib import writes as w
from dashboard_lib import spellbook as sb


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        raise SystemExit(f"Test failed: {label}")


def build_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    with open(os.path.join(PROJECT_ROOT, "schema.sql")) as f:
        conn.executescript(f.read())
    return conn


def _variant(vid, uses, produces, popularity, bracket_tag="P", requires=(),
             notable="", easy="", mana="{1}", commander_legal=True, description=""):
    """Build a Variant in the API's real response shape (see module docstring)."""
    return {
        "id": vid,
        "uses": [{"card": {"name": n}, "quantity": 1} for n in uses],
        "requires": [{"template": {"name": t}} for t in requires],
        "produces": [{"feature": {"name": f}} for f in produces],
        "popularity": popularity,
        "bracketTag": bracket_tag,
        "notablePrerequisites": notable,
        "easyPrerequisites": easy,
        "manaNeeded": mana,
        "description": description,
        "legalities": {"commander": commander_legal},
    }


def _response(included=(), almost=(), identity="B"):
    return {
        "count": None, "next": None, "previous": None,
        "results": {
            "identity": identity,
            "included": list(included),
            "includedByChangingCommanders": [],
            "almostIncluded": list(almost),
            "almostIncludedByAddingColors": [],
            "almostIncludedByChangingCommanders": [],
            "almostIncludedByAddingColorsAndChangingCommanders": [],
        },
    }


class FakeClient:
    """Stands in for SpellbookClient — records the payload it was handed and
    returns a canned response (or None, to exercise the failure branch)."""

    def __init__(self, response, misses=()):
        self.response = response
        self._misses = list(misses)
        self.payloads = []

    def find_my_combos(self, payload):
        self.payloads.append(payload)
        return self.response

    @property
    def misses(self):
        return list(self._misses)


def main():
    # ------------------------------------------------------------------
    # A. schema.sql — fresh database already has both tables
    # ------------------------------------------------------------------
    conn = build_db()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    check("a fresh schema.sql database has deck_combos", "deck_combos" in tables)
    check("a fresh schema.sql database has deck_combo_sync", "deck_combo_sync" in tables)
    check(
        "deck_combos starts empty on a fresh build",
        conn.execute("SELECT COUNT(*) FROM deck_combos").fetchone()[0] == 0,
    )
    # The CHECK constraint is what keeps a typo'd category out of the cache.
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'T')")
    try:
        conn.execute(
            "INSERT INTO deck_combos (deck_id, combo_id, category) VALUES (1, 'x', 'bogus')"
        )
        bad_category_rejected = False
    except sqlite3.IntegrityError:
        bad_category_rejected = True
    check("deck_combos.category CHECK rejects a category outside included/almost", bad_category_rejected)
    conn.close()

    # ------------------------------------------------------------------
    # B. ensure_schema() upgrade path — simulated pre-feature database
    # ------------------------------------------------------------------
    conn = build_db()
    conn.execute("DROP TABLE deck_combos")
    conn.execute("DROP TABLE deck_combo_sync")
    conn.commit()
    before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    check("both combo tables were dropped for the simulated-old-DB test",
          "deck_combos" not in before and "deck_combo_sync" not in before)

    q.ensure_schema(conn)
    after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    check("ensure_schema() recreates deck_combos on an upgraded database", "deck_combos" in after)
    check("ensure_schema() recreates deck_combo_sync too", "deck_combo_sync" in after)

    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'Upgraded')")
    conn.execute(
        "INSERT INTO deck_combos (deck_id, combo_id, category, uses) VALUES (1,'a-b','included','X')"
    )
    conn.commit()
    q.ensure_schema(conn)  # re-run must not wipe or error
    check(
        "re-running ensure_schema() leaves existing deck_combos rows alone",
        conn.execute("SELECT COUNT(*) FROM deck_combos").fetchone()[0] == 1,
    )
    conn.close()

    # ------------------------------------------------------------------
    # C. front_face() normalization
    # ------------------------------------------------------------------
    check("front_face() takes the front face of an MDFC",
          sb.front_face("Secret Arcade // Dusty Parlor") == "secret arcade")
    check("front_face() lowercases and strips", sb.front_face("  Sol Ring  ") == "sol ring")
    check("front_face() keeps commas in a name intact",
          sb.front_face("Vilis, Broker of Blood") == "vilis, broker of blood")
    check("front_face() tolerates None", sb.front_face(None) == "")

    # ------------------------------------------------------------------
    # D. build_deck_payload()
    # ------------------------------------------------------------------
    rows = [
        ("Vilis, Broker of Blood", 1),
        ("Secret Arcade // Dusty Parlor", 1),
        ("Swamp", 12),
        ("Sol Ring", None),
        ("", 1),
    ]
    payload = sb.build_deck_payload(rows, commander="Vilis, Broker of Blood")
    main_names = [c["card"] for c in payload["main"]]
    check("payload puts the commander in `commanders`",
          [c["card"] for c in payload["commanders"]] == ["Vilis, Broker of Blood"])
    check("payload excludes the commander from `main`",
          "Vilis, Broker of Blood" not in main_names)
    check("payload sends an MDFC as its front face only", "Secret Arcade" in main_names)
    check("payload preserves a multi-copy quantity (basic lands)",
          {c["card"]: c["quantity"] for c in payload["main"]}["Swamp"] == 12)
    check("payload coerces a NULL quantity to 1",
          {c["card"]: c["quantity"] for c in payload["main"]}["Sol Ring"] == 1)
    check("payload skips a blank card name", "" not in main_names)

    partnered = sb.build_deck_payload(
        [("Miara, Thorn of the Glade", 1), ("Kamahl, Heart of Krosa", 1), ("Sol Ring", 1)],
        commander="Miara, Thorn of the Glade", partner="Kamahl, Heart of Krosa",
    )
    check("payload sends both commander and partner as commanders",
          len(partnered["commanders"]) == 2 and len(partnered["main"]) == 1)

    # ------------------------------------------------------------------
    # D2. pandas NaN inputs (regression — this shipped broken once)
    #
    # The Decks page passes queries.deck_meta() values (sqlite3.Row, so a
    # missing partner is None), but the Card Database page's bulk path
    # iterates loaders.load_decks_df() — a DataFrame, where a column that's
    # NULL for every row is typed float64 and each value is NaN. NaN is a
    # float AND truthy, so `if partner:` let it through and `.strip()` blew
    # up with AttributeError on the real "Check all decks" button. Every
    # name now routes through clean_name().
    # ------------------------------------------------------------------
    nan = float("nan")
    check("clean_name() treats NaN as absent", sb.clean_name(nan) == "")
    check("clean_name() treats None as absent", sb.clean_name(None) == "")
    check("clean_name() strips a real name", sb.clean_name("  Sol Ring ") == "Sol Ring")
    check("front_face() survives NaN instead of raising", sb.front_face(nan) == "")

    nan_payload = sb.build_deck_payload(
        [("Sol Ring", 1), (nan, 1)], commander="Vilis, Broker of Blood", partner=nan,
    )
    check("a NaN partner is dropped instead of raising AttributeError",
          nan_payload["commanders"] == [{"card": "Vilis, Broker of Blood", "quantity": 1}])
    check("a NaN card name is skipped rather than sent",
          [c["card"] for c in nan_payload["main"]] == ["Sol Ring"])

    nan_qty = sb.build_deck_payload([("Swamp", nan), ("Sol Ring", "3"), ("Forest", 0)])
    sent_qty = {c["card"]: c["quantity"] for c in nan_qty["main"]}
    check("a NaN quantity falls back to 1 (and stays a JSON-safe int)",
          sent_qty["Swamp"] == 1 and isinstance(sent_qty["Swamp"], int))
    check("a numeric-string quantity is coerced to int", sent_qty["Sol Ring"] == 3)
    check("a zero/negative quantity floors at 1", sent_qty["Forest"] == 1)

    nan_norm = sb.normalize_response(
        _response(almost=[_variant("n-1", ["Sol Ring", "Mana Crypt"], ["Fast mana"], 1)]),
        ["Sol Ring", nan, None],
    )
    check("normalize_response() tolerates NaN among the deck's card names",
          nan_norm["almost"][0]["missing"] == ["Mana Crypt"])

    nan_result, nan_error = sb.fetch_deck_combos(
        [("Sol Ring", 1)], commander="Vilis, Broker of Blood", partner=nan,
        client=FakeClient(_response()),
    )
    check("fetch_deck_combos() survives a NaN partner end to end",
          nan_error is None and nan_result["included"] == [])

    # ------------------------------------------------------------------
    # E. normalize_response()
    # ------------------------------------------------------------------
    deck_names = ["Vilis, Broker of Blood", "Peer into the Abyss", "Psychosis Crawler",
                  "Sanctum Weaver", "Repay in Kind", "Blood Celebrant"]
    raw = _response(
        included=[
            _variant("1561-2384", ["Psychosis Crawler", "Peer into the Abyss"],
                     ["Near-infinite lifeloss"], 30793, bracket_tag="P"),
            _variant("2358-2656--84", ["Repay in Kind", "Blood Celebrant"],
                     ["Each opponent loses the game"], 4585, bracket_tag="E",
                     requires=['Permanent with "You don\'t lose the game"']),
        ],
        almost=[
            _variant("1355-4222", ["Sanctum Weaver", "Freed from the Real"],
                     ["Infinite colored mana"], 9269, bracket_tag="S",
                     notable="Your life total is at least 2.", easy="All permanents untapped."),
            _variant("1355-3525", ["Sanctum Weaver", "Pemmin's Aura"],
                     ["Infinite colored mana"], None, bracket_tag="R"),
        ],
        identity="B",
    )
    norm = sb.normalize_response(raw, deck_names)

    check("normalize_response() carries the resolved color identity through", norm["identity"] == "B")
    check("normalize_response() splits included/almost", len(norm["included"]) == 2 and len(norm["almost"]) == 2)
    check("an included combo computes to zero missing cards",
          all(c["missing"] == [] for c in norm["included"]))
    check("an almost combo names the one card that's missing",
          norm["almost"][0]["missing"] == ["Freed from the Real"])
    check("combos are sorted most-popular-first",
          [c["combo_id"] for c in norm["included"]] == ["1561-2384", "2358-2656--84"])
    check("an unranked (None popularity) combo sorts last, without raising",
          [c["combo_id"] for c in norm["almost"]] == ["1355-4222", "1355-3525"])
    check("bracketTag is decoded to its label",
          norm["included"][0]["bracket_tag"] == "Powerful" and norm["almost"][1]["bracket_tag"] == "Ruthless")
    check("template requirements are captured under `requires`",
          norm["included"][1]["requires"] == ['Permanent with "You don\'t lose the game"'])
    check("notable and easy prerequisites are joined, notable first",
          norm["almost"][0]["prerequisites"] == "Your life total is at least 2.\nAll permanents untapped.")
    check("commander legality is carried through as a bool",
          norm["included"][0]["commander_legal"] is True)

    # An MDFC missing card must match the deck by front face, not full string.
    mdfc_norm = sb.normalize_response(
        _response(almost=[_variant("x-y", ["Ondu Spiritdancer", "Secret Arcade // Dusty Parlor"],
                                   ["Infinite creature ETB"], 100)]),
        ["Ondu Spiritdancer", "Secret Arcade // Dusty Parlor"],
    )
    check("an MDFC already in the deck isn't reported missing over a name-format mismatch",
          mdfc_norm["almost"][0]["missing"] == [])

    # ------------------------------------------------------------------
    # F. fetch_deck_combos() with an injected fake client
    # ------------------------------------------------------------------
    fake = FakeClient(raw)
    result, error = sb.fetch_deck_combos(
        [(n, 1) for n in deck_names], commander="Vilis, Broker of Blood", client=fake,
    )
    check("fetch_deck_combos() returns a result and no error on success",
          error is None and len(result["included"]) == 2)
    check("fetch_deck_combos() sent one payload with the commander split out",
          len(fake.payloads) == 1 and fake.payloads[0]["commanders"][0]["card"] == "Vilis, Broker of Blood")

    failed = FakeClient(None, misses=["network error: boom"])
    result_none, error_msg = sb.fetch_deck_combos([("Sol Ring", 1)], client=failed)
    check("fetch_deck_combos() reports the client's failure reason",
          result_none is None and "boom" in error_msg)

    empty_result, empty_error = sb.fetch_deck_combos([], client=FakeClient(raw))
    check("fetch_deck_combos() refuses an empty deck without calling the API",
          empty_result is None and "no cards" in empty_error)

    # ------------------------------------------------------------------
    # F2. combo_list_text() / combo_check_summary() — the actual
    # "what are the combos" text shown after a check, not just a count.
    # This is what the Decks page's post-check flash message and the Card
    # Database bulk summary are both built from.
    # ------------------------------------------------------------------
    check("combo_list_text() of an empty list is empty", sb.combo_list_text([]) == "")

    two_combos = [
        {"uses": ["Sol Ring", "Mana Vault"], "produces": ["Infinite mana", "Infinite colorless mana"]},
        {"uses": ["Thassa's Oracle", "Demonic Consultation"], "produces": ["Win the game"]},
    ]
    text = sb.combo_list_text(two_combos, limit=4)
    check("combo_list_text() names the actual cards and what they produce",
          "Sol Ring + Mana Vault → Infinite mana, Infinite colorless mana" in text
          and "Thassa's Oracle + Demonic Consultation → Win the game" in text)
    check("combo_list_text() has no '+N more' suffix when everything fits under the limit",
          "more" not in text)

    capped = sb.combo_list_text(two_combos, limit=1)
    check("combo_list_text() truncates to `limit` and counts the rest as '+N more'",
          capped.startswith("Sol Ring + Mana Vault") and "(+1 more)" in capped
          and "Thassa's Oracle" not in capped)

    no_produces = sb.combo_list_text([{"uses": ["Card A", "Card B"], "produces": []}])
    check("combo_list_text() omits the arrow when a combo has nothing listed under produces",
          no_produces == "Card A + Card B")

    check("combo_check_summary() of a falsy result is empty", sb.combo_check_summary(None) == "")
    check("combo_check_summary() of an empty result is empty", sb.combo_check_summary({}) == "")

    both_summary = sb.combo_check_summary({"included": two_combos[:1], "almost": two_combos[1:]})
    check("combo_check_summary() names the included combo, not just its count",
          "Sol Ring + Mana Vault → Infinite mana" in both_summary)
    check("combo_check_summary() states the included count",
          both_summary.startswith("1 combo(s) in the deck"))
    check("combo_check_summary() states the almost count as a separate clause",
          "1 one card away" in both_summary)

    included_only = sb.combo_check_summary({"included": two_combos, "almost": []})
    check("combo_check_summary() omits the one-card-away clause when there is none",
          "one card away" not in included_only)

    almost_only = sb.combo_check_summary({"included": [], "almost": two_combos})
    check("combo_check_summary() says plainly that nothing is fully assembled yet",
          almost_only.startswith("No complete combos in the deck yet"))
    check("combo_check_summary() still states the one-card-away count when nothing is included",
          "2 one card away" in almost_only)

    nothing_found = sb.combo_check_summary({"included": [], "almost": []})
    check("combo_check_summary() gives a real, non-blank answer when Spellbook found nothing at all",
          "doesn't know of any combos" in nothing_found)

    # ------------------------------------------------------------------
    # G/H/I. save/clear/read round-trip
    # ------------------------------------------------------------------
    conn = build_db()
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'Vilis, Blood ATM')")
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (2, 'Untouched Deck')")
    conn.commit()

    n_inc, n_alm = w.save_deck_combos(conn, 1, norm)
    check("save_deck_combos() reports what it stored", (n_inc, n_alm) == (2, 2))

    inc_df = q.deck_combos(conn, 1, "included")
    alm_df = q.deck_combos(conn, 1, "almost")
    check("deck_combos() reads back the included rows", len(inc_df) == 2)
    check("deck_combos() reads back the almost rows", len(alm_df) == 2)
    check("deck_combos() orders by popularity descending",
          list(inc_df["combo_id"]) == ["1561-2384", "2358-2656--84"])
    check("newline-joined card lists round-trip intact",
          inc_df.iloc[0]["uses"] == "Psychosis Crawler\nPeer into the Abyss")
    check("splitting a stored list on newlines recovers the exact names",
          inc_df.iloc[0]["uses"].split("\n") == ["Psychosis Crawler", "Peer into the Abyss"])
    # The whole reason these lists are newline-joined rather than
    # comma-joined: card names routinely contain commas, so a comma split
    # would tear one name into two (see schema.sql).
    comma_norm = sb.normalize_response(
        _response(included=[_variant("comma-1",
                                     ["Vilis, Broker of Blood", "Kamahl, Heart of Krosa"],
                                     ["Win"], 5)]),
        ["Vilis, Broker of Blood", "Kamahl, Heart of Krosa"],
    )
    w.save_deck_combos(conn, 2, comma_norm)
    comma_uses = q.deck_combos(conn, 2, "included").iloc[0]["uses"]
    check("a card name containing a comma survives storage intact",
          comma_uses.split("\n") == ["Vilis, Broker of Blood", "Kamahl, Heart of Krosa"])
    w.clear_deck_combos(conn, 2)
    check("the missing card is stored for an almost combo",
          alm_df.iloc[0]["missing"] == "Freed from the Real")
    check("included rows store an empty missing list",
          inc_df.iloc[0]["missing"] == "")

    sync = q.deck_combo_sync(conn, 1)
    check("deck_combo_sync() records the counts", (sync["included_count"], sync["almost_count"]) == (2, 2))
    check("deck_combo_sync() records the identity and a timestamp",
          sync["identity"] == "B" and sync["fetched_at"])
    check("a never-synced deck returns None, distinguishing it from 'found nothing'",
          q.deck_combo_sync(conn, 2) is None)

    sync_all = q.deck_combo_sync_all(conn)
    check("deck_combo_sync_all() lists every deck, synced or not", len(sync_all) == 2)
    check("deck_combo_sync_all() leaves a never-synced deck's counts NULL",
          sync_all[sync_all["deck_name"] == "Untouched Deck"].iloc[0]["included_count"] is None
          or str(sync_all[sync_all["deck_name"] == "Untouched Deck"].iloc[0]["included_count"]) == "nan")

    # Re-sync with fewer combos must REPLACE, not merge (a combo that no
    # longer applies has to disappear).
    smaller = sb.normalize_response(
        _response(included=[_variant("1561-2384", ["Psychosis Crawler", "Peer into the Abyss"],
                                     ["Near-infinite lifeloss"], 30793)]),
        deck_names,
    )
    w.save_deck_combos(conn, 1, smaller)
    check("a re-sync replaces rather than merges the cached combos",
          len(q.deck_combos(conn, 1, "included")) == 1 and len(q.deck_combos(conn, 1, "almost")) == 0)
    check("the sync stamp is updated to the new counts",
          q.deck_combo_sync(conn, 1)["included_count"] == 1)

    # A duplicate combo_id within one response must not abort the refresh.
    dupes = sb.normalize_response(
        _response(included=[
            _variant("dup-1", ["A", "B"], ["Win"], 10),
            _variant("dup-1", ["A", "B"], ["Win"], 10),
        ]),
        ["A", "B"],
    )
    w.save_deck_combos(conn, 1, dupes)
    check("a duplicate combo_id collapses to one row instead of failing",
          len(q.deck_combos(conn, 1, "included")) == 1)

    w.clear_deck_combos(conn, 1)
    check("clear_deck_combos() removes the cached rows",
          len(q.deck_combos(conn, 1, "included")) == 0)
    check("clear_deck_combos() also clears the sync stamp, back to 'never fetched'",
          q.deck_combo_sync(conn, 1) is None)
    conn.close()

    # ------------------------------------------------------------------
    # J. owned_card_quantities()
    # ------------------------------------------------------------------
    conn = build_db()
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'Deck One')")
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (2, 'Deck Two')")
    for sid, name in [("c1", "Freed from the Real"), ("c2", "Sol Ring"),
                      ("c3", "Swamp"), ("c4", "Secret Arcade // Dusty Parlor")]:
        conn.execute(
            "INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES (?,?,'TST','1')",
            (sid, name),
        )
    # Freed: one loose copy (free) + one sleeved in Deck Two (owned, not free)
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('c1', 1, 'Box')")
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('c1', 1, 'Deck Two')")
    # Sol Ring: only copy is sleeved in a deck
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('c2', 1, 'Deck One')")
    # Swamp: untracked bulk quantity (NULL)
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('c3', NULL, 'Lands Box')")
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('c4', 1, NULL)")
    conn.commit()

    owned = q.owned_card_quantities(conn)
    check("owned_card_quantities() keys on the front-face lowercase name",
          "freed from the real" in owned and "secret arcade" in owned)
    check("a loose copy counts as available", owned["freed from the real"]["available_qty"] == 1)
    check("both copies count as owned", owned["freed from the real"]["owned_qty"] == 2)
    check("a card sleeved in another deck is owned but not available",
          owned["sol ring"]["owned_qty"] == 1 and owned["sol ring"]["available_qty"] == 0)
    check("a NULL (bulk) quantity counts as 1 rather than 0",
          owned["swamp"]["owned_qty"] == 1)
    check("a NULL location counts as available", owned["secret arcade"]["available_qty"] == 1)
    check("an unowned card is simply absent", "pemmin's aura" not in owned)

    # ------------------------------------------------------------------
    # K. delete_deck() clears the combo cache (FK would otherwise abort)
    # ------------------------------------------------------------------
    w.save_deck_combos(conn, 1, norm)
    # deck_swap_queue is the frozen pre-rework table (nothing writes it any
    # more), but rows already in a database still carry a deck_id FK, so
    # delete_deck() must keep clearing it. Seeded directly, as the Workbench
    # rework's backfill leaves it. deck_changes is its live replacement.
    conn.execute(
        "INSERT INTO deck_swap_queue (deck_id, add_name, remove_scryfall_id, remove_name, quantity) "
        "VALUES (1, 'Freed from the Real', 'c2', 'Sol Ring', 1)"
    )
    w.add_change(conn, 1, add_name="Freed from the Real", remove_scryfall_id="c2",
                 remove_name="Sol Ring", status="planned")
    conn.commit()
    check("the deck has cached combos and queued/planned swaps before deletion",
          len(q.deck_combos(conn, 1, "included")) == 2
          and conn.execute("SELECT COUNT(*) FROM deck_swap_queue WHERE deck_id=1").fetchone()[0] == 1
          and len(w.list_changes(conn, 1)) == 1)
    deleted_name, delete_error = w.delete_deck(conn, 1)
    check("delete_deck() succeeds with combo rows and a queued swap present",
          delete_error is None and deleted_name == "Deck One")
    check("delete_deck() leaves no orphaned deck_combos rows",
          conn.execute("SELECT COUNT(*) FROM deck_combos WHERE deck_id=1").fetchone()[0] == 0)
    check("delete_deck() leaves no orphaned deck_combo_sync row",
          q.deck_combo_sync(conn, 1) is None)
    check("delete_deck() leaves no orphaned deck_swap_queue rows",
          conn.execute("SELECT COUNT(*) FROM deck_swap_queue WHERE deck_id=1").fetchone()[0] == 0)
    check("delete_deck() leaves no orphaned deck_changes rows",
          conn.execute("SELECT COUNT(*) FROM deck_changes WHERE deck_id=1").fetchone()[0] == 0)
    conn.close()

    # ------------------------------------------------------------------
    # L2. fetch_many() driven from a real DataFrame, exactly as the Card
    # Database page builds its input. This is the shape that caught the NaN
    # bug in production, and the fixture below is deliberately MIXED —
    # one deck with a partner, one without — because that's what actually
    # manufactures the NaN:
    #
    #   all partners NULL  -> dtype object, values are None    (harmless)
    #   SOME partner set   -> dtype str,    NULLs become NaN   (the bug)
    #
    # The real database is the second case (2 of 12 decks have partners),
    # which is why "Check all decks" crashed while the per-deck button —
    # fed by queries.deck_meta(), a sqlite3.Row that yields None — did not.
    # An all-NULL fixture would quietly pass and prove nothing.
    # ------------------------------------------------------------------
    conn = build_db()
    conn.execute("INSERT INTO decks (deck_id, name, commander, partner) "
                 "VALUES (1,'Partnered Deck','Miara, Thorn of the Glade','Kamahl, Heart of Krosa')")
    conn.execute("INSERT INTO decks (deck_id, name, commander) VALUES (2,'Solo Deck','Ghoulcaller Gisa')")
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('c1','Sol Ring','TST','1')")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1,'c1',1)")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (2,'c1',1)")
    conn.commit()

    decks_df = q.list_decks(conn)
    partners = list(decks_df.sort_values("deck_id")["partner"])
    check("a mixed partner column really does yield a float NaN, not None "
          "(the exact condition that broke the bulk refresh)",
          isinstance(partners[1], float) and partners[1] != partners[1])
    check("...and that NaN is truthy, which is why `if partner:` didn't catch it",
          bool(partners[1]) is True)

    deck_inputs = [
        (row["deck_id"], row["name"], row["commander"], row["partner"],
         q.deck_combo_card_rows(conn, row["deck_id"]))
        for _, row in decks_df.iterrows()
    ]
    bulk_client = FakeClient(_response(included=[
        _variant("bulk-1", ["Sol Ring", "Basalt Monolith"], ["Infinite mana"], 500)
    ]))
    bulk_seen = []
    for deck_id, deck_name, result, error in sb.fetch_many(
        deck_inputs, progress_callback=lambda *a: None, pause=0, client=bulk_client,
    ):
        bulk_seen.append((deck_name, error))
        if not error:
            w.save_deck_combos(conn, deck_id, result)

    check("fetch_many() processes every deck off a DataFrame without raising",
          len(bulk_seen) == 2 and all(err is None for _, err in bulk_seen))
    check("the bulk path actually cached combos for both decks",
          len(q.deck_combos(conn, 1, "included")) == 1
          and len(q.deck_combos(conn, 2, "included")) == 1)
    sent_commanders = {
        tuple(c["card"] for c in p["commanders"]) for p in bulk_client.payloads
    }
    check("the partnered deck sent BOTH commanders",
          ("Miara, Thorn of the Glade", "Kamahl, Heart of Krosa") in sent_commanders)
    check("the partnerless deck sent exactly one commander, with no NaN alongside it",
          ("Ghoulcaller Gisa",) in sent_commanders)
    conn.close()

    # ------------------------------------------------------------------
    # L. Source-text checks — the panel and cache invalidation are wired in
    # ------------------------------------------------------------------
    decks_page = open(os.path.join(PROJECT_ROOT, "pages", "2_Decks.py"), encoding="utf-8").read()
    check("the Decks page imports spellbook", "spellbook" in decks_page)
    check("the Decks page renders a combo section", "🔗 Combos" in decks_page)
    check("the Decks page offers a Spellbook refresh button", "Check Spellbook" in decks_page)
    check("the Decks page saves what it fetches", "save_deck_combos" in decks_page)
    check("the Decks page reads the owned-card lookup for the missing-card flag",
          "load_owned_card_quantities" in decks_page)
    check("a Check Spellbook result builds its flash text from combo_check_summary(), "
          "not just a bare count",
          "combo_check_summary" in decks_page)
    check("the post-check flash message is stashed in session_state so it survives the rerun",
          "combo_flash_key" in decks_page and "st.session_state" in decks_page)

    loaders_src = open(os.path.join(PROJECT_ROOT, "dashboard_lib", "loaders.py"), encoding="utf-8").read()
    check("loaders exposes the combo loaders",
          "load_deck_combos" in loaders_src and "load_deck_combo_sync" in loaders_src)
    check("loaders exposes invalidate_combo_caches()", "def invalidate_combo_caches" in loaders_src)
    check("a decklist edit drops the cached combo reads",
          "load_deck_combos.clear()" in loaders_src)
    check("a collection edit drops the owned-card lookup",
          "load_owned_card_quantities.clear()" in loaders_src)

    # ------------------------------------------------------------------
    # M. The OLD hand-typed decks.combos field was actually removed from
    # every surface, not just superseded by a panel sitting alongside it.
    # ------------------------------------------------------------------
    check("the Decks page's Turn 0 grid no longer displays the old Combos field",
          "meta.get('combos')" not in decks_page and '**Combos**\\n\\n' not in decks_page)

    editor_src = open(os.path.join(PROJECT_ROOT, "pages", "4_Deck_Editor.py"), encoding="utf-8").read()
    check("the Deck Editor no longer has a Combos text_area",
          'st.text_area("Combos"' not in editor_src)
    check("the Deck Editor's save call no longer writes decks.combos",
          "combos=combos.strip()" not in editor_src)
    # DECK_META_FIELDS must still ACCEPT "combos" even though nothing in
    # the UI sends it anymore — scripts/migrate.py still seeds it from the
    # CSV's Combos column on initial import (the one-way CSV->DB path is
    # untouched), and removing it from the whitelist would silently drop
    # that seed on a fresh migration.
    writes_src = open(os.path.join(PROJECT_ROOT, "dashboard_lib", "writes.py"), encoding="utf-8").read()
    check('DECK_META_FIELDS still whitelists "combos" for scripts/migrate.py\'s initial CSV import',
          '"combos"' in writes_src)
    migrate_src = open(os.path.join(PROJECT_ROOT, "scripts", "migrate.py"), encoding="utf-8").read()
    check("migrate.py's one-way CSV import still seeds decks.combos from the CSV (untouched)",
          "combos=row.get(\"Combos\")" in migrate_src)

    printout_src = open(os.path.join(PROJECT_ROOT, "dashboard_lib", "deck_printout.py"), encoding="utf-8").read()
    check("the printable deck sheet no longer renders the old Combos field",
          'meta.get("combos")' not in printout_src)

    print("\nAll Commander Spellbook offline checks passed.")


if __name__ == "__main__":
    main()
