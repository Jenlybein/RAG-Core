"""测试助手：把「请求里的质量参数」搬进密钥配置（票 #77）。

请求体只剩 ``kb_ids`` + ``query`` 之后，既有测试的调用点仍写着
``_post(client, mode="hybrid")`` 这种样子。按票里的口径，**调用点一个字都不改**：
各测试模块的 HTTP 助手把写进来的那四个字段摘出来，改用「造一个带配置的假身份」表达
同一件事——配置的档位是 ``custom``，那批手动参数正好等于原来请求里想传的值。

这条路径刻意只走 ``custom`` 分支：它是 **#78（命名档位的真实预设）之后唯一不变**的
分支，因此这批既有用例在下一票里仍然守着同一件事。

用法（每个模块三行）：

```python
IDENTITY = RetrievalIdentity(TENANT_ID)          # 模块级一份
main.app.dependency_overrides[get_current_key] = IDENTITY.context   # 替掉鉴权依赖
payload.update(IDENTITY.apply(body))             # 请求助手：摘走旧字段、换成配置
```
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.core.auth import DEV_KEY_POLICY, KeyContext
from app.models.key_policy import PROFILE_CUSTOM, KeyPolicySnapshot

#: 票 #77 从请求体里删掉的四个字段。助手据此把「老写法」翻译成「密钥配置」。
QUALITY_FIELDS: tuple[str, ...] = ("mode", "top_k", "include_parent", "expand_table_rows")


class RetrievalIdentity:
    """一个可改配置的假身份：``use(...)`` 之后，下一次请求就按这套配置跑。

    状态刻意放在实例上而不是模块全局：每个测试模块一份，两次请求之间不会有别的
    模块悄悄改到它；而每次 ``use`` 都会把四个值**全部**重写，所以也不会把上一个
    用例的配置带进下一个。
    """

    def __init__(self, tenant_id: str, *, policy: KeyPolicySnapshot = DEV_KEY_POLICY) -> None:
        self._tenant_id = tenant_id
        self._policy = policy

    # ── 身份 ──
    def context(self) -> KeyContext:
        """给 ``dependency_overrides[get_current_key]`` 用的可调用对象。"""

        return KeyContext(tenant_id=self._tenant_id, policy=self._policy)

    # ── 配置 ──
    def use_policy(self, policy: KeyPolicySnapshot) -> None:
        """直接指定一份快照（命名档位那几条用例用它）。"""

        self._policy = policy

    def use(
        self,
        *,
        mode: str | None = None,
        top_k: int | None = None,
        include_parent: bool = True,
        expand_table_rows: bool = True,
    ) -> None:
        """造一份 ``custom`` 档的配置：手动参数 = 原来请求里想传的那几个值。

        与旧请求体的缺省一一对应：没传 ``mode`` 就回到「服务端缺省模式」，没传
        ``top_k`` 就回到「服务端缺省条数」——由 ``options_from_policy`` 的逐键回落
        实现，助手这里不复制一份缺省值。
        """

        manual: dict[str, object] = {}
        if mode is not None:
            manual["mode"] = mode
        if top_k is not None:
            manual["top_k"] = top_k

        self._policy = replace(
            # 以内置开发身份那份为底：它的 ``unrestricted_scope`` 打开，白名单关卡
            # 不在本票的范围内（它是 #76 的事，另有专门的用例）。
            DEV_KEY_POLICY,
            profile=PROFILE_CUSTOM,
            include_parent=include_parent,
            expand_table_rows=expand_table_rows,
            custom_options=tuple(sorted(manual.items())),
        )

    def apply(self, body: dict[str, Any]) -> dict[str, Any]:
        """把请求体里遗留的质量字段摘出来写进密钥配置，返回剩下的请求体。"""

        quality = {name: body.pop(name) for name in QUALITY_FIELDS if name in body}
        self.use(**quality)  # type: ignore[arg-type]
        return body
