# RAG-Core Worker：Linux x64 + 预灌 OCR 运行时（构建上下文 = 仓库根）
#
# 上游 pdf-inspector 只在 Linux x64 端到端验证 OCR 路径（macOS / Windows 属 preview），
# 所以 Worker 只跑这个镜像：本机 Windows 不再跑 Worker。
#
#   docker compose -f deploy/docker-compose.yml build worker
#
# 运行期不联网：PDFIUM_LIB_PATH / ORT_DYLIB_PATH 指向镜像内的 .so，PP-OCRv6 模型预灌进
# PDF_INSPECTOR_MODEL_CACHE，OCR_OFFLINE=true 也能命中缓存。构建期才允许下载。

FROM python:3.12-slim-bookworm

# uv：与宿主机同源，保证 uv.lock 可复现。
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# curl 用于构建期拉取 OCR 运行时与模型（slim 镜像不自带）；装完立即清 apt 列表。
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# 1) 依赖层：只有 pyproject.toml / uv.lock 变了才重建。
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-group dev --no-install-project

# 2) OCR 原生运行时（Linux x64）：摊平成 /opt/ocr/lib/libpdfium.so 与 libonnxruntime.so。
ARG PDFIUM_URL=https://github.com/firecrawl/pdfium-rs/releases/download/native-v7988/firecrawl-pdfium-linux-x64.tgz
ARG PDFIUM_SHA256=6248189e07bbc33cdeb31976c539a88614307c8a19f3276dbd018efbe5b4a2a2
ARG ORT_URL=https://github.com/microsoft/onnxruntime/releases/download/v1.27.0/onnxruntime-linux-x64-1.27.0.tgz
ARG ORT_SHA256=547e40a48f1fe73e3f812d7c88a948612c23f896b91e4e2ee1e232d7b468246f
RUN set -eux; \
    mkdir -p /opt/ocr/lib; \
    cd /tmp; \
    curl -fsSL "$PDFIUM_URL" -o pdfium.tgz; \
    curl -fsSL "$ORT_URL" -o ort.tgz; \
    echo "$PDFIUM_SHA256  pdfium.tgz" | sha256sum -c -; \
    echo "$ORT_SHA256  ort.tgz" | sha256sum -c -; \
    tar -xzf pdfium.tgz; \
    tar -xzf ort.tgz; \
    cp -L "$(find . -name 'libpdfium.so' -print -quit)" /opt/ocr/lib/libpdfium.so; \
    cp -L "$(find . -name 'libonnxruntime.so' -print -quit)" /opt/ocr/lib/libonnxruntime.so; \
    test "$(stat -c%s /opt/ocr/lib/libpdfium.so)" = 7668864; \
    test "$(stat -c%s /opt/ocr/lib/libonnxruntime.so)" = 23658512; \
    rm -rf /tmp/*

ENV PDFIUM_LIB_PATH=/opt/ocr/lib/libpdfium.so \
    ORT_DYLIB_PATH=/opt/ocr/lib/libonnxruntime.so \
    PDF_INSPECTOR_MODEL_CACHE=/opt/ocr/models

# 3) OCR 模型：预灌进镜像缓存目录，运行期不再联网。
#    目录布局必须是 <cache_root>/pp-ocrv6-small/oar-ocr-v0.7.0/。
ARG MODEL_BASE=https://github.com/GreatV/oar-ocr/releases/download/v0.7.0
ARG MODEL_DET_SHA256=d73e0058b7a8086bbd57f3d10b8bcd4ff95363f67e06e2762b5e814fe9c9410e
ARG MODEL_REC_SHA256=5435fd747c9e0efe15a96d0b378d5bd157e9492ed8fd80edf08f30d02fa24634
ARG MODEL_DICT_SHA256=b5f2bfe2bdd9448429e3e82b51c789775d9b42f2403d082b00662eb77e401c5d
RUN set -eux; \
    mkdir -p /opt/ocr/models/pp-ocrv6-small/oar-ocr-v0.7.0; \
    cd /opt/ocr/models/pp-ocrv6-small/oar-ocr-v0.7.0; \
    curl -fsSL "$MODEL_BASE/pp-ocrv6_small_det.onnx" -o pp-ocrv6_small_det.onnx; \
    curl -fsSL "$MODEL_BASE/pp-ocrv6_small_rec.onnx" -o pp-ocrv6_small_rec.onnx; \
    curl -fsSL "$MODEL_BASE/ppocrv6_dict.txt" -o ppocrv6_dict.txt; \
    echo "$MODEL_DET_SHA256  pp-ocrv6_small_det.onnx" | sha256sum -c -; \
    echo "$MODEL_REC_SHA256  pp-ocrv6_small_rec.onnx" | sha256sum -c -; \
    echo "$MODEL_DICT_SHA256  ppocrv6_dict.txt" | sha256sum -c -; \
    test "$(stat -c%s pp-ocrv6_small_det.onnx)" = 9880512; \
    test "$(stat -c%s pp-ocrv6_small_rec.onnx)" = 21159378; \
    test "$(stat -c%s ppocrv6_dict.txt)" = 74947

# 4) 项目本体。
COPY . .
# 4b) Qwen 分词词表：切块窗口按 token 计长，词表是**必需资产**（ADR-0015）。
#     缺它 Worker 会在 `worker_process_init` 里直接拒绝启动——这是有意的，
#     比起「跑起来之后切出不可复现的片」，启动失败更好查。
#     脚本自带字节数与 SHA256 双重校验，运行期零网络。
#
#     `PYTHONPATH=/app` 不能省：脚本要 `from app.utils.tokenizer import EXPECTED_SHA256`
#     （校验值只有一份真相，不在这里抄第二份），而这一步排在 `uv sync` 装项目**之前**
#     ——`--no-install-project` 的依赖层只装了第三方包。缺它 `import app` 直接
#     ModuleNotFoundError，镜像构建在这一行失败。
RUN set -eux; \
    PYTHONPATH=/app QWEN_VOCAB_DIR=/app/.tokenizer \
    uv run --no-sync python scripts/fetch_qwen_vocab.py; \
    test "$(stat -c%s /app/.tokenizer/qwen.tiktoken)" = 2561218
ENV QWEN_TOKENIZER_PATH=/app/.tokenizer/qwen.tiktoken
RUN uv sync --frozen --no-group dev

# 5) 非 root 运行；上传目录与宿主机共享（API 与 Worker 必须指向同一个 DOCUMENT_STORAGE_PATH）。
#
# 三个目录都要**先建好并 chown**，否则进程一启动就 PermissionError：
#   - data/uploads：上传原件（与宿主机绑定挂载）
#   - logs/：structlog 的三路文件日志（`setup_logging()` 会 mkdir）
#   - eval/runs：旁证跑批产物（命名卷挂在这里；卷的属主从镜像里这一层继承）
# 只 chown /app/data 是不够的——那正是 api 容器第一次起来就 Exited(1) 的原因。
RUN useradd -m -u 10001 app \
 && mkdir -p /app/data/uploads /app/logs /app/eval/runs \
 && chown -R app:app /app/data /app/logs /app/eval
USER app

# Celery app 的变量名是 celery_app（不是 app），所以必须写 app.celery_app:celery_app。
CMD ["celery", "-A", "app.celery_app:celery_app", "worker", "--loglevel=info", "--concurrency=2"]
