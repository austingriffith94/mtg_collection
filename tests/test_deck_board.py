"""deck_board.board_payload() — the pure data contract behind the Stacks
view (VISUAL_DECK_PLAN.md Phase A). The JS side can't be run here, so the
logic it relies on (ordering, flags, URL-only images) is pinned down on the
Python side."""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard_lib import deck_board  # noqa: E402


def _row(name, type_line="Creature", cmc=2, qty=1, **extra):
    row = {
        "scryfall_id": f"{name}-id", "name": name, "type_line": type_line,
        "mana_cost": "{1}{G}", "cmc": cmc, "color_identity": "G",
        "image_uri": f"https://cards.scryfall.io/{name}.jpg", "local_image_path": None,
        "is_basic_land": 0, "is_game_changer": 0, "is_reserved": 0,
        "current_price_usd": 1.5, "quantity": qty, "strategy_tags": None,
    }
    row.update(extra)
    return row


OWNED = {n.lower(): {"owned_qty": 1, "available_qty": 1} for n in ("Sol Ring", "Forest", "Birds", "Boss")}
META = {"commander": "Boss", "partner": None}


def test_empty_deck_gives_empty_payload():
    assert deck_board.board_payload(pd.DataFrame(), META, OWNED) == []


def test_commander_is_flagged_and_pinned_first():
    df = pd.DataFrame([_row("Birds", cmc=1), _row("Boss", "Legendary Creature", cmc=5), _row("Sol Ring", "Artifact", cmc=1)])
    cards = deck_board.board_payload(df, META, OWNED)
    assert [c["name"] for c in cards] == ["Boss", "Birds", "Sol Ring"]  # commander, then cmc, then name
    assert [c["flags"]["commander"] for c in cards] == [True, False, False]


def test_mdfc_land_bucketed_by_front_face_but_flagged_as_land():
    df = pd.DataFrame([_row("Boss"), _row("Bala Ged", "Sorcery // Land", cmc=0)])
    mdfc = next(c for c in deck_board.board_payload(df, META, OWNED) if c["name"] == "Bala Ged")
    assert mdfc["type"] == "Sorcery"
    assert mdfc["is_land"] is True


def test_not_owned_flag_skips_basics_and_uses_owned_map():
    df = pd.DataFrame([
        _row("Boss"), _row("Birds"), _row("Mystery Card"),
        _row("Plains", "Basic Land — Plains", cmc=0, is_basic_land=1),
    ])
    flags = {c["name"]: c["flags"]["not_owned"] for c in deck_board.board_payload(df, META, OWNED)}
    assert flags == {"Boss": False, "Birds": False, "Mystery Card": True, "Plains": False}


def test_tags_flags_and_nan_handling():
    df = pd.DataFrame([_row(
        "Boss", strategy_tags="Ramp,Removal", is_game_changer=1, is_reserved=1,
        current_price_usd=float("nan"), mana_cost=None,
    )])
    (card,) = deck_board.board_payload(df, META, OWNED)
    assert card["tags"] == ["Ramp", "Removal"]
    assert card["flags"]["game_changer"] and card["flags"]["reserved"]
    assert card["price"] is None
    assert card["mana_cost"] == ""


def test_payload_has_urls_only_and_stays_small():
    df = pd.DataFrame(
        [_row("Boss", "Legendary Creature", cmc=5)]
        + [_row(f"Card {i}", cmc=i % 7, strategy_tags="Ramp") for i in range(98)]
        + [_row("Forest", "Basic Land — Forest", cmc=0, qty=30, is_basic_land=1)]
    )
    cards = deck_board.board_payload(df, META, OWNED)
    blob = repr(cards)
    assert "data:" not in blob and "local_image_path" not in blob
    assert all(c["img"].startswith("http") for c in cards)
    assert next(c for c in cards if c["name"] == "Forest")["qty"] == 30
    assert deck_board.payload_size_bytes(cards) < 50_000
