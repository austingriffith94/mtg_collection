// Stacks deck board (VISUAL_DECK_PLAN.md, Phase A read-only view + Phase B edits).
//
// CCv2 module: `export default function(component)`. Everything is drawn
// under `parentElement` (a shadow root — never `document`, so two boards
// can't collide) and the function returns a cleanup. Only CCv2 APIs here:
// no Streamlit.setComponentValue / postMessage / streamlit-component-lib.
//
// data (from deck_board.render_deck_board):
//   cards         [{id,name,qty,cmc,mana_cost,type_line,img,type,is_land,
//                   colors,tags,price,flags:{game_changer,reserved,
//                   not_owned,commander}}]  — already sorted commander,
//                   then mana value, then name
//   group_by      saved choice, one of group_options
//   group_options ["Type","Mana value","Strategy tag","Color"]
//   type_order    stack order for "Type"
//   editable      true on the Deck Editor's Board tab only
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
  const preview = el("aside", "preview");
  layout.append(stacksEl, preview);
  root.appendChild(layout);

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

  function openMenu(card, node) {
    menuCard = card;
    menu.replaceChildren(
      el("div", "menu-title", card.name),
      menuButton("+1", () => send({ op: "inc", id: card.id })),
      menuButton(card.qty > 1 ? "−1" : "−1 (removes it)", () => send({ op: "dec", id: card.id })),
      menuButton("Remove", () => send({ op: "remove", id: card.id }), "menu-danger"),
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

  // Esc and a click anywhere else close it. Listeners go on `root` (inside
  // the shadow root) plus window for Esc, and are removed in the cleanup.
  const onKey = ev => { if (ev.key === "Escape") closeMenu(); };
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
    if (badges.childElementCount) node.appendChild(badges);

    // pointerenter covers mouse; click covers touch (no hover there).
    node.addEventListener("pointerenter", () => showPreview(card));
    node.addEventListener("click", ev => {
      showPreview(card);
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
      head.appendChild(el("span", "stack-count", String(qtyOf(group.cards))));
      stack.appendChild(head);
      const col = el("div", "stack-cards");
      group.cards.forEach((c, i) => col.appendChild(cardNode(c, i === group.cards.length - 1)));
      stack.appendChild(col);
      stacksEl.appendChild(stack);
    }
  }

  renderPreviewHint();
  renderStacks();

  const cleanup = () => {
    window.removeEventListener("keydown", onKey);
    root.remove();
    parentElement.__boardCleanup = null;
  };
  parentElement.__boardCleanup = cleanup;
  return cleanup;
}
