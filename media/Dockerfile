FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    imagemagick \
    ghostscript \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# Allow ImageMagick to read/write all formats (default policy restricts PDF etc.)
RUN sed -i 's/rights="none" pattern="PDF"/rights="read|write" pattern="PDF"/' \
    /etc/ImageMagick-6/policy.xml 2>/dev/null || true

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

COPY dc/ dc/

ENTRYPOINT ["dc"]
