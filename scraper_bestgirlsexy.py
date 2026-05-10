"""
bestgirlsexy.com album scraper - downloads all photos for a model via WordPress REST API.

Usage:
    python scraper_bestgirlsexy.py [--tag TAG] [--tag-id ID] [--output DIR]

Examples:
    python scraper_bestgirlsexy.py --tag shirato-hana
    python scraper_bestgirlsexy.py --tag shirato-hana --output ./downloads

WordPress REST API. No auth required. No Cloudflare.
"""

import argparse
import io
import json
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

BASE_URL = "https://bestgirlsexy.com"
API_URL = f"{BASE_URL}/wp-json/wp/v2"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}
REQUEST_DELAY = 0.5
DOWNLOAD_DELAY = 0.2
MAX_RETRIES = 3


# ── Feishu: upload via Open API, send via webhook ───────────────

_feishu_token_cache = {"token": None, "expires": 0}


def _get_tenant_token(app_id, app_secret):
    if _feishu_token_cache["token"] and time.time() < _feishu_token_cache["expires"]:
        return _feishu_token_cache["token"]
    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret}, timeout=10)
        data = resp.json()
        if data.get("code") == 0:
            _feishu_token_cache["token"] = data["tenant_access_token"]
            _feishu_token_cache["expires"] = time.time() + data.get("expire", 7200) - 60
            return _feishu_token_cache["token"]
        print(f"    [Feishu] Token error: {data}")
    except Exception as e:
        print(f"    [Feishu] Token exception: {e}")
    return None


def _feishu_upload_image(token, image_url):
    try:
        img_resp = requests.get(image_url, timeout=30)
        if img_resp.status_code != 200:
            return None
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/images",
            headers={"Authorization": f"Bearer {token}"},
            data={"image_type": "message"},
            files={"image": ("img.jpg", img_resp.content, "image/jpeg")},
            timeout=30)
        data = resp.json()
        if data.get("code") == 0:
            return data["data"]["image_key"]
        print(f"    [Feishu] Upload error: {data}")
    except Exception as e:
        print(f"    [Feishu] Upload exception: {e}")
    return None


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


def send_feishu_images(token, webhook_url, title, image_urls):
    if not webhook_url or not image_urls:
        return
    total = len(image_urls)
    batch_size = 10
    for batch_start in range(0, total, batch_size):
        batch = image_urls[batch_start:batch_start + batch_size]
        elements = []
        for i, img_url in enumerate(batch):
            idx = batch_start + i + 1
            if token:
                image_key = _feishu_upload_image(token, img_url)
                if image_key:
                    elements.append({"tag": "img", "img_key": image_key, "alt": {"tag": "plain_text", "content": f"图片{idx}"}})
                    print(f"    [Feishu] Uploaded {idx}/{total}")
                    continue
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"图片{idx}: [查看原图]({img_url})"}})
        if elements:
            batch_title = f"🖼️ {title} ({batch_start+1}-{min(batch_start+batch_size, total)}/{total})" if total > batch_size else f"🖼️ {title}"
            payload = {
                "msg_type": "interactive",
                "card": {
                    "header": {"title": {"tag": "plain_text", "content": batch_title}, "template": "blue"},
                    "elements": elements,
                }
            }
            _feishu_post(webhook_url, payload)
            time.sleep(0.5)
    print(f"    [Feishu] All {total} images sent: {title}")


def send_feishu_album(token, webhook_url, album_name, dl_count, total, album_url, image_urls):
    if not webhook_url:
        return
    lines = [
        f"**图片数:** {dl_count}/{total}",
        f"**链接:** [查看图集]({album_url})",
    ]
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": album_name}, "template": "blue"},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
        }
    }
    _feishu_post(webhook_url, payload)
    if image_urls:
        time.sleep(0.3)
        send_feishu_images(token, webhook_url, album_name, image_urls)


def send_feishu_summary(token, webhook_url, tag, albums_info, total_images, total_downloaded, sample_images=None):
    if not webhook_url:
        return
    lines = [f"**标签:** {tag}"]
    lines.append(f"**来源:** bestgirlsexy.com")
    lines.append(f"**图集数:** {len(albums_info)} | **总图片:** {total_images} | **已下载:** {total_downloaded}")
    lines.append("")
    for i, (name, imgs, dl) in enumerate(albums_info, 1):
        lines.append(f"{i}. {name[:40]} — {dl}/{imgs}张")
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": f"✅ bestgirlsexy 下载完成: {tag}"}, "template": "green"},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
        }
    }
    _feishu_post(webhook_url, payload)
    if token and sample_images:
        time.sleep(0.3)
        send_feishu_images(token, webhook_url, f"{tag} 图集预览", sample_images)


# ── Scraping ────────────────────────────────────────────────────

def resolve_tag_id(tag_slug):
    """Resolve a tag slug to its WordPress term ID."""
    resp = requests.get(f"{API_URL}/tags", params={"slug": tag_slug, "per_page": 10}, headers=HEADERS, timeout=30)
    if resp.status_code == 200:
        data = resp.json()
        if data:
            return data[0]["id"]
    # Try searching by name
    resp = requests.get(f"{API_URL}/tags", params={"search": tag_slug, "per_page": 10}, headers=HEADERS, timeout=30)
    if resp.status_code == 200:
        data = resp.json()
        if data:
            return data[0]["id"]
    return None


def get_albums(tag_slug, tag_id=None):
    """Get album posts from the WordPress REST API."""
    if not tag_id:
        print(f"[1/3] Resolving tag ID for '{tag_slug}'...")
        tag_id = resolve_tag_id(tag_slug)
        if not tag_id:
            print(f"  Tag '{tag_slug}' not found!")
            return []
        print(f"  Tag ID: {tag_id}")
    else:
        print(f"[1/3] Using tag ID: {tag_id}")

    albums = []
    page = 1
    while True:
        url = f"{API_URL}/posts"
        params = {"tags": tag_id, "per_page": 100, "page": page, "_fields": "id,title,link,content"}
        resp = requests.get(url, params=params, headers=HEADERS, timeout=30)
        if resp.status_code != 200:
            print(f"  API error: {resp.status_code}")
            break
        data = resp.json()
        if not data:
            break
        for post in data:
            title = post.get("title", {}).get("rendered", "Untitled")
            # Clean HTML entities from title
            title = BeautifulSoup(title, "html.parser").get_text(strip=True)
            albums.append({
                "id": post["id"],
                "title": title,
                "url": post["link"],
                "content": post.get("content", {}).get("rendered", ""),
            })
        # Check pagination
        total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
        if page >= total_pages:
            break
        page += 1
        time.sleep(REQUEST_DELAY)

    print(f"  Found {len(albums)} albums")
    return albums


def extract_images_from_content(html_content):
    """Extract image URLs from WordPress post content HTML."""
    soup = BeautifulSoup(html_content, "html.parser")
    images = []
    for img in soup.select("img"):
        src = img.get("data-src") or img.get("data-lazy-src") or img.get("src", "")
        if not src or src.startswith("data:"):
            continue
        # Skip small thumbnails and icons
        if any(x in src for x in ["-150x150", "-100x100", "-300x", "icon", "logo", "avatar"]):
            continue
        # Only keep images from the site's uploads
        if "wp-content/uploads" in src or "bestgirlsexy.com" in src:
            # Try to get full-size URL by removing size suffix
            full_src = re.sub(r"-\d+x\d+(?=\.\w+$)", "", src)
            if full_src not in images:
                images.append(full_src)
    return images


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
    parser = argparse.ArgumentParser(description="bestgirlsexy.com album scraper")
    parser.add_argument("--tag", default="shirato-hana", help="Tag slug from URL")
    parser.add_argument("--tag-id", type=int, default=0, help="WordPress tag ID (if known)")
    parser.add_argument("--output", default="./downloads", help="Output directory")
    parser.add_argument("--list-only", action="store_true", help="Only list albums")
    parser.add_argument("--webhook", default=os.environ.get("FEISHU_WEBHOOK", ""),
                        help="Feishu webhook URL (or set FEISHU_WEBHOOK env var)")
    args = parser.parse_args()

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    webhook = args.webhook

    feishu_app_id = os.environ.get("FEISHU_APP_ID", "")
    feishu_app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    feishu_token = _get_tenant_token(feishu_app_id, feishu_app_secret) if feishu_app_id else None

    albums = get_albums(args.tag, tag_id=args.tag_id or None)
    if not albums:
        sys.exit(1)

    print(f"\n[2/3] Processing {len(albums)} albums...")
    total_downloaded = 0
    total_images = 0
    albums_info = []
    sample_images = []

    for i, album in enumerate(albums):
        images = extract_images_from_content(album["content"])
        total_images += len(images)

        print(f"\n  [{i+1}/{len(albums)}] {len(images):>3d} imgs")
        print(f"    {album['title'][:70]}")

        if args.list_only:
            albums_info.append((album["title"], len(images), 0))
            continue

        dl_count = 0
        if images:
            dl_count = download_images(images, output, album["title"])
            total_downloaded += dl_count
            print(f"    Downloaded: {dl_count}/{len(images)}")
            sample_images.append(images[0])

        albums_info.append((album["title"], len(images), dl_count))

        if not args.list_only:
            send_feishu_album(feishu_token, webhook, album["title"], dl_count, len(images), album["url"], images)

    print(f"\n{'='*60}")
    print(f"  Albums: {len(albums)} | Images found: {total_images}")
    if not args.list_only:
        print(f"  Downloaded: {total_downloaded}")
        print(f"  Output: {output.resolve()}")
        if webhook:
            send_feishu_summary(feishu_token, webhook, args.tag, albums_info, total_images, total_downloaded, sample_images)
    else:
        print(f"\n  {'Album':<50} {'Imgs':>5}")
        print(f"  {'-'*55}")
        for name, imgs, _ in albums_info:
            print(f"  {name[:48]:<50} {imgs:>5}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
