# components/

Source for the app's custom Streamlit components (Custom Component v2).
Plain files, no npm / build step: `dashboard_lib/<name>.py` reads the `.js`
and `.css` here at import and passes them to `st.components.v2.component`
as inline strings.

| Component | Files | Python side |
|---|---|---|
| Stacks deck board + Shortlist rail (read-only on the Decks page, editable on the Deck Editor's Board tab) | `deck_board.js`, `deck_board.css` | [../deck_board.py](../deck_board.py) |
| Goldfish: play a deck's first turns alone by clicking (Goldfish page) | `goldfish.js`, `goldfish.css` | [../goldfish.py](../goldfish.py) |

Rules for editing these:

- **Goldfish is the exception to the callback rule below.** It has no
  callbacks: Python sends the deck once and never hears back, so the game
  (shuffle, draw, tap, undo, the turn summary) is JS-only state. It lives in
  a store on `parentElement.__goldfish`, not in the function's closure,
  because the function runs again on every data update and tears down its
  DOM each time. The file is split into a RULES part with no DOM code
  (state, shuffle, draw, mulligan, play, tap, castable check, search,
  look-at-top, `recordTurn`) and a DRAWING part; every click goes through
  `store.act(name, ...args)`, which snapshots for undo, runs the rule,
  records the turn summary and redraws. A headless check can call the rules
  directly (the page is keyed per deck, so a new deck is a new game).

- The JS is a CCv2 module: `export default function(component)`, drawing
  under `component.parentElement` (a shadow root, never `document`) and
  returning a cleanup. No v1 APIs (`Streamlit.setComponentValue`,
  `postMessage`, `streamlit-component-lib`).
- Keep logic in Python where possible; AppTest can't click inside the JS,
  so JS behavior is checked by hand in the running app, or scripted in
  headless Edge over the DevTools protocol against a COPY of the database
  (never the real one). There is no node here: parse-check the JS with
  `esprima` (`parseModule`) installed into a scratch directory.
- Edits go back as `setTriggerValue("action", {...})`, one per committed
  choice; Python (`deck_board.apply_board_action`) is the source of truth
  and every rerun re-hydrates `data`, so don't update state optimistically.
- Pick mode ("Swap in for…" -> click the card to cut) is browser-only state:
  nothing is sent until a card is picked, and Esc / Cancel leaves it. A
  committed action makes the board inert until the rerun re-draws it, with a
  timeout in case Python refuses the edit and `data` doesn't change.
- Colors come from Streamlit's `--st-*` theme variables so light/dark work
  without separate styling.
- Set card text with `textContent`, never `innerHTML`.
- Don't hand the component a filesystem path or base64 image: URLs only.
