"""
Local image cache — downloads Scryfall card images to disk once, so the
dashboard never has to re-fetch them over the network on every load, and
prunes cached files for cards no longer referenced anywhere (removed
from your collection, every decklist, and every maybeboard).

Run standalone, or via the launcher (run.py) menu:
    python sync_images.py            # download missing + prune orphans
    python sync_images.py --no-prune # download only, skip pruning
    python sync_images.py --prune-only

Images are cached as image_cache/<scryfall_id>.<ext>, one file per
PRINTING (not per oracle card) — matches how `cards` is keyed, so the
exact art you own is what gets cached.
"""
import os
import sys
import argparse
import requests

sys.path.insert(0, os.path.dirname(__file__))
import sqlite3

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "mtg_collection.db")
IMAGE_CACHE_DIR = os.path.join(BASE_DIR, "image_cache")


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
        rel_path = os.path.join("image_cache", filename)

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


def prune_orphaned(conn, verbose=True):
    """Delete cached image files for printings no longer referenced by
    your collection, any decklist, or any maybeboard — but leave the
    `cards` metadata row alone (cheap to keep, just clears local_image_path
    so a future re-reference would re-download rather than serve a stale
    dangling path)."""
    active_ids = set(
        sid for (sid,) in conn.execute(
            """SELECT scryfall_id FROM collection
               UNION SELECT scryfall_id FROM deck_cards
               UNION SELECT scryfall_id FROM maybeboard"""
        )
    )

    if not os.path.exists(IMAGE_CACHE_DIR):
        if verbose:
            print("  No image_cache/ directory yet — nothing to prune.")
        return 0

    removed = 0
    for filename in os.listdir(IMAGE_CACHE_DIR):
        scryfall_id = os.path.splitext(filename)[0]
        if scryfall_id not in active_ids:
            os.remove(os.path.join(IMAGE_CACHE_DIR, filename))
            conn.execute(
                "UPDATE cards SET local_image_path = NULL WHERE scryfall_id = ?",
                (scryfall_id,),
            )
            removed += 1

    conn.commit()
    if verbose:
        print(f"  Pruned {removed} orphaned image(s) "
              f"(no longer in collection, any decklist, or any maybeboard).")
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-prune", action="store_true", help="Download only, skip pruning")
    parser.add_argument("--prune-only", action="store_true", help="Prune only, skip downloading")
    parser.add_argument("--force", action="store_true", help="Re-download everything, even if already cached")
    args = parser.parse_args()

    if not os.path.exists(DB_PATH):
        print("No database found — run migrate.py first.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    session = requests.Session()
    session.headers.update({"User-Agent": "MTGCollectionDashboard/1.0 (personal use)"})

    if not args.prune_only:
        print("Downloading missing images...")
        download_missing(conn, session, force=args.force)

    if not args.no_prune:
        print("Pruning orphaned images...")
        prune_orphaned(conn)

    conn.close()


if __name__ == "__main__":
    main()
