"""Commander Spellbook combo feature (dashboard_lib/spellbook.py): sends a
deck to the find-my-combos API and caches two lists per deck, combos fully
present ("included") and combos one card short ("almost"), the latter
cross-referenced against the collection. No network: the client is a fake.

The response shape (_variant / _response below) mirrors real API payloads
captured while building this, not invented fixtures.

Replaces the old scripts/_test_spellbook_offline harness. Its source-text
checks (that pages grep for certain strings) are dropped: the Decks page is
exercised by test_pages_smoke.py instead.
"""
import sqlite3

import pytest

from dashboard_lib import queries as q
from dashboard_lib import spellbook as sb
from dashboard_lib import writes as w

NAN = float("nan")


# ------------------------------------------------------------------
# Canned API pieces
# ------------------------------------------------------------------
def variant(vid, uses, produces, popularity, bracket_tag="P", requires=(),
            notable="", easy="", mana="{1}", commander_legal=True, description=""):
    return {
        "id": vid,
        "uses": [{"card": {"name": n}, "quantity": 1} for n in uses],
        "requires": [{"template": {"name": t}} for t in requires],
        "produces": [{"feature": {"name": f}} for f in produces],
        "popularity": popularity, "bracketTag": bracket_tag,
        "notablePrerequisites": notable, "easyPrerequisites": easy,
        "manaNeeded": mana, "description": description,
        "legalities": {"commander": commander_legal},
    }


def response(included=(), almost=(), identity="B"):
    return {"count": None, "next": None, "previous": None, "results": {
        "identity": identity, "included": list(included),
        "includedByChangingCommanders": [], "almostIncluded": list(almost),
        "almostIncludedByAddingColors": [], "almostIncludedByChangingCommanders": [],
        "almostIncludedByAddingColorsAndChangingCommanders": []}}


class FakeClient:
    """Stands in for SpellbookClient: records payloads, returns a canned
    response (or None to exercise the failure branch)."""

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


DECK_NAMES = ["Vilis, Broker of Blood", "Peer into the Abyss", "Psychosis Crawler",
              "Sanctum Weaver", "Repay in Kind", "Blood Celebrant"]


@pytest.fixture
def raw():
    return response(
        included=[
            variant("1561-2384", ["Psychosis Crawler", "Peer into the Abyss"],
                    ["Near-infinite lifeloss"], 30793, bracket_tag="P"),
            variant("2358-2656--84", ["Repay in Kind", "Blood Celebrant"],
                    ["Each opponent loses the game"], 4585, bracket_tag="E",
                    requires=['Permanent with "You don\'t lose the game"']),
        ],
        almost=[
            variant("1355-4222", ["Sanctum Weaver", "Freed from the Real"],
                    ["Infinite colored mana"], 9269, bracket_tag="S",
                    notable="Your life total is at least 2.", easy="All permanents untapped."),
            variant("1355-3525", ["Sanctum Weaver", "Pemmin's Aura"],
                    ["Infinite colored mana"], None, bracket_tag="R"),
        ])


@pytest.fixture
def norm(raw):
    return sb.normalize_response(raw, DECK_NAMES)


# ------------------------------------------------------------------
# Schema: fresh database and the upgrade path
# ------------------------------------------------------------------
def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_fresh_schema_has_both_combo_tables(conn):
    assert {"deck_combos", "deck_combo_sync"} <= tables(conn)
    assert conn.execute("SELECT COUNT(*) FROM deck_combos").fetchone()[0] == 0


def test_category_check_rejects_a_typo(conn):
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'T')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO deck_combos (deck_id, combo_id, category) VALUES (1, 'x', 'bogus')")


def test_ensure_schema_creates_the_tables_on_an_old_database_and_is_idempotent(conn):
    conn.execute("DROP TABLE deck_combos")
    conn.execute("DROP TABLE deck_combo_sync")
    conn.commit()
    q.ensure_schema(conn)
    assert {"deck_combos", "deck_combo_sync"} <= tables(conn)
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'Upgraded')")
    conn.execute("INSERT INTO deck_combos (deck_id, combo_id, category, uses) VALUES (1,'a-b','included','X')")
    conn.commit()
    q.ensure_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM deck_combos").fetchone()[0] == 1


# ------------------------------------------------------------------
# Name normalization
# ------------------------------------------------------------------
@pytest.mark.parametrize("name, expected", [
    ("Secret Arcade // Dusty Parlor", "secret arcade"),   # MDFC: front face
    ("  Sol Ring  ", "sol ring"),
    ("Vilis, Broker of Blood", "vilis, broker of blood"),  # commas kept
    (None, ""),
    (NAN, ""),
])
def test_front_face(name, expected):
    assert sb.front_face(name) == expected


def test_clean_name_treats_none_and_nan_as_absent():
    assert sb.clean_name(NAN) == "" and sb.clean_name(None) == ""
    assert sb.clean_name("  Sol Ring ") == "Sol Ring"


# ------------------------------------------------------------------
# build_deck_payload
# ------------------------------------------------------------------
def test_payload_splits_the_commander_out_and_normalizes_the_rest():
    rows = [("Vilis, Broker of Blood", 1), ("Secret Arcade // Dusty Parlor", 1),
            ("Swamp", 12), ("Sol Ring", None), ("", 1)]
    payload = sb.build_deck_payload(rows, commander="Vilis, Broker of Blood")
    main = {c["card"]: c["quantity"] for c in payload["main"]}
    assert [c["card"] for c in payload["commanders"]] == ["Vilis, Broker of Blood"]
    assert "Vilis, Broker of Blood" not in main
    assert "Secret Arcade" in main                    # an MDFC is sent as its front face
    assert main["Swamp"] == 12                        # multi-copy quantities kept
    assert main["Sol Ring"] == 1                      # NULL quantity -> 1
    assert "" not in main                             # blank names skipped


def test_payload_sends_commander_and_partner_both_as_commanders():
    payload = sb.build_deck_payload(
        [("Miara, Thorn of the Glade", 1), ("Kamahl, Heart of Krosa", 1), ("Sol Ring", 1)],
        commander="Miara, Thorn of the Glade", partner="Kamahl, Heart of Krosa")
    assert len(payload["commanders"]) == 2 and len(payload["main"]) == 1


# Regression: the Card Database page's "Check all decks" feeds DataFrame
# values, where a column that is NULL for some rows is NaN, which is a float
# AND truthy, so `if partner:` let it through and `.strip()` raised.
def test_nan_partner_and_card_name_are_dropped():
    payload = sb.build_deck_payload([("Sol Ring", 1), (NAN, 1)],
                                    commander="Vilis, Broker of Blood", partner=NAN)
    assert payload["commanders"] == [{"card": "Vilis, Broker of Blood", "quantity": 1}]
    assert [c["card"] for c in payload["main"]] == ["Sol Ring"]


def test_odd_quantities_are_coerced_to_json_safe_ints():
    sent = {c["card"]: c["quantity"]
            for c in sb.build_deck_payload([("Swamp", NAN), ("Sol Ring", "3"), ("Forest", 0)])["main"]}
    assert sent["Swamp"] == 1 and isinstance(sent["Swamp"], int)
    assert sent["Sol Ring"] == 3
    assert sent["Forest"] == 1                        # zero/negative floors at 1


def test_normalize_and_fetch_survive_nan_inputs():
    norm = sb.normalize_response(
        response(almost=[variant("n-1", ["Sol Ring", "Mana Crypt"], ["Fast mana"], 1)]),
        ["Sol Ring", NAN, None])
    assert norm["almost"][0]["missing"] == ["Mana Crypt"]
    result, error = sb.fetch_deck_combos([("Sol Ring", 1)], commander="Vilis, Broker of Blood",
                                         partner=NAN, client=FakeClient(response()))
    assert error is None and result["included"] == []


# ------------------------------------------------------------------
# normalize_response
# ------------------------------------------------------------------
def test_normalize_splits_included_from_almost_and_computes_missing(norm):
    assert norm["identity"] == "B"
    assert len(norm["included"]) == 2 and len(norm["almost"]) == 2
    assert all(c["missing"] == [] for c in norm["included"])
    assert norm["almost"][0]["missing"] == ["Freed from the Real"]


def test_normalize_sorts_by_popularity_with_unranked_last(norm):
    assert [c["combo_id"] for c in norm["included"]] == ["1561-2384", "2358-2656--84"]
    assert [c["combo_id"] for c in norm["almost"]] == ["1355-4222", "1355-3525"]


def test_normalize_decodes_and_carries_the_detail_fields(norm):
    assert norm["included"][0]["bracket_tag"] == "Powerful"
    assert norm["almost"][1]["bracket_tag"] == "Ruthless"
    assert norm["included"][1]["requires"] == ['Permanent with "You don\'t lose the game"']
    assert norm["almost"][0]["prerequisites"] == "Your life total is at least 2.\nAll permanents untapped."
    assert norm["included"][0]["commander_legal"] is True


def test_an_mdfc_in_the_deck_is_not_reported_missing_over_a_name_format_mismatch():
    norm = sb.normalize_response(
        response(almost=[variant("x-y", ["Ondu Spiritdancer", "Secret Arcade // Dusty Parlor"],
                                 ["Infinite creature ETB"], 100)]),
        ["Ondu Spiritdancer", "Secret Arcade // Dusty Parlor"])
    assert norm["almost"][0]["missing"] == []


# ------------------------------------------------------------------
# fetch_deck_combos with an injected client
# ------------------------------------------------------------------
def test_fetch_returns_the_result_and_sends_one_payload(raw):
    fake = FakeClient(raw)
    result, error = sb.fetch_deck_combos([(n, 1) for n in DECK_NAMES],
                                         commander="Vilis, Broker of Blood", client=fake)
    assert error is None and len(result["included"]) == 2
    assert len(fake.payloads) == 1
    assert fake.payloads[0]["commanders"][0]["card"] == "Vilis, Broker of Blood"


def test_fetch_reports_the_clients_failure_reason():
    result, error = sb.fetch_deck_combos([("Sol Ring", 1)],
                                         client=FakeClient(None, misses=["network error: boom"]))
    assert result is None and "boom" in error


def test_fetch_refuses_an_empty_deck_without_calling_the_api(raw):
    fake = FakeClient(raw)
    result, error = sb.fetch_deck_combos([], client=fake)
    assert result is None and "no cards" in error and fake.payloads == []


# ------------------------------------------------------------------
# combo_list_text / combo_check_summary: the flash text naming the combos
# ------------------------------------------------------------------
TWO_COMBOS = [
    {"uses": ["Sol Ring", "Mana Vault"], "produces": ["Infinite mana", "Infinite colorless mana"]},
    {"uses": ["Thassa's Oracle", "Demonic Consultation"], "produces": ["Win the game"]},
]


def test_combo_list_text():
    assert sb.combo_list_text([]) == ""
    text = sb.combo_list_text(TWO_COMBOS, limit=4)
    assert "Sol Ring + Mana Vault → Infinite mana, Infinite colorless mana" in text
    assert "Thassa's Oracle + Demonic Consultation → Win the game" in text
    assert "more" not in text                         # everything fits under the limit


def test_combo_list_text_truncates_and_counts_the_rest():
    capped = sb.combo_list_text(TWO_COMBOS, limit=1)
    assert capped.startswith("Sol Ring + Mana Vault") and "(+1 more)" in capped
    assert "Thassa's Oracle" not in capped


def test_combo_list_text_omits_the_arrow_without_produces():
    assert sb.combo_list_text([{"uses": ["Card A", "Card B"], "produces": []}]) == "Card A + Card B"


def test_check_summary_branches():
    assert sb.combo_check_summary(None) == "" and sb.combo_check_summary({}) == ""

    both = sb.combo_check_summary({"included": TWO_COMBOS[:1], "almost": TWO_COMBOS[1:]})
    assert both.startswith("1 combo(s) in the deck")
    assert "Sol Ring + Mana Vault → Infinite mana" in both     # names them, not just a count
    assert "1 one card away" in both

    assert "one card away" not in sb.combo_check_summary({"included": TWO_COMBOS, "almost": []})

    almost_only = sb.combo_check_summary({"included": [], "almost": TWO_COMBOS})
    assert almost_only.startswith("No complete combos in the deck yet")
    assert "2 one card away" in almost_only

    assert "doesn't know of any combos" in sb.combo_check_summary({"included": [], "almost": []})


# ------------------------------------------------------------------
# save / read / clear round-trip
# ------------------------------------------------------------------
@pytest.fixture
def two_decks(conn):
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'Vilis, Blood ATM')")
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (2, 'Untouched Deck')")
    conn.commit()
    return conn


def test_save_and_read_back(two_decks, norm):
    assert w.save_deck_combos(two_decks, 1, norm) == (2, 2)
    included, almost = q.deck_combos(two_decks, 1, "included"), q.deck_combos(two_decks, 1, "almost")
    assert (len(included), len(almost)) == (2, 2)
    assert list(included["combo_id"]) == ["1561-2384", "2358-2656--84"]   # popularity descending
    assert included.iloc[0]["uses"] == "Psychosis Crawler\nPeer into the Abyss"
    assert almost.iloc[0]["missing"] == "Freed from the Real"
    assert included.iloc[0]["missing"] == ""


def test_card_names_with_commas_survive_storage(two_decks):
    # Why lists are newline-joined: a comma split would tear one name in two.
    norm = sb.normalize_response(
        response(included=[variant("comma-1", ["Vilis, Broker of Blood", "Kamahl, Heart of Krosa"], ["Win"], 5)]),
        ["Vilis, Broker of Blood", "Kamahl, Heart of Krosa"])
    w.save_deck_combos(two_decks, 2, norm)
    uses = q.deck_combos(two_decks, 2, "included").iloc[0]["uses"]
    assert uses.split("\n") == ["Vilis, Broker of Blood", "Kamahl, Heart of Krosa"]


def test_sync_stamp_and_never_synced(two_decks, norm):
    w.save_deck_combos(two_decks, 1, norm)
    sync = q.deck_combo_sync(two_decks, 1)
    assert (sync["included_count"], sync["almost_count"]) == (2, 2)
    assert sync["identity"] == "B" and sync["fetched_at"]
    assert q.deck_combo_sync(two_decks, 2) is None      # "never fetched" is not "found nothing"

    sync_all = q.deck_combo_sync_all(two_decks)
    assert len(sync_all) == 2
    untouched = sync_all[sync_all["deck_name"] == "Untouched Deck"].iloc[0]["included_count"]
    assert untouched is None or str(untouched) == "nan"


def test_resync_replaces_rather_than_merges(two_decks, norm):
    w.save_deck_combos(two_decks, 1, norm)
    smaller = sb.normalize_response(
        response(included=[variant("1561-2384", ["Psychosis Crawler", "Peer into the Abyss"],
                                   ["Near-infinite lifeloss"], 30793)]), DECK_NAMES)
    w.save_deck_combos(two_decks, 1, smaller)
    assert len(q.deck_combos(two_decks, 1, "included")) == 1
    assert len(q.deck_combos(two_decks, 1, "almost")) == 0
    assert q.deck_combo_sync(two_decks, 1)["included_count"] == 1


def test_a_duplicate_combo_id_collapses_instead_of_aborting(two_decks):
    dupes = sb.normalize_response(
        response(included=[variant("dup-1", ["A", "B"], ["Win"], 10)] * 2), ["A", "B"])
    w.save_deck_combos(two_decks, 1, dupes)
    assert len(q.deck_combos(two_decks, 1, "included")) == 1


def test_clear_returns_a_deck_to_never_fetched(two_decks, norm):
    w.save_deck_combos(two_decks, 1, norm)
    w.clear_deck_combos(two_decks, 1)
    assert len(q.deck_combos(two_decks, 1, "included")) == 0
    assert q.deck_combo_sync(two_decks, 1) is None


# ------------------------------------------------------------------
# owned_card_quantities: owned vs free (a copy sleeved in a deck is owned,
# not available)
# ------------------------------------------------------------------
@pytest.fixture
def owned_db(conn):
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (1, 'Deck One')")
    conn.execute("INSERT INTO decks (deck_id, name) VALUES (2, 'Deck Two')")
    for sid, name in [("c1", "Freed from the Real"), ("c2", "Sol Ring"),
                      ("c3", "Swamp"), ("c4", "Secret Arcade // Dusty Parlor")]:
        conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) "
                     "VALUES (?,?,'TST','1')", (sid, name))
    # Freed: one loose copy, one sleeved in Deck Two. Sol Ring: only copy is
    # sleeved. Swamp: untracked bulk (NULL quantity). Arcade: NULL location.
    for sid, qty, loc in [("c1", 1, "Box"), ("c1", 1, "Deck Two"), ("c2", 1, "Deck One"),
                          ("c3", None, "Lands Box"), ("c4", 1, None)]:
        conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES (?,?,?)",
                     (sid, qty, loc))
    conn.commit()
    return conn


def test_owned_card_quantities(owned_db):
    owned = q.owned_card_quantities(owned_db)
    assert "freed from the real" in owned and "secret arcade" in owned   # front-face lowercase keys
    assert owned["freed from the real"]["owned_qty"] == 2
    assert owned["freed from the real"]["available_qty"] == 1
    assert (owned["sol ring"]["owned_qty"], owned["sol ring"]["available_qty"]) == (1, 0)
    assert owned["swamp"]["owned_qty"] == 1               # NULL bulk quantity counts as 1
    assert owned["secret arcade"]["available_qty"] == 1   # NULL location is free
    assert "pemmin's aura" not in owned


def test_delete_deck_clears_the_combo_cache_and_queues(owned_db, norm):
    # Without this the deck_id foreign key would abort the delete.
    conn = owned_db
    w.save_deck_combos(conn, 1, norm)
    # deck_swap_queue is the frozen pre-rework table; databases still hold
    # rows in it, so delete_deck must keep clearing it. deck_changes is the
    # live replacement.
    conn.execute("INSERT INTO deck_swap_queue (deck_id, add_name, remove_scryfall_id, remove_name, quantity) "
                 "VALUES (1, 'Freed from the Real', 'c2', 'Sol Ring', 1)")
    w.add_change(conn, 1, add_name="Freed from the Real", remove_scryfall_id="c2",
                 remove_name="Sol Ring", status="planned")
    conn.commit()

    name, err, _redirected, _dropped = w.delete_deck(conn, 1)
    assert (name, err) == ("Deck One", None)
    for table in ("deck_combos", "deck_swap_queue", "deck_changes"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE deck_id=1").fetchone()[0] == 0
    assert q.deck_combo_sync(conn, 1) is None


# ------------------------------------------------------------------
# fetch_many driven from a real DataFrame, as the Card Database page does
# ------------------------------------------------------------------
def test_fetch_many_from_a_dataframe_with_a_mixed_partner_column(conn):
    # Deliberately MIXED: one deck with a partner, one without. An all-NULL
    # column stays object/None and would pass without proving anything;
    # a partly-filled one types the NULLs as float NaN, the production bug.
    conn.execute("INSERT INTO decks (deck_id, name, commander, partner) "
                 "VALUES (1,'Partnered Deck','Miara, Thorn of the Glade','Kamahl, Heart of Krosa')")
    conn.execute("INSERT INTO decks (deck_id, name, commander) VALUES (2,'Solo Deck','Ghoulcaller Gisa')")
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES ('c1','Sol Ring','TST','1')")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1,'c1',1)")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (2,'c1',1)")
    conn.commit()

    decks_df = q.list_decks(conn)
    solo_partner = list(decks_df.sort_values("deck_id")["partner"])[1]
    assert isinstance(solo_partner, float) and solo_partner != solo_partner   # NaN...
    assert bool(solo_partner) is True                                         # ...and truthy

    deck_inputs = [(row["deck_id"], row["name"], row["commander"], row["partner"],
                    q.deck_combo_card_rows(conn, row["deck_id"])) for _, row in decks_df.iterrows()]
    client = FakeClient(response(included=[variant("bulk-1", ["Sol Ring", "Basalt Monolith"],
                                                   ["Infinite mana"], 500)]))
    seen = []
    for deck_id, deck_name, result, error in sb.fetch_many(
            deck_inputs, progress_callback=lambda *a: None, pause=0, client=client):
        seen.append((deck_name, error))
        if not error:
            w.save_deck_combos(conn, deck_id, result)

    assert len(seen) == 2 and all(err is None for _, err in seen)
    assert len(q.deck_combos(conn, 1, "included")) == 1 and len(q.deck_combos(conn, 2, "included")) == 1
    sent = {tuple(c["card"] for c in p["commanders"]) for p in client.payloads}
    assert ("Miara, Thorn of the Glade", "Kamahl, Heart of Krosa") in sent   # both commanders
    assert ("Ghoulcaller Gisa",) in sent                                     # no NaN alongside
