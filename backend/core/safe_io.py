"""生产落盘的**唯一闸门**（离线模式的单点裁决）。

## 为什么要有这个模块（病案 · 2026-09-16 · 第五次复发 · 探针实测）

`YANCEC_OFFLINE` 闸门此前是**逐点手装**的。装过的四处：
`runstats`（账本）· `report_store`（报告存档）· `audit_bundle`（可复算包）·
`exp_memory`（经验台账）。而 `_gate_probe.py`（把各模块落盘路径改指到临时目录后
置 `YANCEC_OFFLINE=1` 实测）跑出来的是：

    受闸门保护 5 处 · **未受保护 5 处**：metric_ledger（指标时序库）·
    industry_kb（行业卡）· memory（用户档案）· source_health（巡检台账）·
    calibration（校准存档）。**覆盖率 5/10。**

前四次都是"发现一处、补一处"，`_fulltest.test_offline_write_gates` 的 docstring
甚至已经写下「以后再新增写生产档案的模块，也必须过这道闸门」——**然后照样漏了五处**。

**根因不是"忘了"，是闸门装错了位置**：它装在**业务函数**里，而业务函数是不断新增的；
每新增一个写点，就要靠人记得再装一次。所以这里把它下移到 **I/O 层**——
生产落盘只能经由本模块。想绕过闸门就必须在别处直接写 `open()`，
而那种写法会被 `_fulltest.test_prod_writes_are_gated`（AST 机械枚举）当场拦下。

## 用法

    from core import safe_io

    with safe_io.open_prod(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)      # 离线时写进黑洞，不碰磁盘

    safe_io.write_text(path, text)                 # 离线时返回 False，不抛异常
    safe_io.append_text(path, line)                # 同上
    safe_io.makedirs(dirpath)                      # 离线时连目录都不建
    safe_io.replace(tmp, dst)                      # 原子替换，离线时是空操作

**离线是"静默不写"而不是"抛异常"**：交付流水线里落盘失败从来不该影响交付，
把闸门做成异常会把"不写"变成"报错"，反而改了行为。
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "True", "yes", "on")


def is_offline() -> bool:
    """是否处于离线模式（**全仓唯一的判定实现**）。

    此前这段判断在 runstats / report_store / audit_bundle / exp_memory /
    outcome_track 里各抄了一份（五份逐字相同），属"同一动作、两处实现"家族。
    """
    return str(os.environ.get("YANCEC_OFFLINE") or "").strip() in _TRUTHY


def blocked() -> bool:
    """这次落盘是否被闸门挡住。

    语义等同 `is_offline()`；保留这个别名是因为调用点读起来更贴近意图
    （"被挡住了"而不是"现在是离线"）。**不收参数**——早期版本收了一个 `target`，
    只为日志好看，而它在函数体里从未被使用（被 `_fulltest` 的
    「被具名传入却从未使用」那条锁抓到过一次同型问题，干脆去掉）。
    """
    return is_offline()


def _note(target: Any, what: str) -> None:
    logger.info("离线模式：跳过生产落盘（%s，target=%s）", what, str(target)[-70:])


class _Sink:
    """离线时的落盘替身：吞掉一切写入，一个字节都不碰磁盘。

    做成对象而不是 `None`，是为了让 `with open_prod(...) as f: json.dump(x, f)`
    这类既有写法**原样可用**——闸门不该要求调用方改写控制流。
    """

    __slots__ = ()

    def write(self, *a: Any, **k: Any) -> int:
        return 0

    def writelines(self, *a: Any, **k: Any) -> None:
        return None

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None

    def seek(self, *a: Any, **k: Any) -> int:
        return 0

    def tell(self) -> int:
        return 0

    def truncate(self, *a: Any, **k: Any) -> int:
        return 0

    def __enter__(self) -> "_Sink":
        return self

    def __exit__(self, *a: Any) -> bool:
        return False


@contextlib.contextmanager
def open_prod(path: Any, mode: str = "w", **kw: Any) -> Iterator[Any]:
    """打开生产文件；离线时给一个黑洞替身。"""
    if is_offline():
        _note(path, f"open({mode})")
        yield _Sink()
        return
    if "b" not in mode:
        kw.setdefault("encoding", "utf-8")
    with open(path, mode, **kw) as f:
        yield f


def write_text(path: Any, text: str, encoding: str = "utf-8") -> bool:
    """写文本文件 → 是否真的写了（离线时 False）。"""
    if is_offline():
        _note(path, "write_text")
        return False
    Path(path).write_text(text, encoding=encoding)
    return True


def write_bytes(path: Any, data: bytes) -> bool:
    if is_offline():
        _note(path, "write_bytes")
        return False
    Path(path).write_bytes(data)
    return True


def append_text(path: Any, text: str, encoding: str = "utf-8") -> bool:
    if is_offline():
        _note(path, "append_text")
        return False
    with open(path, "a", encoding=encoding) as f:
        f.write(text)
    return True


def makedirs(path: Any, exist_ok: bool = True) -> bool:
    """建目录 → 是否真的建了。离线时连目录都不建（免得留下空壳）。"""
    if is_offline():
        _note(path, "makedirs")
        return False
    os.makedirs(path, exist_ok=exist_ok)
    return True


def replace(src: Any, dst: Any) -> bool:
    """原子替换（临时文件 → 正式文件）→ 是否真的换了。"""
    if is_offline():
        _note(dst, "replace")
        return False
    os.replace(src, dst)
    return True


def remove(path: Any, missing_ok: bool = True) -> bool:
    """删文件 → 是否真的删了。离线时不动手。"""
    if is_offline():
        _note(path, "remove")
        return False
    try:
        os.remove(path)
    except FileNotFoundError:
        if not missing_ok:
            raise
        return False
    return True


def rmtree(path: Any, ignore_errors: bool = True) -> bool:
    """删目录树 → 是否真的删了。离线时不动手。"""
    if is_offline():
        _note(path, "rmtree")
        return False
    shutil.rmtree(path, ignore_errors=ignore_errors)
    return True
