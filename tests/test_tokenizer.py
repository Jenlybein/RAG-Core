"""Qwen token 计数：口径、边界、必需资产的失败二分。

这里的固定数字是**与 DashScope 官方分词器对账过的**（同一 `PAT_STR`、同一词表、
同一组特殊 token）。数字变了就说明词表或参数被换过——这正是要钉住的东西。
"""

from __future__ import annotations

import pathlib

import pytest

from app.core.config import settings
from app.utils import tokenizer

CORPUS = pathlib.Path(__file__).parent.parent / "eval" / "corpus"

# 用官方分词器核对过的基准值：文档 -> (字符数, token 数)。
BASELINE = {
    "support_refund.md": (4046, 2638),
    "api_reference.md": (3058, 1595),
}


@pytest.fixture(scope="module", autouse=True)
def _require_vocab() -> None:
    """词表是必需资产：没有就直接失败并提示，而不是跳过。

    跳过会让「切块口径变了」这件事在没有词表的环境里悄悄发生。
    """

    tokenizer.verify_ready()


@pytest.mark.parametrize(("filename", "expected"), BASELINE.items())
def test_matches_dashscope_official_counts(filename: str, expected: tuple[int, int]) -> None:
    """逐 token 对齐官方：字符数与 token 数都必须命中基准值。"""

    chars, tokens = expected
    body = (CORPUS / filename).read_text(encoding="utf-8")

    assert len(body) == chars, "语料本身变了，基准值需要重新对账"
    assert tokenizer.token_count(body) == tokens


def test_empty_text_is_zero() -> None:
    assert tokenizer.token_count("") == 0


def test_chinese_density_is_above_character_ratio() -> None:
    """中文的 token 密度低于字符：Qwen 词表下 1 token ≈ 1.5–1.9 个汉字。

    这条断言的意义是防止有人把 `len(text)` 当成 token 数——那会把窗口悄悄放大。
    """

    body = (CORPUS / "support_refund.md").read_text(encoding="utf-8")
    tokens = tokenizer.token_count(body)

    assert tokens < len(body), "token 数不应超过字符数"
    assert len(body) / tokens > 1.3, "中文语料上字符/token 应明显大于 1"


def test_token_count_is_additive() -> None:
    """分段编码与整体编码可能差 1–2 个 token——钉住这个**已知近似**。

    BPE 在切点处会重新配对，所以「分段相加 == 整体」不是恒等式（实测个别切点差 1）。
    窗口是 512 量级，这点误差不影响「片是否超窗口」；写成断言是为了防止有人日后
    把它当成精确等式去算账。
    """

    body = (CORPUS / "support_refund.md").read_text(encoding="utf-8")
    whole = tokenizer.token_count(body)

    for cut in (len(body) // 2, 1000, len(body) - 1):
        parts = tokenizer.token_count(body[:cut]) + tokenizer.token_count(body[cut:])
        assert abs(parts - whole) <= 2, f"切点 {cut} 处误差过大：{parts} vs {whole}"


def test_missing_vocab_refuses_to_start(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """词表缺失时**拒绝启动**：部署问题不该被降级成近似口径。"""

    monkeypatch.setattr(settings, "QWEN_TOKENIZER_PATH", str(tmp_path / "absent.tiktoken"))
    tokenizer._encoding.cache_clear()

    with pytest.raises(tokenizer.TokenizerUnavailable, match="fetch_qwen_vocab"):
        tokenizer.verify_ready()

    tokenizer._encoding.cache_clear()


def test_corrupted_vocab_refuses_to_start(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """词表字节数对不上同样拒绝启动——可疑资产绝不能被静默使用。"""

    broken = tmp_path / "broken.tiktoken"
    broken.write_bytes(b"not a vocab")
    monkeypatch.setattr(settings, "QWEN_TOKENIZER_PATH", str(broken))
    tokenizer._encoding.cache_clear()

    with pytest.raises(tokenizer.TokenizerUnavailable, match="校验失败"):
        tokenizer.verify_ready()

    tokenizer._encoding.cache_clear()
