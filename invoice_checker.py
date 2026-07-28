"""
invoice_checker.py
===================
Tính năng !checkinvoice cho bot AsaPNS — bản đơn giản.

Chỉ kiểm tra 3 thông tin cố định của bên mua (Garena) trên hoá đơn nháp:
  - Tên đơn vị
  - Địa chỉ
  - Mã số thuế

Luồng dùng trong SeaTalk group:
  1) Thành viên gửi ẢNH hoặc FILE PDF hoá đơn nháp vào group.
  2) Gõ: !checkinvoice
  3) Bot đọc hoá đơn (qua Claude API), so với 3 thông tin cố định bên dưới,
     trả lời ✅ khớp / ❌ sai lệch cho từng mục.

Muốn sửa thông tin cố định: sửa trực tiếp 3 hằng số EXPECTED_* bên dưới rồi deploy lại,
hoặc dùng lệnh nhanh (không cần deploy lại, nhưng sẽ mất khi bot restart):
  !setref ten_don_vi=... | !setref dia_chi=... | !setref mst=...

Cần cấu hình (env var trên Render):
  ANTHROPIC_API_KEY  - API key Anthropic (dùng để đọc hoá đơn)
"""

import base64
import json
import logging
import os
import re
import time

import httpx

log = logging.getLogger(__name__)

# ─── Thông tin cố định cần đối chiếu (sửa ở đây khi cần) ───────────────────

EXPECTED_TEN_DON_VI = "CÔNG TY CỔ PHẦN GIẢI TRÍ VÀ THỂ THAO ĐIỆN TỬ VIỆT NAM"
EXPECTED_DIA_CHI = "Tầng 6, Tòa nhà Capital Place, 29 Liễu Giai, Phường Ngọc Hà, Thành phố Hà Nội, Việt Nam"
EXPECTED_MST = "0105301438"

# field_key -> (nhãn hiển thị, giá trị mặc định, kiểu so khớp: "exact" | "contains")
FIELDS = {
    "ten_don_vi": ("Tên đơn vị", EXPECTED_TEN_DON_VI, "contains"),
    "dia_chi": ("Địa chỉ", EXPECTED_DIA_CHI, "contains"),
    "mst": ("Mã số thuế", EXPECTED_MST, "exact"),
}

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = "claude-sonnet-5"

# ─── Cache ảnh chờ kiểm tra (gửi ảnh trước, gõ lệnh sau) ───────────────────

_pending_images = {}  # group_id -> {"base64":..., "media_type":..., "is_pdf":..., "ts":...}
PENDING_IMAGE_TTL = 600  # 10 phút

_ref_overrides = {}  # field_key -> value ghi đè tạm qua !setref (mất khi bot restart)


def get_field_value(field_key: str) -> str:
    if field_key in _ref_overrides:
        return _ref_overrides[field_key]
    return FIELDS[field_key][1]


def set_override(field_key: str, value: str):
    _ref_overrides[field_key] = value


def store_pending_image(group_id: str, b64_data: str, media_type: str, is_pdf: bool):
    _pending_images[group_id] = {
        "base64": b64_data,
        "media_type": media_type,
        "is_pdf": is_pdf,
        "ts": time.time(),
    }


def get_pending_image(group_id: str) -> dict | None:
    item = _pending_images.get(group_id)
    if not item:
        return None
    if time.time() - item["ts"] > PENDING_IMAGE_TTL:
        _pending_images.pop(group_id, None)
        return None
    return item


def clear_pending_image(group_id: str):
    _pending_images.pop(group_id, None)


# ─── Tải ảnh/file từ SeaTalk ─────────────────────────────────────────────────

def download_seatalk_media(url_or_key: str, seatalk_token: str) -> bytes:
    """
    LƯU Ý: main.py hiện đã log toàn bộ payload webhook. Khi test gửi thử 1 ảnh vào group,
    xem log Render để biết chính xác field chứa URL ảnh và chỉnh extract_media_info()
    bên dưới nếu cần. Hàm này giả định url_or_key là URL tải trực tiếp kèm Bearer token.
    """
    resp = httpx.get(
        url_or_key,
        headers={"Authorization": f"Bearer {seatalk_token}"},
        timeout=20,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.content


def extract_media_info(message: dict) -> dict | None:
    """Trả về {"url":..., "is_pdf":bool, "media_type":...} nếu message này là ảnh/file."""
    tag = message.get("tag")
    image_obj = message.get("image") if isinstance(message.get("image"), dict) else None
    file_obj = message.get("file") if isinstance(message.get("file"), dict) else None

    if tag == "image" or image_obj:
        obj = image_obj or {}
        url = obj.get("url") or obj.get("image_url") or obj.get("download_url")
        if url:
            return {"url": url, "is_pdf": False, "media_type": "image/jpeg"}

    if tag == "file" or file_obj:
        obj = file_obj or {}
        url = obj.get("url") or obj.get("file_url") or obj.get("download_url")
        filename = (obj.get("filename") or obj.get("name") or "").lower()
        if url:
            is_pdf = filename.endswith(".pdf") or "pdf" in (obj.get("content_type", "") or "")
            return {
                "url": url,
                "is_pdf": is_pdf,
                "media_type": "application/pdf" if is_pdf else obj.get("content_type", "application/octet-stream"),
            }
    return None


# ─── Claude API: trích xuất dữ liệu hoá đơn ─────────────────────────────────

def extract_invoice_data(b64_data: str, media_type: str, is_pdf: bool) -> dict:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("Thiếu env var ANTHROPIC_API_KEY.")

    system_instruction = (
        "Bạn là công cụ trích xuất dữ liệu hoá đơn (hoá đơn GTGT / hoá đơn nháp) tiếng Việt.\n"
        "Đọc kỹ ảnh hoặc tài liệu được cung cấp, tìm phần thông tin ĐƠN VỊ MUA HÀNG "
        "(Company's Name / Company Buyer), và trả về DUY NHẤT một đối tượng JSON hợp lệ, "
        "KHÔNG kèm giải thích, KHÔNG markdown, KHÔNG dấu backtick, đúng 3 khoá sau "
        "(giá trị dạng chuỗi, để chuỗi rỗng \"\" nếu không tìm thấy):\n"
        '{"ten_don_vi": "", "dia_chi": "", "mst": ""}'
    )

    content_block = (
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64_data}}
        if is_pdf
        else {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64_data}}
    )

    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 500,
            "system": system_instruction,
            "messages": [
                {
                    "role": "user",
                    "content": [content_block, {"type": "text", "text": "Trích xuất dữ liệu theo đúng schema JSON đã yêu cầu."}],
                }
            ],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    raw_text = "\n".join(text_blocks).strip()
    cleaned = re.sub(r"^```json\s*|^```\s*|```\s*$", "", raw_text, flags=re.IGNORECASE).strip()
    return json.loads(cleaned)


# ─── So khớp ─────────────────────────────────────────────────────────────────

def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def compare_invoice(extracted: dict) -> dict:
    lines = []
    counts = {"match": 0, "mismatch": 0, "missing": 0}

    for field_key, (label, _default, mode) in FIELDS.items():
        expected = get_field_value(field_key)
        extracted_val = extracted.get(field_key, "")
        if not extracted_val:
            counts["missing"] += 1
            lines.append(f"⚠️ {label}: cần \"{expected}\" — không đọc được trên hoá đơn")
            continue
        en, ex = _normalize(extracted_val), _normalize(expected)
        match = en == ex if mode == "exact" else (ex in en or en in ex)
        if match:
            counts["match"] += 1
            lines.append(f"✅ {label}: khớp ({extracted_val})")
        else:
            counts["mismatch"] += 1
            lines.append(f"❌ {label}: cần \"{expected}\" — hoá đơn ghi \"{extracted_val}\"")

    return {"lines": lines, "counts": counts}


def format_result_message(result: dict) -> str:
    c = result["counts"]
    overall = "✅ ĐÃ KHỚP — có thể duyệt" if c["mismatch"] == 0 and c["missing"] == 0 else "🔴 CẦN KIỂM TRA LẠI trước khi duyệt"
    summary = f"({c['match']} khớp · {c['mismatch']} sai lệch · {c['missing']} thiếu)\n\n"
    body = "\n".join(result["lines"])
    return f"📋 Đối chiếu thông tin đơn vị mua trên hoá đơn\n{overall}\n{summary}{body}"


# ─── Parse lệnh ─────────────────────────────────────────────────────────────

def is_checkinvoice(text: str) -> bool:
    return bool(re.match(r"^!checkinvoice\b", text.strip(), re.IGNORECASE))


def parse_setref(text: str):
    """!setref field=value → (field, value) hoặc None. field phải là 1 trong: ten_don_vi, dia_chi, mst"""
    m = re.match(r"^!setref\s+(\S+?)=(.+)$", text.strip(), re.IGNORECASE)
    if not m:
        return None
    field = m.group(1).strip()
    if field not in FIELDS:
        return None
    return field, m.group(2).strip()
