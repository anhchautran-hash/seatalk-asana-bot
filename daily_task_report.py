# daily_task_report.py
import os, requests
from datetime import datetime

ASANA_TOKEN = os.environ["ASANA_TOKEN"]
PROJECT_GID = os.environ["ASANA_PROJECT_GID"]
SEATALK_WEBHOOK = os.environ["SEATALK_WEBHOOK_URL"]

def get_tasks():
    headers = {"Authorization": f"Bearer {ASANA_TOKEN}"}
    params = {
        "completed_since": "now",
        "opt_fields": "name,assignee.name,due_on,custom_fields"
    }
    r = requests.get(
        f"https://app.asana.com/api/1.0/projects/{PROJECT_GID}/tasks",
        headers=headers, params=params
    )
    return r.json().get("data", [])

def is_low_priority(task):
    for cf in task.get("custom_fields", []):
        if cf.get("name") == "Priority":
            return (cf.get("display_value") or "").lower() == "low"
    return False
if __name__ == "__main__":
    all_tasks = get_tasks()
    print(f"Tổng task lấy được từ Asana: {len(all_tasks)}")  # thêm dòng này
    
    filtered = [t for t in all_tasks if not is_low_priority(t)]
    print(f"Sau khi lọc Priority Low: {len(filtered)}")      # thêm dòng này
    
    if filtered:
        send_report(filtered)
        print(f"Sent {len(filtered)} tasks")
    else:
        print("No tasks to report")
def send_report(tasks):
    today = datetime.today().strftime("%d/%m/%Y")
    lines = [f"📋 *Daily Tasks — {today}*", f"Tổng: {len(tasks)} task đang mở\n"]
    for t in tasks:
        assignee = (t.get("assignee") or {}).get("name", "Unassigned")
        due = t.get("due_on") or "—"
        lines.append(f"• {t['name']}\n  👤 {assignee}  📅 {due}")
    
    r = requests.post(SEATALK_WEBHOOK, json={
        "tag": "text",
        "text": {"content": "\n".join(lines)}
    })
    print(f"SeaTalk response: {r.status_code} - {r.text}")  # thêm dòng này
