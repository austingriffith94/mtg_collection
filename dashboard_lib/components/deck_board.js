// Stacks deck board (docs/archive/VISUAL_DECK_PLAN.md, Phase A read-only view + Phase B
// edits + Phase C Shortlist column and pick mode).
//
// CCv2 module: `export default function(component)`. Everything is drawn
// under `parentElement` (a shadow root — never `document`, so two boards
// can't collide) and the function returns a cleanup. Only CCv2 APIs here:
// no Streamlit.setComponentValue / postMessage / streamlit-component-lib.
//
// data (from deck_board.render_deck_board):
//   cards         [{id,name,qty,cmc,mana_cost,type_line,img,type,is_land,
//                   colors,tags,price,flags:{game_changer,reserved,
//                   not_owned,commander},cut_for:[incoming names]}]
//                   — already sorted commander, then mana value, then name
//   shortlist     [{change_id,id,name,status:"idea"|"planned",qty,img,
//                   type_line,price,replaces,verdict_text,badge:"available"|
//                   "elsewhere"|"buy"|"neutral",unlinked}] — ideas first,
//                   then planned; an unlinked row is a typed name (no id/img)
//                   (deck_board.shortlist_payload); [] hides the column
//                   Phase 2 adds per row: source ("storage" or null = Auto),
//                   source_location, source_options [{location, quantity}]
//                   (storage Locations holding a free copy) and auto_label
//                   (what Auto would do right now)
//                 Phase 3 adds deck_options [{deck, label}] — other decks whose
//                 list runs the card; source "deck" + source_location = that
//                 deck is a take, shown "from Gisa"
//   leaving       [{change_id,id,name,qty,to_deck,receiver_status}] — this
//                   deck's cards another deck is taking (Phase 3): a "Leaving"
//                   rail group with Drop (keep it here), and each card in it
//                   carries leaving:[deck names] for its "→ Prosper" badge
//   locations     storage Locations a removed copy can be put in, Box first
//   copies_move   false while the deck is brewing/dismantled: edits are list
//                   only, so the menu doesn't ask where copies go. A card
//                   with basic:true (Phase 7) never asks either, and a
//                   Shortlist row for one has can_buy:false and no options
//   group_by      saved choice, one of group_options
//   group_options ["Type","Mana value","Strategy tag","Color"]
//   type_order    stack order for "Type"
//   editable      true on the Deck Editor's Board tab only
//
// Where copies come from / go (docs/archive/EDIT_FLOW_PLAN.md Phase 2): "Add" and "Apply"
// on a Shortlist row open a small menu (Auto first, then each Location with a
// free copy) when there is a choice, and the card menu's "-1 to..." / "Remove
// to..." ask where the copy is put. The pick rides on the same trigger as
// extra fields: {source, source_location} / {dest}. Python re-validates all of
// it; nothing here names a lot.
//
// Taking from another deck (Phase 3): the same menu lists each other deck
// that runs the card. Picking one edits THAT deck's list too, so the first
// click only arms it ("Also takes it off Gisa's list — click again"); on a
// brewing deck (copies_move false) nothing leaves Gisa yet, so one click.
//
// Regrouping and the hover preview are pure browser work (no rerun). Sent
// back to Python:
//   - the group-by choice, via setStateValue, so it survives reruns;
//   - when editable, one committed edit per menu choice, via
//     setTriggerValue("action", {op, id[, tags]}). Python applies it
//     (deck_board.apply_board_action) and the next rerun re-hydrates `data`,
//     so the database is always the truth: nothing is updated optimistically
//     here, and the menu just shows "Saving…" until the rerun lands.
//
// Pick mode (Phase C): "Swap in for…" on a Shortlist idea dims the board and
// the NEXT mainboard card clicked is the card to cut — one trigger
// {op:"plan", change_id, cut_id}. Esc, the banner's Cancel, or the same
// button again leaves pick mode. It lives only in this browser: nothing is
// sent until a card is picked. Apply asks for a second click first, since it
// moves collection lots and has no undo.
//
// Card names, tags etc. are set with textContent / attributes, never
// innerHTML, so a card name can't inject markup.

const WUBRG = ["W", "U", "B", "R", "G"];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function qtyOf(cards) {
  // Stack headers count copies (30 basics = 30), not rows.
  return cards.reduce((n, c) => n + c.qty, 0);
}

// ---- grouping ---------------------------------------------------------
// Each grouper returns [{label, cards}] in display order. The commander(s)
// are pulled out into their own first stack before any grouping runs.

function groupByType(cards, typeOrder) {
  const buckets = new Map(typeOrder.map(t => [t, []]));
  buckets.set("Other", []);
  for (const c of cards) {
    (buckets.get(c.type) || buckets.get("Other")).push(c);
  }
  return [...buckets].map(([label, list]) => ({ label, cards: list }));
}

function groupByManaValue(cards) {
  // Lands get their own trailing stack: their mana value of 0 would
  // otherwise bury the real 0-drops.
  const buckets = new Map();
  const lands = [];
  for (const c of cards) {
    if (c.is_land) { lands.push(c); continue; }
    const mv = Math.min(Math.floor(c.cmc), 7);
    if (!buckets.has(mv)) buckets.set(mv, []);
    buckets.get(mv).push(c);
  }
  const out = [...buckets.keys()].sort((a, b) => a - b)
    .map(mv => ({ label: mv >= 7 ? "7+" : String(mv), cards: buckets.get(mv) }));
  out.push({ label: "Land", cards: lands });
  return out;
}

function groupByTag(cards) {
  // First tag only: a card with two tags in two stacks would double-count
  // its copies in the headers.
  const buckets = new Map();
  const untagged = [];
  for (const c of cards) {
    if (!c.tags.length) { untagged.push(c); continue; }
    if (!buckets.has(c.tags[0])) buckets.set(c.tags[0], []);
    buckets.get(c.tags[0]).push(c);
  }
  const out = [...buckets.keys()].sort((a, b) => a.localeCompare(b))
    .map(label => ({ label, cards: buckets.get(label) }));
  out.push({ label: "Untagged", cards: untagged });
  return out;
}

function groupByColor(cards) {
  const buckets = new Map();
  for (const c of cards) {
    const ordered = WUBRG.filter(x => c.colors.includes(x));
    const label = ordered.length ? ordered.join("/") : "Colorless";
    if (!buckets.has(label)) buckets.set(label, []);
    buckets.get(label).push(c);
  }
  // Mono colors in WUBRG order, then multicolor by size, Colorless last.
  const rank = label => {
    if (label === "Colorless") return [9, 0];
    const parts = label.split("/");
    return [parts.length, WUBRG.indexOf(parts[0])];
  };
  return [...buckets.keys()]
    .sort((a, b) => {
      const [na, ia] = rank(a), [nb, ib] = rank(b);
      return na - nb || ia - ib || a.localeCompare(b);
    })
    .map(label => ({ label, cards: buckets.get(label) }));
}

function buildStacks(cards, groupBy, typeOrder) {
  const commanders = cards.filter(c => c.flags.commander);
  const rest = cards.filter(c => !c.flags.commander);
  let groups;
  if (groupBy === "Mana value") groups = groupByManaValue(rest);
  else if (groupBy === "Strategy tag") groups = groupByTag(rest);
  else if (groupBy === "Color") groups = groupByColor(rest);
  else groups = groupByType(rest, typeOrder);
  groups = groups.filter(g => g.cards.length);
  if (commanders.length) groups.unshift({ label: "Commander", cards: commanders });
  return groups;
}

// ---- rendering --------------------------------------------------------

function badge(className, text, title) {
  const b = el("span", "badge " + className, text);
  if (title) b.title = title;
  return b;
}

function money(v) {
  return v === null || v === undefined ? "—" : "$" + v.toFixed(2);
}

export default function (component) {
  const { data, parentElement, setStateValue, setTriggerValue } = component;
  const cards = data.cards || [];
  const typeOrder = data.type_order || [];
  const editable = !!data.editable;
  const shortlist = data.shortlist || [];
  const leaving = data.leaving || [];
  const locations = data.locations || [];
  const copiesMove = data.copies_move !== false;
  const options = data.group_options || ["Type"];
  let groupBy = options.includes(data.group_by) ? data.group_by : options[0];

  // This function runs again on every data update with the SAME
  // parentElement, and the previous run's cleanup is not guaranteed to have
  // happened first — without this, each edit stacked another board below the
  // last. So every run tears down the previous one itself.
  if (parentElement.__boardCleanup) parentElement.__boardCleanup();

  const root = el("div", "board");
  parentElement.appendChild(root);

  if (!cards.length) {
    root.appendChild(el("div", "empty", "No mainboard cards to show."));
    const cleanupEmpty = () => { root.remove(); parentElement.__boardCleanup = null; };
    parentElement.__boardCleanup = cleanupEmpty;
    return cleanupEmpty;
  }

  // Toolbar: group-by switch + total.
  const toolbar = el("div", "toolbar");
  toolbar.appendChild(el("span", "toolbar-label", "Group by"));
  const buttons = new Map();
  for (const opt of options) {
    const btn = el("button", "group-btn", opt);
    btn.type = "button";
    btn.onclick = () => {
      groupBy = opt;
      setStateValue("group_by", opt);
      renderStacks();
    };
    buttons.set(opt, btn);
    toolbar.appendChild(btn);
  }
  toolbar.appendChild(el("span", "toolbar-total", qtyOf(cards) + " cards"));
  root.appendChild(toolbar);

  const layout = el("div", "layout");
  const stacksEl = el("div", "stacks");
  // One sticky right rail: the preview on top (so a long Shortlist can't push
  // it out of view), the Shortlist (its own scroll, capped height) under it.
  // A separate Shortlist column beside the preview took ~470px from the stacks and halved how many fit per row.
  const rail = el("div", "rail");
  const preview = el("aside", "preview");
  const shortlistEl = el("aside", "shortlist");
  rail.append(preview);
  if (shortlist.length || leaving.length) rail.append(shortlistEl);
  layout.append(stacksEl, rail);
  root.appendChild(layout);

  // Banner shown while picking; sits between the toolbar and the board.
  const banner = el("div", "pick-banner");
  banner.hidden = true;
  root.insertBefore(banner, layout);

  // ---- hover / tap preview ----
  function showPreview(card) {
    preview.replaceChildren();
    preview.classList.add("has-card");
    if (card.img) {
      const img = el("img", "preview-img");
      img.src = card.img;
      img.alt = card.name;
      preview.appendChild(img);
    } else {
      preview.appendChild(el("div", "preview-noimg", card.name));
    }
    preview.appendChild(el("div", "preview-name", card.name));
    preview.appendChild(el("div", "preview-type", card.type_line));
    const facts = [];
    if (card.qty > 1) facts.push("×" + card.qty);
    facts.push(money(card.price));
    if (card.flags.game_changer) facts.push("Game Changer");
    if (card.flags.reserved) facts.push("Reserved List");
    if (card.flags.not_owned) facts.push("Not owned");
    preview.appendChild(el("div", "preview-facts", facts.join(" · ")));
    if (card.tags.length) preview.appendChild(el("div", "preview-tags", card.tags.join(", ")));
  }

  function renderPreviewHint() {
    preview.replaceChildren(el("div", "preview-hint", "Hover a card to see it in full."));
    preview.classList.remove("has-card");
  }

  // ---- edit menu (editable boards only) ----
  // One floating menu, repositioned per card. Each choice sends exactly one
  // trigger; Python decides whether it is still valid.
  const menu = el("div", "menu");
  menu.hidden = true;
  root.appendChild(menu);
  let menuCard = null;

  function closeMenu() {
    menu.hidden = true;
    menuCard = null;
  }

  function send(action) {
    menu.replaceChildren(el("div", "menu-saving", "Saving…"));
    setTriggerValue("action", action);
  }

  function menuButton(label, onClick, className) {
    const b = el("button", "menu-btn" + (className ? " " + className : ""), label);
    b.type = "button";
    b.onclick = ev => { ev.stopPropagation(); onClick(); };
    return b;
  }

  function showTagEditor(card) {
    // Comma-separated, prefilled with the card's tags; the datalist offers
    // the tags already used elsewhere on this board.
    const known = [...new Set(cards.flatMap(c => c.tags))].sort((a, b) => a.localeCompare(b));
    const list = el("datalist");
    list.id = "board-tag-options";
    for (const t of known) { const o = el("option"); o.value = t; list.appendChild(o); }
    const input = el("input", "menu-input");
    input.type = "text";
    input.value = card.tags.join(", ");
    input.placeholder = "Ramp, Removal";
    input.setAttribute("list", list.id);
    const save = () => send({
      op: "set_tags", id: card.id,
      tags: input.value.split(",").map(t => t.trim()).filter(Boolean),
    });
    input.addEventListener("keydown", ev => { if (ev.key === "Enter") save(); });
    menu.replaceChildren(
      el("div", "menu-title", "Tags (comma-separated)"), input, list,
      menuButton("Save tags", save, "menu-primary"),
      menuButton("Cancel", closeMenu),
    );
    input.focus();
  }

  // "Put it where?" for -1 / Remove: Box first (locations is sent that way),
  // then the other storage Locations. Back returns to the card menu.
  function showDestMenu(card, node, op, title) {
    menu.replaceChildren(
      el("div", "menu-title", title),
      ...locations.map(loc => menuButton(loc, () => send({ op, id: card.id, dest: loc }))),
      menuButton("Back", () => openMenu(card, node)),
    );
  }

  function openMenu(card, node) {
    menuCard = card;
    // Only worth asking when copies actually move and there is a choice. A
    // basic land's never do (docs/archive/EDIT_FLOW_PLAN.md Phase 7).
    const askWhere = copiesMove && locations.length > 1 && !card.basic;
    menu.replaceChildren(
      el("div", "menu-title", card.name),
      menuButton("+1", () => send({ op: "inc", id: card.id })),
      menuButton(card.qty > 1 ? "−1" : "−1 (removes it)", () => send({ op: "dec", id: card.id })),
      ...(askWhere && card.qty > 1
        ? [menuButton("−1 to…", () => showDestMenu(card, node, "dec", "Put the copy in…"))] : []),
      menuButton("Remove", () => send({ op: "remove", id: card.id }), "menu-danger"),
      ...(askWhere
        ? [menuButton("Remove to…", () => showDestMenu(card, node, "remove", "Put the copies in…"))] : []),
      menuButton("Move to Shortlist", () => send({ op: "to_shortlist", id: card.id })),
      menuButton("Tags…", () => showTagEditor(card)),
    );
    menu.hidden = false;
    // Position under the clicked bar, relative to the board root.
    const r = node.getBoundingClientRect();
    const base = root.getBoundingClientRect();
    menu.style.left = Math.max(0, Math.min(r.left - base.left, base.width - menu.offsetWidth)) + "px";
    menu.style.top = (r.top - base.top + Math.min(r.height, 40)) + "px";
  }

  // ---- pick mode ----
  let pick = null;   // the Shortlist idea waiting for a card to cut, or null

  function setPick(item) {
    pick = item;
    root.classList.toggle("picking", !!item);
    banner.hidden = !item;
    banner.replaceChildren();
    if (item) {
      closeMenu();
      banner.append(
        el("span", null, "Pick the card to cut for " + item.name + " — Esc to cancel"),
        menuButton("Cancel", () => setPick(null)),
      );
    }
    renderShortlist();
  }

  // Everything that sends one trigger and then waits for the rerun to
  // re-hydrate `data`: the board is inert meanwhile so a double-click can't
  // send twice. (Python also rejects a stale second click.)
  function commit(action) {
    root.classList.add("saving");
    setTriggerValue("action", action);
    // A refused edit (stale board, toast shown) may leave `data` unchanged,
    // in which case this function never re-runs to rebuild a fresh board —
    // so don't stay inert forever. Pick mode ends with the attempt either way.
    setTimeout(() => {
      root.classList.remove("saving");
      if (pick) setPick(null);
      if (srcMenu) { srcMenu = null; renderShortlist(); }
    }, 3000);
  }

  // Esc and a click anywhere else close it. Listeners go on `root` (inside
  // the shadow root) plus window for Esc, and are removed in the cleanup.
  const onKey = ev => {
    if (ev.key !== "Escape") return;
    closeMenu();
    if (pick) setPick(null);
  };
  const onOutside = ev => { if (!menu.hidden && !menu.contains(ev.target)) closeMenu(); };
  if (editable) {
    window.addEventListener("keydown", onKey);
    root.addEventListener("click", onOutside);
  }

  // ---- stacks ----
  function cardNode(card, isLast) {
    const node = el("div", "card" + (isLast ? " last" : ""));
    if (card.img) {
      const img = el("img", "card-img");
      img.src = card.img;
      img.alt = card.name;
      img.loading = "lazy";
      node.appendChild(img);
    } else {
      // No cached art: a plain name bar, so the stack is still readable.
      node.appendChild(el("div", "card-noimg", card.name));
    }
    const badges = el("div", "badges");
    if (card.qty > 1) badges.appendChild(badge("b-qty", "×" + card.qty, card.qty + " copies"));
    if (card.flags.game_changer) badges.appendChild(badge("b-gc", "GC", "Game Changer"));
    if (card.flags.not_owned) badges.appendChild(badge("b-no", "✗", "Not owned"));
    // A decided purchase (Phase 4): on the list, no copy sleeved yet.
    if (card.flags.buying) badges.appendChild(badge("b-buy", "🛒", "Buying — no copy sleeved yet"));

    // pointerenter covers mouse; click covers touch (no hover there).
    node.addEventListener("pointerenter", () => showPreview(card));
    // A planned swap's cut card says what it is being cut for.
    if (card.cut_for && card.cut_for.length) {
      node.classList.add("is-cut");
      badges.appendChild(badge("b-cut", "✂ " + card.cut_for.join(", "), "Planned to be cut for " + card.cut_for.join(", ")));
    }
    // Another deck is taking it (Phase 3): it leaves when that take applies.
    if (card.leaving && card.leaving.length) {
      node.classList.add("is-leaving");
      badges.appendChild(badge("b-leave", "→ " + card.leaving.join(", "), "Leaving → " + card.leaving.join(", ")));
    }
    if (badges.childElementCount) node.appendChild(badges);

    node.addEventListener("click", ev => {
      showPreview(card);
      if (pick) {
        ev.stopPropagation();
        if (card.flags.commander) return;   // never offered as the cut
        commit({ op: "plan", change_id: pick.change_id, cut_id: card.id });
        return;
      }
      if (editable) {
        ev.stopPropagation();  // keep the outside-click closer below from eating this open
        openMenu(card, node);
      }
    });
    return node;
  }

  function renderStacks() {
    for (const [opt, btn] of buttons) btn.classList.toggle("active", opt === groupBy);
    stacksEl.replaceChildren();
    for (const group of buildStacks(cards, groupBy, typeOrder)) {
      const stack = el("section", "stack");
      const head = el("header", "stack-head");
      head.appendChild(el("span", "stack-title", group.label));
      const count = el("span", "stack-count", String(qtyOf(group.cards)));
      if (groupBy === "Type" && group.label === "Land") {
        // Land-side MDFCs sit in their front-face stack (Instant, Creature...)
        // but still count toward the land total; the cards stay where they are.
        const mdfc = qtyOf(cards.filter(c => c.is_land && c.type !== "Land" && c.type !== "Commander"));
        if (mdfc) {
          count.textContent = qtyOf(group.cards) + " + " + mdfc;
          count.title = qtyOf(group.cards) + " lands + " + mdfc + " MDFC with a land side";
        }
      }
      head.appendChild(count);
      stack.appendChild(head);
      const col = el("div", "stack-cards");
      group.cards.forEach((c, i) => col.appendChild(cardNode(c, i === group.cards.length - 1)));
      stack.appendChild(col);
      stacksEl.appendChild(stack);
    }
  }

  // ---- Shortlist column ----
  // The preview wants a mainboard-shaped card; a Shortlist row is close
  // enough with the missing fields filled in.
  function previewOf(item) {
    return {
      name: item.name, img: item.img, type_line: item.type_line, qty: item.qty,
      price: item.price, tags: [], flags: {},
    };
  }

  function shortlistCard(item) {
    const box = el("div", "sl-card sl-" + item.status + (pick && pick.change_id === item.change_id ? " sl-picking" : ""));
    if (item.img) {
      const img = el("img", "sl-img");
      img.src = item.img;
      img.alt = item.name;
      img.loading = "lazy";
      box.appendChild(img);
    }
    const body = el("div", "sl-body");
    body.appendChild(el("div", "sl-name", item.qty > 1 ? item.name + " ×" + item.qty : item.name));
    // Verdict badge: color carries have-it / elsewhere / buy; the text says why.
    const verdict = el("div", "sl-verdict sl-v-" + item.badge, item.verdict_text || "—");
    verdict.title = item.verdict_text;
    body.appendChild(verdict);
    if (item.status === "planned" && item.replaces) {
      body.appendChild(el("div", "sl-replaces", "→ replaces " + item.replaces));
    }
    // A saved choice shows where the copy will come from: a storage
    // Location, or another deck (a take, Phase 3).
    if ((item.source === "storage" || item.source === "deck") && item.source_location) {
      body.appendChild(el("div", "sl-route", "from " + item.source_location));
    } else if (item.source === "buy") {
      body.appendChild(el("div", "sl-route", "to buy"));
    }
    if (editable) {
      body.appendChild(shortlistActions(item));
      if (srcMenu && srcMenu.change_id === item.change_id) body.appendChild(sourceMenu(item, srcMenu.op));
    }
    box.appendChild(body);
    if (item.unlinked) box.title = "Name only — linked to a printing when applied";
    box.addEventListener("pointerenter", () => showPreview(previewOf(item)));
    return box;
  }

  // ---- where the copy comes from ----
  // Open only for one Shortlist row at a time: {change_id, op}. Choosing an
  // entry is the confirmation, so Apply's usual second click is skipped.
  let srcMenu = null;

  function hasSourceChoice(item) {
    // Buy (Phase 4) is always a choice, so the menu always opens.
    return item.can_buy !== false || (item.source_options || []).length > 0 || (item.deck_options || []).length > 0;
  }

  function toggleSourceMenu(item, op) {
    srcMenu = srcMenu && srcMenu.change_id === item.change_id && srcMenu.op === op
      ? null : { change_id: item.change_id, op };
    renderShortlist();
  }

  function sourceMenu(item, op) {
    const list = el("div", "sl-sources");
    list.appendChild(el("div", "menu-title", op === "apply" ? "Apply, taking it from…" : "Add, taking it from…"));
    const saved = item.source === "storage" ? item.source_location : null;
    list.appendChild(menuButton(item.auto_label || "Auto",
      () => commit({ op, change_id: item.change_id, source: "auto" }),
      saved || item.source === "buy" ? "" : "menu-primary"));
    for (const o of item.source_options || []) {
      list.appendChild(menuButton(o.location + " — " + o.quantity + " free",
        () => commit({ op, change_id: item.change_id, source: "storage", source_location: o.location }),
        saved === o.location ? "menu-primary" : ""));
    }
    // Another deck (Phase 3). Arm-then-confirm unless the deck is brewing.
    const savedDeck = item.source === "deck" ? item.source_location : null;
    for (const o of item.deck_options || []) {
      const b = menuButton(o.label, () => {
        if (copiesMove && !b.dataset.armed) {
          b.dataset.armed = "1";
          b.textContent = "Also takes it off " + o.deck + "'s list — click again";
          b.classList.add("menu-danger");
          return;
        }
        commit({ op, change_id: item.change_id, source: "deck", source_location: o.deck });
      }, savedDeck === o.deck ? "menu-primary" : "");
      list.appendChild(b);
    }
    // Put it on the list with no copy; the purchase is recorded later.
    if (item.can_buy !== false) {
      list.appendChild(menuButton("🛒 Buy — on the list now, copy when bought",
        () => commit({ op, change_id: item.change_id, source: "buy" }),
        item.source === "buy" ? "menu-primary" : ""));
    }
    return list;
  }

  // The "Leaving" rail group: this deck's cards another deck is taking. Drop
  // cancels the take (both halves) and keeps the card here.
  function leavingCard(item) {
    const box = el("div", "sl-card sl-leaving");
    const body = el("div", "sl-body");
    body.appendChild(el("div", "sl-name", item.qty > 1 ? item.name + " ×" + item.qty : item.name));
    const when = item.receiver_status === "applied" ? " (on its list)"
      : item.receiver_status === "idea" ? " (an idea there)" : "";
    body.appendChild(el("div", "sl-route", "→ " + item.to_deck + when));
    if (editable) {
      const row = el("div", "sl-actions");
      row.appendChild(menuButton("Keep here", () => commit({ op: "drop", change_id: item.change_id }), "menu-danger"));
      body.appendChild(row);
    }
    box.appendChild(body);
    box.title = item.to_deck + " is taking this card";
    return box;
  }

  function shortlistActions(item) {
    const row = el("div", "sl-actions");
    if (item.status === "idea") {
      const picking = pick && pick.change_id === item.change_id;
      row.appendChild(menuButton(picking ? "Cancel" : "Swap in for…", () => setPick(picking ? null : item),
        picking ? "menu-primary" : ""));
      // Straight into the deck with nothing cut (the other ways an idea
      // leaves the Shortlist are Swap in for… and Drop). With a choice of
      // where to take it from, the click opens that menu instead.
      row.appendChild(menuButton("Add", () => {
        if (hasSourceChoice(item)) toggleSourceMenu(item, "add");
        else commit({ op: "add", change_id: item.change_id });
      }));
    } else {
      row.appendChild(menuButton("Unpair", () => commit({ op: "unpair", change_id: item.change_id })));
      const apply = menuButton("Apply", () => {
        // With a choice of source, the menu it opens is the confirmation.
        if (hasSourceChoice(item)) { toggleSourceMenu(item, "apply"); return; }
        // Two clicks: apply moves collection lots and cannot be undone.
        if (!apply.dataset.armed) {
          apply.dataset.armed = "1";
          apply.textContent = "Confirm apply?";
          apply.classList.add("menu-danger");
          setTimeout(() => {
            if (apply.isConnected) {
              delete apply.dataset.armed;
              apply.textContent = "Apply";
              apply.classList.remove("menu-danger");
            }
          }, 4000);
          return;
        }
        commit({ op: "apply", change_id: item.change_id });
      }, "menu-primary");
      row.appendChild(apply);
    }
    // Both kinds can be dropped; no confirm, since the Undo button reopens it.
    row.appendChild(menuButton("Drop", () => commit({ op: "drop", change_id: item.change_id }), "menu-danger"));
    return row;
  }

  function renderShortlist() {
    shortlistEl.replaceChildren();
    for (const [status, label] of [["idea", "Ideas"], ["planned", "Planned"]]) {
      const items = shortlist.filter(i => i.status === status);
      if (!items.length) continue;
      const head = el("header", "stack-head");
      head.append(el("span", "stack-title", label), el("span", "stack-count", String(items.length)));
      shortlistEl.appendChild(head);
      for (const item of items) shortlistEl.appendChild(shortlistCard(item));
    }
    if (leaving.length) {
      const head = el("header", "stack-head");
      head.append(el("span", "stack-title", "Leaving"), el("span", "stack-count", String(leaving.length)));
      shortlistEl.appendChild(head);
      for (const item of leaving) shortlistEl.appendChild(leavingCard(item));
    }
  }

  renderPreviewHint();
  renderStacks();
  renderShortlist();

  const cleanup = () => {
    window.removeEventListener("keydown", onKey);
    root.remove();
    parentElement.__boardCleanup = null;
  };
  parentElement.__boardCleanup = cleanup;
  return cleanup;
}
