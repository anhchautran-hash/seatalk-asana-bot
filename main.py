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
import difflib
import httpx
from fastapi import FastAPI, Header, HTTPException, Request

# ─── Cấu hình ────────────────────────────────────────────────────────────────

SEATALK_APP_ID     = "MzY4ODY3MDkyNzgw"
SEATALK_APP_SECRET = "g0d_-DJAQvRuL1QV8MXNies02WcU7K3U"

ASANA_TOKEN      = "2/1211043881249289/1215711068523662:623ea8a12e256d9c895e6a6de023ed29"
ASANA_PROJECT_ID  = "1215522694635240"
ASANA_DETAILS_FIELD_GID = "1215581143850080"  # Custom field "Details"

# Map tên SeaTalk → email Asana (để assign task chính xác)
MEMBER_MAP = {
    "châu trần":              "anhchau.tran@garena.vn",
    "chau tran":              "anhchau.tran@garena.vn",
    "châu trần 0961103256":   "anhchau.tran@garena.vn",
    "khánh tường":            "tuongclk@gmail.com",
    "khanh tuong":            "tuongclk@gmail.com",
    "khánh tường 0862020108": "tuongclk@gmail.com",
    "nguyễn trúc mai anh":    "maianh.nguyentruc_ctv@garena.vn",
    "nguyen truc mai anh":    "maianh.nguyentruc_ctv@garena.vn",
    "mai anh":                "maianh.nguyentruc_ctv@garena.vn",
}

# Workspace ID — tự động lấy từ project khi khởi động
_asana_workspace_id_cache = {"gid": None}

def get_asana_workspace_id() -> str | None:
    """Tự động lấy workspace GID từ project."""
    if _asana_workspace_id_cache["gid"]:
        return _asana_workspace_id_cache["gid"]
    try:
        resp = httpx.get(
            f"https://app.asana.com/api/1.0/projects/{ASANA_PROJECT_ID}",
            headers={"Authorization": f"Bearer {ASANA_TOKEN}"},
            params={"opt_fields": "workspace.gid,workspace.name"},
            timeout=10,
        )
        resp.raise_for_status()
        gid = resp.json()["data"]["workspace"]["gid"]
        _asana_workspace_id_cache["gid"] = gid
        log.info("Asana workspace GID: %s", gid)
        return gid
    except Exception as e:
        log.error("Could not fetch workspace GID: %s", e)
        return None

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
    """Gửi tin nhắn vào group SeaTalk."""
    token = get_seatalk_token()

    # Endpoint chính xác theo SeaTalk OpenAPI: /messaging/v2/group_chat
    resp = httpx.post(
        "https://openapi.seatalk.io/messaging/v2/group_chat",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "group_id": group_id,
            "message": {"tag": "text", "text": {"content": text}},
        },
        timeout=10,
    )
    log.info("SeaTalk send → %s: %s", resp.status_code, resp.text)
    if resp.status_code == 200:
        log.info("SeaTalk message sent to group %s", group_id)
    else:
        log.error("SeaTalk send failed: %s", resp.text)


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


def fuzzy_match_section(query: str, sections: dict) -> tuple[str | None, str | None]:
    """Tìm section gần đúng nhất. Trả về (key, display_name) hoặc (None, None)."""
    query = query.lower().strip()
    # Khớp chính xác trước
    if query in sections:
        return query, query.capitalize()
    # Fuzzy match
    matches = difflib.get_close_matches(query, sections.keys(), n=1, cutoff=0.5)
    if matches:
        key = matches[0]
        return key, key.capitalize()
    # Tìm theo substring
    for key in sections:
        if query in key or key in query:
            return key, key.capitalize()
    return None, None


def find_asana_user(name_query: str) -> str | None:
    """Tìm Asana user GID theo tên (tìm gần đúng, không phân biệt hoa/thường).
    Trả về GID nếu tìm thấy, None nếu không."""
    if not name_query:
        return None

    # Kiểm tra MEMBER_MAP trước (chính xác nhất)
    query_lower = name_query.lower().strip()
    if query_lower in MEMBER_MAP:
        email = MEMBER_MAP[query_lower]
        log.info("MEMBER_MAP hit: %s → %s", name_query, email)
        name_query = email  # dùng email để search Asana

    # Fuzzy match trong MEMBER_MAP nếu chưa khớp chính xác
    elif "@" not in name_query:
        for key, email in MEMBER_MAP.items():
            if query_lower in key or key in query_lower:
                log.info("MEMBER_MAP fuzzy: %s → %s → %s", name_query, key, email)
                name_query = email
                break

    workspace_id = get_asana_workspace_id()
    if not workspace_id:
        log.error("No workspace ID, cannot search user")
        return None

    params = {"workspace": workspace_id, "opt_fields": "name,email"}
    if "@" in name_query:
        params["text"] = name_query
    else:
        params["text"] = name_query

    try:
        resp = httpx.get(
            "https://app.asana.com/api/1.0/users",
            headers={"Authorization": f"Bearer {ASANA_TOKEN}"},
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        users = resp.json().get("data", [])

        query_lower = name_query.lower().strip()
        for user in users:
            user_name = user.get("name", "").lower()
            user_email = user.get("email", "").lower()
            # Khớp chính xác tên hoặc email
            if query_lower in user_name or query_lower in user_email:
                log.info("Found assignee: %s (%s) → %s", user["name"], user.get("email"), user["gid"])
                return user["gid"]

        log.warning("Assignee not found for query: %s", name_query)
        return None

    except Exception as e:
        log.error("Error finding user %s: %s", name_query, e)
        return None


def create_asana_task(task_name: str, section_gid: str | None, assignee_gid: str | None = None, description: str | None = None) -> dict:
    payload = {
        "data": {
            "name": task_name,
            "projects": [ASANA_PROJECT_ID],
        }
    }
    if assignee_gid:
        payload["data"]["assignee"] = assignee_gid
    if description:
        payload["data"]["custom_fields"] = {
            ASANA_DETAILS_FIELD_GID: description
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

def parse_task_command(text: str) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Parse lệnh !task. Trả về (section, task_name, assignee_name, description).
    Ví dụ: "@AsaPNS !task [Lotte] Tên task - @Nguyễn Trúc Mai Anh | Nội dung chi tiết"
           → ("lotte", "Tên task", "nguyễn trúc mai anh", "Nội dung chi tiết")
    """
    text = text.strip()
    # Bỏ tất cả @mention ở đầu (bot mention)
    text = re.sub(r'^(@\S+\s*)+', '', text).strip()

    if not text.lower().startswith("!task"):
        return None, None, None, None

    content = text[5:].strip()

    # Parse section
    section = "khác"
    if content.startswith("["):
        end = content.find("]")
        if end != -1:
            section = content[1:end].strip().lower()
            content = content[end + 1:].strip()

    # Parse description: tìm " | nội dung" ở cuối
    description = None
    pipe_match = re.search(r'\s*\|\s*(.+)$', content)
    if pipe_match:
        description = pipe_match.group(1).strip()
        content = content[:pipe_match.start()].strip()

    # Parse assignee: tìm " - @tên"
    assignee_name = None
    assignee_match = re.search(r'\s*-\s*@(.+)$', content)
    if assignee_match:
        assignee_name = assignee_match.group(1).strip()
        content = content[:assignee_match.start()].strip()

    task_name = content
    return section, task_name, assignee_name, description


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

    # Sender info nằm trong message.sender (theo payload thực tế của SeaTalk)
    message_obj = event.get("message", {})
    sender_obj = message_obj.get("sender", {}) or event.get("sender", {})
    sender_name = sender_obj.get("name", "") or event.get("sender", {}).get("name", "") or "Thành viên"
    sender_email = sender_obj.get("email", "") or event.get("sender", {}).get("email", "")
    log.info("Sender: name=%s, email=%s", sender_name, sender_email)

    log.info("Text: [%s] | Group: [%s] | Sender: [%s]", text, group_id, sender_name)

    if not text or not group_id:
        log.warning("Missing text or group_id — skipping")
        return {"ok": True}

    # Lệnh !sections — hiện danh sách sections
    stripped = re.sub(r'^(@\S+\s*)+', '', text).strip()
    if stripped.lower().startswith("!sections"):
        try:
            live_sections = get_asana_sections()
            section_list = "\n".join(f"  • {name.capitalize()}" for name in live_sections.keys())
            send_seatalk_group_message(group_id, f"📂 Danh sách sections:\n{section_list}\n\nCú pháp: !task [Section] Tên task - @Assignee | Details")
        except Exception as e:
            send_seatalk_group_message(group_id, f"❌ Lỗi: {str(e)}")
        return {"ok": True}

    section_key, task_name, assignee_name, description = parse_task_command(text)

    if task_name is None:
        log.info("Not a !task command, ignoring")
        return {"ok": True}

    if not task_name:
        send_seatalk_group_message(group_id, "⚠️ Vui lòng nhập tên task. Ví dụ: !task [Lotte] Tên task")
        return {"ok": True}

    log.info("Parsed: section=%s, task=%s, assignee=%s, desc=%s", section_key, task_name, assignee_name, description)

    try:
        live_sections = get_asana_sections()
        section_gid = live_sections.get(section_key)
        section_display = section_key.capitalize() if section_key else "Khác"

        if not section_gid:
            # Thử fuzzy match
            fuzzy_key, fuzzy_display = fuzzy_match_section(section_key, live_sections)
            if fuzzy_key:
                log.info("Fuzzy matched [%s] → [%s]", section_key, fuzzy_key)
                section_gid = live_sections[fuzzy_key]
                section_display = fuzzy_display
            else:
                log.warning("Section [%s] not found, falling back to Khác", section_key)
                section_gid = live_sections.get("khác") or live_sections.get("khac")
                section_display = "Khác"

        # Lookup assignee nếu có
        assignee_gid = None
        assignee_display = None
        if assignee_name:
            # @me → tự assign cho người gửi
            if assignee_name.lower() == "me":
                if sender_email:
                    assignee_gid = find_asana_user(sender_email)
                    assignee_display = sender_name
                    log.info("@me resolved to sender: %s (%s)", sender_name, sender_email)
                else:
                    log.warning("@me used but sender email is empty")
            else:
                assignee_gid = find_asana_user(assignee_name)
                if assignee_gid:
                    assignee_display = assignee_name.title()
                else:
                    log.warning("Could not find assignee: %s", assignee_name)

        task = create_asana_task(task_name, section_gid, assignee_gid, description)
        task_url = f"https://app.asana.com/0/{ASANA_PROJECT_ID}/{task['gid']}"

        assignee_line = f"\n👤 Assignee: {assignee_display}" if assignee_display else (f"\n⚠️ Không tìm thấy assignee: {assignee_name}" if assignee_name else "")
        description_line = f"\n📝 {description}" if description else ""

        msg = (
            f"✅ Task đã được tạo!\n"
            f"📌 {task_name}\n"
            f"📂 Section: {section_display}\n"
            f"🙋 Tạo bởi: {sender_name}"
            f"{assignee_line}"
            f"{description_line}\n"
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
