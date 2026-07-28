FROM python:3.11-slim

WORKDIR /app

# Cài Tesseract OCR (đọc chữ ảnh, miễn phí) + gói ngôn ngữ tiếng Việt,
# và poppler-utils (để chuyển trang PDF thành ảnh trước khi OCR)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-vie \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
