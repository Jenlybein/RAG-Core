"""结构化日志（T2）验收测试。

唯一接缝：ASGI 应用边界。用 TestClient 发真实请求 / 直接让框架 logger 出日志，
然后只观察 `logs/app.log`、`logs/error.log` 两个文件的外部行为：
是否可解析、字段是否齐、分级是否正确、是否滚动、是否重复、是否可配置。
不触碰私有函数与 handler 注册细节。
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _restore_logging_after_each_test(tmp_path: Path) -> None:
    """每条测试结束后把全局 logging 还原成可用状态。

    本文件的测试会反复 dictConfig + shutdown，若不还原，后续票（T3/T4/T7）
    在同一 pytest 进程里会继承已关闭的 handler。
    """

    yield
    logging_mod = importlib.import_module("app.core.logging")
    logging.shutdown()
    logging_mod._configured = False
    logging_mod.setup_logging(log_dir=tmp_path / "logs-restored", log_level="INFO")


@pytest.fixture
def log_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把日志指向临时目录，并让每条测试拿到一次全新的配置。

    生产代码只在 import app.main 时幂等配置一次；测试要换目录/阈值，
    因此显式重置模块级守卫与 settings 单例上的两个值，再调用一次
    ``setup_logging()``。

    返回 ``(log_dir, setup)``：``setup(**kwargs)`` 可覆盖滚动阈值/份数/级别/目录，
    便于逐条测试用不同参数重新初始化。
    """

    log_dir = tmp_path / "logs"
    logging_mod = importlib.import_module("app.core.logging")
    config_mod = importlib.import_module("app.core.config")

    monkeypatch.setattr(config_mod.settings, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(config_mod.settings, "LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(logging_mod, "_configured", False)

    def setup(**kwargs):
        logging_mod.setup_logging(**kwargs)
        return log_dir

    # 默认先按 DEBUG + 临时目录配置好，覆盖「import app.main 即生效」的路径。
    logging_mod.setup_logging()

    yield log_dir, setup


def _records(path: Path) -> list[dict]:
    """读一个 JSON Lines 文件，返回解析后的记录。空文件视为零条。"""

    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_real_request_writes_parseable_jsonl(log_env) -> None:
    """一次真实请求后，全量文件每行可解析，含 time/level/logger/msg。"""

    log_dir, _ = log_env
    import app.main

    client = TestClient(app.main.app)
    assert client.get("/health").json() == {"status": "ok"}

    logger = importlib.import_module("structlog").get_logger("t2.test")
    logger.info("t2 probe", tenant_id="t-1", document_id="d-1")

    records = _records(log_dir / "app.log")
    assert records, "app.log 应至少有一条记录"
    probe = [r for r in records if r.get("msg") == "t2 probe"]
    assert len(probe) == 1, "同一行日志不能重复输出"
    for field in ("time", "level", "logger", "msg"):
        assert field in probe[0], f"缺少字段 {field}"
    assert probe[0]["level"] == "info"
    assert probe[0]["tenant_id"] == "t-1", "业务标识必须随 extra 落盘"
    assert probe[0]["document_id"] == "d-1"


def test_error_log_contains_only_error_records(log_env) -> None:
    """error.log 只收 ERROR；INFO/DEBUG 不进去，且 ERROR 不丢。"""

    log_dir, _ = log_env

    logger = importlib.import_module("structlog").get_logger("t2.levels")
    logger.debug("t2 debug", document_id="d-debug")
    logger.info("t2 info", document_id="d-info")
    logger.warning("t2 warning", document_id="d-warn")
    logger.error("t2 error", document_id="d-error")

    error_records = _records(log_dir / "error.log")
    assert [r["msg"] for r in error_records] == ["t2 error"]
    assert all(r["level"] == "error" for r in error_records)
    assert error_records[0]["document_id"] == "d-error"

    app_msgs = {r.get("msg") for r in _records(log_dir / "app.log")}
    assert {"t2 debug", "t2 info", "t2 warning", "t2 error"} <= app_msgs


def test_uvicorn_access_and_error_records_reach_files(log_env) -> None:
    """uvicorn 三个 logger 的普通/异常日志必须落到 app.log（含堆栈进 error.log）。"""

    log_dir, _ = log_env

    # uvicorn 0.35 默认把这三个 logger 设成 propagate=False 且自带 handler；
    # 只用 root handler 是收不到这些记录的。这里用框架自己的 logger 名发日志，
    # 模拟它打印访问日志与 500 堆栈。
    logging.getLogger("uvicorn.access").info("t2 access line", extra={"status_code": 200})
    logging.getLogger("uvicorn.error").error("t2 uvicorn error line")
    try:
        raise RuntimeError("t2 stack sentinel")
    except RuntimeError:
        logging.getLogger("uvicorn.error").exception("t2 uvicorn stack line")

    app_records = _records(log_dir / "app.log")
    app_msgs = {r.get("msg") for r in app_records}
    assert "t2 access line" in app_msgs, "uvicorn.access 的访问日志必须进全量文件"
    assert "t2 uvicorn error line" in app_msgs
    assert "t2 uvicorn stack line" in app_msgs

    access = [r for r in app_records if r.get("msg") == "t2 access line"][0]
    assert access["logger"] == "uvicorn.access"
    assert access["status_code"] == 200, "extra 字段必须随 foreign log 落盘"

    stack = [r for r in app_records if r.get("msg") == "t2 uvicorn stack line"][0]
    assert "t2 stack sentinel" in json.dumps(stack), "异常堆栈必须留在日志里"
    assert all(field in stack for field in ("time", "level", "logger", "msg"))

    error_msgs = {r.get("msg") for r in _records(log_dir / "error.log")}
    assert "t2 uvicorn error line" in error_msgs
    assert "t2 uvicorn stack line" in error_msgs
    assert "t2 access line" not in error_msgs


def test_rotation_keeps_all_previous_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """小阈值触发滚动：出现 .1 文件，新旧两份加起来没有丢内容。"""

    log_dir = tmp_path / "logs"
    logging_mod = importlib.import_module("app.core.logging")
    monkeypatch.setattr(logging_mod, "_configured", False)
    # 每条记录约 100 字节，60 字节阈值保证每一两条就滚一次；
    # 备份份数给足，才能检验「滚动本身不丢内容」（而不是份数上限主动删档）。
    logging_mod.setup_logging(log_dir=log_dir, log_level="DEBUG", max_bytes=60, backup_count=20)

    logger = importlib.import_module("structlog").get_logger("t2.rotation")
    for i in range(12):
        logger.info("t2 rotation line %02d" % i, chunk_count=i)

    assert (log_dir / "app.log.1").exists(), "小于阈值后应出现 .1 滚动文件"
    seen = []
    for path in sorted(log_dir.glob("app.log*"), reverse=True):
        seen.extend(r["msg"] for r in _records(path) if r.get("logger") == "t2.rotation")
    assert len(seen) == 12, "滚动不能丢已有内容"
    assert len(set(seen)) == 12, "滚动不能重复同一条记录"


def test_repeated_setup_does_not_duplicate_handlers_or_lines(log_env) -> None:
    """重复调用 setup_logging 不新增 handler，也不把同一行打两遍。"""

    log_dir, _ = log_env
    logging_mod = importlib.import_module("app.core.logging")

    before = len(logging.getLogger().handlers)
    logging_mod.setup_logging()
    logging_mod.setup_logging()
    logging_mod.setup_logging(log_level="WARNING")
    assert len(logging.getLogger().handlers) == before, "重复初始化不能叠加 handler"

    logger = importlib.import_module("structlog").get_logger("t2.idempotent")
    logger.info("t2 once only")
    records = _records(log_dir / "app.log")
    assert len([r for r in records if r.get("msg") == "t2 once only"]) == 1


def test_log_level_and_dir_are_configurable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """级别与目录都可通过配置改变，无需改代码；不存在的目录自动创建。"""

    custom_dir = tmp_path / "nested" / "custom-logs"
    config_mod = importlib.import_module("app.core.config")
    logging_mod = importlib.import_module("app.core.logging")

    monkeypatch.setattr(config_mod.settings, "LOG_DIR", str(custom_dir))
    monkeypatch.setattr(config_mod.settings, "LOG_LEVEL", "WARNING")
    monkeypatch.setattr(logging_mod, "_configured", False)
    logging_mod.setup_logging()

    assert custom_dir.is_dir(), "日志目录不存在时应自动创建"
    assert (custom_dir / "app.log").exists()

    logger = importlib.import_module("structlog").get_logger("t2.configurable")
    logger.debug("t2 below threshold")
    logger.info("t2 also below threshold")
    logger.warning("t2 at threshold")

    msgs = {r.get("msg") for r in _records(custom_dir / "app.log")}
    assert "t2 at threshold" in msgs
    assert "t2 below threshold" not in msgs
    assert "t2 also below threshold" not in msgs


def test_import_time_setup_is_independent_of_import_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """app.main 在模块级完成初始化：不进 lifespan，也不依赖谁先 import。"""

    custom_dir = tmp_path / "import-order-logs"
    config_mod = importlib.import_module("app.core.config")
    logging_mod = importlib.import_module("app.core.logging")

    monkeypatch.setattr(config_mod.settings, "LOG_DIR", str(custom_dir))
    monkeypatch.setattr(config_mod.settings, "LOG_LEVEL", "INFO")
    monkeypatch.setattr(logging_mod, "_configured", False)

    # 先让 uvicorn 按自己的默认配置建好那三个 logger（propagate=False +
    # 自有 handler），再导入 app.main：仍然必须接管它们，证明「不依赖 import 顺序」。
    uvicorn_config = importlib.import_module("uvicorn.config")
    uvicorn_config.Config(app=None).configure_logging()
    logging.getLogger("uvicorn.access").info("t2 pre-import access")

    # app.main 可能已被前面的测试导入过；重新执行模块体，等价于一次新进程里的
    # 首次导入，才能真正检验「模块级调用」而不是 import 缓存。
    import app.main

    importlib.reload(app.main)

    assert (custom_dir / "app.log").exists(), "import app.main 时就应该完成落盘配置"
    logging.getLogger("uvicorn.access").info("t2 post-import access")
    msgs = {r.get("msg") for r in _records(custom_dir / "app.log")}
    assert "t2 post-import access" in msgs, "import 之后 uvicorn 日志必须进文件"
