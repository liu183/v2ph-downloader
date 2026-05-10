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
import base64
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


def send_feishu_album(token, chat_id, webhook_url, album_name, badge, dl_count, total, album_url, image_urls):
    if webhook_url:
        lines = [
            f"**标记:** {badge}",
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
        try:
            requests.post(webhook_url, json=payload, timeout=10)
        except Exception:
            pass
    if token and image_urls:
        time.sleep(0.3)
        send_feishu_images(token, chat_id, album_name, image_urls, max_show=3)


def send_feishu_summary(token, chat_id, webhook_url, model, albums_info, total_images, total_downloaded, sample_images=None):
    if webhook_url:
        lines = [f"**模型:** {model}"]
        lines.append(f"**来源:** xasiat.com")
        lines.append(f"**图集数:** {len(albums_info)} | **总图片:** {total_images} | **已下载:** {total_downloaded}")
        lines.append("")
        for i, (name, badge, imgs, dl) in enumerate(albums_info, 1):
            lines.append(f"{i}. 【{badge}】{name[:40]} — {dl}/{imgs}张")
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": f"✅ xasiat 下载完成: {model}"}, "template": "green"},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
            }
        }
        try:
            requests.post(webhook_url, json=payload, timeout=10)
        except Exception:
            pass
    if token and sample_images:
        time.sleep(0.3)
        send_feishu_images(token, chat_id, f"{model} 图集预览", sample_images, max_show=len(sample_images))


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

    feishu_app_id = os.environ.get("FEISHU_APP_ID", "")
    feishu_app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    feishu_chat_id = os.environ.get("FEISHU_CHAT_ID", "")
    feishu_token = _get_tenant_token(feishu_app_id, feishu_app_secret) if feishu_app_id else None

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

        if not args.list_only:
            send_feishu_album(feishu_token, feishu_chat_id, webhook, album["title"], album["photos_text"], dl_count, len(images), album["url"], images)

    print(f"\n{'='*60}")
    print(f"  Albums: {len(albums)} | Images found: {total_images}")
    if not args.list_only:
        print(f"  Downloaded: {total_downloaded}")
        print(f"  Output: {output.resolve()}")
        if webhook or feishu_token:
            send_feishu_summary(feishu_token, feishu_chat_id, webhook, args.model, albums_info, total_images, total_downloaded, sample_images)
    else:
        print(f"\n  {'Album':<50} {'Photos':>8} {'Imgs':>5}")
        print(f"  {'-'*65}")
        for name, badge, imgs, _ in albums_info:
            print(f"  {name[:48]:<50} {badge:>8} {imgs:>5}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
