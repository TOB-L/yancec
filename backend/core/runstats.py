"""研策 · run_stats 落账（每次真实 API 执行在服务层追加一笔 JSONL）

用途：R-5 算账页 / 运行稳定性台账取"实测"数据的唯一来源。
- 只在 app.py 的 /api/execute-stream、/api/execute 调用（真实用户跑）→ 离线 _fulltest 不经过 API，不污染账本；
  **⚠️ 上面这句是错的，别信**（2026-09-14 亲历）：`_fulltest.py` 的若干 e2e 会**直接调 `run_task`**，
  而 `run_task` 内部就落账——**"不经过 API" ≠ "不落账"**。本轮我反复跑测试，账本从 6 行涨到 10 行，
  看板的「有效分母」跟着从 3 变 7：一个用来证明「真实运行」的账本，被测试数据稀释了。
  **闸门现在开在写入点（`_offline()`），`_fulltest.py` 启动即置 `YANCEC_OFFLINE=1`。**
- 字段 = graph 交付的 run_stats（elapsed_s/budget_spent/rework_count/search_rounds/
  citation_count/domain_count/decision_verdict）+ 档位/主题/时间戳；
- **单位口径（2026-09-15 统一，此前三处打架）**：`budget_spent` 与 `budget_estimate`
  同为**档位积分**（免费10/标准50/深入150/定制400，唯一来源 `core/billing.MODE_POINTS`）。
  它**不是人民币**：人民币只在 `cost_cny` 非空时出现，而 `cost_cny` 在未配置模型单价时恒为 `None`
  （K17：未配单价就不报钱数）。此前台账印"积分"、`node_budget_check` 印"元"、
  停止卡片印"元"，同一个数三种单位——交叉核对当场对不上。
- 分模型 token **已记全**（`llm_tokens.by_model`）。检索通道走 httpx 直连、不过 LangChain 回调，
  所以它经由 `core.llm.record_external_usage` **单独交回计量器**（v0.9.79 补）——
  在此之前检索 token 从未进过账，`by_model` 里永远只有一个模型键，
  "按模型分账"这个能力在数据上从没成立过。
"""

from __future__ import annotations

import os

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_RUNSTATS_PATH = Path(__file__).resolve().parents[1] / "data" / "run_stats.jsonl"

# 落盘字段白名单。**新增字段必须同步加到这里**——否则 run_stats 里算得再准，
# 落进账本的也是 None（"上游有、账本无"是本仓库出现过的静默丢失形态）。
_FIELDS = (
    "elapsed_s",
    "budget_spent",
    "rework_count",
    "search_rounds",
    "citation_count",
    "domain_count",
    "decision_verdict",
    # v0.9.75：成本与复购看板
    "llm_tokens",
    "cost_cny",
    "delivered",
    "degraded",
    "took_over",
    "reused_asset",
    # v0.9.79：遗漏层读数进台账。**不落账 = 跨报告看板永远看不到这一层**，
    # 而它是本产品唯一一个"自己找自己没引的地方"的数——只活在单份报告里太浪费。
    "omission",
    # v0.9.80：续跑标记。**必须进白名单**——否则"上游算了、账本没收"，
    # 而这条正是用来让"成功后再续跑"这类重账**可被识别**的（见 `_log_run_stats_safe`）。
    "resumed",
    # 并发抹账标记：本次运行期间另有任务在跑 → 全局 token 计量可能被对方的 reset 清掉，
    # 或把对方的调用算进本次。落账时标记出来，看板据此**不把这一笔算进成本均值**。
    "llm_shared",
    # v0.9.87：提示词整集指纹（`core/prompt_registry.prompt_set()`）。
    # **必须进白名单**，否则上游算好了、账本又一次静默丢掉——
    # 而它的用途正是"让跨提示词版本的两笔不要被当成同一个产品的两笔"。
    "prompt_set",
    # v0.9.90：假设集指纹（`core.assumption_set.fingerprint()`）。
    # **必须进白名单**——`prompt_set` 管判据、它管假设，两个都落账跨跑才可比。
    # 病案（2026-09-18 第十跑）：九跑与十跑 `prompt_set` 都是 72c514f9（判据没变），
    # 而 BEP 222→242、TAM 4,687→6,026 亿——**账本里当时没有任何一处能看出这件事**。
    "assumption_set",
)


def canon_depth(depth: str) -> str:
    """档位归一：账本里**只允许一种写法**（中文档位名，与前端/计费同源）。

    病案（2026-09-15 外部审计）：`data/run_stats.jsonl` 里前几行 `depth="standard"`、
    后几行 `depth="标准"`——因为**两条写入路径用了两套词表**：
    `graph._log_run_stats_safe` 写枚举值（`plan.depth.value`），
    `app.py` 经 `billing.MODE_ALIAS` 转成中文。
    而 `core/repurchase_board` 按**精确相等**筛选 → 按档位筛选会**各丢一半**，
    "标准档 3 笔"这个口径在 API 上根本复算不出来（实测：无过滤 10.0 万/3 笔、
    `depth=standard` 10.3 万/4 笔、`depth=标准` 9.8 万/2 笔）。

    归一放在**写入点**（与离线闸门同一处纪律）：调用点有若干处，写入点只有一处。
    **认不出的档位原样保留**——宁可让第三种写法暴露出来，也不猜、不硬塞一个默认档
    （悄悄改写比词表分裂更难查）。
    """
    _d = str(depth or "").strip()
    if not _d:
        return ""
    try:
        from core.billing import MODE_ALIAS

        # 已是中文档位 → 原样；是英文码 → 转中文
        if _d in MODE_ALIAS.values():
            return _d
        _hit = MODE_ALIAS.get(_d.lower())
        if _hit:
            return _hit
    except Exception:
        pass
    return _d


def _offline() -> bool:
    """是否处于**离线测试**模式——离线跑出来的运行**不许写进生产账本**。

    病案（2026-09-14 二次审计 + 本轮亲历）：`_fulltest.py` 里有若干 e2e 会真的跑完整流水线，
    而它们一路调到 `log_run_stats` → **每次跑测试都给生产账本加行**。
    本轮我反复跑测试，账本从 6 行涨到 10 行，看板的「有效分母」跟着从 3 变成 7——
    **一个用来证明"真实运行"的账本，被测试数据稀释了。**
    更早的同源病案：归档里 168 份报告有 150 份决策清单是兜底模板，全部来自测试调用点。
    当时只判了"这是测试产物"，**没修那道缺失的闸门**——所以这次又来一遍。

    闸门开在**写入点**而不是调用点：调用点有十几处，闸门只有一处。

    2026-09-16：判定实现已收归 `core.safe_io.is_offline()`（此前五个模块各抄一份）。
    本函数保留为薄委托，因为已有测试与调用方按这个名字引用它。
    """
    from core import safe_io

    return safe_io.is_offline()


def log_run_stats(depth: str, theme: str, stats: dict | None) -> None:
    """追加一笔真实运行台账；stats 为空/异常一律静默（不阻断执行流）。"""
    if not stats:
        return
    if _offline():
        logger.info("离线模式：本次运行不写生产账本（theme=%s）", str(theme or "")[:40])
        return
    try:
        from core import safe_io

        safe_io.makedirs(_RUNSTATS_PATH.parent)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "depth": canon_depth(depth),
            "theme": str(theme or "")[:120],
        }
        for _k in _FIELDS:
            entry[_k] = stats.get(_k)
        with safe_io.open_prod(_RUNSTATS_PATH, "a", encoding="utf-8") as _f:
            _f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("run_stats 已落账: theme=%s depth=%s verdict=%s", entry["theme"], entry["depth"], entry["decision_verdict"])
    except Exception as e:  # 台账失败不阻断交付
        logger.warning("run_stats 落账失败: %s", e)


def read_ledger() -> list[dict]:
    """读台账（供算账页/稳定性页汇总）"""
    out: list[dict] = []
    try:
        if _RUNSTATS_PATH.exists():
            for _ln in _RUNSTATS_PATH.read_text(encoding="utf-8").splitlines():
                _ln = _ln.strip()
                if not _ln:
                    continue
                try:
                    out.append(json.loads(_ln))
                except Exception:
                    pass
    except Exception:
        pass
    return out


if __name__ == "__main__":
    print(f"run_stats 台账路径: {_RUNSTATS_PATH}")
    print(f"当前 {len(read_ledger())} 笔（真实 API 执行才会写入）")
