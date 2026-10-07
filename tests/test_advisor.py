"""Advisor grounding layer (Phase 1): context building, guard validation and
the analysis loop, all offline with a scripted FakeProvider.

The five EVAL_* tests are the starter eval set: each feeds the guard a
specific kind of bad model output (fabricated name, misquoted text, wrong
mechanical role, bad `replaces`, commander cut) and checks it is flagged.
"""
import pytest

from dashboard_lib.advisor import analysis, context, guard
from dashboard_lib.advisor.providers import FakeProvider, ProviderError
from dashboard_lib.advisor.schemas import analysis_schema

SOL_RING = "{T}: Add {C}{C}."
RAMP_SIGNET = "{1}, {T}: Add {B}{R}."
BOLT = "Lightning Bolt deals 3 damage to any target."
DIVINATION = "Draw two cards."


@pytest.fixture
def deck(conn, add_card):
    """Deck 'Test' (commander Rakdos, BR): commander, Sol Ring, Signet, Bolt,
    a Swamp-ish land. Collection also owns Divination-like 'Read the Bones'
    (B, free), 'Green Guy' (off-colour) and 'Sleeved Draw' (in another deck)."""
    def card(name, text, type_line="Artifact", ci="", cmc=1, cost="{1}", **kw):
        sid = add_card(name, **kw)
        conn.execute("UPDATE cards SET oracle_text=?, type_line=?, color_identity=?, cmc=?, mana_cost=?,"
                     " commander_legal=1, is_basic_land=0 WHERE scryfall_id=?",
                     (text, type_line, ci, cmc, cost, sid))
        return sid
    conn.execute("INSERT INTO decks (deck_id, name, commander, color_identity, description)"
                 " VALUES (1, 'Test', 'Rakdos', 'BR', 'Aggro')")
    conn.execute("INSERT INTO decks (deck_id, name, color_identity) VALUES (2, 'Other', 'B')")
    ids = [card("Rakdos", "Haste", "Legendary Creature", "B,R", 4, "{2}{B}{R}"),
           card("Sol Ring", SOL_RING),
           card("Arcane Signet", RAMP_SIGNET, ci="B,R"),
           card("Lightning Bolt", BOLT, "Instant", "R", 1, "{R}"),
           card("Blood Crypt", "{T}: Add {B} or {R}.", "Land", "B,R", 0, "")]
    for sid in ids:
        conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1,?,1)", (sid,))
    owned = [card("Read the Bones", DIVINATION, "Sorcery", "B", 3, "{2}{B}"),
             card("Green Guy", "Trample", "Creature", "G", 2, "{1}{G}"),
             card("Sleeved Draw", "Draw a card.", "Instant", "B", 1, "{B}")]
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES (?,1,'Box')", (owned[0],))
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES (?,1,'Box')", (owned[1],))
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES (?,1,'Other')", (owned[2],))
    conn.commit()
    return context.build_deck_context(conn, 1)


def _cut(card="Sol Ring", quote="Add {C}{C}", roles=("ramp",)):
    return {"card": card, "reason": "r", "evidence_quote": quote, "roles": list(roles)}


def _add(card="Read the Bones", replaces="Lightning Bolt", quote="Draw two cards", roles=("draw",)):
    return {"card": card, "replaces": replaces, "reason": "r", "evidence_quote": quote, "roles": list(roles)}


def _answer(cuts=(), adds=()):
    return {"summary": "s", "strengths": [], "weaknesses": [], "cuts": list(cuts), "adds": list(adds)}


# ---- roles and context ----------------------------------------------------

def test_mechanical_roles():
    r = context.mechanical_roles
    assert r(SOL_RING, "Artifact") == {"ramp"}
    assert r(BOLT, "Instant") == {"removal"}
    assert r("Draw two cards.", "Sorcery") == {"draw"}
    assert r("Destroy all creatures.", "Sorcery") == {"wipe"}
    assert r("Search your library for a basic land card, put it onto the battlefield tapped.", "Sorcery") == {"ramp"}
    assert r("Search your library for a card, put it into your hand.", "Sorcery") == {"tutor"}
    assert r("{T}: Add {G}.", "Land") == {"land"}


def test_context_stats_and_commander(deck):
    s = deck.stats
    assert s["total_cards"] == 5 and s["lands"] == 1
    assert s["roles"]["ramp"] == 2 and s["roles"]["removal"] == 1
    assert s["pips"] == {"B": 1, "R": 2}
    assert [c.name for c in deck.cards if c.is_commander] == ["Rakdos"]
    assert "Rakdos" not in deck.deck_names()


def test_candidate_pool_filters_and_ranks(deck):
    names = deck.candidate_names()
    assert "Green Guy" not in names                      # off colour identity
    assert "Sol Ring" not in names                       # already in deck
    assert names.index("Read the Bones") < names.index("Sleeved Draw")  # free before sleeved
    sleeved = next(c for c in deck.candidates if c.name == "Sleeved Draw")
    assert not sleeved.available and sleeved.locations == ["Other"]


def test_schema_enums_match_context(deck):
    props = analysis_schema(deck)["properties"]
    assert props["adds"]["items"]["properties"]["card"]["enum"] == deck.candidate_names()
    assert "Rakdos" not in props["cuts"]["items"]["properties"]["card"]["enum"]


# ---- guard: starter eval set ----------------------------------------------

def test_valid_output_is_verified(deck):
    checked = guard.check_analysis(_answer([_cut()], [_add()]), deck)
    assert not checked.violations and all(i.verified for i in checked.items)


def test_eval_fabricated_name(deck):
    checked = guard.check_analysis(_answer(adds=[_add(card="Rhystic Study")]), deck)
    assert "unknown card 'Rhystic Study'" in checked.violations[0][1]


def test_eval_misquoted_text(deck):
    checked = guard.check_analysis(_answer([_cut(quote="Add {C}{C}{C}")]), deck)
    assert "quote not found" in checked.violations[0][1]


def test_eval_wrong_mechanical_role(deck):
    checked = guard.check_analysis(_answer([_cut(roles=("ramp", "draw"))]), deck)
    assert "claims role(s) draw" in checked.violations[0][1]


def test_eval_replaces_unknown_or_commander(deck):
    bad = guard.check_analysis(_answer(adds=[_add(replaces="Mana Crypt")]), deck)
    cmdr = guard.check_analysis(_answer(adds=[_add(replaces="Rakdos")]), deck)
    assert "unknown deck card" in bad.violations[0][1]
    assert "commander" in cmdr.violations[0][1]


def test_eval_commander_cut_is_flagged(deck):
    checked = guard.check_analysis(_answer([_cut(card="Rakdos", quote="Haste", roles=())]), deck)
    assert "unknown card 'Rakdos'" in checked.violations[0][1]


def test_quote_matching_ignores_case_whitespace_and_curly_quotes(deck):
    checked = guard.check_analysis(_answer([_cut(quote="  {t}:   ADD {C}{C}.")]), deck)
    assert not checked.violations


def test_malformed_sections_do_not_crash(deck):
    checked = guard.check_analysis({"cuts": "oops", "adds": [1, None]}, deck)
    assert checked.items == []


# ---- analysis loop --------------------------------------------------------

def test_clean_answer_makes_one_call(deck):
    p = FakeProvider([_answer([_cut()])])
    res = analysis.run_analysis(p, deck)
    assert len(p.calls) == 1 and not res.retried and res.checked.items[0].verified
    assert p.calls[0]["schema"] and "Sol Ring" in p.calls[0]["messages"][0]["content"]
    assert res.computed["total_cards"] == 5


def test_violation_triggers_one_retry_that_can_fix(deck):
    p = FakeProvider([_answer(adds=[_add(card="Rhystic Study")]), _answer(adds=[_add()])])
    res = analysis.run_analysis(p, deck)
    assert len(p.calls) == 2 and res.retried
    assert "Rhystic Study" in p.calls[1]["messages"][-1]["content"]
    assert not res.checked.violations
    assert (res.input_tokens, res.output_tokens) == (200, 100)


def test_persistent_violation_is_kept_unverified(deck):
    bad = _answer(adds=[_add(card="Rhystic Study")])
    res = analysis.run_analysis(FakeProvider([bad, bad]), deck)
    assert res.retried and not res.checked.items[0].verified      # shown, not dropped


def test_provider_error_and_bad_json_are_reported(deck):
    assert "no more scripted" in analysis.run_analysis(FakeProvider([]), deck).error
    assert "valid JSON" in analysis.run_analysis(FakeProvider(["not json"]), deck).error
    # A failing retry falls back to the first (flagged) answer.
    res = analysis.run_analysis(FakeProvider([_answer(adds=[_add(card="X")])]), deck)
    assert res.retried and res.checked.violations
