"""Qwen 分词计数：切块窗口的**唯一长度口径**。

为什么不用 `tiktoken.get_encoding("cl100k_base")`：那是 OpenAI 的词表，对中文高估
16–56%（实测 `support_refund.md` 4046 字符 → Qwen 2638 token，cl100k 4113 token）。
拿它当窗口尺子，等于用别人的量具量我们的布。

这里手工构造 `tiktoken.Encoding`，参数与 DashScope 官方 `QwenTokenizer` 一致
（同一个 `PAT_STR`、同一张 `qwen.tiktoken`、同一组特殊 token），所以计数**逐 token
对齐官方**。词表文件 `qwen.tiktoken` 由 `scripts/fetch_qwen_vocab.py` 下载并校验。

**词表是必需资产，缺失即拒绝启动**：切块结果由词表决定，缺词表就只能换近似词表，
而近似会让同一个库在不同部署上切出不同的片——那种差异是静默的，比直接失败难查
一个数量级。这与 `pdf-inspector` 的失败二分是同一个原则：部署问题不伪装成质量判断。
"""

from __future__ import annotations

import base64
import hashlib
import functools
import pathlib
import threading

import tiktoken

from app.core.config import settings

# 以下三项与 DashScope `legacy_1x/tokenizers/qwen_tokenizer.py` 逐字一致。
PAT_STR = (
    r"(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}|"
    r" ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+"
)
_SPECIAL_BASE = ("<|endoftext|>", "<|im_start|>", "<|im_end|>")
_SPECIAL_START_ID = 151643
_SPECIAL_TOKENS: dict[str, int] = {
    token: index
    for index, token in enumerate(
        _SPECIAL_BASE + tuple(f"<|extra_{i}|>" for i in range(205)),
        start=_SPECIAL_START_ID,
    )
}

EXPECTED_SIZE = 2_561_218
EXPECTED_SHA256 = "B2B1B8DFB5CC5F024BAFC373121C6ABA3F66F9A5A0269E243470A1DE16A33186"

_lock = threading.Lock()


class TokenizerUnavailable(RuntimeError):
    """词表缺失或损坏。属**部署问题**，不应当被降级成近似口径。"""


def vocab_path() -> pathlib.Path:
    """词表路径：配置优先，否则落在仓库根的 `.tokenizer/`。

    `parents[2]` 是仓库根：`app/utils/tokenizer.py` → `utils` → `app` → 根。
    """

    configured = (settings.QWEN_TOKENIZER_PATH or "").strip()
    if configured:
        return pathlib.Path(configured)
    return pathlib.Path(__file__).resolve().parents[2] / ".tokenizer" / "qwen.tiktoken"


def _load_mergeable_ranks(path: pathlib.Path) -> dict[bytes, int]:
    contents = path.read_bytes()
    return {
        base64.b64decode(token): int(rank)
        for token, rank in (line.split() for line in contents.splitlines() if line)
    }


@functools.lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    path = vocab_path()
    if not path.is_file():
        raise TokenizerUnavailable(
            f"缺少 Qwen 词表 {path}。执行 `uv run python scripts/fetch_qwen_vocab.py` 获取；"
            "容器镜像需在构建期预置该文件。"
        )
    data = path.read_bytes()
    if len(data) != EXPECTED_SIZE or hashlib.sha256(data).hexdigest().upper() != EXPECTED_SHA256:
        raise TokenizerUnavailable(
            f"Qwen 词表校验失败（{path}）：期望 {EXPECTED_SIZE} 字节 / SHA256 {EXPECTED_SHA256}。"
            "重新执行 `uv run python scripts/fetch_qwen_vocab.py`。"
        )
    return tiktoken.Encoding(
        "Qwen",
        pat_str=PAT_STR,
        mergeable_ranks=_load_mergeable_ranks(path),
        special_tokens=_SPECIAL_TOKENS,
    )


def token_count(text: str) -> int:
    """返回 `text` 的 Qwen token 数；空串为 0。

    超长输入分段编码：tiktoken 的 BPE 在很长文本上会深递归，官方实现同样按 10 万
    字符切段。计数口径不受分段影响（逐段相加等于整体）。
    """

    if not text:
        return 0
    with _lock:
        encoding = _encoding()
    chunk = 100_000
    return sum(
        len(encoding.encode(text[index : index + chunk]))
        for index in range(0, len(text), chunk)
    )


def encode_ids(text: str) -> list[int]:
    """把文本编码成 token id 序列（硬切窗口按 token 粒度走时要它）。"""

    if not text:
        return []
    with _lock:
        encoding = _encoding()
    chunk = 100_000
    ids: list[int] = []
    for index in range(0, len(text), chunk):
        ids.extend(encoding.encode(text[index : index + chunk]))
    return ids


def decode_ids(ids: list[int]) -> str:
    """把 token id 序列还原成文本；与 `encode_ids` 互为逆操作。"""

    if not ids:
        return ""
    with _lock:
        return _encoding().decode(ids)


def verify_ready() -> None:
    """启动时调用：词表不可用就抛 `TokenizerUnavailable`，拒绝带病启动。"""

    with _lock:
        _encoding()
