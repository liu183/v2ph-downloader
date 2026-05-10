"""
xasiat.com album scraper - downloads all photos from a model's page.

Usage:
    python scraper_xasiat.py [--model MODEL_SLUG] [--output DIR]

Examples:
    python scraper_xasiat.py --model shirato-hana
    python scraper_xasiat.py --model shirato-hana --output ./downloads

No authentication required. Direct CDN access, no Cloudflare.
"""

import argparse
import io
import os
import re
import sys
import time
import requests
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    from bs4 import BeautifulSoup
except ImportError:
    os.system(f"{sys.executable} -m pip install beautifulsoup4 requests")
    from bs4 import BeautifulSoup

BASE_URL = "https://www.xasiat.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}
REQUEST_DELAY = 0.5
DOWNLOAD_DELAY = 0.2
MAX_RETRIES = 3


def fetch_page(url, retries=MAX_RETRIES):
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 200:
                return BeautifulSoup(resp.text, "html.parser")
            print(f"  [{resp.status_code}] Retrying {url}...")
        except requests.RequestException as e:
            print(f"  [Error] {e}, retrying...")
        time.sleep(REQUEST_DELAY * (attempt + 1))
    return None


def send_feishu(webhook_url, title, content_lines, cover_url=None):
    if not webhook_url:
        return
    post_content = []
    for line in content_lines:
        post_content.append({"tag": "text", "text": line})
        post_content.append({"tag": "text", "text": "\n"})
    if cover_url:
        post_content.append({"tag": "img", "image_key": "", "src": cover_url})
    payload = {
        "msg_type": "post",
        "content": {"post": {"zh_cn": {"title": title, "content": [post_content]}}}
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code == 200 and resp.json().get("code") == 0:
            print(f"    [Feishu] Sent: {title}")
        else:
            print(f"    [Feishu] Error: {resp.text[:100]}")
    except Exception as e:
        print(f"    [Feishu] Exception: {e}")


def get_albums(model_slug):
    """Get album list from model page."""
    url = f"{BASE_URL}/albums/models/{model_slug}/"
    print(f"[1/3] Fetching model page: {url}")
    soup = fetch_page(url)
    if not soup:
        print("Failed to access model page!")
        return []

    albums = []
    for item in soup.select("div.list-albums div.item"):
        link = item.select_one("a[href]")
        title_el = item.select_one("strong.title")
        photos_el = item.select_one("div.photos")

        if not link:
            continue

        href = link.get("href", "")
        title = title_el.get_text(strip=True) if title_el else ""
        photos = photos_el.get_text(strip=True) if photos_el else ""
        cover_img = item.select_one("img[data-original]")
        cover = cover_img.get("data-original", "") if cover_img else ""

        # Extract album ID from URL: /albums/{id}/{slug}/
        m = re.search(r"/albums/(\d+)/", href)
        album_id = int(m.group(1)) if m else 0

        albums.append({
            "url": href if href.startswith("http") else f"{BASE_URL}{href}",
            "title": title,
            "photos_text": photos,
            "album_id": album_id,
            "cover": cover,
        })

    print(f"  Found {len(albums)} albums")
    return albums


def get_album_images(album_url):
    """Get full-size image URLs from an album page."""
    soup = fetch_page(album_url)
    if not soup:
        return []

    images = []
    for a_tag in soup.select("div.images a.item[href]"):
        href = a_tag.get("href", "")
        if not href:
            continue

        # Convert proxy URL to direct CDN URL
        # Proxy: /get_image/2/{hash}/sources/{prefix}/{id}/{photo_id}.jpg/
        # Direct: pic.xascdn.li/contents/albums/sources/{prefix}/{id}/{photo_id}.jpg
        direct = convert_to_direct_cdn(href)
        if direct:
            images.append(direct)

    return images


def convert_to_direct_cdn(url):
    """Convert get_image proxy URL to direct CDN URL."""
    # Match: /get_image/2/{hash}/sources/{prefix}/{album_id}/{photo_id}.jpg/
    m = re.search(r"/sources/(\d+)/(\d+)/(\d+)\.jpg", url)
    if m:
        prefix, album_id, photo_id = m.groups()
        return f"https://pic.xascdn.li/contents/albums/sources/{prefix}/{album_id}/{photo_id}.jpg"

    # Already a direct CDN URL?
    if "pic.xascdn.li" in url and "/sources/" in url:
        return url.split("?")[0]

    return None


def sanitize_filename(name, max_len=80):
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > max_len:
        name = name[:max_len]
    return name


def download_images(images, output_dir, album_name):
    album_dir = output_dir / sanitize_filename(album_name)
    album_dir.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    for i, img_url in enumerate(images):
        ext = "jpg"
        if ".png" in img_url:
            ext = "png"
        elif ".webp" in img_url:
            ext = "webp"

        filepath = album_dir / f"{i+1:04d}.{ext}"
        if filepath.exists():
            downloaded += 1
            continue

        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.get(img_url, headers=HEADERS, timeout=30)
                if resp.status_code == 200:
                    filepath.write_bytes(resp.content)
                    downloaded += 1
                    break
            except requests.RequestException:
                pass
            time.sleep(1)

        time.sleep(DOWNLOAD_DELAY)

    return downloaded


def main():
    parser = argparse.ArgumentParser(description="xasiat.com album scraper")
    parser.add_argument("--model", default="shirato-hana", help="Model slug from URL")
    parser.add_argument("--output", default="./downloads", help="Output directory")
    parser.add_argument("--list-only", action="store_true", help="Only list albums")
    parser.add_argument("--webhook", default=os.environ.get("FEISHU_WEBHOOK", ""),
                        help="Feishu webhook URL (or set FEISHU_WEBHOOK env var)")
    args = parser.parse_args()

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    webhook = args.webhook

    # Step 1: Get albums
    albums = get_albums(args.model)
    if not albums:
        sys.exit(1)

    # Step 2: Process each album
    print(f"\n[2/3] Processing {len(albums)} albums...")
    total_downloaded = 0
    total_images = 0
    albums_info = []

    for i, album in enumerate(albums):
        time.sleep(REQUEST_DELAY)
        images = get_album_images(album["url"])
        total_images += len(images)

        print(f"\n  [{i+1}/{len(albums)}] {album['photos_text']:>6s} | {len(images):>3d} imgs")
        print(f"    {album['title'][:70]}")

        if args.list_only:
            albums_info.append((album["title"], album["photos_text"], len(images), 0))
            continue

        dl_count = 0
        if images:
            dl_count = download_images(images, output, album["title"])
            total_downloaded += dl_count
            print(f"    Downloaded: {dl_count}/{len(images)}")

        albums_info.append((album["title"], album["photos_text"], len(images), dl_count))

        # Feishu notification per album
        if webhook and not args.list_only:
            feishu_lines = [
                f"**图片数:** {dl_count}/{len(images)}",
                f"**来源:** xasiat.com",
                f"**链接:** {album['url']}",
            ]
            cover = album.get("cover", "") or (images[0] if images else "")
            send_feishu(webhook, album["title"], feishu_lines, cover_url=cover)

    # Summary
    print(f"\n{'='*60}")
    print(f"  Albums: {len(albums)} | Images found: {total_images}")
    if not args.list_only:
        print(f"  Downloaded: {total_downloaded}")
        print(f"  Output: {output.resolve()}")
        if webhook:
            lines = [
                f"**模型:** {args.model}",
                f"**来源:** xasiat.com",
                f"**图集数:** {len(albums)} | **总图片:** {total_images} | **已下载:** {total_downloaded}",
                "",
            ]
            for j, (name, badge, imgs, dl) in enumerate(albums_info, 1):
                lines.append(f"{j}. 【{badge}】{name[:40]} — {dl}/{imgs}张")
            send_feishu(webhook, f"xasiat 下载完成: {args.model}", lines)
    else:
        print(f"\n  {'Album':<50} {'Photos':>8} {'Imgs':>5}")
        print(f"  {'-'*65}")
        for name, badge, imgs, _ in albums_info:
            print(f"  {name[:48]:<50} {badge:>8} {imgs:>5}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
