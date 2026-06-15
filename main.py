"""
SeaTalk → Asana Bot
====================
Khi thành viên gõ trong group SeaTalk:
    !task [Section] Nội dung task

Bot sẽ tự động tạo task đó vào đúng section trong Asana project Partnership.

Ví dụ:
    !task [Lotte] Hoàn thiện hợp đồng tháng 7
    !task [realme] Follow up testing timeline
    !task [Khác] Nghiên cứu đối tác mới
"""

import hashlib
import hmac
import json
import logging
import time

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

# ─── Cấu hình ────────────────────────────────────────────────────────────────

SEATALK_APP_ID     = "MzY4ODY3MDkyNzgw"
SEATALK_APP_SECRET = "g0d_-DJAQvRuL1QV8MXNies02WcU7K3U"

ASANA_TOKEN      = "2/1211043881249289/1215711068523662:623ea8a12e256d9c895e6a6de023ed29"
ASANA_PROJECT_ID = "1215522694635240"

# Map tên section (gõ trong chat) → Asana section GID
# Bạn cần điền GID thực tế — xem hướng dẫn bên dưới
SECTION_MAP = {
    "realme":   "",   # điền GID section realme
    "lotte":    "",   # điền GID section Lotte
    "ott":      "",   # điền GID section OTT
    "gyak":     "",   # điền GID section Gyak
    "goodtime": "",   # điền GID section Goodtime
    "shopee":   "",   # điền GID section Shopee
    "pnj":      "",   # điền GID section PNJ
    "mixue":    "",   # điền GID section Mixue
    "khác":     "",   # điền GID section Khác
    "khac":     "",   # alias không dấu
}

# ─── Setup ───────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="SeaTalk–Asana Bot")

# Cache token SeaTalk (hết hạn sau ~2h)
_seatalk_token_cache = {"token": None, "expires_at": 0}


# ─── SeaTalk helpers ─────────────────────────────────────────────────────────

def get_seatalk_token() -> str:
    """Lấy access token từ SeaTalk (có cache)."""
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
    _seatalk_token_cache["expires_at"] = now + expires_in - 60  # trừ 1 phút buffer

    log.info("SeaTalk token refreshed")
    return token


def verify_seatalk_signature(body: bytes, timestamp: str, signature: str) -> bool:
    """Xác thực request đến từ SeaTalk (không phải giả mạo)."""
    message = timestamp + "\n" + body.decode("utf-8")
    expected = hmac.new(
        SEATALK_APP_SECRET.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def send_seatalk_group_message(group_id: str, text: str):
    """Gửi tin nhắn vào group SeaTalk."""
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


# ─── Asana helpers ───────────────────────────────────────────────────────────

def get_asana_sections() -> dict:
    """Lấy tất cả sections của project và trả về dict {tên_lower: gid}."""
    resp = httpx.get(
        f"https://app.asana.com/api/1.0/projects/{ASANA_PROJECT_ID}/sections",
        headers={"Authorization": f"Bearer {ASANA_TOKEN}"},
        timeout=10,
    )
    resp.raise_for_status()
    sections = {}
    for s in resp.json()["data"]:
        sections[s["name"].lower()] = s["gid"]
    return sections


def create_asana_task(task_name: str, section_gid: str | None) -> dict:
    """Tạo task mới trong Asana."""
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

    # Move vào đúng section nếu có
    if section_gid:
        httpx.post(
            f"https://app.asana.com/api/1.0/sections/{section_gid}/addTask",
            headers={"Authorization": f"Bearer {ASANA_TOKEN}"},
            json={"data": {"task": task["gid"]}},
            timeout=10,
        )

    return task


# ─── Parse tin nhắn ──────────────────────────────────────────────────────────

def parse_task_command(text: str) -> tuple[str | None, str | None]:
    """
    Parse lệnh !task từ tin nhắn.
    Trả về (section_name, task_name) hoặc (None, None) nếu không phải lệnh task.

    Ví dụ:
        "!task [Lotte] Hoàn thiện hợp đồng" → ("lotte", "Hoàn thiện hợp đồng")
        "!task Nghiên cứu đối tác"           → (None, "Nghiên cứu đối tác")  → vào Khác
        "xin chào mọi người"                  → (None, None)  → bỏ qua
    """
    text = text.strip()
    if not text.lower().startswith("!task"):
        return None, None

    content = text[5:].strip()  # bỏ "!task"

    # Có [Section] không?
    if content.startswith("["):
        end = content.find("]")
        if end != -1:
            section = content[1:end].strip().lower()
            task_name = content[end + 1:].strip()
            return section, task_name

    # Không có section → gán vào "Khác"
    return "khác", content


# ─── Webhook endpoint ─────────────────────────────────────────────────────────

@app.post("/webhook")
async def seatalk_webhook(
    request: Request,
    x_seatalk_timestamp: str = Header(None),
    x_seatalk_signature: str = Header(None),
):
    body = await request.body()

    # Xác thực chữ ký (bỏ comment dòng dưới khi production)
    # if not verify_seatalk_signature(body, x_seatalk_timestamp, x_seatalk_signature):
    #     raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(body)
    log.info("Received: %s", json.dumps(payload, ensure_ascii=False))

    # SeaTalk gửi challenge khi verify URL — phải trả về ngay
    if "seatalk_challenge" in payload:
        return {"seatalk_challenge": payload["seatalk_challenge"]}
    if "challenge" in payload:
        return {"challenge": payload["challenge"]}

    # Xử lý tin nhắn
    event = payload.get("event", {})
    event_type = event.get("type")

    if event_type != "receive_message":
        return {"ok": True}

    message = event.get("message", {})
    text = message.get("content", {}).get("text", "")
    group_id = event.get("group", {}).get("id")
    sender_name = event.get("sender", {}).get("name", "Thành viên")

    if not text or not group_id:
        return {"ok": True}

    # Parse lệnh
    section_key, task_name = parse_task_command(text)

    if task_name is None:
        # Không phải lệnh !task → bỏ qua
        return {"ok": True}

    if not task_name:
        send_seatalk_group_message(group_id, "⚠️ Vui lòng nhập tên task. Ví dụ: !task [Lotte] Tên task")
        return {"ok": True}

    # Tìm section GID
    try:
        live_sections = get_asana_sections()
        # Tìm theo key người dùng gõ
        section_gid = live_sections.get(section_key)
        section_display = section_key.capitalize() if section_key else "Khác"

        if not section_gid:
            # Thử tìm section Khác làm fallback
            section_gid = live_sections.get("khác") or live_sections.get("khac")
            section_display = "Khác"

        # Tạo task
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
        log.error("Error creating task: %s", e)
        send_seatalk_group_message(group_id, f"❌ Lỗi khi tạo task: {str(e)}")

    return {"ok": True}


# ─── Health check ─────────────────────────────────────────────────────────────

@app.get("/")
def health():
    return {"status": "ok", "bot": "SeaTalk–Asana Bot"}
