import os
import time
import httpx
from datetime import datetime

ASANA_TOKEN  = os.environ["ASANA_TOKEN"]
PROJECT_GID  = os.environ["ASANA_PROJECT_GID"]
SEATALK_APP_ID     = os.environ["SEATALK_APP_ID"]
SEATALK_APP_SECRET = os.environ["SEATALK_APP_SECRET"]
SEATALK_GROUP_ID   = os.environ["SEATALK_GROUP_ID"]

def get_seatalk_token():
    resp = httpx.post(
        "https://openapi.seatalk.io/auth/app_access_token",
        json={"app_id": SEATALK_APP_ID, "app_secret": SEATALK_APP_SECRET},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["app_access_token"]

def get_tasks():
    headers = {"Authorization": f"Bearer {ASANA_TOKEN}"}
    params = {
        "completed_since": "now",
        "opt_fields": "name,assignee.name,due_on,custom_fields"
    }
    r = httpx.get(
        f"https://app.asana.com/api/1.0/projects/{PROJECT_GID}/tasks",
        headers=headers, params=params, timeout=10
    )
    return r.json().get("data", [])

def is_low_priority(task):
    for cf in task.get("custom_fields", []):
        if cf.get("name") == "Priority":
            return (cf.get("display_value") or "").lower() == "low"
    return False

def send_report(tasks):
    token = get_seatalk_token()
    today = datetime.today().strftime("%d/%m/%Y")
    lines = [f"📋 Daily Tasks — {today}", f"Tổng: {len(tasks)} task đang mở\n"]
    for t in tasks:
        assignee = (t.get("assignee") or {}).get("name", "Unassigned")
        due = t.get("due_on") or "—"
        lines.append(f"• {t['name']}\n  👤 {assignee}  📅 {due}")

    r = httpx.post(
        "https://openapi.seatalk.io/messaging/v2/group_chat",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "group_id": SEATALK_GROUP_ID,
            "message": {"tag": "text", "text": {"content": "\n".join(lines)}}
        },
        timeout=10,
    )
    print(f"SeaTalk response: {r.status_code} - {r.text}")

if __name__ == "__main__":
    all_tasks = get_tasks()
    print(f"Tổng task lấy được từ Asana: {len(all_tasks)}")
    filtered = [t for t in all_tasks if not is_low_priority(t)]
    print(f"Sau khi lọc Priority Low: {len(filtered)}")
    if filtered:
        send_report(filtered)
    else:
        print("No tasks to report")
