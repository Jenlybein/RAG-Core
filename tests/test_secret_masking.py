"""#82 / A1：密钥打码只有一份实现（``mask_secret``）。

以前三处各写一份：``id_generator.mask_api_key_value``（前缀 + 末 4）、
``model_config_service.mask_key``（前 3 + 末 4）、``runtime_settings._mask``（只露末 4）。
三处都叫 masked，泄露面却各不相同——一处露出 8 位、另一处露出前缀。现在只有
``app.utils.id_generator.mask_secret`` 一个规则，调用点全部直连。

这里用三种典型输入（API Key、短 Key、普通串）把「它们都走同一个函数」钉住，
并断言那两个被删掉的本地实现没有回来。
"""

from __future__ import annotations

from datetime import datetime

from app.models.model_config import ModelConfig, spec_of
from app.schemas.console_keys import key_masked_of
from app.services import model_config_service, runtime_settings
from app.services.model_config_service import _masked_snapshot
from app.utils.id_generator import (
    API_KEY_PREFIX,
    MASK_HIDDEN,
    mask_api_key_value,
    mask_secret,
)

#: 与 ``new_api_key_value()`` 同形：``rc_live_`` + 32 位十六进制。
API_KEY = "rc_live_0123456789abcdef0123456789ab9f2c"
#: 模型接入的 Key（明文入库，只在响应 / 审计里 masked，见 ADR-0004）。
MODEL_KEY = "sk-live-7d3f9b1c-SENTINEL-abcd"
#: 太短、保留任何一段都接近原文的坏数据。
SHORT_KEY = "rc_x"

STAMP = datetime(2026, 1, 1, 8, 0, 0)


def test_the_api_key_form_is_the_shared_rule_not_a_copy() -> None:
    """API Key 的 ``rc_live_…9f2c`` 就是 ``mask_secret(prefix, 4)`` 的输出。"""

    assert mask_api_key_value(API_KEY) == "rc_live_…9f2c"
    assert mask_api_key_value(API_KEY) == mask_secret(
        API_KEY, keep_head=len(API_KEY_PREFIX), keep_tail=4
    )


def test_a_too_short_value_is_hidden_entirely() -> None:
    """长度不足以「留头留尾还不泄露」时整体隐去——两种入口同一条规则。"""

    assert mask_secret(SHORT_KEY) == MASK_HIDDEN
    assert mask_api_key_value(SHORT_KEY) == MASK_HIDDEN


def test_an_ordinary_string_keeps_its_head_and_tail() -> None:
    """普通串（例如模型 Key）保留前 3 + 末 4：``sk-…abcd``。"""

    assert mask_secret(MODEL_KEY) == "sk-…abcd"
    assert len(mask_secret(MODEL_KEY)) == 3 + len("…") + 4


def test_every_call_site_uses_the_same_rule() -> None:
    """Key 面板、模型配置页 / 审计、配置中台的 secret 项，三处输出都是 ``mask_secret``。"""

    assert key_masked_of(API_KEY) == mask_secret(
        API_KEY, keep_head=len(API_KEY_PREFIX), keep_tail=4
    )

    row = ModelConfig(
        id="config-deepseek",
        label="DeepSeek 生产",
        provider="deepseek",
        base_url="",
        model="deepseek-flash",
        api_key=MODEL_KEY,
        created_at=STAMP,
        updated_at=STAMP,
    )
    snapshot = _masked_snapshot(spec_of(row))
    assert snapshot["api_key"] == mask_secret(MODEL_KEY)
    assert MODEL_KEY not in str(snapshot)

    secret_spec = runtime_settings.SETTING_SPECS["MODEL_API_KEY"]
    assert secret_spec.secret is True
    assert runtime_settings._visible_value(secret_spec, MODEL_KEY) == mask_secret(MODEL_KEY)


def test_no_module_reimplements_the_masking_rule() -> None:
    """两个本地实现（``model_config_service.mask_key`` / ``runtime_settings._mask``）不许回来。"""

    assert not hasattr(model_config_service, "mask_key")
    assert not hasattr(runtime_settings, "_mask")
