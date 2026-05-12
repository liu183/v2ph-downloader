"""
Send downloaded images to Feishu via webhook.

Usage:
    python send_feishu.py --input DIR [--webhook URL] [--title TITLE]

Reads images from a directory and sends them to Feishu as card messages.
Supports batch sending with rate limiting.
"""

import argparse
import io
import os
import sys
import time
import requests
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

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
        print(f"  [Feishu] Token error: {data}")
    except Exception as e:
        print(f"  [Feishu] Token exception: {e}")
    return None


def _feishu_upload_image(token, image_path):
    try:
        with open(image_path, "rb") as f:
            content = f.read()
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/images",
            headers={"Authorization": f"Bearer {token}"},
            data={"image_type": "message"},
            files={"image": (os.path.basename(image_path), content, "image/jpeg")},
            timeout=30)
        data = resp.json()
        if data.get("code") == 0:
            return data["data"]["image_key"]
        print(f"  [Feishu] Upload error: {data}")
    except Exception as e:
        print(f"  [Feishu] Upload exception: {e}")
    return None


def _feishu_post(webhook_url, payload):
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == 0:
                return True
            print(f"  [Feishu] Error: {data}")
        else:
            print(f"  [Feishu] HTTP {resp.status_code}")
    except Exception as e:
        print(f"  [Feishu] Exception: {e}")
    return False


def send_album_images(token, webhook_url, title, image_paths):
    if not webhook_url or not image_paths:
        return
    total = len(image_paths)
    batch_size = 10
    sent = 0
    for batch_start in range(0, total, batch_size):
        batch = image_paths[batch_start:batch_start + batch_size]
        elements = []
        for i, img_path in enumerate(batch):
            idx = batch_start + i + 1
            if token:
                image_key = _feishu_upload_image(token, img_path)
                if image_key:
                    elements.append({"tag": "img", "img_key": image_key, "alt": {"tag": "plain_text", "content": f"Image {idx}"}})
                    sent += 1
                    print(f"  [Feishu] Uploaded {idx}/{total}: {os.path.basename(img_path)}")
                    continue
            # Fallback: just mention the filename
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"Image {idx}: {os.path.basename(img_path)}"}})
        if elements:
            batch_title = f"{title} ({batch_start+1}-{min(batch_start+batch_size, total)}/{total})" if total > batch_size else title
            payload = {
                "msg_type": "interactive",
                "card": {
                    "header": {"title": {"tag": "plain_text", "content": batch_title}, "template": "blue"},
                    "elements": elements,
                }
            }
            _feishu_post(webhook_url, payload)
            time.sleep(1)  # Rate limiting between batches
    print(f"  [Feishu] Sent {sent}/{total} images: {title}")


def send_album_summary(webhook_url, title, total_images, source=""):
    if not webhook_url:
        return
    lines = [f"**Images:** {total_images}"]
    if source:
        lines.append(f"**Source:** {source}")
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "green"},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
        }
    }
    _feishu_post(webhook_url, payload)


def find_images(directory):
    """Find all image files in directory, sorted by name."""
    exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    images = []
    for f in sorted(Path(directory).iterdir()):
        if f.suffix.lower() in exts:
            images.append(str(f))
    return images


def main():
    parser = argparse.ArgumentParser(description="Send images to Feishu")
    parser.add_argument("--input", required=True, help="Input directory with images")
    parser.add_argument("--title", default="", help="Album title")
    parser.add_argument("--source", default="", help="Source site name")
    parser.add_argument("--webhook", default=os.environ.get("FEISHU_WEBHOOK", ""),
                        help="Feishu webhook URL")
    parser.add_argument("--batch-size", type=int, default=10, help="Images per batch")
    args = parser.parse_args()

    webhook = args.webhook
    if not webhook:
        print("Error: No webhook URL provided")
        sys.exit(1)

    input_dir = Path(args.input)
    if not input_dir.exists():
        print(f"Error: Directory not found: {input_dir}")
        sys.exit(1)

    feishu_app_id = os.environ.get("FEISHU_APP_ID", "")
    feishu_app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    feishu_token = _get_tenant_token(feishu_app_id, feishu_app_secret) if feishu_app_id else None

    # Find all album subdirectories
    album_dirs = sorted([d for d in input_dir.iterdir() if d.is_dir()])
    if not album_dirs:
        # Maybe images are directly in the input dir
        images = find_images(input_dir)
        if images:
            title = args.title or input_dir.name
            print(f"Sending {len(images)} images: {title}")
            send_album_summary(webhook, title, len(images), args.source)
            time.sleep(0.5)
            send_album_images(feishu_token, webhook, title, images)
        else:
            print("No images found")
        return

    total_sent = 0
    for album_dir in album_dirs:
        images = find_images(album_dir)
        if not images:
            continue
        title = args.title or album_dir.name
        print(f"\nSending album: {title} ({len(images)} images)")
        send_album_summary(webhook, title, len(images), args.source)
        time.sleep(0.5)
        send_album_images(feishu_token, webhook, title, images)
        total_sent += len(images)
        time.sleep(1)  # Rate limiting between albums

    print(f"\nTotal: {total_sent} images sent across {len(album_dirs)} albums")


if __name__ == "__main__":
    main()
