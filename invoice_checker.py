"""
invoice_checker.py
===================
Tính năng !checkinvoice cho bot AsaPNS — dùng workflow OCR nội bộ tại ai.insea.io
(workflow "OCR", API: POST https://ai.insea.io/api/workflows/25086/run) thay vì
Tesseract, chính xác hơn nhiều vì workflow này tự tách sẵn tên đơn vị / MST / địa chỉ.

Chỉ kiểm tra 3 thông tin cố định của bên mua (Garena) trên hoá đơn nháp:
  - Tên đơn vị
  - Địa chỉ
  - Mã số thuế

LUỒNG DÙNG (Cách B — qua thread reply, vì SeaTalk chỉ gửi webhook cho bot khi có mention
hoặc khi tin nhắn nằm trong 1 thread mà bot đã từng được mention trước đó):

  1) Gõ: @AsaPNS !checkinvoice
  2) Bot trả lời, yêu cầu REPLY (trả lời trong thread) vào đúng tin nhắn đó kèm ảnh/PDF.
  3) Bạn bấm Reply vào tin nhắn của bot, đính kèm ảnh/PDF hoá đơn, gửi.
  4) Bot phản hồi ngay "đang kiểm tra", rồi gọi workflow OCR ở nền và gửi kết quả khi xong.

Muốn sửa thông tin cố định: sửa trực tiếp 3 hằng số EXPECTED_* bên dưới rồi deploy lại,
hoặc dùng lệnh nhanh (không cần deploy lại, nhưng sẽ mất khi bot restart):
  !setref ten_don_vi=... | !setref dia_chi=... | !setref mst=...

Cần cấu hình (env var trên Render):
  INSEA_OCR_API_KEY   - API key của workflow "OCR" trên ai.insea.io
                         (Integrate → API Access → Manage API Keys)
"""

import base64
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

FIELDS = {
    "ten_don_vi": ("Tên đơn vị", EXPECTED_TEN_DON_VI),
    "dia_chi": ("Địa chỉ", EXPECTED_DIA_CHI),
    "mst": ("Mã số thuế", EXPECTED_MST),
}

# ─── Cấu hình workflow OCR nội bộ (ai.insea.io) ────────────────────────────

INSEA_OCR_API_URL = "https://ai.insea.io/api/workflows/25086/run"
INSEA_OCR_API_KEY = os.environ.get("INSEA_OCR_API_KEY", "")

# ─── Cache ảnh chờ kiểm tra ─────────────────────────────────────────────────

_pending_images = {}  # group_id -> {"base64":..., "media_type":..., "is_pdf":..., "filename":..., "ts":...}
PENDING_IMAGE_TTL = 600  # 10 phút

_awaiting_check = {}  # group_id -> ts (đang chờ ảnh sau khi gõ !checkinvoice)
AWAITING_TTL = 900  # 15 phút

_ref_overrides = {}  # field_key -> value ghi đè tạm qua !setref (mất khi bot restart)


def get_field_value(field_key: str) -> str:
    if field_key in _ref_overrides:
        return _ref_overrides[field_key]
    return FIELDS[field_key][1]


def set_override(field_key: str, value: str):
    _ref_overrides[field_key] = value


def store_pending_image(group_id: str, b64_data: str, media_type: str, is_pdf: bool, filename: str = "invoice"):
    _pending_images[group_id] = {
        "base64": b64_data,
        "media_type": media_type,
        "is_pdf": is_pdf,
        "filename": filename,
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


def mark_awaiting(group_id: str):
    _awaiting_check[group_id] = time.time()


def is_awaiting(group_id: str) -> bool:
    ts = _awaiting_check.get(group_id)
    if not ts:
        return False
    if time.time() - ts > AWAITING_TTL:
        _awaiting_check.pop(group_id, None)
        return False
    return True


def clear_awaiting(group_id: str):
    _awaiting_check.pop(group_id, None)


# ─── Tải ảnh/file từ SeaTalk ─────────────────────────────────────────────────

def download_seatalk_media(url_or_key: str, seatalk_token: str) -> bytes:
    """Xác nhận từ log thật: link tải nằm ở field "content", cần Bearer token của bot."""
    resp = httpx.get(
        url_or_key,
        headers={"Authorization": f"Bearer {seatalk_token}"},
        timeout=20,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.content


def extract_media_info(message: dict) -> dict | None:
    """Trả về {"url":..., "is_pdf":bool, "media_type":..., "filename":...} nếu message này là ảnh/file.

    Xác nhận từ log thật (2026-07-28): SeaTalk trả link tải ở field "content", ví dụ:
      {"tag": "file", "file": {"content": "https://openapi.seatalk.io/messaging/v2/file/...", "filename": "Hoadon 9223.pdf"}}
    """
    tag = message.get("tag")
    image_obj = message.get("image") if isinstance(message.get("image"), dict) else None
    file_obj = message.get("file") if isinstance(message.get("file"), dict) else None

    if tag == "image" or image_obj:
        obj = image_obj or {}
        url = obj.get("content") or obj.get("url") or obj.get("image_url") or obj.get("download_url")
        if url:
            return {"url": url, "is_pdf": False, "media_type": "image/jpeg", "filename": "invoice.jpg"}

    if tag == "file" or file_obj:
        obj = file_obj or {}
        url = obj.get("content") or obj.get("url") or obj.get("file_url") or obj.get("download_url")
        filename = obj.get("filename") or obj.get("name") or "invoice"
        if url:
            is_pdf = filename.lower().endswith(".pdf") or "pdf" in (obj.get("content_type", "") or "")
            return {
                "url": url,
                "is_pdf": is_pdf,
                "media_type": "application/pdf" if is_pdf else obj.get("content_type", "application/octet-stream"),
                "filename": filename,
            }
    return None


# ─── Gọi workflow OCR nội bộ (ai.insea.io) ─────────────────────────────────

def extract_invoice_data(b64_data: str, media_type: str, is_pdf: bool, filename: str = "invoice") -> dict:
    if not INSEA_OCR_API_KEY:
        raise RuntimeError("Thiếu env var INSEA_OCR_API_KEY.")

    raw_bytes = base64.b64decode(b64_data)

    resp = httpx.post(
        INSEA_OCR_API_URL,
        headers={"Authorization": f"Bearer {INSEA_OCR_API_KEY}"},
        files={"invoice_file": (filename, raw_bytes, media_type)},
        timeout=60,
    )
    resp.raise_for_status()
    payload = resp.json()
    log.info("insea.io OCR workflow response: %s", payload)

    data = payload.get("data", {})
    if data.get("status") != "succeeded":
        err = payload.get("error") or data.get("error") or "workflow không thành công"
        raise RuntimeError(f"Workflow OCR lỗi: {err}")

    outputs = data.get("outputs", {})
    return {
        "ten_don_vi": (outputs.get("customer_name") or "").strip(),
        "dia_chi": (outputs.get("customer_address") or "").strip(),
        "mst": (outputs.get("customer_tax_code") or "").strip(),
        "comparison_result": (outputs.get("comparison_result") or "").strip(),
    }


# ─── So khớp ─────────────────────────────────────────────────────────────────

def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _clean_tokens(s: str) -> list:
    """Chuẩn hoá CHẶT: hạ chữ thường, bỏ dấu câu, tách từ — dùng để so khớp chính xác từng từ."""
    s = (s or "").lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return [t for t in s.split() if t]


def parse_comparison_result(comparison_result: str) -> dict:
    """
    Cố gắng đọc verdict Khớp/Không khớp mà chính hệ thống OCR nội bộ đã tự so (comparison_result),
    dùng làm lớp kiểm tra chéo — hệ thống này so khớp thông minh hơn (bắt được khác biệt từng từ
    trong câu dài), nên nếu nó báo "không khớp" thì ưu tiên tin theo, dù bên mình có thể tính khác.
    """
    result = {}
    label_map = [
        ("tên người mua", "ten_don_vi"),
        ("tên đơn vị", "ten_don_vi"),
        ("mst người mua", "mst"),
        ("mã số thuế", "mst"),
        ("địa chỉ người mua", "dia_chi"),
        ("địa chỉ", "dia_chi"),
    ]
    for line in (comparison_result or "").split("\n"):
        line_low = line.lower()
        for label_text, field_key in label_map:
            if field_key in result:
                continue
            if label_text in line_low:
                if "không khớp" in line_low:
                    result[field_key] = False
                elif "khớp" in line_low:
                    result[field_key] = True
    return result


def compare_invoice(extracted: dict) -> dict:
    lines = []
    counts = {"match": 0, "mismatch": 0, "missing": 0}
    parsed_verdict = parse_comparison_result(extracted.get("comparison_result", ""))

    for field_key in ("ten_don_vi", "dia_chi"):
        label, _default = FIELDS[field_key]
        expected = get_field_value(field_key)
        got = extracted.get(field_key, "")
        if not got:
            counts["missing"] += 1
            lines.append(f"⚠️ {label}: cần \"{expected}\" — hệ thống OCR không đọc được")
            continue

        exp_tokens = _clean_tokens(expected)
        got_tokens = _clean_tokens(got)
        strict_match = exp_tokens == got_tokens

        # Kiểm tra chéo với verdict của chính hệ thống OCR nội bộ (nếu đọc được) — 1 trong 2
        # báo sai lệch thì kết luận cuối là sai lệch, để ưu tiên an toàn/chính xác.
        cross_check = parsed_verdict.get(field_key)
        final_match = strict_match and (cross_check is not False)

        if final_match:
            counts["match"] += 1
            lines.append(f"✅ {label}: khớp (\"{got}\")")
        else:
            counts["mismatch"] += 1
            missing_words = [t for t in exp_tokens if t not in got_tokens]
            extra_words = [t for t in got_tokens if t not in exp_tokens]
            diff_note = ""
            if missing_words or extra_words:
                diff_note = f"\n    Khác biệt: thiếu {missing_words or '(không)'}, thừa/khác {extra_words or '(không)'}"
            lines.append(f"❌ {label}: cần \"{expected}\"\n    Hoá đơn ghi: \"{got}\"{diff_note}")

    expected_mst = get_field_value("mst")
    expected_mst_digits = re.sub(r"[^\d]", "", expected_mst)
    got_mst = extracted.get("mst", "")
    got_mst_digits = re.sub(r"[^\d]", "", got_mst)
    if not got_mst_digits:
        counts["missing"] += 1
        lines.append(f"⚠️ Mã số thuế: cần \"{expected_mst}\" — hệ thống OCR không đọc được")
    elif expected_mst_digits and expected_mst_digits == got_mst_digits and parsed_verdict.get("mst") is not False:
        counts["match"] += 1
        lines.append(f"✅ Mã số thuế: khớp ({got_mst})")
    else:
        counts["mismatch"] += 1
        lines.append(f"❌ Mã số thuế: cần \"{expected_mst}\" — hoá đơn ghi \"{got_mst}\"")

    return {"lines": lines, "counts": counts, "comparison_result": extracted.get("comparison_result", "")}


def format_result_message(result: dict) -> str:
    c = result["counts"]
    overall = (
        "✅ ĐÃ KHỚP — có thể duyệt"
        if c["mismatch"] == 0 and c["missing"] == 0
        else "🔴 CẦN KIỂM TRA LẠI trước khi duyệt"
    )
    summary = f"({c['match']} khớp · {c['mismatch']} sai lệch · {c['missing']} thiếu)\n\n"
    body = "\n".join(result["lines"])
    extra = ""
    if result.get("comparison_result"):
        extra = f"\n\n(Ghi chú thêm từ hệ thống OCR nội bộ: {result['comparison_result']})"
    return f"📋 Đối chiếu thông tin đơn vị mua trên hoá đơn\n{overall}\n{summary}{body}{extra}"


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
