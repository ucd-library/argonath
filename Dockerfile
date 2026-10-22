ARG DAGSTER_IMAGE
ARG CASKFS_IMAGE
FROM ${CASKFS_IMAGE} as caskfs
FROM ${DAGSTER_IMAGE}

# veraPDF version to install below (validate_ua()'s dependency, see
# exec/digtk/digtk/models/pdf_tag.py) — pinned deliberately, matching the
# current default in veraPDF's own Dockerfile (veraPDF/veraPDF-apps,
# integration branch). Bump both parts together when upgrading.
ARG VERAPDF_VERSION=1.30
ARG VERAPDF_MINOR_VERSION=2

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
    poppler-utils \
    ffmpeg \
    libmagic1 \
    postgresql-client-16 \
    unzip \
    default-jre-headless && \
    rm -rf /var/lib/apt/lists/*

# veraPDF's own installer (IzPack-based, needs a JRE to run) — downloaded
# from their official release server, installed unattended via their own
# docker-install.xml (see that file's header for provenance), verified with
# a real --version call so a broken install fails the build loudly rather
# than shipping a Docker image where `digtk accessibility tag-pdf --validate`
# silently can't find veraPDF. Java stays installed afterward (default-jre-headless,
# above — the version-agnostic metapackage, since a version-pinned openjdk-*
# package name breaks across base-image OS upgrades: this already happened
# once, openjdk-17-jre-headless doesn't exist on this image's Debian trixie,
# only openjdk-21) since the resulting `verapdf` script also needs it at
# runtime, not just for the installer.
COPY docker-install.xml /tmp/docker-install.xml
RUN wget --quiet -O /tmp/verapdf-installer.zip \
    "https://software.verapdf.org/releases/${VERAPDF_VERSION}/verapdf-greenfield-${VERAPDF_VERSION}.${VERAPDF_MINOR_VERSION}-installer.zip" && \
    cd /tmp && unzip -q verapdf-installer.zip && \
    java -jar "verapdf-greenfield-${VERAPDF_VERSION}.${VERAPDF_MINOR_VERSION}/verapdf-izpack-installer-${VERAPDF_VERSION}.${VERAPDF_MINOR_VERSION}.jar" docker-install.xml && \
    ln -s /opt/verapdf/verapdf /usr/local/bin/verapdf && \
    verapdf --version && \
    rm -rf /tmp/verapdf-installer.zip "/tmp/verapdf-greenfield-${VERAPDF_VERSION}.${VERAPDF_MINOR_VERSION}" /tmp/docker-install.xml

RUN pip install --no-cache-dir uv

# openai-whisper's local ASR model is downloaded lazily on first `whisper` invocation
# (cached under ~/.cache/whisper) rather than baked in here — see docs/cask-conventions.md's
# Captions/Transcripts section. The deployment environment needs a persistent volume at
# that cache path so the model isn't re-downloaded on every container restart.
# torch is installed from PyTorch's CPU-only wheel index (not PyPI's default, CUDA-bundled
# build) since no deployment runs Whisper on a GPU — the default GPU wheel drags in the full
# CUDA runtime (nvidia-cublas/cudnn/cufft/nccl, triton, etc.), several GB of unused libraries.
RUN uv pip install --system --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch && \
    uv pip install --system --no-cache-dir openai-whisper

# Allow ImageMagick to read/write all formats (default policy restricts PDF etc.)
RUN sed -i 's/rights="none" pattern="PDF"/rights="read|write" pattern="PDF"/' \
    /etc/ImageMagick-6/policy.xml 2>/dev/null || true

# Install caskfs from the caskfs image
RUN mkdir -p /opt/caskfs
WORKDIR /opt/caskfs
COPY --from=caskfs /opt/caskfs/package.json package.json
RUN npm install --omit=dev
COPY --from=caskfs /opt/caskfs/src src
RUN npm install -g
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
COPY dagster/defs.py dagster/defs.py
COPY dagster/dagster.yaml dagster/dagster.yaml
COPY dagster/celery_config.yaml dagster/celery_config.yaml
COPY dagster/workspace.yaml dagster/workspace.yaml
