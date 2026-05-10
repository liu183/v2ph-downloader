"""
geinou-nude.com album scraper - downloads all photos from a model's page.

Usage:
    python scraper_geinou_nude.py [--model MODEL] [--output DIR]

Examples:
    python scraper_geinou_nude.py --model 白桃はな
    python scraper_geinou_nude.py --model 白桃はな --output ./downloads

WordPress site. Images from wp-content/uploads. No auth required. No Cloudflare.
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

BASE_URL = "https://geinou-nude.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}
REQUEST_DELAY = 0.3
DOWNLOAD_DELAY = 0.15
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
    except Exception:
        pass
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


def send_feishu_summary(token, webhook_url, model, total_images, total_downloaded, sample_images=None):
    if not webhook_url:
        return
    lines = [f"**模特:** {model}"]
    lines.append(f"**来源:** geinou-nude.com")
    lines.append(f"**总图片:** {total_images} | **已下载:** {total_downloaded}")
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": f"✅ geinou-nude 下载完成: {model}"}, "template": "green"},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
        }
    }
    _feishu_post(webhook_url, payload)
    if token and sample_images:
        time.sleep(0.3)
        send_feishu_images(token, webhook_url, f"{model} 图集预览", sample_images)


# ── Scraping ────────────────────────────────────────────────────

def get_model_images(model):
    """Get all image URLs from the model's page."""
    encoded = quote(model)
    url = f"{BASE_URL}/{encoded}/"
    print(f"[1/3] Fetching model page: {url}")
    soup = fetch_page(url)
    if not soup:
        print("Failed to access model page!")
        return []

    # Find the main content area (before related posts)
    # The main article content is in the first article/post
    article = soup.select_one("article, .post, .entry-content, .post-content")
    if not article:
        article = soup  # fallback to whole page

    images = []
    seen = set()
    for img in article.select("img"):
        src = img.get("data-src") or img.get("data-lazy-src") or img.get("src", "")
        if not src or src.startswith("data:"):
            continue
        if "wp-content/uploads" not in src:
            continue
        if any(x in src for x in ["loading", "icon", "logo", "avatar", "antenna-logo"]):
            continue
        # Get full-size URL by removing size suffix
        full_src = re.sub(r"-\d+x\d+(?=\.\w+$)", "", src)
        if full_src not in seen:
            seen.add(full_src)
            images.append(full_src)

    print(f"  Found {len(images)} images")
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
    parser = argparse.ArgumentParser(description="geinou-nude.com album scraper")
    parser.add_argument("--model", default="白桃はな", help="Model name (from URL)")
    parser.add_argument("--output", default="./downloads", help="Output directory")
    parser.add_argument("--list-only", action="store_true", help="Only list images")
    parser.add_argument("--webhook", default=os.environ.get("FEISHU_WEBHOOK", ""),
                        help="Feishu webhook URL (or set FEISHU_WEBHOOK env var)")
    args = parser.parse_args()

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    webhook = args.webhook

    feishu_app_id = os.environ.get("FEISHU_APP_ID", "")
    feishu_app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    feishu_token = _get_tenant_token(feishu_app_id, feishu_app_secret) if feishu_app_id else None

    images = get_model_images(args.model)
    if not images:
        sys.exit(1)

    print(f"\n[2/3] Total unique images: {len(images)}")

    if args.list_only:
        print(f"\n  Images: {len(images)}")
        print(f"  First: {images[0][:80]}")
        print(f"  Last:  {images[-1][:80]}")
        return

    dl_count = download_images(images, output, f"geinou-nude-{args.model}")
    print(f"\n[3/3] Downloaded: {dl_count}/{len(images)}")
    print(f"  Output: {output.resolve()}")

    if webhook:
        encoded = quote(args.model)
        send_feishu_album(feishu_token, webhook, f"geinou-nude-{args.model}", dl_count, len(images),
                          f"{BASE_URL}/{encoded}/", images[:10])
        send_feishu_summary(feishu_token, webhook, args.model, len(images), dl_count, images[:3])


if __name__ == "__main__":
    main()
