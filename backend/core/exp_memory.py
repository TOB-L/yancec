"""研策 · 研究经验记忆——跨任务记住"哪条路走不通"

## 记什么（以及**只**记什么）

一次调研结束后，`source_trace` 里已经有了最硬的一手材料：**本次向每个源发了几次请求、
回来几条**。这是**可核对的计数**，不是印象。

所以本模块只记这一类事实：
> 「在『咖啡连锁』这类问题上，国家统计局被调用 5 次、其中 1 次有返回」

它**不记**：哪次结论更好、哪种写法更受欢迎、模型觉得哪个源"更权威"。
那些要么不可核对，要么会变成自我实现的偏见。

## 三条护栏（缺一条都会变成自腐）

1. **最小样本门**：调用次数 < 3 的**不输出**。一次偶然的空返回不构成"经验"——
   把它当经验，下次就会因为这个巧合少查一个本该查的源。
2. **只作提示，永不作硬规则**。回忆出来的文本进入检索上下文，措辞是
   「历史统计显示…（供参考，**本次仍应尝试**）」。绝不写进 `CLAIM_ROUTES` 那种强制路由。
   理由：源失败往往是**问题相关**的，不是**源本身**的问题。
   "统计局对门店数没返回"不代表"统计局对这个行业没用"——换个问法它可能就返回了。
   把统计升级成规则，等于让一次偶发失败永久锁死一条路。
3. **可追溯**：每条记录带时间戳与行业，能被人工打开核对。经验如果不可核对，
   它就只是"系统的一厢情愿"，而报告里我们一直在批评这种东西。

## 与 `industry_kb` 的分工

- `industry_kb`：记**行业知识**（规模/门店数等指标卡）——是"这个行业是什么样"
- 本模块：记**研究过程经验**（哪条路通、哪条路不通）——是"怎么研究这个行业更省力"
  两者都会随使用增长，但前者可能被污染（已有护栏），后者**只存计数、不可能被污染**。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from typing import Any

from core import safe_io

logger = logging.getLogger(__name__)

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "research_experience.jsonl"
)
_LOCK = threading.Lock()
MAX_KEEP = 800          # 滚动窗口（约几百次调研）
MIN_CALLS = 3           # 最小样本门：少于这个调用次数不输出
_LOW_HIT = 0.34         # 命中率低于此比例才值得提醒（高了没必要说）


def _industry_of(text: str) -> str:
    try:
        from agents.researchers import _extract_industry

        return (_extract_industry(text or "") or "").strip()[:20]
    except Exception:
        return ""


def _offline() -> bool:
    """离线测试模式——**测试产物不许写进生产经验台账**。

    病案（2026-09-16 核验三份外部方案时，由对抗核验员注入探针实测确认）：
    本模块是**第四个**"写生产档案"的写入点。前三处——账本（`runstats.py`）、
    报告存档（`report_store.py`）、可复算包（`audit_bundle.py`）——都装了这道闸门，
    这里没有。实测置 `YANCEC_OFFLINE=1` 后调 `record_run()`，仍返回 1、文件被改写、
    探针源 `zzz_test_src` 落盘。

    **这是同一个病的第四次复发**，而且 `test_offline_write_gates` 的 docstring
    当时自称锁的是「**每一个**生产写入点」——它锁了三处，第四处不在扫描面里。
    所以这回把锁扩到四处，并在那条测试里写明：**新增写入点必须同步加进来**。

    为什么这一处的污染特别隐蔽：经验台账是**喂回检索上下文**的（护栏 2：只作提示）。
    测试用的假源名会以「历史统计显示…」的形式出现在**下一次真实调研**的提示词里——
    污染的不是账，是**下一次的判断**。

    2026-09-16（第五次复发当天）：判定实现已收归 `core.safe_io`；本函数保留为薄委托。
    同日探针实测发现另有五处生产写点没有闸门（见 `core/safe_io.py` 顶部病案），
    闸门已从"逐点手装"下移到 I/O 层。
    """
    from core import safe_io

    return safe_io.is_offline()


def record_run(state: dict[str, Any], result: dict[str, Any] | None = None) -> int:
    """把本次的"源 × 命中"计数落盘。返回写入条数（失败返回 0，不阻断交付）。"""
    if _offline():
        logger.info("离线模式：本次不写生产经验台账")
        return 0
    try:
        from core.source_health import summarize_trace

        tr = (state or {}).get("source_trace") or []
        if not tr:
            return 0
        s = summarize_trace(tr)
        rows = s.get("rows") or []
        if not rows:
            return 0
        q = str((state or {}).get("raw_question") or "")
        ind = _industry_of(q)
        if not ind:
            return 0
        ts = datetime.now().isoformat(timespec="seconds")
        recs = []
        for r in rows:
            # **计划跳过的源不记**：它根本没发请求，命中率为 0 是必然的，
            # 记进去会造出一条"这个源不行"的假经验（而它只是这次没被选中）。
            if "未选中" in str(r.get("note") or ""):
                continue
            calls = int(r.get("calls") or 0)
            if calls <= 0:
                continue
            recs.append(
                {
                    "ts": ts,
                    "industry": ind,
                    "source": str(r.get("src") or r.get("name") or "?")[:40],
                    "name": str(r.get("name") or "")[:40],
                    "calls": calls,
                    "hits": int(r.get("n") or 0),
                }
            )
        if not recs:
            return 0
        safe_io.makedirs(os.path.dirname(_PATH))
        with _LOCK:
            with safe_io.open_prod(_PATH, "a", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            _prune()
        return len(recs)
    except Exception as e:
        logger.warning("研究经验落盘失败（不影响交付）: %s", e)
        return 0


def _prune() -> None:
    try:
        with open(_PATH, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > MAX_KEEP:
            with safe_io.open_prod(_PATH, "w", encoding="utf-8") as f:
                f.writelines(lines[-MAX_KEEP:])
    except Exception:
        pass


def _load() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        if not os.path.exists(_PATH):
            return []
        with open(_PATH, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                    if isinstance(d, dict):
                        out.append(d)
                except Exception:
                    continue
    except Exception as e:
        logger.warning("研究经验读取失败: %s", e)
    return out


def recall(industry: str, min_calls: int = MIN_CALLS) -> list[dict[str, Any]]:
    """按行业聚合 → 只回**值得提醒**的项（样本够 + 命中率低）。

    聚合口径写死为"总调用 / 有返回的次数"，因为这是唯一能从 `source_trace` 里
    **复算**出来的量——复核者/客户拿台账自己也能算，不需要信我们。
    """
    ind = (industry or "").strip()[:20]
    if not ind:
        return []
    agg: dict[str, dict[str, Any]] = {}
    for r in _load():
        if str(r.get("industry") or "") != ind:
            continue
        k = str(r.get("source") or "?")
        cur = agg.setdefault(k, {"source": k, "name": r.get("name") or k, "calls": 0, "hits": 0})
        cur["calls"] += int(r.get("calls") or 0)
        cur["hits"] += int(r.get("hits") or 0)
    out = []
    for cur in agg.values():
        if cur["calls"] < max(1, int(min_calls)):
            continue
        rate = cur["hits"] / cur["calls"] if cur["calls"] else 0.0
        if rate > _LOW_HIT:
            continue
        cur["hit_rate"] = round(rate, 3)
        out.append(cur)
    out.sort(key=lambda x: (x["hit_rate"], -x["calls"]))
    return out[:4]


def format_brief(industry: str) -> str:
    """经验 → 注入检索上下文的短提示。**没有可说的就一个字不给**（不印空壳）。"""
    items = recall(industry)
    if not items:
        return ""
    lines = [
        f"【历史研究经验·{industry}（本系统过往同类任务的**可核对调用统计**，供参考）】",
    ]
    for it in items:
        lines.append(
            f"- {it['name']}：调用 {it['calls']} 次、有返回 {it['hits']} 次"
            f"（命中率 {it['hit_rate'] * 100:.0f}%）"
        )
    lines.append(
        "⚠️ 这只是**统计提示，不是规则**：源在某类问题上没返回，往往是**检索角度**的问题，"
        "不是源本身没用。**本次仍应正常尝试**，换了角度的检索词很可能就命中——"
        "不要因为上面这几行就跳过任何一个源。"
    )
    return "\n".join(lines)


def describe() -> str:
    return (
        "研究经验记忆：把每次调研的「源 × 调用次数 × 有返回次数」计数落盘，"
        "跨任务按行业聚合；样本≥3 且命中率低的源会在检索上下文里得到一句提示。"
        "只记可核对的计数（不记印象），且**只作提示不作硬规则**——"
        "把统计升级成规则会让一次偶发失败永久锁死一条路。"
    )
