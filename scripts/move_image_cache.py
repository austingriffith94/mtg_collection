"""
One-time move of the local image cache from image_cache/ (project root) to
static/image_cache/ (Phase 4 of UI_ACTION_PLAN.md).

Streamlit's static file serving only serves files under static/ next to
the entry script (dashboard.py), so the dashboard can link card art as
`app/static/image_cache/<id>.jpg` instead of pasting every image into the
page as base64. Stored paths (cards.local_image_path,
decks.cover_image_path) are relative — 'image_cache/<id>.jpg' — and are now
resolved against static/ instead of the project root, so no database
rewrite is needed; only the files move.

Idempotent: once image_cache/ is gone this does nothing. A file already
present at the destination is left alone, and its old copy is kept rather
than deleted (deck covers are user uploads, not re-downloadable), so a
re-run never loses anything. Stdlib only, so run.py can call it before
launching the dashboard without needing requests.

Run standalone:  python move_image_cache.py
Called by:       run.py (before launching the dashboard) and sync_images.py
"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEGACY_DIR = os.path.join(BASE_DIR, "image_cache")
NEW_DIR = os.path.join(BASE_DIR, "static", "image_cache")


def move_legacy_image_cache(legacy_dir=LEGACY_DIR, new_dir=NEW_DIR):
    """Move every file under legacy_dir into the same relative spot under
    new_dir, then remove legacy_dir's now-empty folders. Returns
    (moved, kept) — kept counts old files left behind because the
    destination already had one."""
    if not os.path.isdir(legacy_dir):
        return 0, 0
    moved = kept = 0
    # Bottom-up so each folder is empty by the time we try to remove it.
    for root, _dirs, files in os.walk(legacy_dir, topdown=False):
        dest_root = os.path.join(new_dir, os.path.relpath(root, legacy_dir))
        os.makedirs(dest_root, exist_ok=True)
        for name in files:
            src = os.path.join(root, name)
            dest = os.path.join(dest_root, name)
            if os.path.exists(dest):
                kept += 1
                continue
            os.replace(src, dest)
            moved += 1
        try:
            os.rmdir(root)
        except OSError:
            pass  # still holds a kept duplicate
    return moved, kept


if __name__ == "__main__":
    moved, kept = move_legacy_image_cache()
    if not moved and not kept:
        print("Nothing to move — image cache is already in static/image_cache/.")
    else:
        print(f"Moved {moved} file(s) to static/image_cache/.")
        if kept:
            print(f"Left {kept} file(s) in image_cache/ that already exist in the new folder.")
