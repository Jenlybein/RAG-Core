"""控制面身份内核：**只认服务端会话，不认 ``Authorization``**。

为什么不复用数据面的 ``app.core.auth``：浏览器不会带 ``Authorization`` 头，把它套在面板
接口上等于每个请求都 401；反过来更要紧——**数据面的 Key 绝不能被用来管理平台**。两套
身份、两份代码，是这条边界的落地方式，不是「暂时复制一份」。

**每个请求都回账号表重读一遍**（不是把角色塞进 cookie 或会话行）。这是选服务端会话而不是
签名 token 的理由：停用一个账号、把一个管理员降级，都在**下一个请求**生效，不必等会话
过期；签名 token 在签发那一刻就把角色与状态固化了，管理员点完「停用」之后旧 token 还能
用满有效期——那就不叫止血。

会话行里只有「谁的、什么时候过期」；凭证是行 id 本身（随机的不可猜标识）。

跨站防护：带 cookie 的浏览器写操作天然会跟着别的站点发出的请求一起带上 cookie，所以所有
**改了东西**的面板接口都要求一个显式请求头（:data:`WRITE_HEADER_NAME`）。跨站表单发不出
自定义头，跨站脚本发之前要先过 CORS 预检——两类都过不来。登录接口本身不带 cookie，
因此不在这一条之内（它另外要口令，见 ``console_service.login``）。

**两份入口、一套判定**（票 #129）：控制面的读接口已经换到 ``AsyncSession``，写接口与
「我的视图」仍在同步栈上，于是「认出这次是谁」有两个入口
（:func:`resolve_console_identity` / :func:`resolve_console_identity_async`）。它们只差
「怎么读那两行」，四条判定（缺 cookie / 会话不存在 / 会话过期 / 账号停用）收在三个私有
函数里，只有一份实现——这是这条边界的纪律：**判定可以有两个执行方式，规则不能有两份**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Mapping, Protocol

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.db.base import now
from app.models.account import Account
from app.models.console_session import ConsoleSession
from app.repositories import account_repository, console_session_repository

#: 会话 cookie 名。前端与测试都用这一个名字，别处不许再写一次字面量。
SESSION_COOKIE_NAME: Final[str] = "console_session"
#: cookie 只发给控制面路径：面板之外的任何请求都不该带上它。
SESSION_COOKIE_PATH: Final[str] = "/console"

#: 写操作必须带的头（名字大小写不敏感，值固定）。
WRITE_HEADER_NAME: Final[str] = "x-requested-with"
WRITE_HEADER_VALUE: Final[str] = "XMLHttpRequest"

#: 管理员角色名与「可用」状态名。与 accounts 表里的取值一致。
ROLE_ADMIN: Final[str] = "admin"
STATUS_ACTIVE: Final[str] = "active"
STATUS_DISABLED: Final[str] = "disabled"
#: 账号与租户两个状态列允许的取值；改状态时先在这里校验。
ACCOUNT_STATUSES: Final[tuple[str, ...]] = (STATUS_ACTIVE, STATUS_DISABLED)

# 同一档错误码、不同消息：排障时分得清是哪一步拦下的，对外都只是「未授权」。
MSG_MISSING_SESSION = "missing console session"
MSG_INVALID_SESSION = "invalid console session"
MSG_ACCOUNT_DISABLED = "account disabled"
MSG_ADMIN_REQUIRED = "admin role required"
MSG_MISSING_WRITE_HEADER = "missing x-requested-with header"


class ConsoleRequestLike(Protocol):
    """控制面内核眼里的「请求」：只要有大小写不敏感的 ``headers`` 与 ``cookies``。

    写成 Protocol 而不是导入 ``fastapi.Request``：内核不认识 Web 框架，与
    ``app.core.auth.HttpRequestLike`` 同一套做法（但**是另一个协议**——两边读的是
    完全不同的两份凭证）。
    """

    @property
    def headers(self) -> Mapping[str, str]: ...

    @property
    def cookies(self) -> Mapping[str, str]: ...


@dataclass(frozen=True)
class ConsoleIdentity:
    """一次控制面请求的判定结论：谁、什么角色、属于哪个租户（管理员没有租户）。

    ``tenant_id`` 允许为空是**判定的一部分**：管理员跨租户管账号与套餐，它不属于任何
    租户；一个「平台租户」会让那个假租户凭空拥有一整套数据面身份。
    """

    account_id: str
    username: str
    role: str
    tenant_id: str | None

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


def identity_of(account: Account) -> ConsoleIdentity:
    """账号行 → 判定结论。角色每次都从行里取，不从会话或 cookie 里取。"""

    return ConsoleIdentity(
        account_id=account.id,
        username=account.username,
        role=account.role,
        tenant_id=account.tenant_id,
    )


def resolve_console_identity(request: ConsoleRequestLike, db: Session) -> ConsoleIdentity:
    """读会话 cookie → 会话行 → 账号行；任何一步不成立都是 ``20010``。

    四步缺一不可：

    1. 没有 cookie → 未授权；
    2. 会话行不存在 → 未授权（删行就是「服务端登出」，旧 cookie 立刻作废）；
    3. 会话已过期 → 未授权；
    4. 账号不存在或已停用 → 未授权（**这里就是「停用立刻生效」**的那一行：账号状态是
       每个请求现读的，不是登录时快照下来的）。

    这是**同步会话**上的那一份：登录 / 登出、控制面的写接口与「我的视图」还在同步栈上。
    控制面的**读**接口走 :func:`resolve_console_identity_async`——两份只差「怎么读那两行」，
    四条判定本身在 :func:`_ensure_session_usable` / :func:`_ensure_account_usable` 里，
    只有一份实现。
    """

    session_id = _session_id_of(request)
    session = _ensure_session_usable(console_session_repository.get_by_id(db, session_id))
    return identity_of(
        _ensure_account_usable(account_repository.get_by_id(db, session.account_id))
    )


async def resolve_console_identity_async(
    request: ConsoleRequestLike, db: AsyncSession
) -> ConsoleIdentity:
    """:func:`resolve_console_identity` 的异步孪生（控制面读，票 #129）。

    判定顺序、错误码与消息与同步版逐字一致——两个入口读的是同一份会话行与账号行，差别
    只有「用哪套会话读」。这条孪生随控制面写入异步化（#130）一起收掉：那时全仓只剩这一份。
    """

    session_id = _session_id_of(request)
    session = _ensure_session_usable(
        await console_session_repository.get_by_id_async(db, session_id)
    )
    return identity_of(
        _ensure_account_usable(
            await account_repository.get_by_id_async(db, session.account_id)
        )
    )


def _session_id_of(request: ConsoleRequestLike) -> str:
    """第 1 步：没有 cookie 就没有身份。"""

    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        raise AppError(ErrorCode.UNAUTHORIZED, MSG_MISSING_SESSION)
    return session_id


def _ensure_session_usable(session: ConsoleSession | None) -> ConsoleSession:
    """第 2、3 步：会话行存在且没过期；返回收窄后的行。

    返回而不是只断言，是为了让调用方拿到**非空**的行：判定写在函数里、类型收窄也跟着走，
    mypy 因此不需要在每个调用点再 ``assert`` 一次。
    """

    if session is None:
        raise AppError(ErrorCode.UNAUTHORIZED, MSG_INVALID_SESSION)
    if session.expires_at <= now():
        # 过期的行不在这里删：一次 GET 顺手写库会让「读接口」拥有副作用，清理交给后续
        # 的会话清扫任务；判定上它已经等于不存在。
        raise AppError(ErrorCode.UNAUTHORIZED, MSG_INVALID_SESSION)
    return session


def _ensure_account_usable(account: Account | None) -> Account:
    """第 4 步：账号存在且可用——「停用立刻生效」就落在这一行上。返回收窄后的行。"""

    if account is None or account.status != STATUS_ACTIVE:
        raise AppError(ErrorCode.UNAUTHORIZED, MSG_ACCOUNT_DISABLED)
    return account


def require_admin(identity: ConsoleIdentity) -> None:
    """管理员接口的门禁：登录了但不是管理员 → ``20011``（不是「未授权」）。

    两者刻意分开：未登录的调用方要先登录，登录了却没权限的调用方要换账号——把这两种
    情形报成同一个码，面板只能笼统地弹一句「出错了」。
    """

    if not identity.is_admin:
        raise AppError(ErrorCode.FORBIDDEN, MSG_ADMIN_REQUIRED)


def require_write_header(request: ConsoleRequestLike) -> None:
    """写操作要求显式请求头；没有就是 ``20011``。

    为什么是 20011 而不是 20010：**身份本身没问题**，被拒的是这个请求缺少「这不是别的
    站点代发的」的证明。20010 会让前端误以为掉登录了，从而清掉本来还有效的会话。
    """

    if request.headers.get(WRITE_HEADER_NAME, "").strip() != WRITE_HEADER_VALUE:
        raise AppError(ErrorCode.FORBIDDEN, MSG_MISSING_WRITE_HEADER)


def is_production() -> bool:
    """生产环境判定：只有 ``APP_ENV=production`` 才给 cookie 加 ``Secure``。

    本地 ``http://`` 上带 ``Secure`` 的 cookie 会被浏览器直接丢掉，于是本地永远登不进去；
    做成函数是为了让「生产必须 Secure」这条能被测试直接钉住。
    """

    return settings.APP_ENV.strip().lower() == "production"


def session_ttl_seconds() -> int:
    """会话有效期的唯一来源（配置项），登录发 cookie 与会话行的 ``expires_at`` 共用。"""

    return settings.CONSOLE_SESSION_TTL_SECONDS
