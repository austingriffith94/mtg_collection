"""
Local image cache — downloads Scryfall card images to disk once, so the
dashboard never has to re-fetch them over the network on every load, and
prunes cached files for cards no longer referenced anywhere (removed
from your collection, every decklist, and every maybeboard).

Run standalone, or via the launcher (run.py) menu:
    python sync_images.py            # download missing + prune orphans
    python sync_images.py --no-prune # download only, skip pruning
    python sync_images.py --prune-only
    python sync_images.py --dry-run  # list what pruning would remove, change nothing

Images are cached as static/image_cache/<scryfall_id>.<ext>, one file per
PRINTING (not per oracle card) — matches how `cards` is keyed, so the
exact art you own is what gets cached. They live under static/ so the
dashboard can serve them as plain URLs (Phase 4 of UI_ACTION_PLAN.md);
cards.local_image_path stores 'image_cache/<file>', relative to static/.
A cache still in the old root-level image_cache/ is moved over first
(see move_image_cache.py).
"""
import os
import sys
import argparse
import requests

sys.path.insert(0, os.path.dirname(__file__))
import sqlite3

from move_image_cache import move_legacy_image_cache

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "mtg_collection.db")
IMAGE_CACHE_DIR = os.path.join(BASE_DIR, "static", "image_cache")


def guess_extension(url):
    # Scryfall image URIs end in .jpg or .png before any query string
    path = url.split("?")[0]
    ext = os.path.splitext(path)[1]
    return ext if ext in (".jpg", ".jpeg", ".png") else ".jpg"


def download_missing(conn, session, force=False, verbose=True):
    os.makedirs(IMAGE_CACHE_DIR, exist_ok=True)

    if force:
        rows = conn.execute(
            "SELECT scryfall_id, image_uri FROM cards WHERE image_uri IS NOT NULL"
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT scryfall_id, image_uri FROM cards
               WHERE image_uri IS NOT NULL
               AND (local_image_path IS NULL OR local_image_path = '')"""
        ).fetchall()

    downloaded, skipped, failed = 0, 0, []

    for scryfall_id, image_uri in rows:
        ext = guess_extension(image_uri)
        filename = f"{scryfall_id}{ext}"
        filepath = os.path.join(IMAGE_CACHE_DIR, filename)
        # Forward slashes on every OS: the dashboard turns this into a URL.
        rel_path = f"image_cache/{filename}"

        if os.path.exists(filepath) and not force:
            # file already on disk (e.g. from a prior run) — just record it
            conn.execute(
                "UPDATE cards SET local_image_path = ? WHERE scryfall_id = ?",
                (rel_path, scryfall_id),
            )
            skipped += 1
            continue

        try:
            resp = session.get(image_uri, timeout=15)
            resp.raise_for_status()
            with open(filepath, "wb") as f:
                f.write(resp.content)
            conn.execute(
                "UPDATE cards SET local_image_path = ? WHERE scryfall_id = ?",
                (rel_path, scryfall_id),
            )
            downloaded += 1
        except requests.RequestException as e:
            failed.append((scryfall_id, str(e)))

    conn.commit()
    if verbose:
        print(f"  Downloaded: {downloaded}")
        print(f"  Already cached (linked): {skipped}")
        if failed:
            print(f"  Failed: {len(failed)}")
            for sid, err in failed[:10]:
                print(f"    - {sid}: {err}")
    return downloaded, skipped, failed


def prune_orphaned(conn, verbose=True, dry_run=False):
    """Delete cached image files for printings no longer referenced by
    your collection, any decklist, any open shortlist row, or a Game
    Changer / Mana Tag list entry (those lists keep their art) — but leave
    the `cards` metadata row alone (cheap to keep, just clears
    local_image_path so a future re-reference would re-download rather than
    serve a stale dangling path).

    Returns the [(scryfall_id, card_name)] it removed (or, with dry_run=True,
    would remove — nothing is deleted), and prints each one."""
    active_ids = set(
        sid for (sid,) in conn.execute(
            """SELECT scryfall_id FROM collection
               UNION SELECT scryfall_id FROM deck_cards
               UNION SELECT add_scryfall_id FROM deck_changes
                     WHERE status IN ('idea', 'planned')
                       AND add_scryfall_id IS NOT NULL
               UNION SELECT scryfall_id FROM cards
                     WHERE name IN (SELECT card_name FROM game_changer_tags
                                    UNION SELECT card_name FROM mana_tags)"""
        )
    )

    if not os.path.exists(IMAGE_CACHE_DIR):
        if verbose:
            print("  No static/image_cache/ directory yet — nothing to prune.")
        return []

    removed = []
    for filename in sorted(os.listdir(IMAGE_CACHE_DIR)):
        # Skip folders — deck_covers/ (user uploads) lives in here too.
        if not os.path.isfile(os.path.join(IMAGE_CACHE_DIR, filename)):
            continue
        scryfall_id = os.path.splitext(filename)[0]
        if scryfall_id not in active_ids:
            row = conn.execute("SELECT name FROM cards WHERE scryfall_id = ?", (scryfall_id,)).fetchone()
            removed.append((scryfall_id, row[0] if row else filename))
            if not dry_run:
                os.remove(os.path.join(IMAGE_CACHE_DIR, filename))
                conn.execute(
                    "UPDATE cards SET local_image_path = NULL WHERE scryfall_id = ?",
                    (scryfall_id,),
                )

    conn.commit()
    if verbose:
        verb = "Would prune" if dry_run else "Pruned"
        print(f"  {verb} {len(removed)} orphaned image(s) "
              f"(no longer in collection, any decklist, any open shortlist row, or a tag list).")
        for _sid, name in removed:
            print(f"    - {name}")
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-prune", action="store_true", help="Download only, skip pruning")
    parser.add_argument("--prune-only", action="store_true", help="Prune only, skip downloading")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be pruned without downloading or deleting anything")
    parser.add_argument("--force", action="store_true", help="Re-download everything, even if already cached")
    args = parser.parse_args()

    if not os.path.exists(DB_PATH):
        print("No database found — run migrate.py first.")
        sys.exit(1)

    moved, _kept = move_legacy_image_cache()
    if moved:
        print(f"Moved {moved} cached file(s) from image_cache/ to static/image_cache/.")

    conn = sqlite3.connect(DB_PATH)
    session = requests.Session()
    session.headers.update({"User-Agent": "MTGCollectionDashboard/1.0 (personal use)"})

    if args.dry_run:
        print("Dry run — nothing will be downloaded or deleted.")
    if not args.prune_only and not args.dry_run:
        print("Downloading missing images...")
        download_missing(conn, session, force=args.force)

    if not args.no_prune:
        print("Pruning orphaned images...")
        prune_orphaned(conn, dry_run=args.dry_run)

    conn.close()


if __name__ == "__main__":
    main()
