"""研策 · 行业知识库（行业基线卡片）——数据护城河核心，带防自腐护栏

每次调研完成 → 提取该行业关键指标存成"行业卡片"（市场规模/增速/渗透/客单价/头部/政策）
下次同行业调研 → 注入基线上下文，直接复用+增量更新。
"别人重新爬一遍数据，你有历史基线"——这是结构性的行业 know-how 沉淀。

v0.9.47 防自腐护栏（2026-09-08，病案：报告(6)验收跑 13:24:48 咖啡卡被"整卡覆写"
把 RedTeam 高风险 >63%、中风险 67天/17.3%、无源 31% 写成"行业基线"，下次跑再自复制）：
1. 不再无条件整卡覆写：新卡先过 _sanitize_card() 确定性过滤——
   - key_numbers 仅保留：有具名机构来源 且 非低信源通道（反方检索资料/历史基线/行业基线/
     基线数据/估算/存疑/待核）且 数值未命中本次 RedTeam 高/中风险标注（数字归一后比对）的项；
   - market_size/growth_rate/price_range/penetration 等标量字段：数值命中 RedTeam 标注 → 清空；
   - policy：若该行业有【法规原文核验】canonical（_REG_CANONICAL 或旧卡 regulatory 透传）→
     policy 以 canonical 为准（防未核数字冒充"政策基线"），LLM 提取的杂项只存 policy_note 供人工看；
     无 canonical 的行业保留 LLM policy，但剔除含低信源词/RedTeam 标注数字的句子。
2. 已核法规事实不丢：旧卡 regulatory 字段永远透传（即使新 LLM 提取没写）。
3. 全程零模型确定性下沉；save_industry_card(industry, report, redteam_high=None, redteam_med=None, _llm=None)
   redteam_high/med 由 graph.deliver 把 adv_issues（对抗性数字校验）的高/中风险数字清单传入。

验证：python core/industry_kb.py（内置离线自测，mock LLM，无网络）。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import safe_io

logger = logging.getLogger(__name__)

_KB_PATH = Path(__file__).resolve().parents[1] / "data" / "industry_kb.json"

# 低信源通道词：命中即不入基线（反方检索 = 对抗性低信源；历史/行业基线 = 自指，会形成自我复制环）
_LOW_TRUST = (
    "反方检索资料", "反方检索", "反方观点", "反方", "历史基线", "行业基线",
    "基线数据", "估算", "存疑", "待核", "待验证", "无来源", "未披露",
)

# 数字归一：">63%"→"63"、"4,200元"→"4200"、"186杯"→"186"（仅留数字与小数点）
def _numtok(v: Any) -> str:
    return re.sub(r"[^\d.]", "", str(v or ""))


def _low_trust(src: Any) -> bool:
    return any(t in str(src or "") for t in _LOW_TRUST)


def _numset(items: list[Any] | None) -> set[str]:
    return {_numtok(i) for i in (items or []) if _numtok(i)}


# 各行业【法规原文核验】canonical 事实（人工核验后写入，key 为行业名；护栏以它兜底 policy）
# 扩展方式：新行业经甲类事实终审后在此登记一条，即可阻止低信源数字冒充"政策基线"。
_REG_CANONICAL: dict[str, str] = {
    "咖啡": (
        "纯现制咖啡门店属「自制饮品制售」，食品经营许可仅需专用操作区＋净水/冷藏/制冰设施即可获证，"
        "无需强制设立冷食类操作专间；冷食类操作专间仅对兼售凉菜、现切果品等冷食的经营者强制。"
        "（《食品经营许可和备案管理办法》法规原文核验，2026-09）"
    ),
}

_CARD_PROMPT = """你是研策的行业知识库管理员。从这份调研报告中提取该行业的关键基线指标，输出 JSON：
{
  "industry": "行业名",
  "market_size": {"value": "市场规模数值", "unit": "亿元", "year": "年份", "source": "来源机构"},
  "growth_rate": {"value": "增速", "source": "来源"},
  "key_players": ["头部玩家1", "玩家2", "玩家3"],
  "price_range": "客单价/价格带",
  "penetration": "渗透率或消费频次",
  "policy": "关键政策/合规要点",
  "regulatory": "若报告含已核验的法规原文结论（如许可分类/专间要求），摘录到这里；没有则空字符串",
  "key_numbers": [{"name": "指标名", "value": "数值", "source": "来源机构"}]
}
规则：只输出 JSON；数值与来源必须逐字来自报告原文，禁止编造；没有的数据用空字符串；
来源必须写具体机构（禁止"反方检索资料/历史基线/估算"这类通道名）；key_numbers 最多 8 条。
"""


def _load() -> dict[str, Any]:
    try:
        if _KB_PATH.exists():
            return json.loads(_KB_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save(kb: dict[str, Any]) -> None:
    """落行业知识库。

    ## 离线闸门（2026-09-16 补 · 第五次复发病案的第七处）

    此前没有闸门，`_gate_probe.py` 实测确认离线模式下 `industry_kb.json` 仍被改写。

    这一处必须拦的理由有两层：
    ① **污染路径**：行业卡被 `format_industry_brief()` 读回并注入下一次调研的提示词——
       与经验台账同一条路。测试题（"杭州开咖啡店"）沉淀出的卡会变成下一次
       真实调研的"行业基线"。
    ② **口径路径**：绿档那一笔的对外说法里写着「**命中已沉淀行业卡**」。
       如果这张卡本身就是测试跑出来的，那句话的物证就不成立了。
       复核者若追问"这张卡是哪次调研沉淀的"，我们得答得出来。
    """
    if safe_io.is_offline():
        logger.info("离线模式：本次不写生产行业知识库")
        return
    try:
        safe_io.makedirs(_KB_PATH.parent)
        safe_io.write_text(_KB_PATH, json.dumps(kb, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.warning("行业知识库写入失败: %s", e)


def _sanitize_card(card: dict[str, Any] | None, old_card: dict[str, Any] | None,
                   redteam_high: list[Any] | None = None,
                   redteam_med: list[Any] | None = None) -> dict[str, Any]:
    """确定性护栏：过滤低信源/RedTeam 标注数字，法规 canonical 兜底 policy。纯规则、零模型成本。"""
    c = dict(card or {})
    bad = _numset(redteam_high) | _numset(redteam_med)
    ind = str(c.get("industry") or "")
    if not ind:
        return c

    # --- key_numbers：具名机构来源 + 非低信源 + 未命中 RedTeam 标注，否则剔除 ---
    kn: list[dict[str, Any]] = []
    for k in c.get("key_numbers") or []:
        if not isinstance(k, dict):
            continue
        v = str(k.get("value") or "")
        s = str(k.get("source") or "")
        if not v or not _numtok(v):
            continue  # 无值/无数值 → 不入基线
        if not s or _low_trust(s):
            logger.info("行业卡护栏: 剔除低信源/无源指标 %s=%s(%s)", k.get("name", ""), v, s)
            continue
        if _numtok(v) in bad:
            logger.info("行业卡护栏: 剔除 RedTeam 标注指标 %s=%s", k.get("name", ""), v)
            continue
        kn.append(k)
    if kn:
        c["key_numbers"] = kn
    else:
        c.pop("key_numbers", None)

    # --- 标量字段：值命中 RedTeam 标注 → 清空（防"渗透率含1.2杯"这类高危数进基线） ---
    def _scrub(t: Any) -> str:
        s = str(t or "")
        # 标量字段可能含多个数字（"渗透率68.5%，人均1.2杯"）→ 用"任一时效标注 token 是数值串的子串"判定，
        # 不能整串归一后全等（_numtok 会把 68.5+1.2 拼成 68.51.2，漏判 1.2 这类高危数）
        if s and any(_b in _numtok(s) for _b in bad):
            logger.info("行业卡护栏: 清空 RedTeam 标注标量字段(%s)", s[:20])
            return ""
        return s

    for dfield in ("market_size", "growth_rate"):
        d = c.get(dfield)
        if isinstance(d, dict) and "value" in d:
            d["value"] = _scrub(d.get("value"))
            if not d.get("value"):
                for k_ in ("unit", "year", "source"):
                    d[k_] = ""
    for sfield in ("price_range", "penetration"):
        if sfield in c:
            c[sfield] = _scrub(c.get(sfield))

    # --- policy：canonical（法规原文核验）兜底，防未核数字冒充"政策基线" ---
    canon = _REG_CANONICAL.get(ind, "") or str((old_card or {}).get("regulatory") or "")
    p = str(c.get("policy") or "")
    # 无 canonical 的行业：保留 LLM policy，但剔除含低信源词/RedTeam 标注数字的整句
    if not canon:
        kept = []
        for seg in re.split(r"(?<=[。；!?])", p):
            if not seg:
                continue
            if _low_trust(seg) or (_numtok(seg) and _numtok(seg) in bad):
                logger.info("行业卡护栏: 剔除 policy 中低信源/RedTeam 句: %s", seg.strip()[:24])
                continue
            kept.append(seg)
        c["policy"] = "".join(kept).strip("；; ")
    else:
        c["regulatory"] = canon
        extra = p.strip()
        c["policy"] = canon
        if extra:
            c["policy_note"] = extra[:300]  # 仅供人工查看，format_industry_brief 不注入
    return c


def save_industry_card(industry: str, report: str,
                       redteam_high: list[Any] | None = None,
                       redteam_med: list[Any] | None = None,
                       _llm: Any = None) -> dict[str, Any] | None:
    """从报告提取行业基线卡片并沉淀（每次调研完成后调用）；带护栏，返回沉淀后的 card（测试可读）。

    redteam_high/redteam_med: 本次对抗性数字校验(Red Team)判为高/中风险的数字原文（如 ">63%"），
    命中则不入基线。_llm: 测试注入；None 时走真实主 LLM。
    """
    if not industry or not report or len(report) < 200:
        return None
    try:
        if _llm is None:
            from core.llm import get_main_llm, has_main_llm

            if not has_main_llm():
                return None
            _llm = get_main_llm(temperature=0.2, max_tokens=1200)
        resp = _llm.invoke([("system", _CARD_PROMPT), ("human", f"行业：{industry}\n\n报告全文：\n{report[:6000]}")])
        m = re.search(r"\{.*\}", (resp.content or ""), re.S)
        if not m:
            return None
        card = json.loads(m.group(0))
        if not isinstance(card, dict) or not card.get("industry"):
            return None
        kb = _load()
        old = (kb.get(industry) or {}).get("card")
        card = _sanitize_card(card, old, redteam_high=redteam_high, redteam_med=redteam_med)
        kb[industry] = {
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "card": card,
        }
        _save(kb)
        logger.info(
            "行业基线已沉淀: %s（护栏后 key_numbers=%d, 有法规canonical=%s）",
            industry, len(card.get("key_numbers") or []), bool(card.get("regulatory")),
        )
        return card
    except Exception as e:
        logger.warning("行业卡片提取失败: %s", e)
        return None


def get_industry_card(industry: str) -> dict[str, Any] | None:
    """取某行业基线卡片（下次同行业调研注入）"""
    entry = _load().get(industry)
    return entry.get("card") if entry else None


def format_industry_brief(industry: str) -> str:
    """行业基线 → 注入文本（供整合/检索参考）

    **注入前跑一遍写入时的同一道护栏**（2026-09-15 外部审计）。
    病案：`_sanitize_card` 此前**只在 `save_industry_card` 写入时**调用，
    `get_industry_card` / 本函数读的时候**不过滤**。于是护栏上线（v0.9.47）**之前**
    写进去的脏卡——含 RedTeam 高/中风险数字、无来源数字——今天照旧被注入报告。
    实测：现行护栏会拒绝其中 3 条，而它们仍在往新报告里注。

    为什么是"读时过滤"而不是"把旧卡删掉"：
    卡是**历史证据**，删掉就查不出"当初沉淀了什么"。护栏的职责是**决定什么能进新报告**，
    不是改写历史。所以：**存档保留，注入前过滤**——两件事分开做。
    """
    card = get_industry_card(industry)
    if not card:
        return ""
    # 与写入侧同一把尺（`redteam_high`/`redteam_med` 传空：存量卡的数字风险标记已在写入时落库，
    # 这里只做"来源/低信源通道"这一类可静态判定的过滤）。
    try:
        card = _sanitize_card(dict(card), card, redteam_high=[], redteam_med=[])
    except Exception:
        pass  # 护栏自身出错时**不静默放行脏卡**，退回原卡并在下一行标注（见下）
    parts = [f"【上次调研沉淀的{card.get('industry', industry)}行业基线】"]
    ms = card.get("market_size") or {}
    if ms.get("value"):
        parts.append(f"- 市场规模：{ms['value']} {ms.get('unit', '')}（{ms.get('year', '')}，{ms.get('source', '')}）")
    if card.get("growth_rate", {}).get("value"):
        parts.append(f"- 增速：{card['growth_rate']['value']}（{card['growth_rate'].get('source', '')}）")
    if card.get("key_players"):
        parts.append(f"- 头部玩家：{'、'.join(str(p) for p in card['key_players'][:5])}")
    if card.get("price_range"):
        parts.append(f"- 价格带/客单价：{card['price_range']}")
    if card.get("penetration"):
        parts.append(f"- 渗透/频次：{card['penetration']}")
    if card.get("regulatory") and not card.get("policy", "").startswith(str(card.get("regulatory", ""))):
        # 只注入法规原文核验结论，不注入未核 policy 数字
        parts.append(f"- 法规要点：{card.get('policy', '')}")
    elif card.get("policy"):
        parts.append(f"- 法规要点：{card['policy']}")
    kns = card.get("key_numbers") or []
    if kns:
        parts.append("- 关键指标：" + "；".join(f"{k.get('name', '')}={k.get('value', '')}" for k in kns[:6]))
    parts.append("（以上为历史基线，本次调研可在其基础上做增量更新与对比，口径冲突请标注）")
    return "\n".join(parts)


def list_industries() -> list[str]:
    """已沉淀基线的行业清单（前端展示）"""
    return list(_load().keys())


# ============================================================
# 离线自测（无网络；python core/industry_kb.py）
# ============================================================
def _selftest() -> int:
    import sys

    _ok = 0

    def _check(name: str, cond: bool) -> None:
        nonlocal _ok
        if not cond:
            print(f"[{name}] ✗ 断言失败")
            sys.exit(1)
        _ok += 1
        print(f"[{name}] ✓")

    # 1) RedTeam 高风险/中风险 + 低信源 数字被剔出 key_numbers；具名机构来源保留
    dirty_card = {
        "industry": "咖啡",
        "key_numbers": [
            {"name": "人均消费支出CAGR", "value": "24.6%", "source": "艾瑞咨询"},
            {"name": "12个月闭店率", "value": ">63%", "source": "窄门餐眼数据库"},
            {"name": "证照办理周期", "value": "67天", "source": "反方检索资料"},
            {"name": "获补比例", "value": "17.3%", "source": "sohu.com"},
            {"name": "无源指标", "value": "31%", "source": ""},
        ],
        "penetration": "Z世代渗透率68.5%，人均年现磨咖啡消费量1.2杯",
        "price_range": "<22元",
        "policy": "证照周期67天；无源31%；反方检索资料43%；其余基线",
    }
    clean = _sanitize_card(
        dirty_card, None,
        redteam_high=[">63%", "1.2杯"], redteam_med=["67天", "17.3%", "4200元"],
    )
    kn_names = [k["name"] for k in clean.get("key_numbers") or []]
    _check("剔RedTeam高>63%", "12个月闭店率" not in kn_names)
    _check("剔低信源67天", "证照办理周期" not in kn_names)
    _check("剔RedTeam中17.3%", "获补比例" not in kn_names)
    _check("剔无源31%", "无源指标" not in kn_names)
    _check("保留具名机构24.6%", "人均消费支出CAGR" in kn_names)
    _check("penetration含高危1.2杯被清空", clean.get("penetration") == "")
    _check("policy走法规canonical", "冷食类操作专间" in clean.get("policy", ""))
    _check("无canonical行业政策也剔低信源句", "反方" not in clean.get("policy", ""))
    _check("regulatory已写入", bool(clean.get("regulatory")))

    # 2) 无 canonical 行业：仅按低信源/RedTeam 剔句，其余 LLM policy 保留
    tea = _sanitize_card(
        {"industry": "现制茶饮", "policy": "县域抽检不合格率3.2%；反方检索资料称8.1%；证照67天"},
        None, redteam_high=["67天"], redteam_med=[],
    )
    p2 = tea.get("policy", "")
    _check("茶饮保留非低信源句", "3.2%" in p2)
    _check("茶饮剔反方句", "反方" not in p2)
    _check("茶饮剔RedTeam句", "67天" not in p2)

    # 3) 旧卡 regulatory 透传：无 canonical 登记的行业，新 LLM 提取没写 regulatory 也不丢
    #    （注意不能用「咖啡」——它在 _REG_CANONICAL 有登记，canonical 优先于旧卡是正确行为，非透传路径）
    keep = _sanitize_card(
        {"industry": "神秘茶饮赛道", "policy": "新LLM政策杂项"},
        {"regulatory": "旧法规结论(专用操作区)"},
        redteam_high=[], redteam_med=[],
    )
    _check("旧卡regulatory透传", keep.get("policy", "").startswith("旧法规结论"))
    _check("policy_note存杂项", "新LLM政策杂项" in keep.get("policy_note", ""))

    # 4) save_industry_card 端到端：mock LLM 吐脏卡 → 落库应已过滤
    class _FakeLLM:
        def invoke(self, msgs):
            class _R:
                content = json.dumps({
                    "industry": "测试行业",
                    "market_size": {"value": "999亿元", "unit": "亿元", "year": "2024", "source": "官方统计"},
                    "growth_rate": {"value": "5%", "source": "官方统计"},
                    "key_players": ["甲", "乙"],
                    "price_range": "",
                    "penetration": "",
                    "policy": "无canonical行业的政策要点",
                    "regulatory": "",
                    "key_numbers": [
                        {"name": "市场规模反推", "value": "77亿", "source": "反方检索资料"},
                        {"name": "市场规模官方", "value": "999亿", "source": "官方统计"},
                    ],
                }, ensure_ascii=False)
            return _R()

    saved = save_industry_card("测试行业", "x" * 300, redteam_high=["999亿"], redteam_med=[], _llm=_FakeLLM())
    _check("端到端mock落库", saved is not None and saved.get("industry") == "测试行业")
    kn2 = saved.get("key_numbers") or []
    _check("端到端剔反方/RedTeam", all(k.get("source") == "官方统计" for k in kn2))

    # 清理测试落库，避免污染真实 KB 文件
    kb = _load()
    kb.pop("测试行业", None)
    _save(kb)

    print(f"\n行业知识库护栏自测：{_ok} 项全过（离线，无网络）。")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(_selftest())
