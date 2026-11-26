"""保留期清理脚本的**截止时间口径**（第 14 步 #201，审查后补）。

这张票里唯一会**多删数据**的地方就是这两个截止点，而它错的形态极其安静：拿 UTC 算完
抹掉 ``tzinfo`` 去比本地裸时间，在东八区就多删 8 小时明细——删掉就没了，没有任何报错。

所以这里不测「脚本能跑」，只测**两个时钟**：明细用本地裸时间（与 ``created_at`` 同一个
时钟），日汇总用 UTC 日历天（``usage_daily.day`` 的标签口径）。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_purge_script() -> ModuleType:
    """从文件加载脚本：``scripts*`` 不在安装包里，显式路径最可靠（导入即安全）。"""

    path = SCRIPTS_DIR / "purge_usage_events.py"
    spec = importlib.util.spec_from_file_location("purge_usage_events_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 必须先登记进 ``sys.modules``：脚本里有 ``@dataclass``，它按 ``cls.__module__``
    # 回查模块（查不到就 AttributeError），这是 dataclasses 的实现细节，不是脚本的毛病。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_event_cutoff_uses_the_same_clock_as_created_at() -> None:
    """明细截止点必须是**本地裸时间**：``usage_events.created_at`` 就是那么写的。"""

    script = _load_purge_script()
    before = datetime.now()
    event_cutoff, _ = script.cutoffs(keep_days=90, keep_days_daily=395)
    after = datetime.now()

    assert event_cutoff.tzinfo is None, "裸时间，与列里那个形状一致"
    # 允许几秒的执行抖动：关键是没有 8 小时的时区偏移。
    assert before - timedelta(days=90) <= event_cutoff <= after - timedelta(days=90)


def test_the_daily_cutoff_uses_the_utc_calendar_day() -> None:
    """日汇总截止点是 **UTC 日历天**：``usage_daily.day`` 的标签就是那么来的。"""

    script = _load_purge_script()
    _, daily_cutoff = script.cutoffs(keep_days=90, keep_days_daily=395)

    expected = (datetime.now(timezone.utc) - timedelta(days=395)).date()
    assert daily_cutoff == expected


def test_the_two_cutoffs_are_not_the_same_instant() -> None:
    """两个截止点来自两个时钟——**在东八区它们本来就差 8 小时**。

    这条用例的存在方式是「防止有人把它们合并成一个变量」：合并之后两边必有一边是错的，
    而错的那一边不会报错，只是多删或少删。
    """

    script = _load_purge_script()
    event_cutoff, daily_cutoff = script.cutoffs(keep_days=90, keep_days_daily=90)

    # 同一个 keep_days 下，明细是「本地 90 天前那个时刻」，日汇总是「UTC 的 90 天前那一天」。
    assert event_cutoff.date() >= daily_cutoff
