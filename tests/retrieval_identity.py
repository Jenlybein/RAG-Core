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

from typing import Any

from app.core.auth import DEV_KEY_POLICY, KeyContext
from app.models.key_policy import KeyPolicySnapshot
from tests.support.policy import policy

#: 票 #77 从请求体里删掉的四个字段。助手据此把「老写法」翻译成「密钥配置」。
QUALITY_FIELDS: tuple[str, ...] = (
    "mode",
    "top_k",
    "rerank",  # 第 13 步：重排由配置直存参数决定，也走这条「摘请求字段」的路。
    "include_parent",
    "expand_table_rows",
)


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
        rerank: bool | None = None,
        include_parent: bool = True,
        expand_table_rows: bool = True,
    ) -> None:
        """造一份配置：三项参数直存（``retrieval_mode`` / ``retrieval_top_k`` / ``retrieval_rerank``）。

        第 13 步起后端不再有档位，这三项就是「用多好」的全部真相。没传 mode/top_k
        就取部署缺省（``RETRIEVAL_MODE`` / ``TOP_K``）；没传 rerank 就是「不要重排」。
        """

        manual: dict[str, object] = {}
        if mode is not None:
            manual["mode"] = mode
        if top_k is not None:
            manual["top_k"] = top_k
        if rerank is not None:
            manual["rerank"] = rerank

        # 以内置开发身份那份为底：它的 ``unrestricted_scope`` 打开，白名单关卡
        # 不在本票的范围内（它是 #76 的事，另有专门的用例）。
        self._policy = policy(
            custom_options=tuple(sorted(manual.items())),
            include_parent=include_parent,
            expand_table_rows=expand_table_rows,
            unrestricted_scope=True,
        )

    def apply(self, body: dict[str, Any]) -> dict[str, Any]:
        """把请求体里遗留的质量字段摘出来写进密钥配置，返回剩下的请求体。"""

        quality = {name: body.pop(name) for name in QUALITY_FIELDS if name in body}
        self.use(**quality)  # type: ignore[arg-type]
        return body
