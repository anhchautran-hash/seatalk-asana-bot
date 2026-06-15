# SeaTalk → Asana Bot

Bot tự động tạo task Asana khi thành viên gõ lệnh trong group SeaTalk.

## Cách dùng trong group SeaTalk

```
!task [Lotte] Hoàn thiện hợp đồng tháng 7
!task [realme] Follow up testing timeline
!task [Goodtime] Chuẩn bị thiết bị booth
!task [Khác] Nghiên cứu đối tác mới
```

Bot sẽ reply confirm vào group với link task Asana.

---

## Deploy lên VPS

### 1. Copy code lên VPS
```bash
scp -r seatalk_asana_bot/ user@YOUR_VPS_IP:/home/user/
```

### 2. Cài dependencies
```bash
cd seatalk_asana_bot
pip install -r requirements.txt
```

### 3. Chạy bot
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

### 4. Chạy nền (dùng systemd hoặc screen)
```bash
# Dùng screen
screen -S asana-bot
uvicorn main:app --host 0.0.0.0 --port 8000
# Ctrl+A+D để detach
```

### 5. Mở port firewall
```bash
sudo ufw allow 8000
```

---

## Cấu hình SeaTalk Open Platform

1. Vào https://open.seatalk.io → App của bạn
2. Tab **Event Callback** → điền URL:
   ```
   http://YOUR_VPS_IP:8000/webhook
   ```
3. Bật event: `receive_message` (group messages)
4. Tab **Permissions** → bật:
   - Receive group messages
   - Send message to group

---

## Lấy Section GID của Asana

Chạy lệnh sau để lấy GID của tất cả sections:

```bash
curl -H "Authorization: Bearer YOUR_ASANA_TOKEN" \
  "https://app.asana.com/api/1.0/projects/1215522694635240/sections" \
  | python3 -m json.tool
```

Sau đó điền GID vào `SECTION_MAP` trong `main.py`.

---

## Test thử

```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "event": {
      "type": "receive_message",
      "message": {"content": {"text": "!task [Lotte] Test task"}},
      "group": {"id": "test_group_id"},
      "sender": {"name": "Chau Tran"}
    }
  }'
```
