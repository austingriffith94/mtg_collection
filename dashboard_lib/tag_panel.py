"""
The Deck Editor's tag tools (docs/TAG_SUGGESTIONS_PLAN.md Phase 4): the "Suggest
tags" pop-up, its Undo, and the vocabulary editor, all in the Card Tags tab.

The Advisor package only proposes. This module holds its proposals in session
state, keyed by deck and layer, and writes only when a button is pressed:
Apply writes the ticked rows through writes.apply_tag_proposals, and the
vocabulary editor saves what you typed. A refresh loses proposals; Close keeps
them, ticks included, so reopening doesn't pay for another request.

The review table's data_editor is built once from a frozen `Proposals.base` and
only rebuilt when `ver` changes. Streamlit resets an editor whose input data
changes, so feeding ticks back into its input would lose every other click.
Ticks are copied out of the editor on each run instead (`_sync_ticks`).
"""
import dataclasses
from dataclasses import dataclass, field

import pandas as pd
import streamlit as st

from . import card_view as cv
from . import loaders, queries as q, tag_vocab, writes
from .advisor import providers, tagging
from .advisor.context import build_tag_context
from .advisor.guard import CheckedTagRow
from .advisor.providers import ProviderError

# Rows the panel can act on; `same` rows are hidden until asked for.
ACTIONABLE = ("new", "conflict")
STATUS_TEXT = {"new": "adds", "conflict": "conflict", "same": "same"}


@dataclass
class ReviewRow:
    row: CheckedTagRow
    accept: bool = False
    replace: bool = False


@dataclass
class Proposals:
    ctx: object                   # TagContext as of the Suggest
    rows: list                    # ReviewRow, one per card
    seen: dict                    # {scryfall_id: [labels]} as of the Suggest
    missing: list                 # cards not reviewed
    model: str
    input_tokens: int
    output_tokens: int
    unknown_rows: int = 0         # rows naming no usable card
    held: list = field(default_factory=list)   # labels thin at the start, their rows unticked
    show_same: bool = False
    ver: int = 0                  # bumped to rebuild the table
    base: object = None           # the frozen table, see the module docstring

    def rebuild(self):
        self.ver += 1
        self.base = None

    def actionable(self):
        return [r for r in self.rows if r.row.status in ACTIONABLE]

    def ticked(self):
        return [r for r in self.rows if r.accept]

    def focus(self):
        """The thin / broad report if every ticked row were applied."""
        ticked = self.ticked()
        return tagging.focus_report(self.ctx, [r.row for r in ticked],
                                    replaced={r.row.card.name for r in ticked if r.replace})


def _store_key(deck_id, tag_type):
    return f"tag_proposals_{deck_id}_{tag_type}"


def _error_key(deck_id, tag_type):
    return f"{_store_key(deck_id, tag_type)}_error"


def _undo_key(deck_id):
    return f"tag_panel_{deck_id}_undo"


def _dialog_key(deck_id):
    return f"tag_panel_{deck_id}_open"


def waiting_proposals(deck_id):
    """How many rows are waiting for review on this deck, across its layers."""
    prefix = _store_key(deck_id, "")
    return sum(len(v.actionable()) for k, v in st.session_state.items()
               if isinstance(k, str) and k.startswith(prefix) and isinstance(v, Proposals))


def proposals_from_result(ctx, seen, result, model):
    """A TagResult as Proposals, ticked the way the plan starts them."""
    rows = [r for r in result.checked.rows if r.card is not None]
    ticked, held = tagging.starts_ticked(ctx, rows)
    return Proposals(ctx, [ReviewRow(r, r.card.name in ticked) for r in rows], seen, result.checked.missing, model,
                     result.input_tokens, result.output_tokens,
                     unknown_rows=len(result.checked.rows) - len(rows), held=held)


def merge_retry(p, result):
    """Add a retry of the unreviewed cards to `p`. Rows already there keep their
    ticks; new ones start the usual way, judged with the whole deck's rows."""
    new = [r for r in result.checked.rows if r.card is not None]
    ticked, held = tagging.starts_ticked(p.ctx, [r.row for r in p.rows] + new)
    p.rows += [ReviewRow(r, r.card.name in ticked) for r in new]
    p.missing = result.checked.missing
    p.input_tokens += result.input_tokens
    p.output_tokens += result.output_tokens
    p.unknown_rows += len(result.checked.rows) - len(new)
    p.held = list(dict.fromkeys(p.held + held))
    p.rebuild()


def apply_rows(p):
    """The ticked rows, as writes.apply_tag_proposals takes them."""
    return [{"card": r.row.card.name, "tags": list(r.row.tags), "replace": r.replace,
             "seen": {sid: p.seen.get(sid, []) for sid in r.row.card.scryfall_ids}} for r in p.ticked()]


def apply_message(result):
    """The line a finished Apply shows, and that labels its Undo."""
    text = f"Added {result['tags_added']} tag(s) on {result['cards']} card(s)"
    if result["replaced"]:
        text += f", replacing the tags on {result['replaced']}"
    return text


def tick_label(p, label, accept=True):
    """Tick (or untick) every row that would add `label` to its card."""
    for r in p.rows:
        if label in r.row.tags and label not in p.ctx.current.get(r.row.card.name, []):
            r.accept = accept
    p.rebuild()


def drop_label(p, label):
    """Take `label` out of the proposals, after it left the vocabulary."""
    for r in p.rows:
        if label not in r.row.tags:
            continue
        r.accept = False
        r.row.tags = [t for t in r.row.tags if t != label]
        if r.row.primary == label:
            r.row.primary = r.row.tags[0] if r.row.tags else None
        r.row.status = tagging.classify(r.row.tags, p.ctx.current.get(r.row.card.name, []))
    p.ctx = dataclasses.replace(p.ctx, vocabulary=[v for v in p.ctx.vocabulary if v["label"] != label])
    p.held = [held for held in p.held if held != label]
    p.rebuild()


def _check_text(row):
    return "✅ verified" if row.verified else f"⚠️ unverified: {row.violations[0]}"


def _table(p):
    shown = ACTIONABLE + (("same",) if p.show_same else ())
    rows = [{"Accept": r.accept, "Replace": r.replace, "Card": r.row.card.name,
             "Change": STATUS_TEXT[r.row.status],
             "Current": ", ".join(p.ctx.current.get(r.row.card.name, [])),
             "Proposed": ", ".join(r.row.tags),
             "Reason": str(r.row.data.get("reason") or ""), "Quote": str(r.row.data.get("evidence_quote") or ""),
             "Check": _check_text(r.row)}
            for r in p.rows if r.row.status in shown]
    columns = ["Accept", "Replace", "Card", "Change", "Current", "Proposed", "Reason", "Quote", "Check"]
    return pd.DataFrame(rows, columns=columns)


def _sync_ticks(p, edited):
    """Copy the table's ticks into the rows, so they survive Close."""
    by_card = {r.row.card.name: r for r in p.rows}
    for _, row in edited.iterrows():
        r = by_card[row["Card"]]
        r.accept, r.replace = bool(row["Accept"]), bool(row["Replace"])


# ------------------------------------------------------------------
# The Suggest pop-up
# ------------------------------------------------------------------
def _run_suggest(conn, deck_id, tag_type, choice, retry_missing=False):
    """One Suggest, or with `retry_missing` a retry of the cards not reviewed.
    A failure leaves any proposals on screen; a failed batch keeps the ones
    that finished. Either way the error waits for the next run to show."""
    key, error_key = _store_key(deck_id, tag_type), _error_key(deck_id, tag_type)
    st.session_state.pop(error_key, None)
    old = st.session_state.get(key)
    try:
        provider = providers.make_provider(choice)
    except ProviderError as e:
        st.session_state[error_key] = str(e)
        return
    if retry_missing:
        ctx = old.ctx
        part = dataclasses.replace(ctx, cards=[c for c in ctx.cards if c.name in old.missing])
        seen, examples = old.seen, tagging.pick_examples(ctx)
    else:
        # Before the call, which takes minutes: an edit made during it has to
        # count as a change since Suggest.
        seen = q.get_card_tags_by_type(conn, deck_id, tag_type)
        ctx = part = build_tag_context(conn, deck_id, tag_type)
        examples = None
    with st.spinner(f"Asking {choice} to read {len(part.cards)} cards. This can take a few minutes…"):
        result = tagging.run_tag_proposals(provider, part, examples=examples)
    if result.error:
        st.session_state[error_key] = result.error
    if result.checked is None:
        return
    if retry_missing:
        merge_retry(old, result)
    else:
        seen = {sid: seen.get(sid, []) for c in ctx.cards for sid in c.scryfall_ids}
        st.session_state[key] = proposals_from_result(ctx, seen, result, choice)


def _render_controls(conn, deck_id, tag_type, p):
    """The model picker, cost note and Suggest button, and the confirmation a
    new Suggest needs while proposals are waiting."""
    choice = st.selectbox("Model", providers.PROVIDER_CHOICES, key=f"tag_panel_{deck_id}_model")
    confirm_key = f"tag_panel_{deck_id}_{tag_type}_confirm"
    if p is None:
        ctx = build_tag_context(conn, deck_id, tag_type)
        undefined = len(q.deck_tag_vocabulary(conn, deck_id, tag_type)) - len(ctx.vocabulary)
        if undefined:
            st.caption(f"{undefined} label(s) have no definition, so the model won't be offered them. "
                       "Add definitions under **Labels and definitions**.")
        if not ctx.vocabulary:
            st.info(f"The {tag_type} layer has no defined labels on this deck yet.")
            return
        tokens_in, tokens_out = tagging.estimate_tokens(ctx)
        st.caption(f"{len(ctx.cards)} cards, {len(ctx.vocabulary)} labels. "
                   f"Estimated use: about {tokens_in:,} tokens in and {tokens_out:,} out.")
    else:
        st.caption(f"Last run: {p.model}, {p.input_tokens:,} tokens in and {p.output_tokens:,} out.")

    label = "✨ Suggest again" if p else "✨ Suggest"
    if st.button(label, key=f"tag_panel_{deck_id}_{tag_type}_suggest"):
        if p and p.actionable():
            st.session_state[confirm_key] = True
        else:
            _run_suggest(conn, deck_id, tag_type, choice)
            st.rerun()
    if st.session_state.get(confirm_key):
        st.warning(f"{len(p.actionable())} proposal(s) are still unapplied. A new Suggest replaces them "
                   "and spends tokens again.")
        replace_col, keep_col = st.columns(2)
        if replace_col.button("Replace them", key=f"tag_panel_{deck_id}_{tag_type}_confirm_yes"):
            st.session_state[confirm_key] = False
            _run_suggest(conn, deck_id, tag_type, choice)
            st.rerun()
        if keep_col.button("Keep them", key=f"tag_panel_{deck_id}_{tag_type}_confirm_no"):
            st.session_state[confirm_key] = False
            st.rerun()


def _render_failures(conn, deck_id, tag_type, p, choice):
    error = st.session_state.get(_error_key(deck_id, tag_type))
    if error:
        st.error(error)
    missing = p.missing if p else []
    if missing:
        st.warning(f"{len(missing)} card(s) were not reviewed: " + ", ".join(missing))
    if (error and p is None) or missing:
        label = f"🔁 Retry {len(missing)} card(s)" if missing else "🔁 Retry"
        if st.button(label, key=f"tag_panel_{deck_id}_{tag_type}_retry"):
            _run_suggest(conn, deck_id, tag_type, choice, retry_missing=bool(missing))
            st.rerun()


def _on_tick_label(p, label):
    tick_label(p, label)


def _on_drop_label(conn, deck_id, tag_type, p, label):
    writes.delete_tag_vocab_label(conn, deck_id, tag_type, label)
    loaders.invalidate_deck_caches()
    drop_label(p, label)


def _render_summary(conn, deck_id, tag_type, p):
    """The line above the table, written after the table has run so it counts
    this run's ticks. Thin labels get their two buttons."""
    focus = p.focus()
    thin = list(dict.fromkeys(focus.thin + p.held))
    parts = [f"{len(p.ticked())} of {len(p.actionable())} ticked", f"{focus.tags_per_card} tags per tagged card"]
    if thin:
        parts.append("thin: " + ", ".join(f"{label} ({focus.counts.get(label, 0)})" for label in thin))
    if focus.broad:
        parts.append("broad: " + ", ".join(f"{label} ({focus.counts[label]})" for label in focus.broad))
    st.markdown(" · ".join(parts))
    for label in thin:
        unticked = [r for r in p.rows if label in r.row.tags and not r.accept
                    and label not in p.ctx.current.get(r.row.card.name, [])]
        tick_col, drop_col, _ = st.columns([2, 3, 3])
        tick_col.button(f"Tick {len(unticked)} {label} row(s)", key=f"tag_panel_{deck_id}_{tag_type}_tick_{label}",
                        disabled=not unticked, on_click=_on_tick_label, args=(p, label))
        drop_col.button(f"Drop {label} from this deck's labels", key=f"tag_panel_{deck_id}_{tag_type}_drop_{label}",
                        on_click=_on_drop_label, args=(conn, deck_id, tag_type, p, label))


def _render_table(deck_id, tag_type, p):
    show_same = st.toggle("Show rows that change nothing", value=p.show_same,
                          key=f"tag_panel_{deck_id}_{tag_type}_show_same")
    if show_same != p.show_same:
        p.show_same = show_same
        p.rebuild()
    if p.base is None:
        p.base = _table(p)
    if p.base.empty:
        st.caption("Nothing to review: the model proposed no changes.")
        return
    edited = st.data_editor(
        p.base,
        column_config={
            "Accept": st.column_config.CheckboxColumn("Accept", width="small"),
            "Replace": st.column_config.CheckboxColumn(
                "Replace", width="small", help="Replace the card's tags in this layer instead of adding to them."),
        },
        disabled=[c for c in p.base.columns if c not in ("Accept", "Replace")],
        hide_index=True,
        width="stretch",
        key=f"tag_panel_{deck_id}_{tag_type}_table_{p.ver}",
    )
    _sync_ticks(p, edited)


def _apply(conn, deck_id, tag_type, p):
    result = writes.apply_tag_proposals(conn, deck_id, tag_type, apply_rows(p))
    done = {row["card"] for row in apply_rows(p)}
    p.rows = [r for r in p.rows if r.row.card.name not in done]
    p.rebuild()
    if not p.actionable() and not p.missing:
        st.session_state.pop(_store_key(deck_id, tag_type), None)
    skipped = [f"{card} ({why})" for card, why in result["skipped"]]
    message = apply_message(result)
    if result["changes"]:
        st.session_state[_undo_key(deck_id)] = {"label": message, "tag_type": tag_type, "changes": result["changes"],
                                                "skipped": skipped}
    else:
        st.session_state.pop(_undo_key(deck_id), None)
        message = "No tags changed."
    cv.close_dialog(_dialog_key(deck_id))
    cv.finish_edit({"message": message, "warnings": ["Skipped: " + "; ".join(skipped)] if skipped else []})


@st.dialog("✨ Suggest tags", width="large", dismissible=False)
def _panel_dialog(conn, deck_id, layers):
    tag_type = st.selectbox("Tag layer", layers, key=f"tag_panel_{deck_id}_layer")
    key = _store_key(deck_id, tag_type)
    p = st.session_state.get(key)
    _render_controls(conn, deck_id, tag_type, p)
    _render_failures(conn, deck_id, tag_type, p, st.session_state.get(f"tag_panel_{deck_id}_model"))

    if p is not None:
        summary = st.container()
        _render_table(deck_id, tag_type, p)
        if p.unknown_rows:
            st.caption(f"{p.unknown_rows} row(s) named no usable card and were left out.")
        with summary:
            _render_summary(conn, deck_id, tag_type, p)

    apply_col, discard_col, close_col = st.columns(3)
    if p is not None:
        ticked = len(p.ticked())
        if apply_col.button(f"✅ Apply {ticked} row(s)", key=f"tag_panel_{deck_id}_{tag_type}_apply",
                            type="primary", disabled=not ticked):
            _apply(conn, deck_id, tag_type, p)
        if discard_col.button("🗑️ Discard", key=f"tag_panel_{deck_id}_{tag_type}_discard"):
            st.session_state.pop(key, None)
            st.session_state.pop(_error_key(deck_id, tag_type), None)
            cv.close_dialog(_dialog_key(deck_id))
            st.rerun()
    if close_col.button("Close", key=f"tag_panel_{deck_id}_close"):
        if p is not None:
            p.rebuild()   # the table's widget state goes when the dialog does
        cv.close_dialog(_dialog_key(deck_id))
        st.rerun()


# ------------------------------------------------------------------
# The vocabulary editor
# ------------------------------------------------------------------
def _vocab_keys(deck_id, tag_type):
    return f"tag_vocab_{deck_id}_{tag_type}_drafts", f"tag_vocab_{deck_id}_{tag_type}_ver"


def _draft(conn, deck_id, tag_type, choice, labels):
    drafts_key, ver_key = _vocab_keys(deck_id, tag_type)
    try:
        provider = providers.make_provider(choice)
    except ProviderError as e:
        st.session_state[drafts_key] = tagging.DefinitionDrafts({}, [], [], list(labels), error=str(e))
        return
    with st.spinner(f"Asking {choice} to draft {len(labels)} definition(s)…"):
        st.session_state[drafts_key] = tagging.draft_definitions(
            provider, build_tag_context(conn, deck_id, tag_type), labels)
    st.session_state[ver_key] = st.session_state.get(ver_key, 0) + 1


def save_vocabulary(conn, deck_id, tag_type, edited):
    """Save the editor's rows: `edited` has Label, Definition and Remove. A
    definition matching the built-in default is stored blank, so a later change
    to the default reaches it. Returns how many labels changed."""
    defaults = tag_vocab.default_definitions(tag_type)
    current = {v["label"]: v["definition"] or "" for v in q.deck_tag_vocabulary(conn, deck_id, tag_type)}
    changed = 0
    for _, row in edited.iterrows():
        label = row["Label"]
        text = "" if pd.isna(row["Definition"]) else str(row["Definition"]).strip()
        if row["Remove"]:
            writes.delete_tag_vocab_label(conn, deck_id, tag_type, label)
            changed += 1
        elif text != current.get(label, ""):
            writes.set_tag_vocab_label(conn, deck_id, tag_type, label,
                                       None if text == defaults.get(label.casefold()) else text)
            changed += 1
    return changed


def _render_add_label(conn, deck_id, tag_type, labels):
    have = {label.casefold() for label in labels}
    menu = [label for label in tag_vocab.FUNCTION_MENU if label.casefold() not in have] \
        if tag_type == tag_vocab.FUNCTION_LAYER else []
    label_col, text_col = st.columns([1, 2])
    new_label = label_col.selectbox("Add a label", menu, index=None, accept_new_options=True,
                                    placeholder="Pick or type a label", key=f"tag_vocab_{deck_id}_{tag_type}_add")
    text = text_col.text_input("Its definition", key=f"tag_vocab_{deck_id}_{tag_type}_add_text",
                               placeholder="Blank uses the built-in definition, if it has one")
    if st.button("➕ Add label", key=f"tag_vocab_{deck_id}_{tag_type}_add_btn", disabled=not new_label):
        writes.set_tag_vocab_label(conn, deck_id, tag_type, new_label, text)
        cv.finish_edit({"message": f"Added {new_label.strip()} to this deck's {tag_type} labels."})


def _render_vocabulary(conn, deck_id, tag_type):
    drafts_key, ver_key = _vocab_keys(deck_id, tag_type)
    vocabulary = q.deck_tag_vocabulary(conn, deck_id, tag_type)
    tags = q.get_card_tags_by_type(conn, deck_id, tag_type)
    counts = {}
    for labels in tags.values():
        for label in labels:
            counts[label.casefold()] = counts.get(label.casefold(), 0) + 1
    result = st.session_state.get(drafts_key)
    drafted = result.drafts if result else {}
    undefined = [v["label"] for v in vocabulary if not v["definition"]]

    st.caption("The model may only use labels with a definition, and it follows the wording closely.")
    if len(vocabulary) > tag_vocab.MAX_ACTIVE_LABELS:
        st.warning(f"This deck has {len(vocabulary)} {tag_type} labels. "
                   f"Past {tag_vocab.MAX_ACTIVE_LABELS}, tags get too thin to sort a deck by.")

    if vocabulary:
        table = pd.DataFrame([{"Label": v["label"], "Cards": counts.get(v["label"].casefold(), 0),
                               "Definition": v["definition"] or drafted.get(v["label"], ""),
                               "Note": ("Draft, not saved" if not v["definition"] and v["label"] in drafted else "")
                               + (" (from one card)" if result and v["label"] in result.one_card else ""),
                               "Remove": False} for v in vocabulary])
        edited = st.data_editor(
            table,
            column_config={"Definition": st.column_config.TextColumn("Definition", width="large"),
                           "Remove": st.column_config.CheckboxColumn(
                               "Remove", help="A label still on cards stays listed, without its saved definition.")},
            disabled=["Label", "Cards", "Note"],
            hide_index=True,
            width="stretch",
            key=f"tag_vocab_{deck_id}_{tag_type}_editor_{st.session_state.get(ver_key, 0)}",
        )
        if st.button("💾 Save definitions", key=f"tag_vocab_{deck_id}_{tag_type}_save"):
            changed = save_vocabulary(conn, deck_id, tag_type, edited)
            st.session_state.pop(drafts_key, None)
            st.session_state[ver_key] = st.session_state.get(ver_key, 0) + 1
            cv.finish_edit({"message": f"Saved {changed} label(s)."})
    else:
        st.caption(f"No {tag_type} labels on this deck yet.")

    if undefined:
        model_col, draft_col = st.columns([1, 2], vertical_alignment="bottom")
        choice = model_col.selectbox("Model", providers.PROVIDER_CHOICES, key=f"tag_vocab_{deck_id}_{tag_type}_model")
        if draft_col.button(f"✍️ Draft {len(undefined)} definition(s)", key=f"tag_vocab_{deck_id}_{tag_type}_draft"):
            _draft(conn, deck_id, tag_type, choice, undefined)
            st.rerun()
    if result:
        if result.error:
            st.error(result.error)
        if result.no_cards or result.missing:
            st.caption("No draft for: " + ", ".join(result.no_cards + result.missing))
        if result.drafts:
            st.caption(f"Drafted with {result.input_tokens:,} tokens in and {result.output_tokens:,} out. "
                       "Nothing is saved until you press Save definitions.")
    _render_add_label(conn, deck_id, tag_type, [v["label"] for v in vocabulary])


# ------------------------------------------------------------------
# The Card Tags tab's entry point
# ------------------------------------------------------------------
def render_tag_tools(conn, deck_id, tag_type, existing_types):
    """The Suggest button, its Undo, the vocabulary editor and, when open, the
    pop-up. `tag_type` is the tab's current layer, where the pop-up starts."""
    suggest_col, note_col = st.columns([1, 3], vertical_alignment="center")
    if suggest_col.button("✨ Suggest tags", key=f"tag_panel_{deck_id}_open_btn"):
        st.session_state[f"tag_panel_{deck_id}_layer"] = tag_type
        cv.open_dialog(_dialog_key(deck_id))
    waiting = waiting_proposals(deck_id)
    if waiting:
        note_col.caption(f"🕓 {waiting} proposal(s) waiting")

    undo = st.session_state.get(_undo_key(deck_id))
    if undo:
        if st.button(f"↶ Undo: {undo['label']}", key=f"tag_panel_{deck_id}_undo_btn"):
            restored, left = writes.undo_tag_proposals(conn, deck_id, undo["tag_type"], undo["changes"])
            st.session_state.pop(_undo_key(deck_id), None)
            cv.finish_edit({"message": f"Restored the tags on {restored} card(s).",
                            "warnings": ["Left alone, edited since: " + ", ".join(left)] if left else []})
        if undo["skipped"]:
            st.caption("Skipped by that Apply: " + "; ".join(undo["skipped"]))

    with st.expander("📚 Labels and definitions"):
        _render_vocabulary(conn, deck_id, tag_type)

    if cv.dialog_is_open(_dialog_key(deck_id)):
        layers = sorted(set(existing_types) | {tag_vocab.STRATEGY_LAYER, tag_vocab.FUNCTION_LAYER, tag_type})
        _panel_dialog(conn, deck_id, layers)
