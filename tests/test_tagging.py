"""Tag proposals (docs/TAG_SUGGESTIONS_PLAN.md Phase 2): context, schema, guard,
the retry path, diff classification and the thin / broad report. Offline with
a scripted FakeProvider, as in test_advisor.py."""
import pytest

from dashboard_lib import writes
from dashboard_lib.advisor import guard, providers, tagging
from dashboard_lib.advisor.context import MAX_TAGS_PER_CARD, build_tag_context
from dashboard_lib.advisor.providers import AnthropicProvider, FakeProvider
from dashboard_lib.advisor.schemas import tag_schema

SOL_RING = "{T}: Add {C}{C}."
BOLT = "Lightning Bolt deals 3 damage to any target."


@pytest.fixture
def deck(conn, add_card):
    """Deck 1 (commander Rakdos): the commander, Sol Ring tagged Ramp, Bolt
    tagged Removal, Changeling Bear, Bear Hug (an MDFC) and a Swamp."""
    def card(name, text, type_line, basic=0):
        sid = add_card(name)
        conn.execute("UPDATE cards SET oracle_text=?, type_line=?, mana_cost='{1}', is_basic_land=? WHERE scryfall_id=?",
                     (text, type_line, basic, sid))
        conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1,?,1)", (sid,))
        return sid
    conn.execute("INSERT INTO decks (deck_id, name, commander, color_identity, description)"
                 " VALUES (1, 'Test', 'Rakdos', 'BR', 'Aggro')")
    card("Rakdos", "Haste", "Legendary Creature — Demon")
    sol = card("Sol Ring", SOL_RING, "Artifact")
    bolt = card("Lightning Bolt", BOLT, "Instant")
    card("Changeling Bear", "Changeling", "Creature — Shapeshifter")
    card("Bear Hug // Bear Rug", "Draw a card. // Trample", "Sorcery // Enchantment")
    card("Swamp", "", "Basic Land — Swamp", basic=1)
    writes.set_card_tags(conn, 1, sol, "function", ["Ramp"])
    writes.set_card_tags(conn, 1, bolt, "function", ["Removal"])
    conn.commit()
    return 1


@pytest.fixture
def ctx(conn, deck):
    return build_tag_context(conn, deck, "function")


def _row(card, tags=("Ramp",), primary=None, quote="Add {C}{C}"):
    row = {"card": card, "tags": list(tags), "evidence_quote": quote, "reason": "r"}
    if tags:
        row["primary"] = primary or tags[0]
    return row


def _empty(card):
    return {"card": card, "tags": [], "evidence_quote": "", "reason": ""}


def _full(**over):
    """A clean answer; `over` swaps in rows by short name."""
    rows = {
        "rakdos": _empty("Rakdos"),
        "sol": _row("Sol Ring"),
        "bolt": _row("Lightning Bolt", ["Removal"], quote="deals 3 damage to any target"),
        "bear": _empty("Changeling Bear"),
        "hug": _empty("Bear Hug // Bear Rug"),
    }
    rows.update(over)
    return {"cards": [r for r in rows.values() if r]}


def _by_card(checked):
    return {r.card.name: r for r in checked.rows if r.card}


# ---- context, prompt, schema ------------------------------------------------

def test_context_skips_basics_keeps_the_commander_and_loads_tags(ctx):
    assert ctx.card_names() == ["Bear Hug // Bear Rug", "Changeling Bear", "Lightning Bolt", "Rakdos", "Sol Ring"]
    assert ctx.current == {"Sol Ring": ["Ramp"], "Lightning Bolt": ["Removal"]}
    assert ctx.labels() == ["Ramp", "Removal"]


def test_two_printings_are_one_entry_with_both_sets_of_tags(conn, deck, add_card):
    other = add_card("Sol Ring", set_code="xyz", collector_number="9")
    conn.execute("UPDATE cards SET oracle_text=?, type_line='Artifact', is_basic_land=0 WHERE scryfall_id=?",
                 (SOL_RING, other))
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1,?,2)", (other,))
    writes.set_card_tags(conn, 1, other, "function", ["Removal"])
    conn.commit()
    ctx = build_tag_context(conn, deck, "function")
    assert ctx.card_names().count("Sol Ring") == 1
    assert sorted(ctx.current["Sol Ring"]) == ["Ramp", "Removal"]
    sol = next(c for c in ctx.cards if c.name == "Sol Ring")
    assert sol.quantity == 3 and len(sol.scryfall_ids) == 2


def test_undefined_labels_are_not_offered(conn, deck):
    sol = next(c for c in build_tag_context(conn, deck, "strategy").cards if c.name == "Sol Ring")
    writes.set_card_tags(conn, 1, sol.scryfall_ids[0], "strategy", ["Rakdos Target"])
    conn.commit()
    ctx = build_tag_context(conn, deck, "strategy")
    assert ctx.current == {"Sol Ring": ["Rakdos Target"]} and ctx.labels() == []


def test_prompt_has_the_plan_definitions_examples_and_current_tags(ctx):
    prompt = tagging.build_prompt(ctx)
    assert "Stated plan: Aggro" in prompt
    assert "- Ramp: Produces extra mana" in prompt
    assert "EXAMPLES OF HOW THE USER USES THESE LABELS:\n- Removal: Lightning Bolt" in prompt
    assert "Sol Ring | {1} | Artifact [current tags: Ramp]" in prompt
    assert "Rakdos | {1} | Legendary Creature — Demon [COMMANDER; current tags: none]" in prompt
    assert "Swamp" not in prompt


def test_hidden_current_tags_leave_no_answers_in_the_prompt(ctx):
    prompt = tagging.build_prompt(ctx, hide_current=True)
    assert "current tags" not in prompt and "EXAMPLES" not in prompt
    held_out = tagging.build_prompt(ctx, hide_current=True, examples={"Ramp": ["Sol Ring"]})
    assert "- Ramp: Sol Ring" in held_out and "Lightning Bolt\n" not in held_out.split("CARDS")[0]


def test_schema_is_enum_constrained(ctx):
    item = tag_schema(ctx)["properties"]["cards"]["items"]["properties"]
    assert item["card"]["enum"] == ctx.card_names()
    assert item["tags"]["items"]["enum"] == ["Ramp", "Removal"]
    assert item["tags"]["maxItems"] == MAX_TAGS_PER_CARD
    assert "primary" not in tag_schema(ctx)["properties"]["cards"]["items"]["required"]


def test_schema_survives_translation_for_claude(ctx):
    out = providers._gemini_schema_to_json_schema(tag_schema(ctx))
    tags = out["properties"]["cards"]["items"]["properties"]["tags"]
    assert tags["type"] == "array" and tags["maxItems"] == MAX_TAGS_PER_CARD and tags["items"]["type"] == "string"


# ---- guard --------------------------------------------------------------------

def test_clean_answer_has_no_violations(ctx):
    checked = guard.check_tags(_full(), ctx)
    assert not checked.violations and not checked.missing
    assert all(r.verified for r in checked.rows)


def test_unknown_card_is_kept_marked_and_carries_no_tags(ctx):
    checked = guard.check_tags({"cards": [_row("Black Lotus")]}, ctx)
    row = checked.rows[0]
    assert "unknown card" in row.violations[0] and row.card is None and row.tags == []


def test_out_of_vocabulary_tag_is_left_off_the_row(ctx):
    row = guard.check_tags(_full(sol=_row("Sol Ring", ["Banana", "Ramp"], primary="Ramp")), ctx).rows[1]
    assert row.tags == ["Ramp"] and "'Banana'" in row.violations[0]


def test_labels_take_the_vocabulary_spelling(ctx):
    row = guard.check_tags(_full(sol=_row("Sol Ring", ["ramp"], primary="RAMP")), ctx).rows[1]
    assert row.tags == ["Ramp"] and row.primary == "Ramp" and row.verified


def test_too_many_tags_is_a_violation_and_keeps_the_primary(ctx):
    ctx.vocabulary += [{"label": x, "definition": "d"} for x in ("A", "B")]
    raw = _row("Sol Ring", ["Ramp", "Removal", "A", "B"], primary="B")
    row = guard.check_tags(_full(sol=raw), ctx).rows[1]
    assert row.tags == ["B", "Ramp"] and "at most 2" in row.violations[0]


def test_removal_and_interaction_are_exclusive_and_the_primary_wins(ctx):
    ctx.vocabulary.append({"label": "Interaction", "definition": "d"})
    raw = _row("Lightning Bolt", ["Removal", "Interaction"], primary="Interaction", quote="deals 3 damage")
    row = guard.check_tags(_full(bolt=raw), ctx).rows[2]
    assert row.tags == ["Interaction"] and "exclusive" in row.violations[0] and not row.verified
    assert row.primary == "Interaction"


@pytest.mark.parametrize("raw,message", [
    ({"card": "Sol Ring", "tags": ["Ramp"], "primary": "Removal", "evidence_quote": "Add {C}{C}"}, "not one of"),
    ({"card": "Sol Ring", "tags": ["Ramp"], "evidence_quote": "Add {C}{C}"}, "no primary"),
    ({"card": "Sol Ring", "tags": [], "primary": "Ramp", "evidence_quote": ""}, "with no tags"),
])
def test_primary_must_belong_to_the_card(ctx, raw, message):
    row = guard.check_tags(_full(sol=raw), ctx).rows[1]
    assert message in row.violations[0]


def test_a_card_returned_twice_counts_once(ctx):
    checked = guard.check_tags({"cards": _full()["cards"] + [_row("Sol Ring", ["Removal"])]}, ctx)
    assert "more than once" in checked.rows[-1].violations[0]
    assert checked.rows[-1].tags == [] and _by_card(checked)["Sol Ring"].tags == ["Ramp"]


def test_failed_quote_marks_the_row_but_keeps_it(ctx):
    row = guard.check_tags(_full(sol=_row("Sol Ring", quote="makes lots of mana")), ctx).rows[1]
    assert not row.verified and row.tags == ["Ramp"] and "quote not found" in row.violations[0]


def test_empty_quote_only_matters_on_a_tagged_row(ctx):
    assert guard.check_tags(_full(), ctx).rows[0].verified
    row = guard.check_tags(_full(sol=_row("Sol Ring", quote="")), ctx).rows[1]
    assert row.violations == ["missing evidence_quote"]


def test_quote_may_come_from_the_type_line(ctx):
    ctx.vocabulary.append({"label": "Demon", "definition": "Is a Demon."})
    row = guard.check_tags(_full(rakdos=_row("Rakdos", ["Demon"], quote="Creature - Demon")), ctx).rows[0]
    assert row.verified                                  # also proves the dash is normalised


def test_either_face_of_a_card_resolves(ctx):
    row = guard.check_tags(_full(hug=_row("Bear Hug", ["Ramp"], quote="Draw a card")), ctx).rows[-1]
    assert row.card.name == "Bear Hug // Bear Rug"


def test_a_skipped_card_is_missing_while_an_empty_tag_list_is_an_answer(ctx):
    checked = guard.check_tags(_full(bear=None), ctx)
    assert checked.missing == ["Changeling Bear"]


def test_malformed_answers_do_not_raise(ctx):
    for parsed in (None, [], {"cards": None}, {"cards": ["x", 3]}):
        assert guard.check_tags(parsed, ctx).rows == []
    assert guard.check_tags({"cards": [{"card": "Sol Ring", "tags": "Ramp"}]}, ctx).rows[0].tags == []


# ---- classification and the focus report --------------------------------------

@pytest.mark.parametrize("tags,current,status", [
    ([], [], "none"),
    ([], ["Ramp"], "none"),
    (["Ramp"], [], "new"),
    (["Ramp"], ["ramp"], "same"),
    (["Ramp"], ["Ramp", "Removal"], "same"),
    (["Ramp", "Fixing"], ["Ramp"], "new"),
    (["Removal"], ["Ramp"], "conflict"),
])
def test_classify(tags, current, status):
    assert tagging.classify(tags, current) == status


def _checked_rows(ctx, answer):
    return guard.check_tags(answer, ctx).rows


def test_focus_report_adds_rows_to_current_tags(ctx):
    rows = _checked_rows(ctx, {"cards": [_row("Changeling Bear", ["Ramp"], quote="Changeling")]})
    report = tagging.focus_report(ctx, rows)
    assert report.counts == {"Ramp": 2, "Removal": 1}
    assert report.tags_per_card == 1.0
    assert report.thin == ["Ramp", "Removal"]


def test_focus_report_counts_copies_and_finds_broad_labels(ctx):
    sol = next(c for c in ctx.cards if c.name == "Sol Ring")
    sol.quantity = 4                                     # 4 of 8 copies is past 40%
    report = tagging.focus_report(ctx, [])
    assert report.counts["Ramp"] == 4
    assert report.broad == ["Ramp"] and report.thin == ["Removal"]


def test_focus_report_ignores_rows_without_a_card(ctx):
    report = tagging.focus_report(ctx, _checked_rows(ctx, {"cards": [_row("Black Lotus")]}))
    assert report.counts == {"Ramp": 1, "Removal": 1}


# ---- the run loop ---------------------------------------------------------------

def test_clean_run_makes_one_call_and_classifies(ctx):
    answer = _full(bear=_row("Changeling Bear", ["Ramp"], quote="Changeling"),
                   bolt=_row("Lightning Bolt", ["Ramp"], quote="deals 3 damage"))
    p = FakeProvider([answer])
    res = tagging.run_tag_proposals(p, ctx)
    assert len(p.calls) == 1 and not res.retried and not res.error
    assert res.input_tokens == 100 and res.output_tokens == 50
    rows = _by_card(res.checked)
    assert rows["Sol Ring"].status == "same"
    assert rows["Changeling Bear"].status == "new"
    assert rows["Lightning Bolt"].status == "conflict"
    assert rows["Rakdos"].status == "none"
    assert res.focus.counts == {"Ramp": 2, "Removal": 1}  # only verified `new` rows count


def test_run_asks_for_room_for_a_whole_deck(ctx):
    p = FakeProvider([_full()])
    tagging.run_tag_proposals(p, ctx)
    assert p.calls[0]["max_tokens"] == tagging.MAX_OUTPUT_TOKENS
    assert p.calls[0]["timeout"] == tagging.TIMEOUT_SECONDS
    assert p.calls[0]["schema"]["properties"]["cards"]
    assert "never both" in p.calls[0]["system"]


def test_retry_names_problems_and_merges_only_the_flagged_cards(ctx):
    bad = _full(sol=_row("Sol Ring", quote="made up"), bear=None)
    fixed = {"cards": [_row("Sol Ring"), _empty("Changeling Bear"),
                       _row("Lightning Bolt", ["Ramp"], quote="deals 3")]}   # not flagged: ignored
    p = FakeProvider([bad, fixed])
    res = tagging.run_tag_proposals(p, ctx)
    assert res.retried and len(p.calls) == 2
    retry = p.calls[1]["messages"][-1]["content"]
    assert "quote not found" in retry and "'Changeling Bear': not returned" in retry
    assert "these cards only" in retry
    assert not res.checked.violations and not res.checked.missing
    assert _by_card(res.checked)["Lightning Bolt"].tags == ["Removal"]
    assert res.input_tokens == 200


def test_a_failing_retry_keeps_the_first_answer_marked(ctx):
    bad = _full(sol=_row("Sol Ring", quote="made up"))
    for script in ([bad], [bad, "not json"]):
        res = tagging.run_tag_proposals(FakeProvider(list(script)), ctx)
        assert res.retried and not res.error
        assert not _by_card(res.checked)["Sol Ring"].verified


def test_rows_still_bad_after_the_retry_stay_marked(ctx):
    bad = _full(sol=_row("Sol Ring", quote="made up"))
    res = tagging.run_tag_proposals(FakeProvider([bad, {"cards": []}]), ctx)
    row = _by_card(res.checked)["Sol Ring"]
    assert not row.verified and row.tags == ["Ramp"] and row.status == "same"


def test_retry_keeps_an_unknown_card_visible(ctx):
    res = tagging.run_tag_proposals(FakeProvider([{"cards": _full()["cards"] + [_row("Black Lotus")]}, {"cards": []}]), ctx)
    assert [r.status for r in res.checked.rows if r.card is None] == ["none"]
    assert res.checked.violations


def test_provider_failure_and_bad_json_are_errors(ctx):
    assert "no more scripted" in tagging.run_tag_proposals(FakeProvider([]), ctx).error
    assert "valid JSON" in tagging.run_tag_proposals(FakeProvider(["nope"]), ctx).error


def test_nothing_to_ask_makes_no_call(conn, deck, ctx):
    p = FakeProvider([])
    assert "no labels" in tagging.run_tag_proposals(p, build_tag_context(conn, deck, "strategy")).error.lower()
    ctx.cards = []
    assert "no cards" in tagging.run_tag_proposals(p, ctx).error
    assert p.calls == []


def test_a_run_writes_nothing(conn, ctx):
    count = lambda: conn.execute("SELECT COUNT(*) FROM card_tags").fetchone()[0]
    before = count()
    tagging.run_tag_proposals(FakeProvider([_full(bear=_row("Changeling Bear", ["Ramp"], quote="Changeling"))]), ctx)
    assert count() == before


def test_examples_skip_labels_outside_the_vocabulary(ctx):
    ctx.current["Rakdos"] = ["Not Offered"]
    assert tagging.pick_examples(ctx) == {"Removal": ["Lightning Bolt"], "Ramp": ["Sol Ring"]}


# ---- providers ------------------------------------------------------------------

def test_make_provider_picks_by_choice(monkeypatch):
    monkeypatch.setattr(providers, "load_advisor_secrets", lambda: {"google_api_key": "k", "anthropic_api_key": "k"})
    assert providers.make_provider("Gemini").name == "gemini"
    p = providers.make_provider("Claude Opus 5.5")
    assert p.name == "anthropic" and p.model == "claude-opus-5-5"


def test_anthropic_output_limit_and_timeout_can_be_set_per_call():
    seen = []
    p = AnthropicProvider(api_key="k", model="m")
    p._post = lambda body, timeout: seen.append((body["max_tokens"], timeout)) or {
        "content": [{"type": "text", "text": "ok"}]}
    p.chat([{"role": "user", "content": "hi"}])
    p.chat([{"role": "user", "content": "hi"}], max_tokens=16000, timeout=300)
    assert seen == [(4096, 120), (16000, 300)]


# ---- batching and failures ------------------------------------------------------

def test_batches_are_even_and_cover_every_card():
    cards = list(range(101))
    parts = tagging._batches(cards, 50)
    assert [len(p) for p in parts] == [34, 34, 33] and sum(parts, []) == cards
    assert tagging._batches(cards[:50], 50) == [cards[:50]]


def test_a_big_deck_goes_out_in_batches_and_comes_back_as_one_result(ctx, monkeypatch):
    monkeypatch.setattr(tagging, "BATCH_SIZE", 3)
    names = ctx.card_names()
    p = FakeProvider([{"cards": [_empty(n) for n in names[:3]]}, {"cards": [_empty(n) for n in names[3:]]}])
    res = tagging.run_tag_proposals(p, ctx)
    assert len(p.calls) == 2 and not res.error and not res.checked.missing and not res.retried
    assert len(res.checked.rows) == 5 and res.input_tokens == 200
    # Each request's schema only offers its own batch's cards.
    enum = lambda call: call["schema"]["properties"]["cards"]["items"]["properties"]["card"]["enum"]
    assert enum(p.calls[0]) == names[:3] and enum(p.calls[1]) == names[3:]


def test_a_failed_batch_keeps_the_finished_ones_and_says_what_failed(ctx, monkeypatch, caplog):
    monkeypatch.setattr(tagging, "BATCH_SIZE", 3)
    names = ctx.card_names()
    with caplog.at_level("WARNING"):
        res = tagging.run_tag_proposals(FakeProvider([{"cards": [_empty(n) for n in names[:3]]}]), ctx)
    assert "batch 2 of 2" in res.error and names[3] in res.error and "1 of 2 batches finished" in res.error
    assert len(res.checked.rows) == 3 and res.checked.missing == names[3:]
    assert "batch 2 of 2" in caplog.text


def test_a_first_batch_failure_returns_the_error_alone(ctx):
    res = tagging.run_tag_proposals(FakeProvider([]), ctx)
    assert res.checked is None and "the request" in res.error and "no more scripted" in res.error


# ---- pass one of the two-pass mode: propose a vocabulary ---------------------

def test_a_proposed_vocabulary_keeps_menu_spelling_and_drops_repeats_and_surplus():
    parsed = {"labels": [
        {"label": " ramp ", "definition": ""},                   # menu label, blank: menu definition
        {"label": "Removal", "definition": "Sharper\n for this deck."},
        {"label": "REMOVAL", "definition": "again"},             # repeat
        {"label": "Ghost Plan", "definition": "Own label."},
        {"label": "", "definition": "no label"},
        {"label": "Unreadable", "definition": " "},               # no definition, not on the menu
        "junk",
    ]}
    menu = {"Ramp": "Menu ramp.", "Removal": "Menu removal."}
    labels, dropped = guard.check_vocabulary(parsed, menu, limit=3)
    assert labels == {"Ramp": "Menu ramp.", "Removal": "Sharper for this deck.", "Ghost Plan": "Own label."}
    assert dropped == 1
    labels, dropped = guard.check_vocabulary(parsed, menu, limit=2)
    assert list(labels) == ["Ramp", "Removal"] and dropped == 2
    assert guard.check_vocabulary("nope", menu, 3) == ({}, 0)


def test_propose_vocabulary_sends_menu_and_every_card_and_writes_nothing(conn, ctx):
    p = FakeProvider([{"labels": [{"label": "Ramp", "definition": "Mana."}]}])
    draft = tagging.propose_vocabulary(p, ctx, {"Ramp": "Menu ramp."})
    assert draft.labels == {"Ramp": "Mana."} and not draft.error
    call = p.calls[0]
    prompt = call["messages"][0]["content"]
    assert "MENU:\n- Ramp: Menu ramp." in prompt and "Stated plan: Aggro" in prompt
    assert "Changeling Bear" in prompt and "Swamp" not in prompt        # basics are skipped
    assert "current tags" not in prompt                                   # an untagged deck's pass
    assert call["schema"]["properties"]["labels"]["maxItems"] == tagging.MAX_PROPOSED_LABELS
    assert conn.execute("SELECT COUNT(*) FROM deck_tag_vocab").fetchone()[0] == 0


def test_propose_vocabulary_without_a_menu_and_on_failure(ctx):
    p = FakeProvider([{"labels": [{"label": "Demons", "definition": "Demon creatures."}]}])
    assert tagging.propose_vocabulary(p, ctx).labels == {"Demons": "Demon creatures."}
    assert "choose your own labels" in p.calls[0]["messages"][0]["content"]
    assert "no more scripted" in tagging.propose_vocabulary(FakeProvider([]), ctx).error
    assert tagging.propose_vocabulary(FakeProvider([{"labels": []}]), ctx).error == "The model returned no usable labels."


def test_temporary_http_errors_read_as_try_again_and_others_keep_the_detail():
    for status in (429, 503):
        msg = providers._http_error(status, '{"error": {"message": "raw json"}}')
        assert msg.startswith(f"HTTP {status}") and "try again" in msg and "raw json" not in msg
    assert providers._http_error(400, "API key not valid") == "HTTP 400: API key not valid"
