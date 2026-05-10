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


# ── Feishu webhook ──────────────────────────────────────────────

def _feishu_post(webhook_url, payload):
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == 0:
                return True
            print(f"    [Feishu] Error: {data}")
        else:
            print(f"    [Feishu] HTTP {resp.status_code}")
    except Exception as e:
        print(f"    [Feishu] Exception: {e}")
    return False


def send_feishu_card(webhook_url, title, content_lines, header_template="blue"):
    if not webhook_url:
        return
    md_lines = "\n".join(content_lines)
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": header_template},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": md_lines}}],
        }
    }
    if _feishu_post(webhook_url, payload):
        print(f"    [Feishu] Card: {title}")


def send_feishu_images(webhook_url, title, image_urls, max_show=3):
    if not webhook_url or not image_urls:
        return
    content = []
    content.append({"tag": "text", "text": f"📷 {title}（共{len(image_urls)}张，展示前{min(max_show, len(image_urls))}张）\n"})
    for i, img_url in enumerate(image_urls[:max_show]):
        content.append({"tag": "text", "text": f"\n图片 {i+1}: "})
        content.append({"tag": "a", "text": "查看原图", "href": img_url})
        content.append({"tag": "text", "text": "\n"})
        content.append({"tag": "text", "text": f"{img_url}\n"})
    payload = {
        "msg_type": "post",
        "content": {"post": {"zh_cn": {"title": f"🖼️ {title}", "content": [content]}}}
    }
    if _feishu_post(webhook_url, payload):
        print(f"    [Feishu] Images: {title} ({min(max_show, len(image_urls))} shown)")


def send_feishu_album(webhook_url, album_name, badge, dl_count, total, album_url, image_urls):
    if not webhook_url:
        return
    lines = [
        f"**标记:** {badge}",
        f"**图片数:** {dl_count}/{total}",
        f"**链接:** [查看图集]({album_url})",
    ]
    send_feishu_card(webhook_url, album_name, lines)
    if image_urls:
        time.sleep(0.3)
        send_feishu_images(webhook_url, album_name, image_urls, max_show=3)


def send_feishu_summary(webhook_url, model, albums_info, total_images, total_downloaded, sample_images=None):
    lines = [f"**模型:** {model}"]
    lines.append(f"**来源:** xasiat.com")
    lines.append(f"**图集数:** {len(albums_info)} | **总图片:** {total_images} | **已下载:** {total_downloaded}")
    lines.append("")
    for i, (name, badge, imgs, dl) in enumerate(albums_info, 1):
        lines.append(f"{i}. 【{badge}】{name[:40]} — {dl}/{imgs}张")
    send_feishu_card(webhook_url, f"✅ xasiat 下载完成: {model}", lines, header_template="green")
    if sample_images:
        time.sleep(0.3)
        send_feishu_images(webhook_url, f"{model} 图集预览", sample_images, max_show=len(sample_images))


# ── Scraping ────────────────────────────────────────────────────

def get_albums(model_slug):
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
        m = re.search(r"/albums/(\d+)/", href)
        album_id = int(m.group(1)) if m else 0
        albums.append({
            "url": href if href.startswith("http") else f"{BASE_URL}{href}",
            "title": title, "photos_text": photos, "album_id": album_id, "cover": cover,
        })
    print(f"  Found {len(albums)} albums")
    return albums


def get_album_images(album_url):
    soup = fetch_page(album_url)
    if not soup:
        return []
    images = []
    for a_tag in soup.select("div.images a.item[href]"):
        href = a_tag.get("href", "")
        if not href:
            continue
        direct = convert_to_direct_cdn(href)
        if direct:
            images.append(direct)
    return images


def convert_to_direct_cdn(url):
    m = re.search(r"/sources/(\d+)/(\d+)/(\d+)\.jpg", url)
    if m:
        prefix, album_id, photo_id = m.groups()
        return f"https://pic.xascdn.li/contents/albums/sources/{prefix}/{album_id}/{photo_id}.jpg"
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

    albums = get_albums(args.model)
    if not albums:
        sys.exit(1)

    print(f"\n[2/3] Processing {len(albums)} albums...")
    total_downloaded = 0
    total_images = 0
    albums_info = []
    sample_images = []

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
            sample_images.append(images[0])

        albums_info.append((album["title"], album["photos_text"], len(images), dl_count))

        if webhook and not args.list_only:
            send_feishu_album(webhook, album["title"], album["photos_text"], dl_count, len(images), album["url"], images)

    print(f"\n{'='*60}")
    print(f"  Albums: {len(albums)} | Images found: {total_images}")
    if not args.list_only:
        print(f"  Downloaded: {total_downloaded}")
        print(f"  Output: {output.resolve()}")
        if webhook:
            send_feishu_summary(webhook, args.model, albums_info, total_images, total_downloaded, sample_images)
    else:
        print(f"\n  {'Album':<50} {'Photos':>8} {'Imgs':>5}")
        print(f"  {'-'*65}")
        for name, badge, imgs, _ in albums_info:
            print(f"  {name[:48]:<50} {badge:>8} {imgs:>5}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
