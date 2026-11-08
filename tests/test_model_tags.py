"""票 #48：型号边界标记。

`XR-2000` 是 `XR-2000 Pro` 的子串，两条只差型号的切片在向量空间里几乎重合。
把型号从正文里抽出来单独成段，给它一个独立可对齐的位置。
"""

from __future__ import annotations

import pytest

from app.utils.retrieval_text import build_retrieval_text, model_tokens


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("XR-2000 的单价是 3800。", ["XR-2000"]),
        ("XR-2000 Pro 带导轨，XR-2000 不带。", ["XR-2000 Pro", "XR-2000"]),
        ("SFP-10G 光模块 1180 元。", ["SFP-10G"]),
        ("没有型号的一段话。", []),
        ("XR-2000、XR-2000 重复出现。", ["XR-2000"]),
    ],
)
def test_model_tokens_are_extracted_longest_first(content: str, expected: list[str]) -> None:
    assert model_tokens(content) == expected


def test_model_segment_sits_before_the_content() -> None:
    """型号是一个独立的段，排在正文之前——埋进长句子就没有这个位置优势。"""

    text = build_retrieval_text(
        "价格与折扣对照表", "价格表 > 整机价格", "XR-2000 Pro 单价 4200。", model_tags=True
    )

    assert "型号：XR-2000 Pro" in text
    assert text.index("型号：") < text.index("正文：")


def test_model_tags_off_keeps_the_old_format() -> None:
    """不开标记时检索文本与改造前一致：这是一项可选增强，不是格式变更。"""

    without = build_retrieval_text(
        "价格与折扣对照表", "价格表 > 整机价格", "XR-2000 Pro 单价 4200。"
    )

    assert "型号：" not in without
    assert without == "文档：价格与折扣对照表 章节：价格表 > 整机价格 正文：XR-2000 Pro 单价 4200。"


def test_no_model_means_no_segment() -> None:
    """正文里没有型号时不产生空的「型号：」段。"""

    text = build_retrieval_text("部署实施指南", "上架", "机架款要上柜。", model_tags=True)

    assert "型号：" not in text
