"""研策 · 会话记忆（跨任务记住用户偏好与行业知识）

对应 v3 方案 §4.1 进阶功能 12：跨任务记住用户偏好与已积累的行业知识。

实现（简单、可落地、不翻车）：
- 长周期记忆 = 资产库（core/rag.py，跨任务复用检索结果）
- 会话记忆 = 用户偏好 JSON（本模块）：记住用户常问的行业/深度档/目标，
  下次提问自动带入，省去重复澄清。

存储：memory/user_profile.json（D 盘项目内，不写系统盘）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from core import safe_io

logger = logging.getLogger(__name__)

# 用户档案文件（项目内绝对路径）
#
# ⚠️ **路径原先落在 `backend/data` 之外**（2026-09-16 全量审计，`parents[2]` = `yancec/`）：
# 全仓**其他每一个持久化文件**都在 `backend/data/` 下（账本 / 报告存档 / 指标库 /
# 行业卡 / 资产库 / 回访记录…），只有这一份在**上一层**。
# 后果不是"路径难看"，是**运维面漏一个**：备份、重置、清理、"清测试产物"
# 这些动作都是按 `backend/data/` 扫的——扫不到它，于是
# **一份带着旧行业偏好与历史决策的档案会静默活过所有清理**，
# 并在下一次规划时以"用户画像"的身份反灌回来（`profile_block` 会注入写作上下文）。
# 盘上实测：`yancec/data/user_profile.json` 11.5KB 真实数据，而 `backend/data/` 下没有。
# 现在与全仓对齐；旧位置的文件已一并迁入（见同目录迁移记录）。
PROFILE_PATH = Path(__file__).resolve().parents[1] / "data" / "user_profile.json"

# 内存态（避免频繁读写磁盘）
_profile: dict | None = None


def _load() -> dict:
    global _profile
    if _profile is not None:
        return _profile
    try:
        if PROFILE_PATH.exists():
            _profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        else:
            _profile = {"industries": [], "prefs": {}}
    except Exception as e:
        logger.warning("用户档案读取失败: %s", e)
        _profile = {"industries": [], "prefs": {}}
    return _profile


def _save() -> None:
    """落用户档案（行业偏好 + 历史决策）。

    ## 离线闸门（2026-09-16 补 · 第五次复发病案的第八处）

    此前没有闸门，`_gate_probe.py` 实测确认离线模式下 `user_profile.json` 仍被改写。
    污染路径：档案被 `recall()` 读回、注入下一次调研的上下文；
    `record_decision()` 写进去的"历史决策"会出现在**决策记忆**面板与提示词里。
    测试跑出来的假决策一旦落档，下次真实调研就会看到一条自己从没做过的决策。
    """
    if safe_io.is_offline():
        logger.info("离线模式：本次不写生产用户档案")
        return
    try:
        safe_io.makedirs(PROFILE_PATH.parent)
        safe_io.write_text(PROFILE_PATH, json.dumps(_load(), ensure_ascii=False, indent=2))
    except Exception as e:
        logger.warning("用户档案写入失败: %s", e)


def remember(question: str, context: dict[str, str] | None = None) -> None:
    """记录一次提问：行业词 + 偏好（深度档/目标）+ 会话摘要

    四层记忆之一（语义/画像层）：
    - industries：用户调研过的行业（语义知识，可注入下次规划）
    - prefs：偏好（深度档/目标）
    - sessions：最近会话摘要（行业+问题+时间），供"续写上下文"参考
    """
    from agents.researchers import _extract_industry
    from datetime import datetime

    profile = _load()
    industry = _extract_industry(question)
    if industry and industry not in profile["industries"]:
        profile["industries"].append(industry)
        profile["industries"] = profile["industries"][-5:]  # 最多记5个行业
    context = context or {}
    # 记住偏好（深度档、调研目标）
    if context.get("depth"):
        profile["prefs"]["depth"] = context["depth"]
    if context.get("objective"):
        profile["prefs"]["objective"] = context["objective"]
    profile["prefs"]["last_question"] = question[:80]
    # 会话摘要（情景记忆）：最近10次调研，按行业+问题+时间归档
    sessions = profile.get("sessions", [])
    sessions.insert(
        0,
        {
            "industry": industry or "通用",
            "question": question[:40],
            "ts": datetime.now().strftime("%m-%d %H:%M"),
        },
    )
    profile["sessions"] = sessions[:10]
    _save()


def recall(question: str, context: dict[str, str] | None = None) -> dict[str, str]:
    """把记忆补进本次调研上下文（缺什么补什么，已有优先）

    返回的 ctx 会透传到 Router 规划 prompt —— 让 LLM 知道用户调研过什么，
    实现"跨任务续写上下文"（长短期记忆的工程落地）。
    """
    profile = _load()
    prefs = profile.get("prefs", {})
    ctx = dict(context or {})
    # 若本次没指定深度/目标，用记忆里的偏好
    if not ctx.get("depth") and prefs.get("depth"):
        ctx["depth"] = prefs["depth"]
    if not ctx.get("objective") and prefs.get("objective"):
        ctx["objective"] = prefs["objective"]
    # 附上历史行业（供 LLM 参考：用户之前调研过什么）
    if profile.get("industries"):
        ctx["历史行业"] = "、".join(profile["industries"])
    # 附上最近调研主题（情景记忆：续写上下文，避免用户重复说明背景）
    sessions = profile.get("sessions", [])
    if sessions:
        ctx["历史调研"] = "；".join(
            f"{s['industry']}：{s['question']}" for s in sessions[:3]
        )
    # 附上最近决策（Decision Memory：上次怎么判断的，本次更新需对比变化）
    decisions = profile.get("decisions", [])
    if decisions:
        d = decisions[0]
        conds = "；".join(d.get("monitor_conditions", [])[:2])
        ctx["最近决策"] = (
            f"{d['industry']}：上次判定 {d['verdict']}（{d['question'][:30]}）"
            + (f"；监控条件：{conds}" if conds else "")
        )
    return ctx


def clear() -> None:
    """清空记忆（用户主动清除）"""
    global _profile
    _profile = {"industries": [], "prefs": {}}
    _save()


# ============================================================
# Decision Memory（决策记忆）—— 长期护城河：决策 → 结果 → 再决策
# 记录"当时为什么这么判断 + 关键假设 + 监控条件"，下次任务 recall 带入，
# 实现"市场变了 → 重新评估"的持续决策闭环（培训：今天用了明天还用吗）。
# ============================================================
def record_decision(
    question: str,
    industry: str,
    verdict: str,
    key_assumptions: str = "",
    monitor_conditions: list[str] | None = None,
) -> None:
    """记录一次决策（Go/No-Go/Validate + 关键假设 + 监控条件）"""
    from datetime import datetime

    profile = _load()
    decisions = profile.setdefault("decisions", [])
    decisions.insert(
        0,
        {
            "question": question[:80],
            "industry": industry or "通用",
            "verdict": verdict,
            "key_assumptions": key_assumptions[:200],
            "monitor_conditions": monitor_conditions or [],
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
        },
    )
    profile["decisions"] = decisions[:20]
    _save()


def list_decisions() -> list[dict]:
    """列出历史决策（供前端"持续监控"面板展示）"""
    return _load().get("decisions", [])


def get_last_decision() -> dict | None:
    """最近一次决策（供 Market Monitor 展示）"""
    decisions = _load().get("decisions", [])
    return decisions[0] if decisions else None
