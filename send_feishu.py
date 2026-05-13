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


SENT_STATE_FILE = Path(__file__).parent / ".feishu_sent_state.json"


def _load_sent_state():
    if SENT_STATE_FILE.exists():
        try:
            import json
            with open(SENT_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {}


def _save_sent_state(state):
    import json
    with open(SENT_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def filter_unsent_images(images, site_key):
    """Filter out images that have already been sent. Returns (unsent_list, total_sent_count)."""
    state = _load_sent_state()
    sent_set = set(state.get(site_key, []))
    unsent = [img for img in images if os.path.basename(img) not in sent_set]
    return unsent, len(sent_set)


def mark_images_sent(images, site_key):
    """Mark images as sent in the state file."""
    state = _load_sent_state()
    sent_list = state.get(site_key, [])
    for img in images:
        name = os.path.basename(img)
        if name not in sent_list:
            sent_list.append(name)
    state[site_key] = sent_list
    _save_sent_state(state)


def main():
    parser = argparse.ArgumentParser(description="Send images to Feishu")
    parser.add_argument("--input", required=True, help="Input directory with images")
    parser.add_argument("--title", default="", help="Album title")
    parser.add_argument("--source", default="", help="Source site name")
    parser.add_argument("--webhook", default=os.environ.get("FEISHU_WEBHOOK", ""),
                        help="Feishu webhook URL")
    parser.add_argument("--batch-size", type=int, default=10, help="Images per batch")
    parser.add_argument("--max-images", type=int, default=100, help="Max images to send per run")
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

    max_images = args.max_images
    total_sent = 0
    site_key = args.source or input_dir.name

    # Find all album subdirectories
    album_dirs = sorted([d for d in input_dir.iterdir() if d.is_dir()])
    if not album_dirs:
        # Maybe images are directly in the input dir
        all_images = find_images(input_dir)
        if all_images:
            unsent, already = filter_unsent_images(all_images, site_key)
            to_send = unsent[:max_images]
            if not to_send:
                print(f"All {len(all_images)} images already sent, skipping")
                return
            title = args.title or input_dir.name
            print(f"Sending {len(to_send)} images (unsent: {len(unsent)}, already sent: {already}): {title}")
            send_album_summary(webhook, title, len(all_images), args.source)
            time.sleep(0.5)
            send_album_images(feishu_token, webhook, title, to_send)
            mark_images_sent(to_send, site_key)
            total_sent += len(to_send)
        else:
            print("No images found")
        print(f"\nTotal: {total_sent} images sent this run (limit: {max_images})")
        return

    for album_dir in album_dirs:
        if total_sent >= max_images:
            break
        all_images = find_images(album_dir)
        if not all_images:
            continue
        unsent, already = filter_unsent_images(all_images, site_key + "/" + album_dir.name)
        if not unsent:
            print(f"Album {album_dir.name}: all {len(all_images)} already sent, skipping")
            continue
        remaining = max_images - total_sent
        to_send = unsent[:remaining]
        title = args.title or album_dir.name
        print(f"\nSending album: {title} ({len(to_send)}/{len(unsent)} unsent images)")
        send_album_summary(webhook, title, len(all_images), args.source)
        time.sleep(0.5)
        send_album_images(feishu_token, webhook, title, to_send)
        mark_images_sent(to_send, site_key + "/" + album_dir.name)
        total_sent += len(to_send)
        time.sleep(1)  # Rate limiting between albums

    print(f"\nTotal: {total_sent} images sent this run (limit: {max_images})")


if __name__ == "__main__":
    main()
