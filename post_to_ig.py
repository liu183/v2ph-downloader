"""
Post downloaded album images to Instagram as carousels.

Usage:
    python post_to_ig.py [--input DIR] [--username USER] [--password PASS]

Examples:
    python post_to_ig.py --input ./downloads/xasiat-shirato-hana
    python post_to_ig.py --input ./downloads --username myuser --password mypass

Requires: pip install instagrapi
"""

import argparse
import io
import os
import re
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    from instagrapi import Client
except ImportError:
    os.system(f"{sys.executable} -m pip install instagrapi")
    from instagrapi import Client

MAX_CAROUSEL = 10  # Instagram carousel limit


def get_image_files(album_dir):
    """Get sorted list of image files in an album directory."""
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    files = sorted([
        f for f in album_dir.iterdir()
        if f.is_file() and f.suffix.lower() in exts
    ])
    return files


def select_images(files, max_count=MAX_CAROUSEL):
    """Select up to max_count images evenly spaced from the list."""
    if len(files) <= max_count:
        return files
    step = len(files) / max_count
    return [files[int(i * step)] for i in range(max_count)]


def post_album_as_carousel(cl, album_dir, caption=""):
    """Post an album's images as an Instagram carousel."""
    files = get_image_files(album_dir)
    if not files:
        print(f"  No images in {album_dir.name}")
        return False

    selected = select_images(files)
    print(f"  Selected {len(selected)}/{len(files)} images")

    try:
        # instagrapi carousel upload
        media = cl.album_upload(
            paths=[str(f) for f in selected],
            caption=caption
        )
        print(f"  Posted! Media ID: {media.pk}")
        return True
    except Exception as e:
        print(f"  Error posting: {e}")
        # Fallback: try single image post
        if len(selected) == 1:
            try:
                media = cl.photo_upload(str(selected[0]), caption=caption)
                print(f"  Posted as single image! Media ID: {media.pk}")
                return True
            except Exception as e2:
                print(f"  Single image error: {e2}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Post album images to Instagram")
    parser.add_argument("--input", default="./downloads", help="Directory containing album folders")
    parser.add_argument("--username", default=os.environ.get("IG_USERNAME", ""), help="Instagram username")
    parser.add_argument("--password", default=os.environ.get("IG_PASSWORD", ""), help="Instagram password")
    parser.add_argument("--dry-run", action="store_true", help="Only list albums, don't post")
    parser.add_argument("--start", type=int, default=0, help="Start from album index (0-based)")
    args = parser.parse_args()

    input_dir = Path(args.input)
    if not input_dir.exists():
        print(f"Directory not found: {input_dir}")
        sys.exit(1)

    # Find album directories
    album_dirs = sorted([
        d for d in input_dir.iterdir()
        if d.is_dir() and get_image_files(d)
    ])

    if not album_dirs:
        # Try one level deeper (e.g. downloads/xasiat-model/album/)
        for parent in sorted(input_dir.iterdir()):
            if parent.is_dir():
                for d in sorted(parent.iterdir()):
                    if d.is_dir() and get_image_files(d):
                        album_dirs.append(d)

    if not album_dirs:
        print("No album directories with images found!")
        sys.exit(1)

    print(f"Found {len(album_dirs)} albums:")
    for i, d in enumerate(album_dirs):
        imgs = get_image_files(d)
        print(f"  [{i}] {d.name} ({len(imgs)} images)")

    if args.dry_run:
        print("\nDry run - not posting.")
        return

    # Login via session or credentials
    session_json = os.environ.get("IG_SESSION", "")
    cl = Client()

    if session_json:
        print("\nLoading session from IG_SESSION env var...")
        import json
        session_data = json.loads(session_json)
        cl.set_settings(session_data)
        # Verify session is still valid
        try:
            user = cl.account_info()
            print(f"Session valid! Logged in as: {user.username}")
        except Exception:
            # Session expired, re-login with credentials
            print("Session expired, re-logging in...")
            if args.username and args.password:
                cl.login(args.username, args.password)
                print(f"Re-logged in as: {args.username}")
            else:
                print("Error: session expired and no credentials provided")
                sys.exit(1)
    elif args.username and args.password:
        print(f"\nLogging in as {args.username}...")
        cl.login(args.username, args.password)
        print("Login OK!")
    else:
        print("Error: need IG_SESSION env var or --username/--password")
        sys.exit(1)

    # Post each album
    posted = 0
    failed = 0
    for i, album_dir in enumerate(album_dirs):
        if i < args.start:
            print(f"\n[{i+1}/{len(album_dirs)}] Skipping {album_dir.name} (--start={args.start})")
            continue

        print(f"\n[{i+1}/{len(album_dirs)}] {album_dir.name}")

        # Build caption
        caption = f"{album_dir.name}\n\n#白桃はな #ShiratoHana #写真集 #gravure"

        if post_album_as_carousel(cl, album_dir, caption):
            posted += 1
        else:
            failed += 1

        # Rate limit: wait between posts
        if i < len(album_dirs) - 1:
            wait = 30
            print(f"  Waiting {wait}s before next post...")
            time.sleep(wait)

    print(f"\n{'='*50}")
    print(f"  Posted: {posted} | Failed: {failed}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
