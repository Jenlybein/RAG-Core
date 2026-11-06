#!/usr/bin/env bash
# 下载并校验 Linux x64 的 OCR 原生运行时与 PP-OCRv6 模型（可幂等重跑）。
#
# 产物：
#   $OCR_LIB_DIR/libpdfium.so       ← firecrawl-pdfium-linux-x64.tgz（native-v7988）
#   $OCR_LIB_DIR/libonnxruntime.so  ← onnxruntime-linux-x64-1.27.0.tgz
#   $OCR_MODEL_CACHE/pp-ocrv6-small/oar-ocr-v0.7.0/{det,rec}.onnx + ppocrv6_dict.txt
#
# 目标路径默认落在仓库根的 .ocr/ 下（.dockerignore 已排除，不进镜像）；CI 可用
# OCR_LIB_DIR / OCR_MODEL_CACHE 指到别处，并把 OCR_DOWNLOAD_CACHE 指到 actions/cache
# 覆盖的 ~/.cache/ocr/{pdfium,onnxruntime,models}，这样缓存命中时只做本地拷贝。
#
# 任何一个文件的字节数或 SHA-256 对不上 → 打印差异并 exit 1（绝不静默使用可疑资产）。
#
# 用法：
#   bash deploy/ocr/fetch_runtime.sh
#   OCR_LIB_DIR=... OCR_MODEL_CACHE=... OCR_DOWNLOAD_CACHE=... bash deploy/ocr/fetch_runtime.sh
#
# 注意：本脚本只负责「拿资产」，不负责在容器里安装——镜像里的安装见 deploy/Dockerfile。

set -euo pipefail

PDFIUM_RELEASE="native-v7988"
PDFIUM_ARCHIVE="firecrawl-pdfium-linux-x64.tgz"
PDFIUM_URL="https://github.com/firecrawl/pdfium-rs/releases/download/${PDFIUM_RELEASE}/${PDFIUM_ARCHIVE}"
PDFIUM_ARCHIVE_SIZE=3676776
PDFIUM_ARCHIVE_SHA256="6248189e07bbc33cdeb31976c539a88614307c8a19f3276dbd018efbe5b4a2a2"
PDFIUM_LIB="libpdfium.so"
PDFIUM_LIB_SIZE=7668864
PDFIUM_LIB_SHA256="0c6b5e32e878b04784ced0995dc42c6f106ad02348b2fd3aa89d7886e075b66e"

ORT_VERSION="1.27.0"
ORT_ARCHIVE="onnxruntime-linux-x64-${ORT_VERSION}.tgz"
ORT_URL="https://github.com/microsoft/onnxruntime/releases/download/v${ORT_VERSION}/${ORT_ARCHIVE}"
ORT_ARCHIVE_SIZE=8831605
ORT_ARCHIVE_SHA256="547e40a48f1fe73e3f812d7c88a948612c23f896b91e4e2ee1e232d7b468246f"
ORT_LIB="libonnxruntime.so"
ORT_LIB_SIZE=23658512
ORT_LIB_SHA256="4061866361d9a8d2872f5f419c5515ce35a830a0c5c77ce1723320ac0dbabfc7"

MODEL_RELEASE_TAG="v0.7.0"           # GitHub release 标签
MODEL_REVISION="oar-ocr-v0.7.0"      # 库的模型缓存 revision 目录名（<cache>/pp-ocrv6-small/<revision>/）
MODEL_SET="pp-ocrv6-small"
MODEL_BASE_URL="https://github.com/GreatV/oar-ocr/releases/download/${MODEL_RELEASE_TAG}"
MODEL_DET="pp-ocrv6_small_det.onnx"
MODEL_REC="pp-ocrv6_small_rec.onnx"
MODEL_DICT="ppocrv6_dict.txt"
MODEL_DET_SIZE=9880512
MODEL_REC_SIZE=21159378
MODEL_DICT_SIZE=74947
MODEL_DET_SHA256="d73e0058b7a8086bbd57f3d10b8bcd4ff95363f67e06e2762b5e814fe9c9410e"
MODEL_REC_SHA256="5435fd747c9e0efe15a96d0b378d5bd157e9492ed8fd80edf08f30d02fa24634"
MODEL_DICT_SHA256="b5f2bfe2bdd9448429e3e82b51c789775d9b42f2403d082b00662eb77e401c5d"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LIB_DIR="${OCR_LIB_DIR:-$REPO_ROOT/.ocr/lib}"
MODEL_CACHE="${OCR_MODEL_CACHE:-$REPO_ROOT/.ocr/models}"
DOWNLOAD_CACHE="${OCR_DOWNLOAD_CACHE:-$HOME/.cache/ocr}"

log() { printf '[fetch_runtime] %s\n' "$*"; }
die() {
    printf '[fetch_runtime] ERROR: %s\n' "$*" >&2
    exit 1
}

file_size() { wc -c <"$1" | tr -d '[:space:]'; }

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        # coreutils 会给含反斜杠的路径加转义前缀（Git Bash / Windows 路径）；
        # 摘出来的摘要因此可能带一个前导 '\'，tr 去掉它（十六进制里不会出现反斜杠）。
        sha256sum "$1" | cut -d' ' -f1 | tr -d '\\'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    else
        die "需要 sha256sum 或 shasum 来校验资产"
    fi
}

# 校验单个文件；不一致时打印期望/实际并 exit 1。
assert_file() { # path expected_size expected_sha label
    local path="$1" expected_size="$2" expected_sha="$3" label="$4"
    [ -f "$path" ] || die "$label 不存在：$path"
    local actual_size actual_sha
    actual_size="$(file_size "$path")"
    if [ "$actual_size" != "$expected_size" ]; then
        die "$label 字节数不符：期望 $expected_size，实际 $actual_size（$path）"
    fi
    actual_sha="$(sha256_of "$path")"
    if [ "$actual_sha" != "$expected_sha" ]; then
        die "$label SHA-256 不符：期望 $expected_sha，实际 $actual_sha（$path）"
    fi
}

# 已校验过（尺寸 + 摘要都对）就跳过，避免缓存命中的 CI 重复下载。
is_valid() { # path expected_size expected_sha
    local path="$1" expected_size="$2" expected_sha="$3"
    [ -f "$path" ] || return 1
    [ "$(file_size "$path")" = "$expected_size" ] || return 1
    [ "$(sha256_of "$path")" = "$expected_sha" ] || return 1
}

download() { # url dest label
    local url="$1" dest="$2" label="$3"
    mkdir -p "$(dirname "$dest")"
    log "下载 $label ← $url"
    curl -fsSL --retry 3 --retry-delay 2 "$url" -o "$dest"
}

ensure_archive() { # url dest expected_size expected_sha label
    local url="$1" dest="$2" expected_size="$3" expected_sha="$4" label="$5"
    if is_valid "$dest" "$expected_size" "$expected_sha"; then
        log "缓存命中：$label"
        return 0
    fi
    download "$url" "$dest.partial" "$label"
    assert_file "$dest.partial" "$expected_size" "$expected_sha" "$label"
    mv "$dest.partial" "$dest"
}

install_lib() { # archive lib_name expected_size expected_sha
    local archive="$1" lib="$2" expected_size="$3" expected_sha="$4"
    local target="$LIB_DIR/$lib"
    if is_valid "$target" "$expected_size" "$expected_sha"; then
        log "缓存命中：$target"
        return 0
    fi
    local work_dir
    work_dir="$(mktemp -d)"
    # 在压缩包所在目录里按 basename 解压：Windows 路径里的 "C:" 会被 GNU tar 当成远端主机。
    if ! (cd "$(dirname "$archive")" && tar -xzf "$(basename "$archive")" -C "$work_dir"); then
        # Windows / Git Bash 建不了符号链接，tar 只在该步报错；Linux（CI 与容器）不受影响。
        [ "$(uname -s)" = "Linux" ] && die "$archive 解压失败"
        log "警告：解压 $archive 时符号链接创建失败（非 Linux 权限限制），改用版本化实体 .so"
    fi
    # ONNX Runtime 的 libonnxruntime.so 是链接（→ .so.1 → .so.1.27.0），cp -L 取实体内容。
    local source
    source="$(find "$work_dir" -name "$lib" -print -quit)"
    if [ -z "$source" ]; then
        # 非 Linux 上链接没建出来时，退回按版本名匹配的实体文件（如 libonnxruntime.so.1.27.0）。
        source="$(find "$work_dir" -name "${lib}.*" -type f -print -quit)"
    fi
    [ -n "$source" ] || die "$archive 里找不到 $lib"
    mkdir -p "$LIB_DIR"
    cp -L "$source" "$target.partial"
    assert_file "$target.partial" "$expected_size" "$expected_sha" "$lib"
    mv "$target.partial" "$target"
    rm -rf "$work_dir"
    log "已安装 $target"
}

ensure_model() { # filename expected_size expected_sha
    local name="$1" expected_size="$2" expected_sha="$3"
    local cached="$DOWNLOAD_CACHE/models/$MODEL_SET/$MODEL_REVISION/$name"
    local target="$MODEL_CACHE/$MODEL_SET/$MODEL_REVISION/$name"

    if is_valid "$target" "$expected_size" "$expected_sha"; then
        log "缓存命中：$target"
        return 0
    fi
    if ! is_valid "$cached" "$expected_size" "$expected_sha"; then
        download "$MODEL_BASE_URL/$name" "$cached.partial" "$MODEL_SET/$name"
        assert_file "$cached.partial" "$expected_size" "$expected_sha" "$MODEL_SET/$name"
        mv "$cached.partial" "$cached"
    fi
    mkdir -p "$(dirname "$target")"
    cp "$cached" "$target"
    assert_file "$target" "$expected_size" "$expected_sha" "$MODEL_SET/$name"
    log "已安装 $target"
}

[ "$(uname -s)" = "Linux" ] || log "警告：非 Linux（$(uname -s)）；上游只在 Linux x64 端到端验证 OCR 路径"
command -v curl >/dev/null 2>&1 || die "需要 curl"

log "仓库根：$REPO_ROOT"
log "原生库目录：$LIB_DIR"
log "模型缓存目录：$MODEL_CACHE"
log "下载缓存：$DOWNLOAD_CACHE"

ensure_archive "$PDFIUM_URL" "$DOWNLOAD_CACHE/pdfium/$PDFIUM_ARCHIVE" \
    "$PDFIUM_ARCHIVE_SIZE" "$PDFIUM_ARCHIVE_SHA256" "$PDFIUM_ARCHIVE"
install_lib "$DOWNLOAD_CACHE/pdfium/$PDFIUM_ARCHIVE" "$PDFIUM_LIB" \
    "$PDFIUM_LIB_SIZE" "$PDFIUM_LIB_SHA256"

ensure_archive "$ORT_URL" "$DOWNLOAD_CACHE/onnxruntime/$ORT_ARCHIVE" \
    "$ORT_ARCHIVE_SIZE" "$ORT_ARCHIVE_SHA256" "$ORT_ARCHIVE"
install_lib "$DOWNLOAD_CACHE/onnxruntime/$ORT_ARCHIVE" "$ORT_LIB" \
    "$ORT_LIB_SIZE" "$ORT_LIB_SHA256"

ensure_model "$MODEL_DET" "$MODEL_DET_SIZE" "$MODEL_DET_SHA256"
ensure_model "$MODEL_REC" "$MODEL_REC_SIZE" "$MODEL_REC_SHA256"
ensure_model "$MODEL_DICT" "$MODEL_DICT_SIZE" "$MODEL_DICT_SHA256"

log "完成。运行时环境变量："
printf '  export PDFIUM_LIB_PATH=%s\n' "$LIB_DIR/$PDFIUM_LIB"
printf '  export ORT_DYLIB_PATH=%s\n' "$LIB_DIR/$ORT_LIB"
printf '  export PDF_INSPECTOR_MODEL_CACHE=%s\n' "$MODEL_CACHE"
