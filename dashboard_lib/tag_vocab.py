"""The `function` tag layer's menu (docs/archive/TAG_SUGGESTIONS_PLAN.md).

A tag type is a layer, and a layer's vocabulary is the labels the Advisor's
model may use. FUNCTION_MENU holds the default labels and definitions for the
`function` layer. It can be broad because a deck uses only part of it: only a
deck's active labels go to the model (queries.deck_tag_vocabulary). The model
follows these definitions, so the edge cases are spelled out in them.

Pure data with no database or Streamlit use, so anything can import it.
"""

FUNCTION_LAYER = "function"
STRATEGY_LAYER = "strategy"
# The vocabulary editor warns past this many labels for a deck and layer.
MAX_ACTIVE_LABELS = 10

FUNCTION_MENU = {
    "Card Adv": "Draws extra cards or otherwise gives repeatable access to more cards than it costs.",
    "Ramp": "Produces extra mana or puts extra lands into play, ahead of the normal curve.",
    "Removal": (
        "Takes a specific thing off an opponent's board: destroy, exile, sacrifice, or fight effects. "
        "Bounce counts, since it takes the thing off the board. A card is Removal or Interaction, never "
        "both, and a sweep of the whole board is Wipe."
    ),
    "Interaction": (
        "Affects an opponent's plays or board without taking things off it: counterspells, tapping down, "
        "goad or forced combat, stealing, taxing, and damage pingers. A card is Removal or Interaction, "
        "never both."
    ),
    "Wipe": "Clears most or all of the board, or a whole class of permanents, in one effect.",
    "Protection": "Keeps your own commander, creatures or board alive against removal, wipes or combat.",
    "Tutor": "Searches the library for a specific card or kind of card.",
    "Recursion": "Returns cards from a graveyard to hand or to the battlefield.",
    "Token Maker": "Creates creature or other tokens as a main part of what it does.",
    "Sac Outlet": "Lets you sacrifice permanents on demand, at little or no cost.",
    "Fodder": "Cheap or disposable bodies that exist to be sacrificed, discarded or spent by the deck's plan.",
    "Pressure": "Ends games through combat: efficient attackers, evasion, extra combats, or team-wide pumps.",
    "Voltron": "Makes one creature a lethal threat: equipment, auras, and commander-damage pieces.",
    "Tax/Stax": "Slows every opponent: extra costs, limits on casting or attacking, or denied resources.",
    "Politics": "Steers the table: offers deals, rewards other players, or points opponents at each other.",
    "Combo Piece": "A named part of a specific game-winning or engine combo in this deck.",
    "Win Con": "A card the deck actually plans to end the game with.",
    "Fixing": "Produces or finds mana of the deck's different colors.",
}

# The labels that already sit in `strategy` and are copied once into
# `function` (writes.copy_strategy_tags_to_function).
COPIED_FUNCTION_LABELS = ("Card Adv", "Interaction", "Ramp", "Removal", "Protection", "Wipe")


def default_definitions(tag_type):
    """{label.casefold(): definition} of a layer's built-in defaults. Only
    `function` has any; every other layer's definitions are saved per deck."""
    if tag_type != FUNCTION_LAYER:
        return {}
    return {label.casefold(): definition for label, definition in FUNCTION_MENU.items()}
