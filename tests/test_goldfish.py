"""dashboard_lib.goldfish — the pure payload and mana parsing behind the
Goldfish page (docs/GOLDFISH_PLAN.md Phase 1). The component can't run here,
so everything it relies on (costs, mana abilities, enters-tapped, per-face
reading) is pinned down on the Python side."""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard_lib import goldfish  # noqa: E402
from dashboard_lib.goldfish import enters_tapped, mana_abilities, parse_cost  # noqa: E402


def _row(name, type_line="Creature", mana_cost="{1}{G}", oracle_text="", qty=1, cmc=2, **extra):
    row = {
        "scryfall_id": f"{name}-id", "name": name, "type_line": type_line,
        "mana_cost": mana_cost, "oracle_text": oracle_text, "cmc": cmc,
        "image_uri": f"https://cards.scryfall.io/{name}.jpg", "local_image_path": None,
        "quantity": qty, "strategy_tags": None,
    }
    row.update(extra)
    return row


def _payload(rows, meta=None, **kw):
    return goldfish.goldfish_payload(pd.DataFrame(rows), meta or {"commander": "Boss", "partner": None}, **kw)


def _face(row):
    return goldfish.card_faces(row)


# ---------------------------------------------------------------- payload
def test_cards_carry_qty_and_commanders_leave_the_library():
    out = _payload([_row("Boss", "Legendary Creature"), _row("Forest", "Basic Land — Forest", None, qty=30, cmc=0),
                    _row("Birds", cmc=1), _row("Filler", qty=6)])
    assert [c["name"] for c in out["commanders"]] == ["Boss"]
    assert [(c["name"], c["qty"]) for c in out["library"]] == [("Forest", 30), ("Birds", 1), ("Filler", 6)]
    assert out["commanders"][0]["is_commander"] is True
    assert out["warnings"] == []


def test_partner_comes_out_after_commander_whatever_the_row_order():
    meta = {"commander": "Boss", "partner": "Sidekick"}
    out = _payload([_row("Sidekick", qty=1), _row("Boss"), _row("Filler", qty=98)], meta)
    assert [c["name"] for c in out["commanders"]] == ["Boss", "Sidekick"]


def test_double_faced_commander_matches_on_its_front_face():
    out = _payload([_row("Boss // Back", "Legendary Creature // Legendary Creature", None), _row("Filler", qty=99)])
    assert [c["name"] for c in out["commanders"]] == ["Boss // Back"]


def test_no_matching_commander_warns_and_plays_everything_as_library():
    out = _payload([_row("Filler", qty=99)], {"commander": "Nobody", "partner": None})
    assert out["commanders"] == []
    assert len(out["library"]) == 1
    assert goldfish.NO_COMMANDER_WARNING in out["warnings"]


def test_short_library_warns():
    out = _payload([_row("Boss"), _row("Filler", qty=3)])
    assert any("only 3 cards" in w for w in out["warnings"])


def test_empty_deck_gives_empty_lists():
    assert goldfish.goldfish_payload(pd.DataFrame(), {}) == {"commanders": [], "library": [], "warnings": []}
    assert goldfish.goldfish_payload(None, {}) == {"commanders": [], "library": [], "warnings": []}


def test_one_card_and_lands_only_decks_load():
    assert _payload([_row("Boss")])["commanders"][0]["name"] == "Boss"
    lands = _payload([_row("Forest", "Basic Land — Forest", None, "({T}: Add {G}.)", qty=99, cmc=0)])
    assert lands["library"][0]["faces"][0]["mana"] == [{"colors": ["G"], "any": False, "amount": 1}]


def test_tags_and_layers_ride_along():
    out = _payload([_row("Boss"), _row("Birds", strategy_tags="Ramp, Draw", cmc=1)],
                   layer_tags={"strategy": {"Birds-id": ["Ramp", "Draw"]}, "role": {}})
    birds = out["library"][0]
    assert birds["tags"] == ["Ramp", "Draw"]
    assert birds["layers"] == {"strategy": ["Ramp", "Draw"], "role": []}


# ---------------------------------------------------------------- faces
def test_one_face_card_has_one_face():
    (face,) = _face(_row("Cultivate", "Sorcery", "{2}{G}"))
    assert (face["name"], face["kind"], face["mana"], face["enters_tapped"]) == ("Cultivate", "spell", None, None)
    assert face["cost"]["generic"] == 2 and face["cost"]["G"] == 1


def test_mdfc_with_land_back_is_read_per_face_and_has_no_cost():
    row = _row("Boggart Trawler // Boggart Bog", "Artifact Creature — Goblin // Land", None,
               "When this creature enters, exile target player's graveyard. "
               "// Boggart Bog enters tapped.\n{T}: Add {B}.")
    front, back = _face(row)
    assert [f["name"] for f in (front, back)] == ["Boggart Trawler", "Boggart Bog"]
    assert [f["kind"] for f in (front, back)] == ["creature", "land"]
    assert front["cost"] is None and back["cost"] is None
    assert front["mana"] is None                      # the back land's "Add {B}" is not the front's
    assert back["mana"] == [{"colors": ["B"], "any": False, "amount": 1}]
    assert front["enters_tapped"] is None and back["enters_tapped"] == "always"


def test_spell_spell_card_has_two_spell_faces():
    row = _row("Consecrate // Consume", "Instant // Instant", None,
               "Exile target player's graveyard.\nDraw a card. // Consume deals damage.")
    assert [f["kind"] for f in _face(row)] == ["spell", "spell"]


def test_pathway_has_two_land_faces_each_with_its_own_color():
    row = _row("Riverglide Pathway // Lavaglade Pathway", "Land // Land", None,
               "{T}: Add {U}. // {T}: Add {R}.")
    front, back = _face(row)
    assert [f["kind"] for f in (front, back)] == ["land", "land"]
    assert front["mana"][0]["colors"] == ["U"] and back["mana"][0]["colors"] == ["R"]


def test_adventure_front_is_a_creature_back_a_spell():
    row = _row("Bonecrusher Giant // Stomp", "Creature — Giant // Instant — Adventure", None, "x // y")
    assert [f["kind"] for f in _face(row)] == ["creature", "spell"]


def test_dryad_arbor_style_land_creature_is_a_land():
    assert goldfish.face_kind("Land Creature — Forest Dryad") == "land"
    assert goldfish.face_kind("Artifact Creature — Golem") == "creature"
    assert goldfish.face_kind("Kindred Sorcery — Elf") == "spell"
    assert goldfish.face_kind("Legendary Planeswalker — Jace") == "permanent"


# ---------------------------------------------------------------- costs
def test_cost_plain_colored():
    assert parse_cost("{2}{G}{G}") == {
        "generic": 2, "W": 0, "U": 0, "B": 0, "R": 0, "G": 2, "C": 0,
        "hybrid": [], "twobrid": [], "phyrexian": [], "snow": 0, "x": False}


def test_cost_hybrid_phyrexian_x_and_colorless():
    assert parse_cost("{1}{W/U}{W/U}")["hybrid"] == [["W", "U"], ["W", "U"]]
    assert parse_cost("{2}{B/P}")["phyrexian"] == ["B"]
    assert parse_cost("{X}{R}{R}")["x"] is True
    assert parse_cost("{X}{R}{R}")["R"] == 2
    assert parse_cost("{C}{C}")["C"] == 2
    assert parse_cost("{0}")["generic"] == 0


def test_cost_twobrid_snow_and_colorless_hybrid():
    assert parse_cost("{2/W}{2/W}{2/W}")["twobrid"] == ["W", "W", "W"]
    snow = parse_cost("{1}{S}{S}")
    assert (snow["generic"], snow["snow"]) == (3, 2)
    assert parse_cost("{C/W}")["hybrid"] == [["C", "W"]]


def test_cost_unreadable_is_none():
    for bad in (None, "", float("nan"), "junk", "{Q}"):
        assert parse_cost(bad) is None


# ---------------------------------------------------------------- mana
def test_basic_land_reminder_text():
    assert mana_abilities("({T}: Add {G}.)") == [{"colors": ["G"], "any": False, "amount": 1}]


def test_dual_is_one_mana_of_two_colors():
    assert mana_abilities("({T}: Add {G} or {W}.)") == [{"colors": ["G", "W"], "any": False, "amount": 1}]


def test_triome_is_one_mana_of_three_colors():
    text = "({T}: Add {G}, {W}, or {U}.)\nThis land enters tapped."
    assert mana_abilities(text) == [{"colors": ["G", "W", "U"], "any": False, "amount": 1}]


def test_command_tower_and_birds_are_any_color():
    tower = mana_abilities("{T}: Add one mana of any color in your commander's color identity.")
    assert tower == [{"colors": [], "any": True, "amount": 1}]
    assert mana_abilities("Flying\n{T}: Add one mana of any color.")[0]["any"] is True


def test_sol_ring_is_two_colorless():
    assert mana_abilities("{T}: Add {C}{C}.") == [{"colors": ["C"], "any": False, "amount": 2}]


def test_colored_dork():
    assert mana_abilities("{T}: Add {G}.") == [{"colors": ["G"], "any": False, "amount": 1}]


def test_talisman_is_two_abilities_and_ignores_the_damage_sentence():
    text = "{T}: Add {C}.\n{T}: Add {U} or {B}. This artifact deals 1 damage to you."
    assert mana_abilities(text) == [
        {"colors": ["C"], "any": False, "amount": 1},
        {"colors": ["U", "B"], "any": False, "amount": 1},
    ]


def test_unreadable_amounts_are_none():
    coffers = "{2}, {T}: Add {B} for each Swamp you control."
    assert mana_abilities(coffers)[0]["amount"] is None
    filter_land = "{1}, {T}: Add {W}{W}, {W}{U}, or {U}{U}."
    assert mana_abilities(filter_land)[0]["amount"] is None
    assert mana_abilities("{T}: Add {G} for each creature you control.")[0]["amount"] is None
    assert mana_abilities("{T}: Add X mana of any one color.")[0]["amount"] is None


def test_spend_only_restriction_is_normal_mana():
    text = "{T}: Add {C}. Spend this mana only to cast artifact spells."
    assert mana_abilities(text) == [{"colors": ["C"], "any": False, "amount": 1}]


def test_non_mana_text_has_no_abilities():
    assert mana_abilities("Flying\nWhen this enters, draw a card.") == []
    assert mana_abilities("{T}: Draw a card.") == []
    assert mana_abilities(None) == []
    assert mana_abilities("Sacrifice this artifact: Add {C}.") == []   # one-shot, no {T}


def test_only_mana_producers_get_mana_on_a_face():
    (rock,) = _face(_row("Sol Ring", "Artifact", "{1}", "{T}: Add {C}{C}.", cmc=1))
    (dork,) = _face(_row("Birds", "Creature — Bird", "{G}", "Flying\n{T}: Add one mana of any color.", cmc=1))
    (plain,) = _face(_row("Bear", "Creature — Bear", "{1}{G}", "Trample"))
    (spell,) = _face(_row("Dark Ritual", "Instant", "{B}", "Add {B}{B}{B}.", cmc=1))
    assert rock["mana"][0]["amount"] == 2
    assert dork["mana"][0]["any"] is True
    assert plain["mana"] is None
    assert spell["mana"] is None


def test_non_mana_creature_and_utility_land_are_null():
    (land,) = _face(_row("Maze of Ith", "Land", None, "Untap target attacking creature."))
    assert land["mana"] is None


# ---------------------------------------------------------------- enters tapped
def test_tapland_is_always():
    assert enters_tapped("This land enters tapped.\n{T}: Add {G} or {W}.") == "always"
    assert enters_tapped("Boggart Bog enters the battlefield tapped.") == "always"
    assert enters_tapped("Hagra Broodpit enters tapped.\n{T}: Add {B} or {G}.") == "always"


def test_conditional_lands_are_maybe():
    check = "This land enters tapped unless you control a Swamp or an Island.\n{T}: Add {U} or {B}."
    shock = ("As this land enters, you may pay 2 life. If you don't, it enters tapped.\n"
             "({T}: Add {G} or {W}.)")
    assert enters_tapped(check) == "maybe"
    assert enters_tapped(shock) == "maybe"


def test_basic_and_blank_text_are_none():
    assert enters_tapped("({T}: Add {G}.)") is None
    assert enters_tapped("") is None
    assert enters_tapped(None) is None


def test_back_land_of_a_mdfc_is_read_from_its_own_face_only():
    row = _row("Hagra Mauling // Hagra Broodpit", "Instant // Land", None,
               "This spell costs {1} less to cast if an opponent controls a Swamp. // "
               "Hagra Broodpit enters tapped.\n{T}: Add {B}.")
    front, back = _face(row)
    assert front["enters_tapped"] is None            # only land faces carry it
    assert back["enters_tapped"] == "always"
