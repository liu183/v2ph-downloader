"""
v2ph.com album scraper - downloads all photos from an actor's page.

Usage:
    python scraper.py [--actor ACTOR_SLUG] [--cookies COOKIE_STRING] [--output DIR]

Examples:
    python scraper.py --actor Shirato-Hana
    python scraper.py --actor Shirato-Hana --cookies "frontend=abc123; cf_clearance=xyz789"

Page structure:
    Actor page -> N album pages (each album has paginated photos)
    Album page 1 is public; pages 2+ require login cookies.
"""

import argparse
import base64
import io
import json
import os
import re
import sys
import time
import requests
from pathlib import Path

# Fix Windows console encoding for CJK characters
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Installing beautifulsoup4...")
    os.system(f"{sys.executable} -m pip install beautifulsoup4 requests")
    from bs4 import BeautifulSoup


DEFAULT_ACTOR = "Shirato-Hana"
BASE_URL = "https://www.v2ph.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": BASE_URL,
}
REQUEST_DELAY = 1.0
DOWNLOAD_DELAY = 0.3
MAX_RETRIES = 3


def create_session(cookies_str=None):
    session = requests.Session()
    session.headers.update(HEADERS)
    if cookies_str:
        for pair in cookies_str.split(";"):
            pair = pair.strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                session.cookies.set(k.strip(), v.strip())
    return session


def fetch_page(session, url, retries=MAX_RETRIES):
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                return BeautifulSoup(resp.text, "html.parser")
            if resp.status_code == 403:
                return None
            print(f"  [{resp.status_code}] Retrying {url}...")
        except requests.RequestException as e:
            print(f"  [Error] {e}, retrying...")
        time.sleep(REQUEST_DELAY * (attempt + 1))
    return None


# ── Feishu Open API (image upload + send) ───────────────────────

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


def _feishu_send(token, chat_id, msg_type, content, retry_auth=True):
    try:
        resp = requests.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"receive_id": chat_id, "msg_type": msg_type, "content": json.dumps(content)},
            timeout=15)
        data = resp.json()
        if data.get("code") == 0:
            return True
        if data.get("code") == 99991663 and retry_auth:
            _feishu_token_cache["token"] = None
            new_token = _get_tenant_token(os.environ.get("FEISHU_APP_ID", ""), os.environ.get("FEISHU_APP_SECRET", ""))
            if new_token:
                return _feishu_send(new_token, chat_id, msg_type, content, retry_auth=False)
        print(f"    [Feishu] Send error: {data}")
    except Exception as e:
        print(f"    [Feishu] Send exception: {e}")
    return False


def send_feishu_images(token, chat_id, title, image_urls, max_show=3):
    if not token or not chat_id or not image_urls:
        return
    content = [[{"tag": "text", "text": f"📷 {title}（共{len(image_urls)}张，展示前{min(max_show, len(image_urls))}张）\n"}]]
    for i, img_url in enumerate(image_urls[:max_show]):
        image_key = _feishu_upload_image(token, img_url)
        if image_key:
            content.append([{"tag": "img", "image_key": image_key, "width": 800, "height": 600}])
            print(f"    [Feishu] Uploaded image {i+1}/{min(max_show, len(image_urls))}")
        else:
            content.append([{"tag": "text", "text": f"图片{i+1}上传失败: {img_url}\n"}])
        time.sleep(0.3)
    post = {"zh_cn": {"title": f"🖼️ {title}", "content": content}}
    if _feishu_send(token, chat_id, "post", post):
        print(f"    [Feishu] Images sent: {title}")


def send_feishu_album(token, chat_id, webhook_url, album_name, badge, dl_count, total, max_page, album_url, image_urls):
    # Card via webhook (text details)
    if webhook_url:
        pages_info = f"{max_page}页" if max_page > 1 else "1页"
        lines = [
            f"**标记:** {badge}",
            f"**图片数:** {dl_count}/{total}",
            f"**页数:** {pages_info}",
            f"**链接:** [查看图集]({album_url})",
        ]
        if dl_count < total and max_page > 1:
            lines.append("\n⚠️ 第2页起需登录，仅下载了第1页")
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": album_name}, "template": "blue"},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
            }
        }
        try:
            requests.post(webhook_url, json=payload, timeout=10)
        except Exception:
            pass
    # Images via Open API
    if token and image_urls:
        time.sleep(0.3)
        send_feishu_images(token, chat_id, album_name, image_urls, max_show=3)


def send_feishu_summary(token, chat_id, webhook_url, actor, albums_info, total_images, total_downloaded, sample_images=None):
    # Summary card via webhook
    if webhook_url:
        lines = [f"**模特:** {actor}"]
        lines.append(f"**图集数:** {len(albums_info)} | **总图片:** {total_images} | **已下载:** {total_downloaded}")
        lines.append("")
        for i, (name, badge, imgs, pages, dl) in enumerate(albums_info, 1):
            lines.append(f"{i}. 【{badge}】{name[:40]} — {dl}/{imgs}张")
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": f"✅ 下载完成: {actor}"}, "template": "green"},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
            }
        }
        try:
            requests.post(webhook_url, json=payload, timeout=10)
        except Exception:
            pass
    # Preview gallery via Open API
    if token and sample_images:
        time.sleep(0.3)
        send_feishu_images(token, chat_id, f"{actor} 图集预览", sample_images, max_show=len(sample_images))


# ── Scraping ────────────────────────────────────────────────────

def check_cookies(session, webhook_url=""):
    """Check if login cookies are still valid by accessing a page that requires auth."""
    print("[0/3] Checking cookie validity...")
    resp = session.get(f"{BASE_URL}/user/index", timeout=30, allow_redirects=False)
    # If redirected to login page, cookies are expired
    if resp.status_code in (301, 302) and "login" in resp.headers.get("Location", ""):
        msg = "登录 cookie 已失效！请重新登录 v2ph.com 并更新 GitHub Secret: V2PH_COOKIES"
        print(f"  [EXPIRED] {msg}")
        if webhook_url:
            send_feishu_card(webhook_url, "Cookie 已失效", [
                "⚠️ **V2PH_COOKIES 已过期**",
                "",
                "请重新登录并更新 cookie：",
                "1. 浏览器登录 v2ph.com",
                "2. F12 → Application → Cookies → 复制全部 cookie",
                f"3. 更新 GitHub Secret: `gh secret set V2PH_COOKIES`",
            ])
        return False
    # Check if we can access the account page content
    soup = BeautifulSoup(resp.text, "html.parser")
    if soup.select_one('a[href*="/user/index"]'):
        print("  [OK] Cookies are valid")
        return True
    # Ambiguous - try an album page 2 to confirm
    print("  [WARN] Could not confirm, continuing...")
    return True


def get_albums_from_actor(session, actor_slug):
    """Get album page URLs and their badge counts from the actor page."""
    url = f"{BASE_URL}/actor/{actor_slug}"
    print(f"[1/3] Fetching actor page: {url}")
    soup = fetch_page(session, url)
    if not soup:
        print("Failed to access actor page!")
        return []

    al = soup.select_one(".albums-list")
    if not al:
        print("No .albums-list found on actor page!")
        return []

    albums = []
    seen = set()
    for child in al.find_all("div", recursive=False):
        link = child.select_one('a[href*="/album/"]')
        badge_el = child.select_one(".badge")
        title_el = child.select_one(".media-meta, .card-body")
        img_el = child.select_one("img[data-src]")

        if not link:
            continue

        href = link.get("href", "").split("?")[0]
        if href in seen:
            continue
        seen.add(href)

        badge = badge_el.get_text(strip=True) if badge_el else ""
        title = title_el.get_text(strip=True) if title_el else ""
        cover = img_el.get("data-src", "") if img_el else ""
        albums.append({"path": href, "badge": badge, "title": title, "cover": cover})

    print(f"  Found {len(albums)} albums")
    return albums


def get_album_images(session, album_path, use_pagination=False):
    """Get image URLs from an album page (page 1 always, more if use_pagination)."""
    url = f"{BASE_URL}{album_path}"
    soup = fetch_page(session, url)
    if not soup:
        return [], "", 0

    title_el = soup.select_one("h1")
    title = title_el.get_text(strip=True) if title_el else ""

    images = []
    for img in soup.select("img[data-src]"):
        src = img.get("data-src", "")
        if "cdn.v2ph.com" in src:
            images.append(src)

    max_page = 1
    for a in soup.select('a[href*="page="]'):
        m = re.search(r"page=(\d+)", a.get("href", ""))
        if m:
            max_page = max(max_page, int(m.group(1)))

    if use_pagination and max_page > 1:
        for page in range(2, max_page + 1):
            time.sleep(REQUEST_DELAY)
            page_soup = fetch_page(session, f"{url}?page={page}")
            if not page_soup:
                print(f"    Page {page}/{max_page}: blocked (login required?)")
                break
            for img in page_soup.select("img[data-src]"):
                src = img.get("data-src", "")
                if "cdn.v2ph.com" in src and src not in images:
                    images.append(src)

    return images, title, max_page


def sanitize_filename(name, max_len=80):
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > max_len:
        name = name[:max_len]
    return name


def download_images(session, images, output_dir, album_name):
    """Download all images for an album into a subfolder."""
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
                resp = session.get(img_url, timeout=30)
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
    parser = argparse.ArgumentParser(description="v2ph album scraper")
    parser.add_argument("--actor", default=DEFAULT_ACTOR, help="Actor slug from URL")
    parser.add_argument("--cookies", default=os.environ.get("V2PH_COOKIES", ""),
                        help="Cookie string (or set V2PH_COOKIES env var)")
    parser.add_argument("--output", default="./downloads", help="Output directory")
    parser.add_argument("--full", action="store_true", help="Fetch all pages (requires cookies)")
    parser.add_argument("--list-only", action="store_true", help="Only list albums, don't download")
    parser.add_argument("--webhook", default=os.environ.get("FEISHU_WEBHOOK", ""),
                        help="Feishu webhook URL (or set FEISHU_WEBHOOK env var)")
    args = parser.parse_args()

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    webhook = args.webhook

    # Feishu Open API token for image sending
    feishu_app_id = os.environ.get("FEISHU_APP_ID", "")
    feishu_app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    feishu_chat_id = os.environ.get("FEISHU_CHAT_ID", "")
    feishu_token = _get_tenant_token(feishu_app_id, feishu_app_secret) if feishu_app_id else None

    session = create_session(args.cookies)
    session.get(BASE_URL, timeout=30)
    time.sleep(1)

    # Validate cookies before proceeding
    if args.cookies:
        check_cookies(session, webhook)

    # Step 1: Get albums
    albums = get_albums_from_actor(session, args.actor)
    if not albums:
        sys.exit(1)

    # Step 2: Process each album
    print(f"\n[2/3] Processing {len(albums)} albums...")
    total_downloaded = 0
    total_images = 0
    albums_info = []
    sample_images = []  # one cover per album for summary preview

    for i, album in enumerate(albums):
        time.sleep(REQUEST_DELAY)
        images, title, max_page = get_album_images(
            session, album["path"], use_pagination=args.full
        )

        album_name = title or album["path"].split("/")[-1].replace(".html", "")
        total_images += len(images)

        pages_info = f"{max_page}p" if max_page > 1 else "1p"
        print(f"\n  [{i+1}/{len(albums)}] {album['badge']:>5s} | {len(images):>3d} imgs | {pages_info}")
        print(f"    {album_name[:70]}")

        if args.list_only:
            albums_info.append((album_name, album["badge"], len(images), max_page, 0))
            continue

        # Download
        dl_count = 0
        if images:
            dl_count = download_images(session, images, output, album_name)
            total_downloaded += dl_count
            print(f"    Downloaded: {dl_count}/{len(images)}")
            sample_images.append(images[0])  # first image as album cover
        else:
            print(f"    No images (may need --cookies for this album)")

        albums_info.append((album_name, album["badge"], len(images), max_page, dl_count))

        # Send Feishu notification per album: card + real images
        if not args.list_only:
            album_url = f"{BASE_URL}{album['path']}"
            send_feishu_album(
                feishu_token, feishu_chat_id, webhook,
                album_name, album["badge"],
                dl_count, len(images), max_page, album_url, images
            )

    # Step 3: Summary
    print(f"\n{'='*60}")
    print(f"  Albums: {len(albums)} | Images found: {total_images}")
    if not args.list_only:
        print(f"  Downloaded: {total_downloaded}")
        print(f"  Output: {output.resolve()}")

        # Send summary to Feishu with preview gallery
        if webhook or feishu_token:
            send_feishu_summary(feishu_token, feishu_chat_id, webhook, args.actor, albums_info, total_images, total_downloaded, sample_images)
    else:
        print(f"\n  {'Album':<55} {'Badge':>6} {'Imgs':>5} {'Pages':>6}")
        print(f"  {'-'*75}")
        for name, badge, imgs, pages, _ in albums_info:
            print(f"  {name[:53]:<55} {badge:>6} {imgs:>5} {pages:>6}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
