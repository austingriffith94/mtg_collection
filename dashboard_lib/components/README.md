# components/

Source for the app's custom Streamlit components (Custom Component v2).
Plain files, no npm / build step: `dashboard_lib/<name>.py` reads the `.js`
and `.css` here at import and passes them to `st.components.v2.component`
as inline strings.

| Component | Files | Python side |
|---|---|---|
| Stacks deck board (read-only on the Decks page, editable on the Deck Editor's Board tab) | `deck_board.js`, `deck_board.css` | [../deck_board.py](../deck_board.py) |

Rules for editing these:

- The JS is a CCv2 module: `export default function(component)`, drawing
  under `component.parentElement` (a shadow root, never `document`) and
  returning a cleanup. No v1 APIs (`Streamlit.setComponentValue`,
  `postMessage`, `streamlit-component-lib`).
- Keep logic in Python where possible; AppTest can't click inside the JS,
  so JS behavior is checked by hand in the running app.
- Edits go back as `setTriggerValue("action", {...})`, one per committed
  choice; Python (`deck_board.apply_board_action`) is the source of truth
  and every rerun re-hydrates `data`, so don't update state optimistically.
- Colors come from Streamlit's `--st-*` theme variables so light/dark work
  without separate styling.
- Set card text with `textContent`, never `innerHTML`.
- Don't hand the component a filesystem path or base64 image: URLs only.
