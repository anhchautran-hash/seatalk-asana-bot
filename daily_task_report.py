import os
import httpx
from datetime import datetime

ASANA_TOKEN        = os.environ["ASANA_TOKEN"]
PROJECT_GID        = os.environ["ASANA_PROJECT_GID"]
SEATALK_APP_ID     = os.environ["SEATALK_APP_ID"]
SEATALK_APP_SECRET = os.environ["SEATALK_APP_SECRET"]
SEATALK_GROUP_ID   = os.environ["SEATALK_GROUP_ID"]

MEMBERS = [
   {"email": "tuongclk@gmail.com",                "name": "Khánh Tường"},
   {"email": "maianh.nguyentruc_ctv@garena.vn",   "name": "Trúc Mai Anh"},
   {"email": "anhchau.tran@garena.vn",             "name": "Chau Tran"},
]

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
           "opt_fields": "name,completed,assignee.email"
       },
       timeout=10,
   )
   return r.json().get("data", [])

def send_message(token, text):
   r = httpx.post(
       "https://openapi.seatalk.io/messaging/v2/group_chat",
       headers={"Authorization": f"Bearer {token}"},
       json={
           "group_id": SEATALK_GROUP_ID,
           "message": {"tag": "text", "text": {"content": text}}
       },
       timeout=10,
   )
   print(f"SeaTalk response: {r.status_code} - {r.text}")

if __name__ == "__main__":
   sections = get_sections()
   today = datetime.today().strftime("%d/%m/%Y")

   # Gom tất cả task theo section
   all_sections_tasks = {}
   for section in sections:
       tasks = get_tasks_in_section(section["gid"])
       filtered = [t for t in tasks if not t.get("completed")]
       if filtered:
           all_sections_tasks[section["name"]] = filtered

   token = get_seatalk_token()

   # Gửi 1 tin nhắn riêng cho mỗi thành viên
   for member in MEMBERS:
       lines = [f"📋 Daily Tasks — {today} | 👤 {member['name']}\n"]
       total = 0

       for section_name, tasks in all_sections_tasks.items():
           member_tasks = [
               t for t in tasks
               if (t.get("assignee") or {}).get("email", "").lower() == member["email"].lower()
           ]
           if member_tasks:
               lines.append(f"📂 {section_name}")
               for t in member_tasks:
                   lines.append(f"  • {t['name']}")
               lines.append("")
               total += len(member_tasks)

       if total == 0:
           lines.append("✅ Không có task nào đang mở!")

       lines.insert(1, f"Tổng: {total} task đang mở\n")
       send_message(token, "\n".join(lines))
       print(f"Sent {total} tasks for {member['name']}")
