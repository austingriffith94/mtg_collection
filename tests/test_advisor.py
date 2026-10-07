"""Advisor grounding layer: context building, guard validation and the
analysis loop (Phase 1), plus tools and the chat loop (Phase 3) — all
offline with a scripted FakeProvider.

The five EVAL_* tests are the starter eval set: each feeds the guard a
specific kind of bad model output (fabricated name, misquoted text, wrong
mechanical role, bad `replaces`, commander cut) and checks it is flagged.
"""
import pytest

from dashboard_lib.advisor import analysis, chat, context, guard, tools
from dashboard_lib.advisor.providers import Capabilities, FakeProvider, ProviderError, ProviderResponse
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


# ---- context.lookup_card_text (Phase 3: shared name resolution) -----------

def test_lookup_card_text_matches_front_face(conn, add_card):
    add_card("Front // Back")
    conn.execute("UPDATE cards SET oracle_text='Do a thing.', type_line='Creature' "
                 "WHERE name='Front // Back'")
    conn.commit()
    found = context.lookup_card_text(conn, "front")
    assert found == {"name": "Front // Back", "type_line": "Creature", "oracle_text": "Do a thing."}
    assert context.lookup_card_text(conn, "Nope") is None
    assert context.lookup_card_text(conn, "") is None


# ---- tools (Phase 3: chat-mode tools) --------------------------------------

def test_get_card_known_and_unknown(conn, deck):
    r = tools.get_card(conn, "Sol Ring")
    assert r["oracle_text"] == SOL_RING and r["mainboard_in"] == ["Test"]
    assert "No card named" in tools.get_card(conn, "Nonexistent Card")["error"]


def test_get_card_matches_front_face(conn, add_card):
    sid = add_card("Front // Back")
    conn.execute("UPDATE cards SET oracle_text='Do a thing.' WHERE scryfall_id=?", (sid,))
    conn.commit()
    r = tools.get_card(conn, "Front")
    assert r["name"] == "Front // Back" and r["oracle_text"] == "Do a thing."


def test_search_cards(conn, deck):
    assert "Sol Ring" in tools.search_cards(conn, "sol")["names"]
    assert tools.search_cards(conn, "")["names"] == []


def test_list_decks(conn, deck):
    names = {d["name"] for d in tools.list_decks(conn)["decks"]}
    assert names == {"Test", "Other"}


def test_get_deck_by_name_and_id(conn, deck):
    by_name, by_id = tools.get_deck(conn, "Test"), tools.get_deck(conn, "1")
    assert by_name == by_id
    assert by_name["commander"] == "Rakdos"
    assert {c["name"] for c in by_name["cards"]} == \
        {"Rakdos", "Sol Ring", "Arcane Signet", "Lightning Bolt", "Blood Crypt"}
    assert "Read the Bones" in by_name["owned_not_in_deck"]
    assert "error" in tools.get_deck(conn, "Nope")


def test_dispatch_unknown_tool_and_bad_args(conn, deck):
    assert "unknown tool" in tools.dispatch(conn, "delete_everything", {})["error"]
    assert "bad arguments" in tools.dispatch(conn, "get_card", {"bogus": 1})["error"]


# ---- guard.check_chat_answer (Phase 3: open-ended name/quote/role check) -

def test_check_chat_answer_valid_and_fabricated(deck, conn):
    ok = guard.check_chat_answer(
        conn, {"answer": "a", "mentions": [{"card": "Sol Ring", "evidence_quote": "Add {C}{C}", "roles": ["ramp"]}]})
    assert not ok.violations
    bad = guard.check_chat_answer(
        conn, {"answer": "a", "mentions": [{"card": "Made Up Card", "evidence_quote": ""}]})
    assert "unknown card" in bad.violations[0][1]


def test_check_chat_answer_empty_quote_allowed(deck, conn):
    ok = guard.check_chat_answer(
        conn, {"answer": "a", "mentions": [{"card": "Sol Ring", "evidence_quote": ""}]})
    assert not ok.violations


def test_check_chat_answer_wrong_role_is_flagged(deck, conn):
    bad = guard.check_chat_answer(
        conn, {"answer": "a", "mentions": [{"card": "Sol Ring", "evidence_quote": "", "roles": ["draw"]}]})
    assert "claims role(s) draw" in bad.violations[0][1]


def test_check_chat_answer_malformed_is_safe(conn):
    assert guard.check_chat_answer(conn, {"mentions": "oops"}).mentions == []
    assert guard.check_chat_answer(conn, None).mentions == []


# ---- providers: Gemini message translation (pure, no network) ------------

def test_to_gemini_contents_translates_tool_calls_and_results():
    from dashboard_lib.advisor.providers import _to_gemini_contents
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [{"name": "get_card", "args": {"name": "Sol Ring"}}]},
        {"role": "tool", "name": "get_card", "content": {"oracle_text": "Add {C}{C}."}},
        {"role": "assistant", "content": "Sol Ring taps for colorless mana."},
    ]
    contents = _to_gemini_contents(msgs)
    assert contents[0] == {"role": "user", "parts": [{"text": "hi"}]}
    assert contents[1]["role"] == "model"
    assert contents[1]["parts"][0]["functionCall"] == {"name": "get_card", "args": {"name": "Sol Ring"}}
    assert contents[2] == {"role": "function", "parts": [
        {"functionResponse": {"name": "get_card", "response": {"oracle_text": "Add {C}{C}."}}}]}
    assert contents[3] == {"role": "model", "parts": [{"text": "Sol Ring taps for colorless mana."}]}


# ---- chat mode (Phase 3: tool loop + mention check) -----------------------

TOOLS_CAP = Capabilities(tools=True)


def _tool_call(name, args):
    return ProviderResponse(text="", tool_calls=[{"name": name, "args": args}], input_tokens=10, output_tokens=5)


def test_chat_gated_off_for_tool_incapable_provider(conn):
    p = FakeProvider([], capabilities=Capabilities(tools=False))
    res = chat.run_chat_turn(p, conn, [{"role": "user", "content": "hi"}])
    assert "does not support tool calling" in res.error
    assert p.calls == []


def test_chat_calls_tool_then_answers_and_checks_mention(conn, deck):
    p = FakeProvider([
        _tool_call("get_card", {"name": "Sol Ring"}),
        ProviderResponse(text="Sol Ring taps for {C}{C}.", input_tokens=20, output_tokens=15),
        {"answer": "Sol Ring taps for {C}{C}.",
         "mentions": [{"card": "Sol Ring", "evidence_quote": "Add {C}{C}"}]},
    ], capabilities=TOOLS_CAP)
    res = chat.run_chat_turn(p, conn, [{"role": "user", "content": "What does Sol Ring do?"}])
    assert not res.error
    assert res.tool_log == [{"name": "get_card", "args": {"name": "Sol Ring"},
                              "result": tools.get_card(conn, "Sol Ring")}]
    assert res.checked.answer == "Sol Ring taps for {C}{C}."
    assert res.checked.mentions[0].verified
    assert (res.input_tokens, res.output_tokens) == (10 + 20 + 100, 5 + 15 + 50)


def test_chat_mention_violation_is_flagged_not_hidden(conn, deck):
    p = FakeProvider([
        ProviderResponse(text="Rhystic Study draws you cards.", input_tokens=5, output_tokens=5),
        {"answer": "Rhystic Study draws you cards.",
         "mentions": [{"card": "Rhystic Study", "evidence_quote": ""}]},
    ], capabilities=TOOLS_CAP)
    res = chat.run_chat_turn(p, conn, [{"role": "user", "content": "tell me about rhystic study"}])
    assert not res.checked.mentions[0].verified
    assert "unknown card" in res.checked.mentions[0].violations[0]
    assert res.checked.answer == "Rhystic Study draws you cards."      # shown, not hidden


def test_chat_gives_up_after_max_tool_iterations(conn):
    replies = [_tool_call("list_decks", {}) for _ in range(chat.MAX_TOOL_ITERATIONS)]
    p = FakeProvider(replies, capabilities=TOOLS_CAP)
    res = chat.run_chat_turn(p, conn, [{"role": "user", "content": "hi"}])
    assert "Gave up after" in res.error
    assert len(p.calls) == chat.MAX_TOOL_ITERATIONS


def test_chat_provider_error_is_reported(conn):
    res = chat.run_chat_turn(FakeProvider([], capabilities=TOOLS_CAP), conn, [{"role": "user", "content": "hi"}])
    assert "no more scripted" in res.error


def test_chat_tool_resolved_card_is_verified_even_if_restate_omits_it(conn, deck):
    """A live check against the real API showed the model can quote a card
    plainly in its answer but return an empty `mentions` list on restate;
    the card it actually looked up must still come back verified."""
    p = FakeProvider([
        _tool_call("get_card", {"name": "Sol Ring"}),
        ProviderResponse(text="Sol Ring taps for {C}{C}.", input_tokens=20, output_tokens=15),
        {"answer": "Sol Ring taps for {C}{C}.", "mentions": []},
    ], capabilities=TOOLS_CAP)
    res = chat.run_chat_turn(p, conn, [{"role": "user", "content": "What does Sol Ring do?"}])
    assert not res.error
    assert len(res.checked.mentions) == 1
    assert res.checked.mentions[0].data == {"card": "Sol Ring", "evidence_quote": "", "roles": []}
    assert res.checked.mentions[0].verified


def test_chat_restate_failure_still_shows_answer(conn):
    p = FakeProvider([
        ProviderResponse(text="Just chatting, no cards.", input_tokens=5, output_tokens=5),
        "not json",
    ], capabilities=TOOLS_CAP)
    res = chat.run_chat_turn(p, conn, [{"role": "user", "content": "hi"}])
    assert not res.error
    assert res.checked.answer == "Just chatting, no cards." and res.checked.mentions == []


def test_chat_failed_get_card_lookup_is_not_added_as_a_mention(conn):
    p = FakeProvider([
        _tool_call("get_card", {"name": "Not A Real Card"}),
        ProviderResponse(text="No such card.", input_tokens=5, output_tokens=5),
        {"answer": "No such card.", "mentions": []},
    ], capabilities=TOOLS_CAP)
    res = chat.run_chat_turn(p, conn, [{"role": "user", "content": "tell me about Not A Real Card"}])
    assert res.checked.mentions == []      # the lookup errored, so nothing was "resolved"
