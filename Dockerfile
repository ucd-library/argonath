ARG DAGSTER_IMAGE
ARG CASKFS_IMAGE
FROM ${CASKFS_IMAGE} as caskfs
FROM ${DAGSTER_IMAGE}

RUN apt-get update && \
    apt-get install -y wget gnupg2 lsb-release && \
    echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list && \
    wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor > /etc/apt/trusted.gpg.d/postgres.gpg && \
    apt-get update && \
    apt-get install -y \
    build-essential \
    make \
    tesseract-ocr \
    tesseract-ocr-eng \
    imagemagick \
    ghostscript \
    ffmpeg \
    libmagic1 \
    postgresql-client-16 && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

# openai-whisper's local ASR model is downloaded lazily on first `whisper` invocation
# (cached under ~/.cache/whisper) rather than baked in here — see docs/cask-conventions.md's
# Captions/Transcripts section. The deployment environment needs a persistent volume at
# that cache path so the model isn't re-downloaded on every container restart.
RUN uv pip install --system --no-cache-dir openai-whisper

# Allow ImageMagick to read/write all formats (default policy restricts PDF etc.)
RUN sed -i 's/rights="none" pattern="PDF"/rights="read|write" pattern="PDF"/' \
    /etc/ImageMagick-6/policy.xml 2>/dev/null || true

# Install caskfs from the caskfs image
RUN mkdir -p /opt/caskfs
WORKDIR /opt/caskfs
COPY --from=caskfs /opt/caskfs/package.json package.json
RUN npm install --omit=dev
COPY --from=caskfs /opt/caskfs/src src
RUN cd /opt/caskfs && npm install -g
RUN mkdir -p /cork-build-info
COPY --from=caskfs /cork-build-info/caskfs.json /cork-build-info/caskfs.json

# Install argonath
RUN mkdir -p /opt/argonath/dagster
RUN mkdir -p /opt/argonath/exec
WORKDIR /opt/argonath

COPY exec/pyproject.toml exec/pyproject.toml
RUN cd exec && uv pip install --system --no-cache-dir -r pyproject.toml

COPY exec/digtk exec/digtk
RUN cd exec && uv pip install --system --no-cache-dir --no-deps -e .

# Install dagster configuration files
COPY dagster/lib dagster/lib
COPY dagster/dagster.yaml dagster/dagster.yaml
COPY dagster/celery_config.yaml dagster/celery_config.yaml
COPY dagster/workspace.yaml dagster/workspace.yaml
