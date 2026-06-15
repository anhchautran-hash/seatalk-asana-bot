import os
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

def get_sections():
    r = httpx.get(
        f"https://app.asana.com/api/1.0/projects/{PROJECT_GID}/sections",
        headers={"Authorization": f"Bearer {ASANA_TOKEN}"},
        timeout=10,
    )
    return r.json().get("data", [])

def get_tasks_in_section(section_gid):
    r = httpx.get(
        f"https://app.asana.com/api/1.0/sections/{section_gid}/tasks",
        headers={"Authorization": f"Bearer {ASANA_TOKEN}"},
        params={
            "completed_since": "now",
            "opt_fields": "name,completed,custom_fields"
        },
        timeout=10,
    )
    return r.json().get("data", [])

def is_low_priority(task):
    for cf in task.get("custom_fields", []):
        if cf.get("name") == "Priority":
            return (cf.get("display_value") or "").lower() == "low"
    return False

def send_report(sections_data):
    today = datetime.today().strftime("%d/%m/%Y")
    lines = [f"📋 Daily Tasks — {today}\n"]

    for section_name, tasks in sections_data.items():
        if not tasks:
            continue
        lines.append(f"📂 {section_name}")
        for t in tasks:
            lines.append(f"  • {t['name']}")
        lines.append("")

    r = httpx.post(
        "https://openapi.seatalk.io/messaging/v2/group_chat",
        headers={"Authorization": f"Bearer {get_seatalk_token()}"},
        json={
            "group_id": SEATALK_GROUP_ID,
            "message": {"tag": "text", "text": {"content": "\n".join(lines)}}
        },
        timeout=10,
    )
    print(f"SeaTalk response: {r.status_code} - {r.text}")

if __name__ == "__main__":
    sections = get_sections()
    sections_data = {}
    total = 0

    for section in sections:
        tasks = get_tasks_in_section(section["gid"])
        filtered = [t for t in tasks if not t.get("completed") and not is_low_priority(t)]
        if filtered:
            sections_data[section["name"]] = filtered
            total += len(filtered)

    print(f"Tổng task: {total}")
    if sections_data:
        send_report(sections_data)
    else:
        print("No tasks to report")
