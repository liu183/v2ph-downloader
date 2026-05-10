"""
everiaclub.com album scraper - downloads all photos matching a keyword.

Uses Playwright to bypass Cloudflare protection.

Usage:
    python scraper_everiaclub.py [--keyword KEYWORD] [--output DIR]

Examples:
    python scraper_everiaclub.py --keyword 白桃はな
    python scraper_everiaclub.py --keyword "Hana Shirato" --output ./downloads

Images use lazy-loading (data-original). No auth required.
"""

import argparse
import io
import os
import re
import sys
import time
import requests
from pathlib import Path
from urllib.parse import quote

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

BASE_URL = "https://www.everiaclub.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}
REQUEST_DELAY = 0.5
DOWNLOAD_DELAY = 0.2
MAX_RETRIES = 3


def _get_playwright_browser():
    """Launch a Playwright browser instance with stealth settings."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        os.system(f"{sys.executable} -m pip install playwright")
        os.system(f"{sys.executable} -m playwright install chromium")
        from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    browser = p.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    return p, browser


def _wait_for_cloudflare(page, max_wait=15):
    """Wait for Cloudflare challenge to complete."""
    for _ in range(max_wait):
        time.sleep(2)
        html = page.content()
        if "challenge" not in html.lower()[:2000]:
            return html
    return page.content()


def _get_flaresolverr_url():
    return os.environ.get("FLARESOLVERR_URL", "")


def fetch_page_with_flaresolverr(url, retries=2):
    """Use FlareSolverr to bypass Cloudflare."""
    fs_url = _get_flaresolverr_url()
    if not fs_url:
        return None
    for attempt in range(retries):
        try:
            resp = requests.post(fs_url, json={
                "cmd": "request.get",
                "url": url,
                "maxTimeout": 60000,
            }, timeout=90)
            data = resp.json()
            if data.get("status") == "ok":
                html = data["solution"]["response"]
                soup = BeautifulSoup(html, "html.parser")
                if soup.select("div.mainleft") or soup.select("div.leftp"):
                    print(f"  [FlareSolverr] Page loaded successfully")
                    return soup
                if "challenge" not in html.lower()[:2000]:
                    return soup
                print(f"  [FlareSolverr] Got challenge page, retrying...")
            else:
                print(f"  [FlareSolverr] Error: {data.get('message', 'unknown')}")
        except Exception as e:
            print(f"  [FlareSolverr Error] {e}, retrying...")
        time.sleep(REQUEST_DELAY * (attempt + 1))
    return None


def fetch_page_with_cloudscraper(url, retries=MAX_RETRIES):
    """Try cloudscraper (handles Cloudflare JS challenges)."""
    if not _scraper:
        return None
    for attempt in range(retries):
        try:
            resp = _scraper.get(url, timeout=30)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                if soup.select("div.mainleft") or soup.select("div.leftp"):
                    return soup
                if "challenge" not in resp.text.lower()[:2000]:
                    return soup
                print(f"  [CF] Got challenge page via cloudscraper, retrying...")
            else:
                print(f"  [{resp.status_code}] cloudscraper retrying {url}...")
        except Exception as e:
            print(f"  [cloudscraper Error] {e}, retrying...")
        time.sleep(REQUEST_DELAY * (attempt + 1))
    return None


def fetch_page_with_playwright(url, wait_selector="div.mainleft", timeout=60000):
    """Use Playwright to bypass Cloudflare and get page HTML."""
    p, browser = _get_playwright_browser()
    try:
        page = browser.new_page()
        page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
        page.goto(url, timeout=timeout)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass
        # Wait for Cloudflare challenge to complete
        html = _wait_for_cloudflare(page)
        # Try waiting for actual content
        try:
            page.wait_for_selector(wait_selector, timeout=15000)
            html = page.content()
        except Exception:
            pass
    finally:
        browser.close()
        p.stop()
    return BeautifulSoup(html, "html.parser")


def fetch_page_simple(url, retries=MAX_RETRIES):
    """Simple HTTP fetch (for non-Cloudflare pages)."""
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


def send_feishu_summary(token, webhook_url, keyword, albums_info, total_images, total_downloaded, sample_images=None):
    if not webhook_url:
        return
    lines = [f"**关键词:** {keyword}"]
    lines.append(f"**来源:** everiaclub.com")
    lines.append(f"**图集数:** {len(albums_info)} | **总图片:** {total_images} | **已下载:** {total_downloaded}")
    lines.append("")
    for i, (name, imgs, dl) in enumerate(albums_info, 1):
        lines.append(f"{i}. {name[:40]} — {dl}/{imgs}张")
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": f"✅ everiaclub 下载完成: {keyword}"}, "template": "green"},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
        }
    }
    _feishu_post(webhook_url, payload)
    if token and sample_images:
        time.sleep(0.3)
        send_feishu_images(token, webhook_url, f"{keyword} 图集预览", sample_images)


# ── Scraping ────────────────────────────────────────────────────

def get_albums(keyword):
    """Get album URLs from search results."""
    encoded = quote(keyword)
    url = f"{BASE_URL}/search/?keyword={encoded}"
    print(f"[1/3] Fetching search page: {url}")
    # Try FlareSolverr → cloudscraper → Playwright
    soup = fetch_page_with_flaresolverr(url)
    if not soup:
        print("  [FlareSolverr] Failed, trying cloudscraper...")
        soup = fetch_page_with_cloudscraper(url)
    if not soup:
        print("  [cloudscraper] Failed, falling back to Playwright...")
        soup = fetch_page_with_playwright(url)
    if not soup:
        print("Failed to access search page!")
        return []

    albums = []
    seen = set()
    for item in soup.select("div.mainleft div.leftp"):
        link = item.select_one("a[href]")
        if not link:
            continue
        href = link.get("href", "")
        if not href or href in seen:
            continue
        seen.add(href)

        title_el = item.select_one("p a")
        title = title_el.get_text(strip=True) if title_el else href.split("/")[-1]

        full_url = href if href.startswith("http") else f"{BASE_URL}{href}"
        albums.append({"url": full_url, "title": title})

    # Check pagination
    pagination = soup.select("ul.pagination li a[href*='page=']")
    for page_link in pagination:
        page_url = page_link.get("href", "")
        if not page_url:
            continue
        full_page_url = page_url if page_url.startswith("http") else f"{BASE_URL}{page_url}"
        time.sleep(REQUEST_DELAY)
        page_soup = fetch_page_with_flaresolverr(full_page_url) or fetch_page_with_cloudscraper(full_page_url) or fetch_page_with_playwright(full_page_url)
        if not page_soup:
            continue
        for item in page_soup.select("div.mainleft div.leftp"):
            link = item.select_one("a[href]")
            if not link:
                continue
            href = link.get("href", "")
            if not href or href in seen:
                continue
            seen.add(href)
            title_el = item.select_one("p a")
            title = title_el.get_text(strip=True) if title_el else href.split("/")[-1]
            full_url = href if href.startswith("http") else f"{BASE_URL}{href}"
            albums.append({"url": full_url, "title": title})

    print(f"  Found {len(albums)} albums")
    return albums


def get_album_images(album_url):
    """Get image URLs from an album detail page."""
    soup = fetch_page_with_flaresolverr(album_url) or fetch_page_with_cloudscraper(album_url) or fetch_page_with_playwright(album_url)
    if not soup:
        return []

    images = []
    # Skip ad containers
    for img in soup.select("div.mainleft img"):
        # Skip if inside ad container
        parent = img.parent
        skip = False
        while parent:
            if parent.name in ("p", "div") and parent.get("class"):
                classes = parent.get("class", [])
                if any(c in classes for c in ["sk", "sk-desktop", "sk-mobile"]):
                    skip = True
                    break
            parent = parent.parent
        if skip:
            continue

        # Use data-original for lazy-loaded images
        src = img.get("data-original") or img.get("data-src") or img.get("src", "")
        if not src or src.startswith("data:") or "/static/loading" in src:
            continue
        if not src.startswith("http"):
            src = f"{BASE_URL}{src}"
        if "avatar" in src or "icon" in src or "logo" in src:
            continue
        if src not in images:
            images.append(src)

    return images


def sanitize_filename(name, max_len=80):
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > max_len:
        name = name[:max_len]
    return name


def download_images(images, output_dir, album_name):
    """Download images, trying cloudscraper first, then Playwright."""
    album_dir = output_dir / sanitize_filename(album_name)
    album_dir.mkdir(parents=True, exist_ok=True)
    downloaded = 0

    # Phase 1: try cloudscraper for all images
    failed_urls = []
    for i, img_url in enumerate(images):
        ext = "jpg"
        if ".png" in img_url:
            ext = "png"
        elif ".webp" in img_url:
            ext = "webp"
        elif ".jpeg" in img_url:
            ext = "jpeg"
        filepath = album_dir / f"{i+1:04d}.{ext}"
        if filepath.exists():
            downloaded += 1
            continue

        success = False
        if _scraper:
            for attempt in range(MAX_RETRIES):
                try:
                    resp = _scraper.get(img_url, timeout=30)
                    if resp.status_code == 200 and len(resp.content) > 1000:
                        filepath.write_bytes(resp.content)
                        downloaded += 1
                        success = True
                        break
                except Exception:
                    pass
                time.sleep(1)

        if not success:
            failed_urls.append((i, img_url, filepath))
        time.sleep(DOWNLOAD_DELAY)

    # Phase 2: use Playwright for failed images
    if failed_urls:
        print(f"    [cloudscraper] {downloaded}/{len(images)} downloaded, using Playwright for {len(failed_urls)} remaining...")
        p, browser = _get_playwright_browser()
        try:
            page = browser.new_page()
            page.goto(BASE_URL, timeout=60000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=30000)
            except Exception:
                pass
            time.sleep(3)

            for i, img_url, filepath in failed_urls:
                for attempt in range(MAX_RETRIES):
                    try:
                        resp = page.goto(img_url, timeout=30000)
                        if resp and resp.status == 200:
                            body = resp.body()
                            filepath.write_bytes(body)
                            downloaded += 1
                            break
                    except Exception:
                        pass
                    time.sleep(1)
                time.sleep(DOWNLOAD_DELAY)
        finally:
            browser.close()
            p.stop()

    return downloaded


def main():
    parser = argparse.ArgumentParser(description="everiaclub.com album scraper")
    parser.add_argument("--keyword", default="白桃はな", help="Search keyword")
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

    albums = get_albums(args.keyword)
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
            send_feishu_summary(feishu_token, webhook, args.keyword, albums_info, total_images, total_downloaded, sample_images)
    else:
        print(f"\n  {'Album':<50} {'Imgs':>5}")
        print(f"  {'-'*55}")
        for name, imgs, _ in albums_info:
            print(f"  {name[:48]:<50} {imgs:>5}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
