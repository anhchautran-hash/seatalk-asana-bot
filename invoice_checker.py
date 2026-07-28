"""
invoice_checker.py
===================
Tính năng !checkinvoice cho bot AsaPNS — bản dùng OCR miễn phí (Tesseract),
KHÔNG cần API key Anthropic hay bất kỳ tài khoản trả phí nào.

Chỉ kiểm tra 3 thông tin cố định của bên mua (Garena) trên hoá đơn nháp:
  - Tên đơn vị
  - Địa chỉ
  - Mã số thuế

LUỒNG DÙNG (Cách B — qua thread reply, vì SeaTalk chỉ gửi webhook cho bot khi có mention
hoặc khi tin nhắn nằm trong 1 thread mà bot đã từng được mention trước đó):

  1) Gõ: @AsaPNS !checkinvoice
  2) Bot trả lời, yêu cầu REPLY (trả lời trong thread) vào đúng tin nhắn đó kèm ảnh/PDF.
  3) Bạn bấm Reply vào tin nhắn của bot, đính kèm ảnh/PDF hoá đơn, gửi.
  4) Bot tự nhận diện ảnh/PDF trong thread đó và chạy kiểm tra ngay, không cần gõ lệnh lại.

Muốn sửa thông tin cố định: sửa trực tiếp 3 hằng số EXPECTED_* bên dưới rồi deploy lại,
hoặc dùng lệnh nhanh (không cần deploy lại, nhưng sẽ mất khi bot restart):
  !setref ten_don_vi=... | !setref dia_chi=... | !setref mst=...

Cần cài thêm gói hệ thống tesseract-ocr (+ gói ngôn ngữ tiếng Việt) và poppler-utils
(để đọc PDF) — xem Dockerfile đính kèm. Không cần env var nào cả.
"""

import base64
import io
import logging
import re
import time
import unicodedata

from PIL import Image
import pytesseract
from pdf2image import convert_from_bytes

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

OCR_LANGS = "vie+eng"  # cần đã cài gói ngôn ngữ tesseract-ocr-vie

# ─── Cache ảnh chờ kiểm tra ─────────────────────────────────────────────────

_pending_images = {}  # group_id -> {"base64":..., "media_type":..., "is_pdf":..., "ts":...}
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
    """
    LƯU Ý: main.py hiện đã log toàn bộ payload webhook. Khi test gửi thử 1 ảnh vào group,
    xem log Render để biết chính xác field chứa URL ảnh và chỉnh extract_media_info()
    bên dưới nếu cần. Hàm này giả định url_or_key là URL tải trực tiếp kèm Bearer token.
    """
    import httpx

    resp = httpx.get(
        url_or_key,
        headers={"Authorization": f"Bearer {seatalk_token}"},
        timeout=20,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.content


def extract_media_info(message: dict) -> dict | None:
    """Trả về {"url":..., "is_pdf":bool, "media_type":...} nếu message này là ảnh/file.

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
            return {"url": url, "is_pdf": False, "media_type": "image/jpeg"}

    if tag == "file" or file_obj:
        obj = file_obj or {}
        url = obj.get("content") or obj.get("url") or obj.get("file_url") or obj.get("download_url")
        filename = (obj.get("filename") or obj.get("name") or "").lower()
        if url:
            is_pdf = filename.endswith(".pdf") or "pdf" in (obj.get("content_type", "") or "")
            return {
                "url": url,
                "is_pdf": is_pdf,
                "media_type": "application/pdf" if is_pdf else obj.get("content_type", "application/octet-stream"),
            }
    return None


# ─── OCR: đọc chữ trên hoá đơn (Tesseract, miễn phí, chạy local) ────────────

def extract_invoice_data(b64_data: str, media_type: str, is_pdf: bool) -> dict:
    raw_bytes = base64.b64decode(b64_data)
    texts = []

    if is_pdf:
        try:
            # Chỉ render trang 1 ở độ phân giải vừa phải để tránh tràn RAM
            # (gói Free trên Render thường giới hạn RAM rất thấp).
            pages = convert_from_bytes(raw_bytes, dpi=150, first_page=1, last_page=1)
        except Exception as e:
            raise RuntimeError(f"Không đọc được file PDF: {e}")
        for page in pages:
            texts.append(pytesseract.image_to_string(page, lang=OCR_LANGS))
    else:
        try:
            img = Image.open(io.BytesIO(raw_bytes))
        except Exception as e:
            raise RuntimeError(f"Không đọc được file ảnh: {e}")
        texts.append(pytesseract.image_to_string(img, lang=OCR_LANGS))

    raw_text = "\n".join(texts)
    log.info("OCR raw_text (%d ký tự): %s", len(raw_text), raw_text[:500])
    return {"raw_text": raw_text}


# ─── So khớp ─────────────────────────────────────────────────────────────────

def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFC", s or "")
    return re.sub(r"\s+", " ", s.strip()).lower()


def _tokens(s: str) -> list:
    s = unicodedata.normalize("NFC", s or "")
    return [t for t in re.findall(r"[^\W\d_]+", s.lower(), flags=re.UNICODE) if len(t) >= 2]


def _fuzzy_contains(expected: str, text_norm: str, threshold: float = 0.8) -> bool:
    """OCR có thể lệch vài ký tự/dấu câu — so theo % từ khớp thay vì so nguyên câu 1:1."""
    toks = _tokens(expected)
    if not toks:
        return False
    found = sum(1 for t in toks if t in text_norm)
    return (found / len(toks)) >= threshold


def _best_matching_line(expected: str, raw_text: str) -> tuple:
    """
    So khớp CHẶT HƠN: chỉ tính % từ khớp trong TỪNG DÒNG (hoặc 2 dòng liền kề gộp lại,
    để không bị hỏng khi địa chỉ dài bị OCR ngắt xuống dòng) — không gộp cả văn bản,
    để tránh trường hợp các từ trùng khớp bị "nhặt" rải rác từ nhiều dòng/trường khác nhau
    (VD: dòng bên bán + dòng bên mua cộng lại vô tình đủ % từ).
    Trả về (dòng khớp nhất, điểm khớp 0..1).
    """
    toks = _tokens(expected)
    if not toks:
        return "", 0.0
    raw_lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
    candidates = list(raw_lines)
    for i in range(len(raw_lines) - 1):
        candidates.append(f"{raw_lines[i]} {raw_lines[i + 1]}")

    best_line, best_score = "", 0.0
    for line in candidates:
        line_norm = _normalize(line)
        found = sum(1 for t in toks if t in line_norm)
        score = found / len(toks)
        if score > best_score:
            best_score = score
            best_line = line
    return best_line, best_score


def compare_invoice(extracted: dict) -> dict:
    raw_text = extracted.get("raw_text", "")
    raw_digits = re.sub(r"[^\d]", "", raw_text)

    lines = []
    counts = {"match": 0, "mismatch": 0}

    # Tên đơn vị & Địa chỉ: so khớp CHẶT trong từng dòng (không gộp cả văn bản),
    # ngưỡng cao hơn (75%) vì giờ đã so trong phạm vi 1 dòng nên khó bị "ăn may".
    # Luôn hiển thị nguyên văn dòng OCR đọc được để tự kiểm tra lại bằng mắt.
    for field_key in ("ten_don_vi", "dia_chi"):
        label, _default = FIELDS[field_key]
        expected = get_field_value(field_key)
        best_line, score = _best_matching_line(expected, raw_text)
        threshold = 0.75
        if score >= threshold:
            counts["match"] += 1
            lines.append(f"✅ {label}: khớp\n    OCR đọc: \"{best_line}\"")
        else:
            counts["mismatch"] += 1
            lines.append(
                f"❌ {label}: cần \"{expected}\"\n"
                f"    OCR đọc được (gần nhất): \"{best_line or '(không thấy dòng nào tương ứng)'}\""
            )

    expected_mst = get_field_value("mst")
    expected_mst_digits = re.sub(r"[^\d]", "", expected_mst)
    if expected_mst_digits and expected_mst_digits in raw_digits:
        counts["match"] += 1
        lines.append(f"✅ Mã số thuế: khớp ({expected_mst})")
    else:
        counts["mismatch"] += 1
        lines.append(f"❌ Mã số thuế: KHÔNG tìm thấy \"{expected_mst}\" trên hoá đơn (hoặc OCR đọc chưa rõ)")

    return {"lines": lines, "counts": counts}


def format_result_message(result: dict) -> str:
    c = result["counts"]
    overall = "✅ ĐÃ KHỚP — có thể duyệt" if c["mismatch"] == 0 else "🔴 CẦN KIỂM TRA LẠI trước khi duyệt"
    summary = f"({c['match']} khớp · {c['mismatch']} sai lệch)\n\n"
    body = "\n".join(result["lines"])
    note = "\n\n(Đọc bằng OCR miễn phí — nếu ảnh mờ/nghiêng có thể đọc sai, nên kiểm tra lại bằng mắt khi thấy ❌.)"
    return f"📋 Đối chiếu thông tin đơn vị mua trên hoá đơn\n{overall}\n{summary}{body}{note}"


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
