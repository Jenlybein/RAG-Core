"""#82 / A1：密钥打码只有一份规则，而且 API Key 那一份已经不再从原文算。

以前三处各写一份（``id_generator.mask_api_key_value`` 前缀 + 末 4、
``model_config_service.mask_key`` 前 3 + 末 4、``runtime_settings._mask`` 只露末 4），
三处都叫 masked，泄露面却各不相同。现在分两种情况：

* **模型接入与配置中台的 secret** 仍然从原文算，走 :func:`app.utils.id_generator.mask_secret`；
* **数据面 API Key** 已经没有原文可算——库里只存 scrypt 哈希，能显示的只有 ``key_tail``
  那一截，所以走 :func:`app.utils.id_generator.mask_api_key_tail`（前缀常量 + 末 5 位）。

两条路都不许出现第二份实现：这里把「谁走哪一条」钉住，并断言那三个被删掉的本地实现没有回来。
"""

from __future__ import annotations

from datetime import datetime

from app.models.model_config import ModelConfig, spec_of
from app.schemas.console_keys import key_masked_of
from app.services import model_config_service, runtime_settings
from app.services.model_config_service import _masked_snapshot
from app.utils.id_generator import (
    API_KEY_MASK_TAIL,
    MASK_HIDDEN,
    api_key_tail_of,
    mask_api_key_tail,
    mask_secret,
)

#: 与 ``new_api_key_value()`` 同形：``rc_live_<行身份证>.<32 位十六进制>``。
KEY_ID = "0f8c1d6a-2b3e-4c5d-8e9f-0a1b2c3d4e5f"
API_KEY = f"rc_live_{KEY_ID}." + "0123456789abcdef0123456789ab9f2c"
#: 模型接入的 Key（明文入库，只在响应 / 审计里 masked）。
MODEL_KEY = "sk-live-7d3f9b1c-SENTINEL-abcd"
#: 太短、保留任何一段都接近原文的坏数据。
SHORT_KEY = "rc_x"

STAMP = datetime(2026, 1, 1, 8, 0, 0)


def test_the_api_key_tail_is_the_last_five_characters() -> None:
    """落库那一列只有末 5 位：它连同「前缀 + 行身份证」也不足以重建密钥。"""

    assert API_KEY_MASK_TAIL == 5
    assert api_key_tail_of(API_KEY[-32:]) == "b9f2c"
    assert api_key_tail_of(API_KEY) == "b9f2c"


def test_the_api_key_mask_is_built_from_the_stored_tail_not_from_the_plaintext() -> None:
    """``rc_live_…b9f2c``：前缀是常量，末 5 位来自库里那一列。"""

    assert mask_api_key_tail("b9f2c") == "rc_live_…b9f2c"
    assert key_masked_of("b9f2c") == mask_api_key_tail("b9f2c")


def test_an_empty_tail_is_hidden_entirely() -> None:
    """手里那一截是空的（手写进库的坏数据）：整体隐去，而不是显示成 ``rc_live_…``。"""

    assert mask_api_key_tail("") == MASK_HIDDEN


def test_the_other_two_call_sites_still_use_the_shared_rule() -> None:
    """模型接入页 / 审计、配置中台的 secret 项：这两处仍有原文，走 ``mask_secret``。"""

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
    assert snapshot["api_key"] == mask_secret(MODEL_KEY) == "sk-…abcd"
    assert MODEL_KEY not in str(snapshot)

    secret_spec = runtime_settings.SETTING_SPECS["MODEL_API_KEY"]
    assert secret_spec.secret is True
    assert runtime_settings._visible_value(secret_spec, MODEL_KEY) == mask_secret(MODEL_KEY)


def test_a_too_short_value_is_hidden_entirely() -> None:
    """长度不足以「留头留尾还不泄露」时整体隐去——普通密钥那一条路。"""

    assert mask_secret(SHORT_KEY) == MASK_HIDDEN


def test_no_module_reimplements_the_masking_rule() -> None:
    """三个本地实现（``model_config_service.mask_key`` / ``runtime_settings._mask`` /
    ``id_generator.mask_api_key_value``）都不许回来。

    ``mask_api_key_value`` 随「库里不再有原文」一起删掉了：它的入参是原文，而那个东西
    在签发之后就不存在了。
    """

    import app.utils.id_generator as id_generator

    assert not hasattr(model_config_service, "mask_key")
    assert not hasattr(runtime_settings, "_mask")
    assert not hasattr(id_generator, "mask_api_key_value")
