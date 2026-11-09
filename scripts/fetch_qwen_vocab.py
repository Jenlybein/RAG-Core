"""下载并校验 Qwen 分词词表（可幂等重跑）。

词表来自 `dashscope` 的 wheel：`dashscope/legacy_1x/tokenizers/qwen.tiktoken`。
**不把它作为依赖装进来**——为 2.56 MB 词表装 54 个包（约 45 MB）不划算，
所以直接从 PyPI 取 wheel 并按 zip 内路径抽取。

为什么是**必需资产**：切块按 token 计长，词表决定每一片落在哪。缺词表就只能用近似
词表替代，而近似会让同一库在不同部署上切出不同的片，且差异完全静默。所以缺失即
拒绝启动（见 `app/utils/tokenizer.py`），而不是降级。

用法：

    uv run python scripts/fetch_qwen_vocab.py
    QWEN_VOCAB_DIR=/somewhere uv run python scripts/fetch_qwen_vocab.py

产物：`<默认 .tokenizer>/qwen.tiktoken`（`.gitignore` 已排除，不进仓库）。
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import tempfile
import urllib.request
import zipfile

from app.utils.tokenizer import EXPECTED_SHA256 as EXPECTED_SHA256_FROM_APP
from app.utils.tokenizer import EXPECTED_SIZE as EXPECTED_SIZE_FROM_APP

DASHSCOPE_VERSION = "2.0.4"
WHEEL_PATH_IN_ARCHIVE = "dashscope/legacy_1x/tokenizers/qwen.tiktoken"
VOCAB_NAME = "qwen.tiktoken"
# 字节数与 SHA-256 只从 tokenizer 那边取一份：两个地方各写一遍，迟早有一个没跟上。
EXPECTED_SIZE = EXPECTED_SIZE_FROM_APP
EXPECTED_SHA256 = EXPECTED_SHA256_FROM_APP

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def target_path() -> pathlib.Path:
    directory = os.environ.get("QWEN_VOCAB_DIR") or str(REPO_ROOT / ".tokenizer")
    return pathlib.Path(directory) / VOCAB_NAME


def _is_valid(path: pathlib.Path) -> bool:
    if not path.is_file():
        return False
    if path.stat().st_size != EXPECTED_SIZE:
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest().upper() == EXPECTED_SHA256


def _wheel_url() -> str:
    api = f"https://pypi.org/pypi/dashscope/{DASHSCOPE_VERSION}/json"
    with urllib.request.urlopen(api, timeout=30) as response:
        payload = json.load(response)
    for item in payload["urls"]:
        if item["packagetype"] == "bdist_wheel" and item["filename"].endswith(
            "-none-any.whl"
        ):
            return str(item["url"])
    raise SystemExit(f"dashscope {DASHSCOPE_VERSION} 没有找到 any-wheel")


def _download_wheel(url: str, dest: pathlib.Path) -> None:
    print(f"[fetch_vocab] 下载 wheel ← {url}")
    with urllib.request.urlopen(url, timeout=120) as response, dest.open("wb") as handle:
        handle.write(response.read())


def _extract(wheel: pathlib.Path, dest: pathlib.Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        try:
            info = archive.getinfo(WHEEL_PATH_IN_ARCHIVE)
        except KeyError:
            raise SystemExit(f"wheel 里找不到 {WHEEL_PATH_IN_ARCHIVE}") from None
        data = archive.read(info)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def main() -> int:
    dest = target_path()
    if _is_valid(dest):
        print(f"[fetch_vocab] 缓存命中：{dest}")
        return 0

    url = _wheel_url()
    with tempfile.TemporaryDirectory() as workdir:
        wheel = pathlib.Path(workdir) / "dashscope.whl"
        _download_wheel(url, wheel)
        temp_target = dest.with_suffix(dest.suffix + ".partial")
        _extract(wheel, temp_target)

    size = temp_target.stat().st_size
    if size != EXPECTED_SIZE:
        temp_target.unlink(missing_ok=True)
        raise SystemExit(f"[fetch_vocab] 字节数不符：期望 {EXPECTED_SIZE}，实际 {size}")

    digest = hashlib.sha256(temp_target.read_bytes()).hexdigest().upper()
    if digest != EXPECTED_SHA256:
        temp_target.unlink(missing_ok=True)
        raise SystemExit(f"[fetch_vocab] SHA-256 不符：期望 {EXPECTED_SHA256}，实际 {digest}")

    temp_target.replace(dest)
    print(f"[fetch_vocab] 已安装：{dest}（{size} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
