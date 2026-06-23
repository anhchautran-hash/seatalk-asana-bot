```python
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
from datetime import datetime
from fastapi import FastAPI, Header, HTTPException, Request

# ─── Cấu hình ────────────────────────────────────────────────────────────────

SEATALK_APP_ID     = "MzY4ODY3MDkyNzgw"
SEATALK_APP_SECRET = "g0d_-DJAQvRuL1QV8MXNies02WcU7K3U"

ASANA_TOKEN      = "2/1211043881249289/1215711068523662:623ea8a12e256d9c895e6a6de023ed29"
ASANA_PROJECT_ID  = "1215522694635240"
ASANA_DETAILS_FIELD_GID = "1215581143850080"

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
    "9251829263":             "anhchau.tran@garena.vn",
    "1294552826":             "maianh.nguyentruc_ctv@garena.vn",
    "9252055694":             "tuongclk@gmail.com",
}

MEMBERS = [
    {"email": "tuongclk@gmail.com",              "name": "Khánh Tường"},
    {"email": "maianh.nguyentruc_ctv@garena.vn", "name": "Trúc Mai Anh"},
    {"email": "anhchau.tran@garena.vn",           "name": "Chau Tran"},
]

_asana_workspace_id_cache = {"gid": None}

def get_asana_workspace_id() -> str | None:
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="SeaTalk–Asana Bot")

_seatalk_token_cache = {"token": None, "expires_at": 0}

_processed_message_ids: list = []
MAX_PROCESSED_IDS = 100


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
        data = resp.json()
        if data.get("code") == 100:
            log.warning("Token expired, refreshing and retrying...")
            _seatalk_token_cache["token"] = None
            _seatalk_token_cache["expires_at"] = 0
            token = get_seatalk_token()
            resp = httpx.post(
                "https://openapi.seatalk.io/messaging/v2/group_chat",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "group_id": group_id,
                    "message": {"tag": "text", "text": {"content": text}},
                },
                timeout=10,
            )
            log.info("SeaTalk retry → %s: %s", resp.status_code, resp.text)

    if resp.status_code == 200 and resp.json().get("code") == 0:
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
    query = query.lower().strip()
    if query in sections:
        return query, query.capitalize()
    matches = difflib.get_close_matches(query, sections.keys(), n=1, cutoff=0.5)
    if matches:
        key = matches[0]
        return key, key.capitalize()
    for key in sections:
        if query in key or key in query:
            return key, key.capitalize()
    return None, None


def find_asana_user(name_query: str) -> str | None:
    if not name_query:
        return None

    query_lower = name_query.lower().strip()
    if query_lower in MEMBER_MAP:
        email = MEMBER_MAP[query_lower]
        log.info("MEMBER_MAP hit: %s → %s", name_query, email)
        name_query = email
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

    params = {"workspace": workspace_id, "opt_fields": "name,email", "text": name_query}

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
    text = text.strip()
    text = re.sub(r'^(@\S+\s*)+', '', text).strip()

    if not text.lower().startswith("!task"):
        return None, None, None, None

    content = text[5:].strip()

    if "/" in content:
        parts = [p.strip() for p in content.split("/")]
        section = parts[0].lower() if len(parts) > 0 else "khác"
        task_name = parts[1] if len(parts) > 1 else ""
        assignee_raw = parts[2] if len(parts) > 2 else None
        description = parts[3] if len(parts) > 3 else None
        assignee_name = None
        if assignee_raw:
            assignee_name = re.sub(r'^@', '', assignee_raw.strip())
        return section, task_name, assignee_name, description

    section = "khác"
    if content.startswith("["):
        end = content.find("]")
        if end != -1:
            section = content[1:end].strip().lower()
            content = content[end + 1:].strip()

    description = None
    pipe_match = re.search(r'\s*\|\s*(.+)$', content)
    if pipe_match:
        description = pipe_match.group(1).strip()
        content = content[:pipe_match.start()].strip()

    assignee_name = None
    assignee_match = re.search(r'\s*-\s*@(.+)$', content)
    if assignee_match:
        assignee_name = assignee_match.group(1).strip()
        content = content[:assignee_match.start()].strip()

    task_name = content
    return section, task_name, assignee_name, description


# ─── Webhook endpoint ─────────────────────────────────────────────────────────

MESSAGE_EVENT_TYPES = {
    "receive_message",
    "new_mentioned_message_received_from_group_chat",
    "group_message",
    "message",
    "bot_mentioned",
    "mention",
}


def safe_str(val) -> str:
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        for k in ("text", "content", "plain_text", "value"):
            if isinstance(val.get(k), str):
                return val[k]
        return json.dumps(val, ensure_ascii=False)
    return str(val) if val else ""


def extract_text_and_group(event: dict) -> tuple[str, str]:
    message = event.get("message", {})
    text = (
        safe_str(message.get("plain_text"))
        or safe_str(message.get("content", {}).get("text") if isinstance(message.get("content"), dict) else message.get("content"))
        or safe_str(message.get("text"))
        or safe_str(event.get("text"))
        or ""
    )
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

    log.info("=== WEBHOOK RECEIVED ===")
    log.info("Payload: %s", json.dumps(payload, ensure_ascii=False, indent=2))

    msg_id = payload.get("event", {}).get("message", {}).get("message_id", "")
    if msg_id and msg_id in _processed_message_ids:
        log.info("Duplicate message_id %s, skipping", msg_id)
        return {"ok": True}
    if msg_id:
        _processed_message_ids.append(msg_id)
        if len(_processed_message_ids) > MAX_PROCESSED_IDS:
            _processed_message_ids.pop(0)

    if payload.get("event_type") == "event_verification":
        challenge = payload.get("event", {}).get("seatalk_challenge", "")
        return {"seatalk_challenge": challenge}
    if "seatalk_challenge" in payload:
        return {"seatalk_challenge": payload["seatalk_challenge"]}
    if "challenge" in payload:
        return {"challenge": payload["challenge"]}

    event_type = payload.get("event_type", "")
    event = payload.get("event", {})

    log.info("Event type: [%s]", event_type)

    text, group_id = extract_text_and_group(event)

    is_known_message_event = event_type in MESSAGE_EVENT_TYPES
    has_task_command = "!task" in text.lower()
    has_report_command = "!report" in text.lower()

    if not is_known_message_event and not has_task_command and not has_report_command:
        log.info("Skipping event_type=%s", event_type)
        return {"ok": True}

    message_obj = event.get("message", {})
    sender_obj = message_obj.get("sender", {}) or event.get("sender", {})
    sender_seatalk_id = str(sender_obj.get("seatalk_id", "") or "")
    sender_name = sender_obj.get("name", "") or event.get("sender", {}).get("name", "") or "Thành viên"
    sender_email = sender_obj.get("email", "") or event.get("sender", {}).get("email", "")
    if sender_name == "Thành viên" and sender_seatalk_id and sender_seatalk_id in MEMBER_MAP:
        sender_email = MEMBER_MAP[sender_seatalk_id]

    log.info("Text: [%s] | Group: [%s] | Sender: [%s]", text, group_id, sender_name)

    if not text or not group_id:
        log.warning("Missing text or group_id — skipping")
        return {"ok": True}

    stripped = re.sub(r'^(@\S+\s*)+', '', text).strip()

    # Lệnh !sections
    if stripped.lower().startswith("!sections"):
        try:
            live_sections = get_asana_sections()
            section_list = "\n".join(f"  • {name.capitalize()}" for name in live_sections.keys())
            send_seatalk_group_message(group_id, f"📂 Danh sách sections:\n{section_list}\n\nCú pháp: !task Section / Tên task / @Assignee / Ghi chú\nVí dụ: !task Lotte / Cập nhật HĐ / @Mai Anh / gửi trước 5h")
        except Exception as e:
            send_seatalk_group_message(group_id, f"❌ Lỗi: {str(e)}")
        return {"ok": True}

    # Lệnh !report
    if stripped.lower().startswith("!report"):
        try:
            live_sections = get_asana_sections()
            all_sections_tasks = {}
            for sec_name, sec_gid in live_sections.items():
                resp = httpx.get(
                    f"https://app.asana.com/api/1.0/sections/{sec_gid}/tasks",
                    headers={"Authorization": f"Bearer {ASANA_TOKEN}"},
                    params={"completed_since": "now", "opt_fields": "name,completed,assignee.email"},
                    timeout=10,
                )
                tasks = resp.json().get("data", [])
                filtered = [t for t in tasks if not t.get("completed")]
                if filtered:
                    all_sections_tasks[sec_name] = filtered

            today = datetime.today().strftime("%d/%m/%Y")
            for member in MEMBERS:
                lines = [f"📋 Daily Tasks — {today} | 👤 {member['name']}\n"]
                total = 0
                for sec_name, tasks in all_sections_tasks.items():
                    member_tasks = [
                        t for t in tasks
                        if (t.get("assignee") or {}).get("email", "").lower() == member["email"].lower()
                    ]
                    if member_tasks:
                        lines.append(f"📂 {sec_name.capitalize()}")
                        for t in member_tasks:
                            lines.append(f"  • {t['name']}")
                        lines.append("")
                        total += len(member_tasks)
                if total == 0:
                    lines.append("✅ Không có task nào đang mở!")
                lines.insert(1, f"Tổng: {total} task đang mở\n")
                send_seatalk_group_message(group_id, "\n".join(lines))
            log.info("Report sent for %d members", len(MEMBERS))
        except Exception as e:
            log.error("Error sending report: %s", e, exc_info=True)
            send_seatalk_group_message(group_id, f"❌ Lỗi: {str(e)}")
        return {"ok": True}

    # Lệnh !task
    section_key, task_name, assignee_name, description = parse_task_command(text)

    if task_name is None:
        log.info("Not a !task command, ignoring")
        return {"ok": True}

    if not task_name:
        send_seatalk_group_message(group_id, "⚠️ Vui lòng nhập tên task. Ví dụ: !task Lotte / Tên task / @Mai Anh")
        return {"ok": True}

    log.info("Parsed: section=%s, task=%s, assignee=%s, desc=%s", section_key, task_name, assignee_name, description)

    try:
        live_sections = get_asana_sections()
        section_gid = live_sections.get(section_key)
        section_display = section_key.capitalize() if section_key else "Khác"

        if not section_gid:
            fuzzy_key, fuzzy_display = fuzzy_match_section(section_key, live_sections)
            if fuzzy_key:
                log.info("Fuzzy matched [%s] → [%s]", section_key, fuzzy_key)
                section_gid = live_sections[fuzzy_key]
                section_display = fuzzy_display
            else:
                log.warning("Section [%s] not found, falling back to Khác", section_key)
                section_gid = live_sections.get("khác") or live_sections.get("khac")
                section_display = "Khác"

        assignee_gid = None
        assignee_display = None
        if assignee_name:
            if assignee_name.lower() == "me":
                if sender_email:
                    assignee_gid = find_asana_user(sender_email)
                    assignee_display = sender_name
                else:
                    log.warning("@me used but sender email is empty")
            else:
                assignee_gid = find_asana_user(assignee_name)
                if assignee_gid:
                    assignee_display = assignee_name.title()

        task = create_asana_task(task_name, section_gid, assignee_gid, description)
        task_url = f"https://app.asana.com/0/{ASANA_PROJECT_ID}/{task['gid']}"

        assignee_line = f"\n👤 Assignee: {assignee_display}" if assignee_display else (f"\n⚠️ Không tìm thấy assignee: {assignee_name}" if assignee_name else "")
        description_line = f"\n📝 {description}" if description else ""

        msg = (
            f"✅ Task đã được tạo!\n"
            f"📌 {task_name}\n"
            f"📂 Section: {section_display}"
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
```