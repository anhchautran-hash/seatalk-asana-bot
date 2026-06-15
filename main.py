"""
SeaTalk → Asana Bot
====================
Khi thành viên gõ trong group SeaTalk:
    !task [Section] Nội dung task

Bot sẽ tự động tạo task đó vào đúng section trong Asana project Partnership.
"""

import hashlib
import hmac
import json
import logging
import time
import re
import httpx
from fastapi import FastAPI, Header, HTTPException, Request

# ─── Cấu hình ────────────────────────────────────────────────────────────────

SEATALK_APP_ID     = "MzY4ODY3MDkyNzgw"
SEATALK_APP_SECRET = "g0d_-DJAQvRuL1QV8MXNies02WcU7K3U"

ASANA_TOKEN      = "2/1211043881249289/1215711068523662:623ea8a12e256d9c895e6a6de023ed29"
ASANA_PROJECT_ID = "1215522694635240"

SECTION_MAP = {
    "realme":   "",
    "lotte":    "",
    "ott":      "",
    "gyak":     "",
    "goodtime": "",
    "shopee":   "",
    "pnj":      "",
    "mixue":    "",
    "khác":     "",
    "khac":     "",
}

# ─── Setup ───────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="SeaTalk–Asana Bot")

_seatalk_token_cache = {"token": None, "expires_at": 0}


# ─── SeaTalk helpers ─────────────────────────────────────────────────────────

def get_seatalk_token() -> str:
    now = time.time()
    if _seatalk_token_cache["token"] and now < _seatalk_token_cache["expires_at"]:
        return _seatalk_token_cache["token"]

    resp = httpx.post(
        "https://openapi.seatalk.io/auth/app_access_token",
        json={"app_id": SEATALK_APP_ID, "app_secret": SEATALK_APP_SECRET},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data["app_access_token"]
    expires_in = data.get("expire", 7200)

    _seatalk_token_cache["token"] = token
    _seatalk_token_cache["expires_at"] = now + expires_in - 60

    log.info("SeaTalk token refreshed")
    return token


def verify_seatalk_signature(body: bytes, timestamp: str, signature: str) -> bool:
    message = timestamp + "\n" + body.decode("utf-8")
    expected = hmac.new(
        SEATALK_APP_SECRET.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def send_seatalk_group_message(group_id: str, text: str):
    token = get_seatalk_token()
    resp = httpx.post(
        "https://openapi.seatalk.io/messaging/v2/send_group_message",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "group_id": group_id,
            "message": {"tag": "text", "text": {"content": text}},
        },
        timeout=10,
    )
    if resp.status_code != 200:
        log.error("SeaTalk send failed: %s", resp.text)
    else:
        log.info("SeaTalk message sent to group %s", group_id)


# ─── Asana helpers ───────────────────────────────────────────────────────────

def get_asana_sections() -> dict:
    resp = httpx.get(
        f"https://app.asana.com/api/1.0/projects/{ASANA_PROJECT_ID}/sections",
        headers={"Authorization": f"Bearer {ASANA_TOKEN}"},
        timeout=10,
    )
    resp.raise_for_status()
    sections = {}
    for s in resp.json()["data"]:
        sections[s["name"].lower()] = s["gid"]
    log.info("Loaded sections: %s", list(sections.keys()))
    return sections


def create_asana_task(task_name: str, section_gid: str | None) -> dict:
    payload = {
        "data": {
            "name": task_name,
            "projects": [ASANA_PROJECT_ID],
        }
    }
    resp = httpx.post(
        "https://app.asana.com/api/1.0/tasks",
        headers={"Authorization": f"Bearer {ASANA_TOKEN}"},
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    task = resp.json()["data"]

    if section_gid:
        move_resp = httpx.post(
            f"https://app.asana.com/api/1.0/sections/{section_gid}/addTask",
            headers={"Authorization": f"Bearer {ASANA_TOKEN}"},
            json={"data": {"task": task["gid"]}},
            timeout=10,
        )
        log.info("Move to section result: %s", move_resp.status_code)

    return task


# ─── Parse tin nhắn ──────────────────────────────────────────────────────────

def parse_task_command(text: str) -> tuple[str | None, str | None]:
    text = text.strip()
    # Bỏ tất cả @mention ở đầu (có thể nhiều mention)
    text = re.sub(r'^(@\S+\s*)+', '', text).strip()

    if not text.lower().startswith("!task"):
        return None, None

    content = text[5:].strip()

    if content.startswith("["):
        end = content.find("]")
        if end != -1:
            section = content[1:end].strip().lower()
            task_name = content[end + 1:].strip()
            return section, task_name

    return "khác", content


# ─── Webhook endpoint ─────────────────────────────────────────────────────────

# Tất cả event type SeaTalk có thể gửi khi có tin nhắn
MESSAGE_EVENT_TYPES = {
    "receive_message",
    "new_mentioned_message_received_from_group_chat",
    "group_message",
    "message",
    "bot_mentioned",
    "mention",
}


def safe_str(val) -> str:
    """Đảm bảo giá trị là string thuần, không phải dict/list."""
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        # Thử các key phổ biến trong object text
        for k in ("text", "content", "plain_text", "value"):
            if isinstance(val.get(k), str):
                return val[k]
        return json.dumps(val, ensure_ascii=False)
    return str(val) if val else ""


def extract_text_and_group(event: dict) -> tuple[str, str]:
    """Trích xuất text và group_id từ event — thử nhiều cấu trúc JSON khác nhau."""
    message = event.get("message", {})

    # Text — mỗi field đều qua safe_str để tránh dict
    text = (
        safe_str(message.get("plain_text"))
        or safe_str(message.get("content", {}).get("text") if isinstance(message.get("content"), dict) else message.get("content"))
        or safe_str(message.get("text"))
        or safe_str(event.get("text"))
        or ""
    )

    # Group ID
    group = event.get("group", {})
    if not isinstance(group, dict):
        group = {}
    group_id = (
        safe_str(group.get("group_id"))
        or safe_str(group.get("id"))
        or safe_str(event.get("group_id"))
        or safe_str(event.get("chat", {}).get("id") if isinstance(event.get("chat"), dict) else None)
        or ""
    )

    log.info("extract_text_and_group → text=%r, group_id=%r", text, group_id)
    return text, group_id


@app.post("/webhook")
async def seatalk_webhook(
    request: Request,
    x_seatalk_timestamp: str = Header(None),
    x_seatalk_signature: str = Header(None),
):
    body = await request.body()
    payload = json.loads(body)

    # LOG TOÀN BỘ PAYLOAD để debug
    log.info("=== WEBHOOK RECEIVED ===")
    log.info("Payload: %s", json.dumps(payload, ensure_ascii=False, indent=2))

    # Challenge verification
    if payload.get("event_type") == "event_verification":
        challenge = payload.get("event", {}).get("seatalk_challenge", "")
        log.info("Challenge response: %s", challenge)
        return {"seatalk_challenge": challenge}
    if "seatalk_challenge" in payload:
        return {"seatalk_challenge": payload["seatalk_challenge"]}
    if "challenge" in payload:
        return {"challenge": payload["challenge"]}

    event_type = payload.get("event_type", "")
    event = payload.get("event", {})

    log.info("Event type: [%s]", event_type)

    # Xử lý tất cả event type liên quan đến message
    # Nếu event_type không rõ, vẫn thử parse nếu có text
    text, group_id = extract_text_and_group(event)

    # Fallback: nếu event_type không trong danh sách biết,
    # vẫn thử xử lý nếu tìm thấy text có !task
    is_known_message_event = event_type in MESSAGE_EVENT_TYPES
    has_task_command = "!task" in text.lower()

    if not is_known_message_event and not has_task_command:
        log.info("Skipping event_type=%s (no !task found)", event_type)
        return {"ok": True}

    sender_name = event.get("sender", {}).get("name", "Thành viên")

    log.info("Text: [%s] | Group: [%s] | Sender: [%s]", text, group_id, sender_name)

    if not text or not group_id:
        log.warning("Missing text or group_id — skipping")
        return {"ok": True}

    section_key, task_name = parse_task_command(text)

    if task_name is None:
        log.info("Not a !task command, ignoring")
        return {"ok": True}

    if not task_name:
        send_seatalk_group_message(group_id, "⚠️ Vui lòng nhập tên task. Ví dụ: !task [Lotte] Tên task")
        return {"ok": True}

    log.info("Parsed: section=%s, task=%s", section_key, task_name)

    try:
        live_sections = get_asana_sections()
        section_gid = live_sections.get(section_key)
        section_display = section_key.capitalize() if section_key else "Khác"

        if not section_gid:
            log.warning("Section [%s] not found, falling back to Khác", section_key)
            section_gid = live_sections.get("khác") or live_sections.get("khac")
            section_display = "Khác"

        task = create_asana_task(task_name, section_gid)
        task_url = f"https://app.asana.com/0/{ASANA_PROJECT_ID}/{task['gid']}"

        msg = (
            f"✅ Task đã được tạo!\n"
            f"📌 {task_name}\n"
            f"📂 Section: {section_display}\n"
            f"👤 Tạo bởi: {sender_name}\n"
            f"🔗 {task_url}"
        )
        send_seatalk_group_message(group_id, msg)
        log.info("Task created: %s in section %s", task_name, section_display)

    except Exception as e:
        log.error("Error creating task: %s", e, exc_info=True)
        send_seatalk_group_message(group_id, f"❌ Lỗi khi tạo task: {str(e)}")

    return {"ok": True}


# ─── Health check ─────────────────────────────────────────────────────────────

@app.get("/")
def health():
    return {"status": "ok", "bot": "SeaTalk–Asana Bot"}
