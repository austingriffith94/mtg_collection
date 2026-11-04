// Goldfish: play a deck's first turns alone by clicking (docs/GOLDFISH_PLAN.md,
// Phase 2).
//
// CCv2 module: `export default function(component)`. Everything is drawn
// under `parentElement` (a shadow root, never `document`) and the function
// returns a cleanup. No callbacks: Python sends the deck once and never hears
// back, so nothing here calls setStateValue / setTriggerValue.
//
// data (from goldfish.render_goldfish, built by goldfish.goldfish_payload):
//   commanders, library  [{id,name,qty,cmc,img,is_commander,tags,layers,
//                          faces:[{name,type_line,kind,cost,mana,enters_tapped}]}]
//                        kind is land / creature / permanent / spell; mana is
//                        [{colors,any,amount|null}] or null. Each card is sent
//                        once with a qty; the copies are expanded here as
//                        "<id>#1" … "<id>#n", since each copy taps separately.
//
// The file has two parts. The RULES part (state, shuffle, draw, mulligan,
// play, tap, move, next turn, mana count) has no DOM code, so a headless
// check can call it directly. The DRAWING part reads the state and renders
// it; every click goes through act(), which saves an undo snapshot first.
//
// The game survives reruns by living in a store on parentElement, not in
// this function's closure: the function runs again on every data update with
// the same parentElement, tears down the previous run's DOM, and redraws
// from the store. The page keys the component per deck, so changing deck
// mounts a fresh component with a fresh game. A store whose deck signature
// doesn't match the data (the deck was edited elsewhere) is replaced too.
//
// Card names and texts are set with textContent, never innerHTML.

// ======================================================================
// RULES (no DOM)
// ======================================================================

const OPENING_HAND = 7;
const BATTLEFIELD = ["lands", "permanents"];
const ZONES = ["library", "hand", "lands", "permanents", "graveyard", "exile", "command"];

function shuffle(list, random = Math.random) {
  // Fisher–Yates, in place. No seed, on purpose (see the plan's Decisions).
  for (let i = list.length - 1; i > 0; i--) {
    const j = Math.floor(random() * (i + 1));
    [list[i], list[j]] = [list[j], list[i]];
  }
  return list;
}

function expandDeck(data) {
  // {defs: {copyId: card}, commanders: [copyId], library: [copyId], signature}
  const defs = {};
  const expand = cards => {
    const ids = [];
    for (const card of cards || []) {
      for (let k = 1; k <= Math.max(1, card.qty || 1); k++) {
        const id = card.id + "#" + k;
        defs[id] = card;
        ids.push(id);
      }
    }
    return ids;
  };
  const commanders = expand(data.commanders);
  const library = expand(data.library);
  return { defs, commanders, library, signature: commanders.concat(library).join("|") };
}

function newGame(deck, opts = {}) {
  // A game in its setup phase: nothing drawn until start() picks play/draw.
  return {
    phase: "setup",            // setup -> mulligan -> bottom -> play
    onPlay: opts.onPlay !== false,
    freeMulligan: opts.freeMulligan !== false,
    mulligans: 0,
    toBottom: 0,               // cards still to put on the bottom after Keep
    turn: 0,
    landsPlayed: 0,            // land drops used this turn
    library: shuffle(deck.library.slice()),   // index 0 is the top
    hand: [], lands: [], permanents: [], graveyard: [], exile: [],
    command: deck.commanders.slice(),
    marks: {},                 // copyId -> {tapped, sick, face}; battlefield only
    casts: {},                 // commander copyId -> times cast
    log: [],                   // [{turn, entries: [text]}]
  };
}

function faceIndex(state, id) {
  const mark = state.marks[id];
  return mark && mark.face != null ? mark.face : 0;
}

function faceOf(state, deck, id) {
  const faces = deck.defs[id].faces || [];
  return faces[faceIndex(state, id)] || faces[0] || { name: deck.defs[id].name, kind: "permanent" };
}

function zoneOf(state, id) {
  return ZONES.find(z => state[z].includes(id)) || null;
}

function removeFromZone(state, id) {
  const zone = zoneOf(state, id);
  if (zone) state[zone].splice(state[zone].indexOf(id), 1);
  return zone;
}

function logEntry(state, text) {
  const last = state.log[state.log.length - 1];
  if (last && last.turn === state.turn) last.entries.push(text);
  else state.log.push({ turn: state.turn, entries: [text] });
}

function drawCards(state, n = 1) {
  // Draws up to n. An empty library logs it and draws nothing; the game goes on.
  let drawn = 0;
  while (drawn < n && state.library.length) {
    state.hand.push(state.library.shift());
    drawn++;
  }
  if (drawn < n) {
    logEntry(state, "Library is empty");
    return { drawn, toast: "Library is empty." };
  }
  return { drawn };
}

function start(state, onPlay) {
  // Pick play/draw and draw the opening hand.
  if (state.phase !== "setup") return { error: "The game has already started." };
  state.onPlay = !!onPlay;
  state.phase = "mulligan";
  logEntry(state, state.onPlay ? "On the play" : "On the draw");
  return drawCards(state, OPENING_HAND);
}

function mulliganBottomCount(state) {
  // London mulligan: one card to the bottom per mulligan, minus Commander's
  // free first one when that's switched on.
  const free = state.freeMulligan && state.mulligans > 0 ? 1 : 0;
  return Math.max(0, state.mulligans - free);
}

function mulligan(state) {
  if (state.phase !== "mulligan") return { error: "Mulligans happen before you keep." };
  state.library.push(...state.hand.splice(0));
  shuffle(state.library);
  state.mulligans++;
  logEntry(state, "Mulligan " + state.mulligans);
  return drawCards(state, OPENING_HAND);
}

function beginPlay(state) {
  state.phase = "play";
  state.turn = 1;
  state.landsPlayed = 0;
  // No draw on turn 1 on the play.
  return state.onPlay ? {} : drawCards(state, 1);
}

function keep(state) {
  if (state.phase !== "mulligan") return { error: "There's no hand to keep." };
  state.toBottom = Math.min(mulliganBottomCount(state), state.hand.length);
  logEntry(state, "Kept " + (state.hand.length - state.toBottom));
  if (state.toBottom > 0) {
    state.phase = "bottom";
    return {};
  }
  return beginPlay(state);
}

function bottomCard(state, id) {
  if (state.phase !== "bottom" || !state.hand.includes(id)) return { error: "Not a card to put on the bottom." };
  removeFromZone(state, id);
  state.library.push(id);
  state.toBottom--;
  return state.toBottom > 0 ? {} : beginPlay(state);
}

function putOntoBattlefield(state, deck, id, face) {
  // Where a face goes when it's played or cast, with its marks.
  const card = deck.defs[id];
  const f = card.faces[face] || card.faces[0];
  const chosen = card.faces.length > 1 ? face : null;
  if (f.kind === "spell") {
    state.graveyard.push(id);
    return f;
  }
  if (f.kind === "land") {
    state.lands.push(id);
    state.marks[id] = { tapped: f.enters_tapped === "always", sick: false, face: chosen };
  } else {
    state.permanents.push(id);
    state.marks[id] = { tapped: false, sick: f.kind === "creature", face: chosen };
  }
  return f;
}

function playFromHand(state, deck, id, face = 0) {
  // A land uses the land drop (a second one warns but is allowed); a
  // permanent goes to the battlefield; an instant or sorcery to the graveyard.
  if (state.phase !== "play" || !state.hand.includes(id)) return { error: "That card isn't in your hand." };
  removeFromZone(state, id);
  const f = putOntoBattlefield(state, deck, id, face);
  logEntry(state, f.name);
  if (f.kind === "land") {
    state.landsPlayed++;
    if (state.landsPlayed > 1) {
      return { toast: state.landsPlayed + " lands played this turn (normally 1)." };
    }
  }
  return {};
}

function commanderTax(state, id) {
  // The tax on the NEXT cast: 2 for each earlier cast.
  return 2 * (state.casts[id] || 0);
}

function castCommander(state, deck, id, face = 0) {
  if (state.phase !== "play" || !state.command.includes(id)) return { error: "That card isn't in the command zone." };
  const tax = commanderTax(state, id);
  removeFromZone(state, id);
  const f = putOntoBattlefield(state, deck, id, face);
  state.casts[id] = (state.casts[id] || 0) + 1;
  logEntry(state, f.name + (tax ? " (tax " + tax + ")" : ""));
  return {};
}

function toggleTap(state, id) {
  const zone = zoneOf(state, id);
  if (!BATTLEFIELD.includes(zone)) return { error: "Only cards on the battlefield tap." };
  const mark = state.marks[id] || (state.marks[id] = { tapped: false, sick: false, face: null });
  mark.tapped = !mark.tapped;
  return {};
}

const DESTINATIONS = {
  hand: "Hand", graveyard: "Graveyard", exile: "Exile",
  top: "Top of library", bottom: "Bottom of library", command: "Command zone",
};

function moveCard(state, deck, id, dest) {
  // The ⋯ menu. A card leaving the battlefield loses its tapped and
  // summoning-sick marks and its face choice.
  if (!(dest in DESTINATIONS)) return { error: "Unknown destination." };
  if (dest === "command" && !deck.defs[id].is_commander) return { error: "Only a commander goes to the command zone." };
  const name = faceOf(state, deck, id).name;
  const from = removeFromZone(state, id);
  if (!from) return { error: "That card isn't in the game." };
  delete state.marks[id];
  if (dest === "top") state.library.unshift(id);
  else if (dest === "bottom") state.library.push(id);
  else state[dest].push(id);
  if (state.phase === "play") logEntry(state, name + " → " + DESTINATIONS[dest].toLowerCase());
  return {};
}

function nextTurn(state) {
  // Untap, clear summoning sickness, reset the land drop, draw.
  if (state.phase !== "play") return { error: "Keep a hand first." };
  state.turn++;
  state.landsPlayed = 0;
  for (const id of Object.keys(state.marks)) {
    state.marks[id].tapped = false;
    state.marks[id].sick = false;
  }
  return drawCards(state, 1);
}

function manaAvailable(state, deck) {
  // {amount, unknown}: untapped, non-sick sources. A source with several
  // abilities (Talisman) counts its best readable one; one whose only
  // abilities can't be read (Cabal Coffers) is an unknown, the "?".
  let amount = 0;
  let unknown = 0;
  for (const id of state.lands.concat(state.permanents)) {
    const mark = state.marks[id] || {};
    if (mark.tapped || mark.sick) continue;
    const abilities = faceOf(state, deck, id).mana;
    if (!abilities || !abilities.length) continue;
    const known = abilities.map(a => a.amount).filter(a => a != null);
    if (known.length) amount += Math.max(...known);
    else unknown++;
  }
  return { amount, unknown };
}

function counts(state) {
  const out = {};
  for (const z of ZONES) out[z] = state[z].length;
  return out;
}

const RULES = {
  shuffle, expandDeck, newGame, start, mulligan, mulliganBottomCount, keep, bottomCard,
  playFromHand, castCommander, commanderTax, toggleTap, moveCard, nextTurn, drawCards,
  manaAvailable, faceOf, zoneOf, counts,
};

// ======================================================================
// STORE: the game, its undo stack and the UI bits that survive a rerun
// ======================================================================

function makeStore(deck) {
  const store = {
    deck,
    state: newGame(deck),
    history: [],               // JSON snapshots, newest last
    ui: { pile: null },        // which pile list is open (graveyard / exile)
    rules: RULES,
    images: new Map(),         // copyId -> <img>, reused so redraws don't reload art
    render: () => {},
    toast: () => {},
  };
  // Every action goes through act(): snapshot, run, redraw. A refused action
  // (one that returns {error}) leaves no snapshot behind.
  store.act = (name, ...args) => {
    const before = JSON.stringify(store.state);
    const result = RULES[name](store.state, ...args) || {};
    if (result.error) {
      store.toast(result.error);
    } else {
      store.history.push(before);
      if (result.toast) store.toast(result.toast);
    }
    store.render();
    return result;
  };
  store.undo = () => {
    if (!store.history.length) return false;
    store.state = JSON.parse(store.history.pop());
    store.render();
    return true;
  };
  // "New hand" keeps the play/draw choice and draws 7; "Reset" goes back to
  // setup. Both clear the undo stack.
  store.newHand = () => {
    const { onPlay, freeMulligan } = store.state;
    store.state = newGame(deck, { onPlay, freeMulligan });
    store.history = [];
    start(store.state, onPlay);
    store.render();
  };
  store.reset = () => {
    store.state = newGame(deck, { freeMulligan: store.state.freeMulligan });
    store.history = [];
    store.render();
  };
  return store;
}

// ======================================================================
// DRAWING
// ======================================================================

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function button(label, onClick, className = "gbtn") {
  const b = el("button", className, label);
  b.type = "button";
  b.onclick = onClick;
  return b;
}

const KIND_LABEL = { land: "land", creature: "creature", permanent: "permanent", spell: "spell" };

export default function (component) {
  const { data, parentElement } = component;
  if (parentElement.__goldfishCleanup) parentElement.__goldfishCleanup();

  const deck = expandDeck(data || {});
  let store = parentElement.__goldfish;
  if (!store || store.deck.signature !== deck.signature) {
    store = makeStore(deck);
    parentElement.__goldfish = store;
  }

  const root = el("div", "goldfish");
  parentElement.appendChild(root);

  if (!deck.signature) {
    root.appendChild(el("div", "empty", "No mainboard cards to play."));
    const cleanupEmpty = () => { root.remove(); parentElement.__goldfishCleanup = null; };
    parentElement.__goldfishCleanup = cleanupEmpty;
    return cleanupEmpty;
  }

  const bar = el("div", "bar");
  const status = el("div", "status");
  const banner = el("div", "banner");
  const layout = el("div", "layout");
  const table = el("div", "table");
  const rail = el("aside", "rail");
  const preview = el("div", "preview");
  const menu = el("div", "menu");
  const toastEl = el("div", "toast");
  menu.hidden = true;
  toastEl.hidden = true;
  rail.appendChild(preview);
  layout.append(table, rail);
  root.append(bar, status, banner, layout, menu, toastEl);

  // ---- toast ----
  let toastTimer = null;
  store.toast = text => {
    toastEl.textContent = text;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.hidden = true; }, 2600);
  };

  // ---- preview ----
  function showPreview(id) {
    const card = deck.defs[id];
    const state = store.state;
    preview.replaceChildren();
    if (card.img) {
      const img = el("img", "preview-img");
      img.src = card.img;
      img.alt = card.name;
      preview.appendChild(img);
    } else {
      preview.appendChild(el("div", "preview-noimg", card.name));
    }
    const chosen = state.marks[id] && state.marks[id].face;
    card.faces.forEach((f, i) => {
      const row = el("div", "preview-face" + (chosen === i ? " chosen" : ""));
      row.append(el("div", "preview-name", f.name), el("div", "preview-type", f.type_line || ""));
      preview.appendChild(row);
    });
    const facts = ["Mana value " + card.cmc];
    if (card.tags && card.tags.length) facts.push(card.tags.join(", "));
    preview.appendChild(el("div", "preview-facts", facts.join(" · ")));
  }
  function previewHint() {
    preview.replaceChildren(el("div", "preview-hint", "Hover a card to see it in full."));
  }

  // ---- menus (⋯ and the two-face chooser) ----
  function closeMenu() {
    menu.hidden = true;
    menu.replaceChildren();
  }
  function openMenu(anchor, items) {
    // items: [{label, run}]
    menu.replaceChildren();
    for (const item of items) {
      menu.appendChild(button(item.label, () => { closeMenu(); item.run(); }, "menu-item"));
    }
    menu.appendChild(button("Cancel", closeMenu, "menu-item menu-cancel"));
    menu.hidden = false;
    const a = anchor.getBoundingClientRect();
    const r = root.getBoundingClientRect();
    const left = Math.max(0, Math.min(a.left - r.left, r.width - menu.offsetWidth));
    menu.style.left = left + "px";
    menu.style.top = (a.top - r.top + 24) + "px";
  }
  // A click outside an open menu only cancels it: the click that follows the
  // pointerdown is swallowed, so it can't also play or tap another card.
  let swallowClick = false;
  const onKey = e => { if (e.key === "Escape") closeMenu(); };
  const onPointer = e => {
    swallowClick = !menu.hidden && !e.composedPath().includes(menu);
    if (swallowClick) closeMenu();
  };
  const onClickCapture = e => {
    if (!swallowClick) return;
    swallowClick = false;
    e.stopPropagation();
    e.preventDefault();
  };
  window.addEventListener("keydown", onKey);
  window.addEventListener("pointerdown", onPointer, true);
  root.addEventListener("click", onClickCapture, true);

  function faceChooser(anchor, id, run) {
    // Single-face cards skip it. Escape or a click elsewhere cancels and
    // leaves the card where it was.
    const faces = deck.defs[id].faces;
    if (faces.length < 2) { run(0); return; }
    openMenu(anchor, faces.map((f, i) => ({
      label: (f.kind === "land" ? "Play " : "Cast ") + f.name + " — " + (KIND_LABEL[f.kind] || f.kind),
      run: () => run(i),
    })));
  }

  function moreMenu(anchor, id, zone) {
    const here = zone === "library" ? null : zone;
    const dests = ["hand", "graveyard", "exile", "top", "bottom"].filter(d => d !== here);
    if (deck.defs[id].is_commander && zone !== "command") dests.push("command");
    openMenu(anchor, dests.map(d => ({ label: DESTINATIONS[d], run: () => store.act("moveCard", deck, id, d) })));
  }

  // ---- card tiles ----
  function imageFor(id) {
    let img = store.images.get(id);
    if (!img) {
      const card = deck.defs[id];
      img = el("img", "gcard-img");
      img.src = card.img;
      img.alt = card.name;
      img.loading = "lazy";
      img.draggable = false;
      store.images.set(id, img);
    }
    return img;
  }

  function tile(id, zone) {
    const state = store.state;
    const card = deck.defs[id];
    const mark = state.marks[id] || {};
    const face = faceOf(state, deck, id);
    const node = el("div", "gcard");
    node.dataset.id = id;
    node.dataset.zone = zone;
    if (mark.tapped) node.classList.add("tapped");
    if (card.img) node.appendChild(imageFor(id));
    else node.appendChild(el("div", "gcard-noimg", card.name));
    // A back face on the battlefield shows its own name over the front art.
    if (mark.face) node.appendChild(el("div", "gcard-face", face.name));

    const badges = el("div", "badges");
    if (mark.tapped) badges.appendChild(el("span", "badge b-tap", "⤵"));
    if (mark.sick) badges.appendChild(el("span", "badge b-sick", "sick"));
    if (face.kind === "land" && face.enters_tapped === "maybe" && zone !== "lands") {
      badges.appendChild(el("span", "badge b-maybe", "tapped?"));
    }
    if (zone === "command") badges.appendChild(el("span", "badge", "tax " + commanderTax(state, id)));
    if (badges.childNodes.length) node.appendChild(badges);

    if (state.phase === "play" && zone !== "library") {
      const more = button("⋯", e => { e.stopPropagation(); moreMenu(more, id, zone); }, "more");
      more.title = "Move this card";
      node.appendChild(more);
    }

    node.addEventListener("pointerenter", () => showPreview(id));
    node.onclick = () => onCardClick(node, id, zone);
    return node;
  }

  function onCardClick(node, id, zone) {
    const state = store.state;
    if (zone === "hand" && state.phase === "bottom") {
      store.act("bottomCard", id);
    } else if (state.phase !== "play") {
      return;
    } else if (zone === "hand") {
      faceChooser(node, id, face => store.act("playFromHand", deck, id, face));
    } else if (zone === "command") {
      faceChooser(node, id, face => store.act("castCommander", deck, id, face));
    } else if (BATTLEFIELD.includes(zone)) {
      store.act("toggleTap", id);
    }
  }

  function zoneRow(title, zone, extraClass = "") {
    const section = el("section", "zone zone-" + zone + " " + extraClass);
    const head = el("header", "zone-head");
    head.append(el("span", "zone-title", title), el("span", "zone-count", String(store.state[zone].length)));
    const cards = el("div", "zone-cards");
    for (const id of store.state[zone]) cards.appendChild(tile(id, zone));
    if (!store.state[zone].length) cards.appendChild(el("div", "zone-empty", "—"));
    section.append(head, cards);
    return section;
  }

  // ---- the bar, status line and phase banner ----
  function renderBar() {
    const state = store.state;
    bar.replaceChildren();
    const parts = [];
    if (state.phase === "play") {
      parts.push("Turn " + state.turn);
      parts.push(state.onPlay ? "On the play" : "On the draw");
      parts.push("Land drop: " + (state.landsPlayed === 0 ? "available"
        : state.landsPlayed === 1 ? "✓ used" : "⚠ " + state.landsPlayed + " played"));
    } else {
      parts.push(state.phase === "setup" ? "New game" : "Opening hand");
    }
    bar.appendChild(el("span", "bar-title", parts.join(" · ")));
    const actions = el("span", "bar-actions");
    const next = button("Next turn ▶", () => store.act("nextTurn"), "gbtn primary");
    next.disabled = state.phase !== "play";
    const undo = button("Undo", () => store.undo());
    undo.disabled = !store.history.length;
    const newHand = button("New hand", () => store.newHand());
    newHand.disabled = state.phase === "setup";
    actions.append(next, undo, newHand, button("Reset", () => store.reset()));
    bar.appendChild(actions);
  }

  function renderStatus() {
    const state = store.state;
    status.replaceChildren();
    const mana = manaAvailable(state, deck);
    const manaText = mana.unknown ? mana.amount + " + ?" : String(mana.amount);
    status.appendChild(el("span", "mana", "Mana available: " + manaText));
    const piles = el("span", "piles");
    piles.appendChild(el("span", "pile-count", "Library " + state.library.length));
    for (const [zone, label] of [["graveyard", "Graveyard"], ["exile", "Exile"]]) {
      const b = button(label + " " + state[zone].length, () => {
        store.ui.pile = store.ui.pile === zone ? null : zone;
        store.render();
      }, "pile-btn" + (store.ui.pile === zone ? " active" : ""));
      piles.appendChild(b);
    }
    status.appendChild(piles);
  }

  function renderBanner() {
    const state = store.state;
    banner.replaceChildren();
    banner.hidden = state.phase === "play";
    if (state.phase === "play") return;
    if (state.phase === "setup" || state.phase === "mulligan") {
      const label = el("label", "free-mull");
      const box = el("input");
      box.type = "checkbox";
      box.checked = state.freeMulligan;
      box.disabled = state.phase === "mulligan" && state.mulligans > 0;
      box.onchange = () => { state.freeMulligan = box.checked; store.render(); };
      label.append(box, document.createTextNode(" Free first mulligan"));
      if (state.phase === "setup") {
        banner.append(
          el("span", "banner-text", "Choose who goes first, then draw 7."),
          button("On the play", () => store.act("start", true), "gbtn primary"),
          button("On the draw", () => store.act("start", false), "gbtn primary"),
          label,
        );
      } else {
        const after = { ...state, mulligans: state.mulligans + 1 };
        const nextBottom = mulliganBottomCount(after);
        const bottom = mulliganBottomCount(state);
        banner.append(
          el("span", "banner-text", state.mulligans ? "Mulligans: " + state.mulligans + "." : "Opening hand."),
          button(bottom ? "Keep (put " + bottom + " on the bottom)" : "Keep", () => store.act("keep"), "gbtn primary"),
          button("Mulligan (keep " + Math.max(0, OPENING_HAND - nextBottom) + ")", () => store.act("mulligan")),
          label,
        );
      }
    } else if (state.phase === "bottom") {
      banner.append(el("span", "banner-text",
        "Click " + state.toBottom + " card" + (state.toBottom === 1 ? "" : "s") + " in your hand to put on the bottom."));
    }
  }

  function renderTable() {
    const state = store.state;
    table.replaceChildren();
    const top = el("div", "board-top");
    const left = el("div", "board-left");
    if (deck.commanders.length) left.appendChild(zoneRow("Command", "command"));
    const right = el("div", "board-right");
    right.append(zoneRow("Lands", "lands"), zoneRow("Permanents", "permanents"));
    top.append(left, right);
    table.appendChild(top);
    table.appendChild(zoneRow("Hand", "hand", state.phase === "bottom" ? "picking" : ""));
    if (store.ui.pile) table.appendChild(zoneRow(DESTINATIONS[store.ui.pile], store.ui.pile));

    const log = el("section", "log");
    log.appendChild(el("header", "zone-head", "Log"));
    for (const line of state.log) {
      log.appendChild(el("div", "log-line", (line.turn ? "T" + line.turn : "Setup") + ": " + line.entries.join(", ")));
    }
    if (!state.log.length) log.appendChild(el("div", "zone-empty", "Nothing yet."));
    table.appendChild(log);
  }

  store.render = () => {
    closeMenu();
    renderBar();
    renderStatus();
    renderBanner();
    renderTable();
  };

  previewHint();
  store.render();

  const cleanup = () => {
    window.removeEventListener("keydown", onKey);
    window.removeEventListener("pointerdown", onPointer, true);
    clearTimeout(toastTimer);
    store.render = () => {};
    store.toast = () => {};
    root.remove();
    parentElement.__goldfishCleanup = null;
  };
  parentElement.__goldfishCleanup = cleanup;
  return cleanup;
}
