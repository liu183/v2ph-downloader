"""
erotok.com album scraper - downloads all photos tagged with a model.

Usage:
    python scraper_erotok.py [--tag TAG] [--output DIR]

Examples:
    python scraper_erotok.py --tag 白桃はな
    python scraper_erotok.py --tag 白桃はな --output ./downloads

WordPress site with lazy-loaded images (data-src). No auth required.
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

try:
    import cloudscraper
    _scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
except ImportError:
    _scraper = None

BASE_URL = "https://erotok.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}
REQUEST_DELAY = 0.5
DOWNLOAD_DELAY = 0.2
MAX_RETRIES = 3

_use_playwright = False
_pw = None
_browser = None


def _init_playwright():
    global _use_playwright, _pw, _browser
    _use_playwright = True
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        os.system(f"{sys.executable} -m pip install playwright")
        os.system(f"{sys.executable} -m playwright install chromium")
        from playwright.sync_api import sync_playwright
    _pw = sync_playwright().start()
    _browser = _pw.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )


def _close_playwright():
    global _pw, _browser
    if _browser:
        _browser.close()
    if _pw:
        _pw.stop()


def fetch_page(url, retries=MAX_RETRIES):
    for attempt in range(retries):
        try:
            if _use_playwright and _browser:
                page = _browser.new_page()
                page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
                page.goto(url, timeout=60000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                except Exception:
                    pass
                # Wait for Cloudflare challenge to complete
                for _ in range(6):
                    time.sleep(2)
                    html = page.content()
                    if "challenge" not in html.lower()[:2000]:
                        break
                else:
                    html = page.content()
                page.close()
                soup = BeautifulSoup(html, "html.parser")
                # Verify we got real content, not a challenge page
                if soup.select("li.p-postList__item") or soup.select("article"):
                    return soup
                if "challenge" in html.lower()[:2000]:
                    print(f"  [CF] Challenge page persisted, retrying...")
                    time.sleep(5)
                    continue
                return soup
            else:
                # Try cloudscraper first
                if _scraper:
                    resp = _scraper.get(url, timeout=30)
                else:
                    resp = requests.get(url, headers=HEADERS, timeout=30)
                if resp.status_code == 200:
                    return BeautifulSoup(resp.text, "html.parser")
                if resp.status_code == 403:
                    print(f"  [403] Switching to Playwright...")
                    _init_playwright()
                    continue
                print(f"  [{resp.status_code}] Retrying {url}...")
        except Exception as e:
            print(f"  [Error] {e}, retrying...")
        time.sleep(REQUEST_DELAY * (attempt + 1))
    return None


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
    lines.append(f"**来源:** erotok.com")
    lines.append(f"**图集数:** {len(albums_info)} | **总图片:** {total_images} | **已下载:** {total_downloaded}")
    lines.append("")
    for i, (name, imgs, dl) in enumerate(albums_info, 1):
        lines.append(f"{i}. {name[:40]} — {dl}/{imgs}张")
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": f"✅ erotok 下载完成: {tag}"}, "template": "green"},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
        }
    }
    _feishu_post(webhook_url, payload)
    if token and sample_images:
        time.sleep(0.3)
        send_feishu_images(token, webhook_url, f"{tag} 图集预览", sample_images)


# ── Scraping ────────────────────────────────────────────────────

def get_albums(tag):
    """Get album URLs from the tag page."""
    from urllib.parse import quote
    encoded_tag = quote(tag)
    url = f"{BASE_URL}/archives/tag/{encoded_tag}"
    print(f"[1/3] Fetching tag page: {url}")
    soup = fetch_page(url)
    if not soup:
        print("Failed to access tag page!")
        return []

    albums = []
    seen = set()
    for item in soup.select("li.p-postList__item"):
        link = item.select_one("a[href*='/archives/']")
        if not link:
            continue
        href = link.get("href", "")
        if not href or href in seen:
            continue
        # Skip category/tag links
        if "/category/" in href or "/tag/" in href:
            continue
        seen.add(href)
        title = link.get_text(strip=True)
        # Filter: only include albums whose title contains the keyword
        if tag not in title:
            continue
        if href:
            albums.append({
                "url": href if href.startswith("http") else f"{BASE_URL}{href}",
                "title": title,
            })
    print(f"  Found {len(albums)} albums")
    return albums


def get_album_images(album_url):
    """Get image URLs from an album page, handling multi-page posts."""
    images = []
    page = 1
    base_url = album_url.rstrip("/")

    while True:
        url = base_url if page == 1 else f"{base_url}/{page}"
        soup = fetch_page(url)
        if not soup:
            break

        # Extract images from post content
        content = soup.select_one("div.post_content, div.entry-content, div.article-content, .post-body")
        if not content:
            content = soup  # fallback to whole page

        found_on_page = 0
        for img in content.select("img"):
            # Prefer data-src (lazy load), fall back to src
            src = img.get("data-src") or img.get("data-lazy-src") or img.get("src", "")
            if not src or src.startswith("data:"):
                continue
            # Skip non-content images
            skip_patterns = ["avatar", "icon", "logo", "nukistagram", "/thumbnail/",
                             "-150x150", "-100x100", "-300x", "ad-", "banner"]
            if any(x in src.lower() for x in skip_patterns):
                continue
            if not src.startswith("http"):
                src = f"{BASE_URL}{src}"
            if src not in images:
                images.append(src)
                found_on_page += 1

        # Check for next page
        pagination = soup.select("div.c-pagination a.post-page-numbers, .page-numbers")
        has_next = False
        for p in pagination:
            href = p.get("href", "")
            if f"/{page + 1}" in href:
                has_next = True
                break
        if not has_next:
            break

        page += 1
        time.sleep(REQUEST_DELAY)

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
    parser = argparse.ArgumentParser(description="erotok.com album scraper")
    parser.add_argument("--tag", default="白桃はな", help="Tag to search for")
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

    albums = get_albums(args.tag)
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
    _close_playwright()


if __name__ == "__main__":
    main()
