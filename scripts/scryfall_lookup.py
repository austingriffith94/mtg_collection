"""
Scryfall lookup helper — lazy fetch-and-cache.

Only fetches printings actually referenced by the collection, decklists,
or maybeboards (never bulk-downloads the full Scryfall catalog).

Two lookup paths:
  - get_by_set_number(set_code, collector_number): exact printing lookup,
    used for collection.csv rows where you already know the printing.
  - get_by_name(name): used for decks.csv / maybeboard.csv rows that
    don't have a pinned printing. Falls back exact -> fuzzy, and handles
    MDFC names ("A // B") by trying the front face if the full name misses.

Respects Scryfall's rate-limit guidance (~50-100ms between requests).
Requires network access and the `requests` package — run this on your
own machine, not in a sandboxed environment.
"""
import time
import sys
import requests

SCRYFALL_API = "https://api.scryfall.com"
RATE_LIMIT_SECONDS = 0.1  # be polite; Scryfall asks for 50-100ms between calls


class ScryfallClient:
    def __init__(self, session=None, verbose=True):
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "MTGCollectionDashboard/1.0 (personal use)",
            "Accept": "application/json",
        })
        self.verbose = verbose
        self._miss_log = []  # collects (query, reason) for anything unresolved

    def _get(self, url, params=None):
        try:
            resp = self.session.get(url, params=params, timeout=15)
        except requests.RequestException as e:
            self._miss_log.append((url, f"network error: {e}"))
            return None
        time.sleep(RATE_LIMIT_SECONDS)
        if resp.status_code == 404:
            return None
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            self._miss_log.append((url, f"http error: {e}"))
            return None
        return resp.json()

    def get_by_set_number(self, set_code, collector_number):
        """Exact printing lookup — the preferred path for owned collection cards."""
        url = f"{SCRYFALL_API}/cards/{set_code.strip().lower()}/{str(collector_number).strip()}"
        data = self._get(url)
        if data is None:
            self._miss_log.append((f"{set_code}/{collector_number}", "not found by set+number"))
        return data

    def get_by_name(self, name):
        """
        Name-based lookup for cards without a pinned printing (e.g. new
        maybeboard adds). Tries exact match, then MDFC front face, then fuzzy.
        Returns whatever printing Scryfall considers canonical/default —
        NOT necessarily the printing you own, so treat this as a placeholder
        until reconciled against your actual collection.
        """
        name = name.strip()
        front_face = name.split(" // ")[0].strip()

        data = self._get(f"{SCRYFALL_API}/cards/named", params={"exact": name})
        if data:
            return data

        if front_face != name:
            data = self._get(f"{SCRYFALL_API}/cards/named", params={"exact": front_face})
            if data:
                return data

        data = self._get(f"{SCRYFALL_API}/cards/named", params={"fuzzy": front_face})
        if data:
            return data

        self._miss_log.append((name, "not found by exact or fuzzy name"))
        return None

    def print_misses(self):
        if not self._miss_log:
            print("  (no Scryfall lookup misses)")
            return
        print(f"  {len(self._miss_log)} unresolved Scryfall lookups:")
        for query, reason in self._miss_log:
            print(f"    - {query}: {reason}")


def to_card_row(data):
    """Normalize a Scryfall card JSON object into a `cards` table row dict."""
    if not data:
        return None

    image_uri = None
    if data.get("image_uris"):
        image_uri = data["image_uris"].get("normal") or data["image_uris"].get("large")
    elif data.get("card_faces"):
        face0 = data["card_faces"][0]
        if face0.get("image_uris"):
            image_uri = face0["image_uris"].get("normal") or face0["image_uris"].get("large")

    oracle_text = data.get("oracle_text")
    if not oracle_text and data.get("card_faces"):
        oracle_text = " // ".join(
            f.get("oracle_text", "") for f in data["card_faces"] if f.get("oracle_text")
        )

    legalities = data.get("legalities", {})
    prices = data.get("prices", {}) or {}
    price = prices.get("usd") or prices.get("usd_foil") or prices.get("usd_etched")

    type_line = data.get("type_line", "") or ""

    return {
        "scryfall_id": data["id"],
        "oracle_id": data.get("oracle_id"),
        "name": data["name"],
        "set_code": data["set"],
        "collector_number": data["collector_number"],
        "type_line": type_line,
        "mana_cost": data.get("mana_cost"),
        "cmc": data.get("cmc"),
        "color_identity": ",".join(data.get("color_identity", [])),
        "oracle_text": oracle_text,
        "rarity": data.get("rarity"),
        "image_uri": image_uri,
        "is_basic_land": 1 if type_line.startswith("Basic Land") else 0,
        # Pulled live from Scryfall's own official field — reflects WotC's
        # Commander Bracket Game Changers list and updates automatically
        # as Scryfall's data changes, no manual seed list to maintain.
        "is_game_changer": 1 if data.get("game_changer") else 0,
        "commander_legal": 1 if legalities.get("commander") == "legal" else 0,
        "current_price_usd": float(price) if price else None,
        "price_updated_at": None,  # set by caller to today's date
        "last_fetched_at": None,   # set by caller to today's date
    }


if __name__ == "__main__":
    # quick manual smoke test: python scryfall_lookup.py "Sol Ring"
    if len(sys.argv) > 1:
        client = ScryfallClient()
        result = client.get_by_name(" ".join(sys.argv[1:]))
        print(to_card_row(result))
        client.print_misses()
    else:
        print("Usage: python scryfall_lookup.py <card name>")
