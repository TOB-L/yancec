"""研策 · Skill 体系（专业咨询方法论包）

对应 v3 方案 §3.4 + 企业级对标：
- Skill = 可插拔方法论包：换行业/换场景 = 换/增 Skill，同一套状态机复用
- 每个维度绑定专业分析框架（PEST / 波特五力 / STP / TAM-SAM-SOM / 4P），
  让报告达到咨询公司（麦肯锡/尼尔森）的专业结构，而非"通用模板"

目录结构：
backend/skills/
├── market-entry/SKILL.md    # 行业进入研究（默认）
├── pet-economy/SKILL.md     # 宠物经济（演示换行业）
└── (可继续扩展)

每个 Skill 定义：
- 维度模板（子任务骨架 + 对应分析框架 + 检索词）
- 信源分级标准
- 决策SOP输出模板
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Skill 根目录
SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"

# 行业关键词 → Skill 名（换行业演示的关键）
_SKILL_ROUTING: dict[str, str] = {
    # 跨境电商（垂直 Skill）优先：含"跨境/亚马逊/出海/TikTok/Shopee/外贸"等 → cross-border
    "跨境": "cross-border",
    "出海": "cross-border",
    "亚马逊": "cross-border",
    "Amazon": "cross-border",
    "tiktok": "cross-border",
    "TikTok": "cross-border",
    "Shopee": "cross-border",
    "shopee": "cross-border",
    "外贸": "cross-border",
    "进口": "cross-border",
    "独立站": "cross-border",
    # 通用行业进入研究
    "咖啡": "market-entry",
    "奶茶": "market-entry",
    "零食": "market-entry",
    "美妆": "market-entry",
    "服饰": "market-entry",
    "母婴": "market-entry",
    "数码": "market-entry",
    "家电": "market-entry",
    "餐饮": "market-entry",
    "宠物": "pet-economy",
    "教育": "market-entry",
    "健身": "market-entry",
    "预制菜": "market-entry",
}

# 默认 Skill（兜底）
_DEFAULT = "market-entry"

# ============================================================
# 专业分析框架（企业级方法论）
# ============================================================
# 每个框架：维度名 → 方法论 + 检索要求（写进子任务描述，让子Agent按框架调研）
FRAMEWORKS: dict[str, dict[str, str]] = {
    "TAM_SAM_SOM": {
        "label": "市场规模测算（TAM/SAM/SOM）",
        "method": "TAM（潜在总市场）→ SAM（可服务市场）→ SOM（可获得市场）三层测算，用'人数×客单价×消费频次'自下而上推算，并用自上而下（机构数据）交叉验证",
        "query_hint": "市场规模 总市场 可服务市场 可触达市场 客单价 消费频次 消费人数 测算",
    },
    "PEST": {
        "label": "宏观环境分析（PEST）",
        "method": "从政治(Policy)/经济(Economy)/社会(Society)/技术(Technology)四维度扫描行业宏观环境与政策影响",
        "query_hint": "政策 法规 补贴 经济增速 人均收入 人口结构 消费趋势 技术变革",
    },
    "PORTER_FIVE": {
        "label": "竞争格局分析（波特五力）",
        "method": "用波特五力分析：现有竞争强度、潜在进入者威胁、替代品威胁、供应商议价力、买方议价力",
        "query_hint": "主要玩家 市场份额 头部品牌 新进入者 替代品 供应链 渠道议价",
    },
    "STP": {
        "label": "目标市场定位（STP）",
        "method": "市场细分(Segmentation)→选择目标市场(Targeting)→定位(Positioning)，输出核心客群画像与差异化定位",
        "query_hint": "消费者画像 细分人群 年龄 收入 消费习惯 需求痛点 购买决策",
    },
    "COMPETITIVE_EDGE": {
        "label": "竞争优势与壁垒分析",
        "method": "识别进入壁垒（资金/技术/牌照/渠道/品牌）与可持续竞争优势，评估新进入者存活概率",
        "query_hint": "进入壁垒 门槛 资质 牌照 资金门槛 技术门槛 供应链 存活率",
    },
    "UNIT_ECONOMICS": {
        "label": "单位经济模型测算",
        "method": "测算单店/单产品的成本结构、客单价、毛利、盈亏平衡点（BEP），评估商业可行性",
        "query_hint": "开店成本 租金 人力 原材料 客单价 毛利率 盈亏平衡 回本周期",
    },
    "TREND_RISK": {
        "label": "趋势与风险扫描",
        "method": "扫描行业增长趋势、颠覆性变化、主要风险（政策/竞争/需求/成本），并给对冲建议",
        "query_hint": "行业趋势 增长 风险 挑战 政策变动 替代 衰退",
    },
}

# ============================================================
# 各 Skill 的维度模板（子任务骨架 + 分析框架 + 检索词）
# ============================================================
_SKILL_TEMPLATES: dict[str, dict[str, list[dict[str, Any]]]] = {
    "market-entry": {
        "进入市场": [
            {"dimension": "宏观环境", "agent": "industry", "framework": "PEST",
             "queries": ["政策 经济 社会 技术 影响", "行业政策", "消费趋势"]},
            {"dimension": "市场规模测算", "agent": "industry", "framework": "TAM_SAM_SOM",
             "queries": ["市场规模", "客单价", "消费人数", "增速"]},
            {"dimension": "竞争格局", "agent": "competition", "framework": "PORTER_FIVE",
             "queries": ["主要玩家", "市场份额", "头部品牌", "价格带"]},
            {"dimension": "目标市场", "agent": "user", "framework": "STP",
             "queries": ["消费者画像", "细分人群", "需求痛点", "消费习惯"]},
            {"dimension": "进入壁垒", "agent": "industry", "framework": "COMPETITIVE_EDGE",
             "queries": ["门槛", "资质", "资金门槛", "供应链"]},
            {"dimension": "单位经济", "agent": "statistics", "framework": "UNIT_ECONOMICS",
             "queries": ["开店成本", "客单价", "毛利率", "盈亏平衡"]},
            {"dimension": "趋势风险", "agent": "industry", "framework": "TREND_RISK",
             "queries": ["行业趋势", "风险", "挑战", "政策变动"]},
        ],
        "经营诊断": [
            {"dimension": "经营现状", "agent": "industry", "framework": "UNIT_ECONOMICS",
             "queries": ["行业平均", "毛利", "成本结构", "对标"]},
            {"dimension": "竞争格局", "agent": "competition", "framework": "PORTER_FIVE",
             "queries": ["竞品", "差异化", "对标"]},
            {"dimension": "用户反馈", "agent": "user", "framework": "STP",
             "queries": ["评价", "投诉", "复购", "口碑"]},
        ],
        "竞品/趋势跟踪": [
            {"dimension": "竞品动态", "agent": "competition", "framework": "PORTER_FIVE",
             "queries": ["竞品", "新品", "动作", "份额"]},
            {"dimension": "宏观趋势", "agent": "industry", "framework": "PEST",
             "queries": ["趋势", "政策", "报告", "技术"]},
            {"dimension": "用户情绪", "agent": "user", "framework": "STP",
             "queries": ["热点", "情绪", "讨论", "口碑"]},
        ],
    },
    "pet-economy": {
        "进入市场": [
            {"dimension": "宏观环境", "agent": "industry", "framework": "PEST",
             "queries": ["宠物行业政策", "经济环境", "社会养宠趋势"]},
            {"dimension": "市场规模测算", "agent": "industry", "framework": "TAM_SAM_SOM",
             "queries": ["宠物市场规模", "养宠人数", "客单价", "增速"]},
            {"dimension": "细分赛道", "agent": "industry", "framework": "TAM_SAM_SOM",
             "queries": ["宠物食品", "宠物医疗", "宠物用品", "细分增长"]},
            {"dimension": "竞争格局", "agent": "competition", "framework": "PORTER_FIVE",
             "queries": ["宠物品牌", "市场份额", "价格带", "头部玩家"]},
            {"dimension": "目标市场", "agent": "user", "framework": "STP",
             "queries": ["养宠人群", "消费习惯", "复购", "需求痛点"]},
            {"dimension": "单位经济", "agent": "statistics", "framework": "UNIT_ECONOMICS",
             "queries": ["宠物店成本", "客单价", "毛利率", "盈利"]},
            {"dimension": "趋势风险", "agent": "industry", "framework": "TREND_RISK",
             "queries": ["宠物趋势", "风险", "政策", "挑战"]},
        ],
        "经营诊断": [
            {"dimension": "经营现状", "agent": "industry", "framework": "UNIT_ECONOMICS",
             "queries": ["宠物行业平均", "毛利", "成本"]},
            {"dimension": "竞争格局", "agent": "competition", "framework": "PORTER_FIVE",
             "queries": ["宠物竞品", "差异化"]},
            {"dimension": "用户反馈", "agent": "user", "framework": "STP",
             "queries": ["宠物服务评价", "投诉"]},
        ],
        "竞品/趋势跟踪": [
            {"dimension": "竞品动态", "agent": "competition", "framework": "PORTER_FIVE",
             "queries": ["宠物品牌", "新品", "动作"]},
            {"dimension": "宏观趋势", "agent": "industry", "framework": "PEST",
             "queries": ["宠物趋势", "政策", "报告"]},
            {"dimension": "用户情绪", "agent": "user", "framework": "STP",
             "queries": ["宠物热点", "情绪"]},
        ],
    },
    "cross-border": {
        "进入市场": [
            {"dimension": "跨境宏观与政策", "agent": "industry", "framework": "PEST",
             "queries": ["跨境电商政策 关税 合规", "目标市场政策", "贸易环境 汇率"]},
            {"dimension": "目标市场与容量", "agent": "industry", "framework": "TAM_SAM_SOM",
             "queries": ["目标国家电商市场规模", "品类渗透率", "客单价", "增速"]},
            {"dimension": "平台与渠道", "agent": "competition", "framework": "PORTER_FIVE",
             "queries": ["亚马逊 TikTok Shop 独立站", "平台费用 佣金", "渠道规则", "流量获取"]},
            {"dimension": "品类机会", "agent": "industry", "framework": "TAM_SAM_SOM",
             "queries": ["跨境热销品类", "品类竞争", "价格带", "蓝海机会"]},
            {"dimension": "物流与成本", "agent": "industry", "framework": "UNIT_ECONOMICS",
             "queries": ["跨境物流 头程 尾程", "关税 仓储", "FBA费用", "物流成本占比"]},
            {"dimension": "合规与风险", "agent": "industry", "framework": "TREND_RISK",
             "queries": ["平台封号 合规", "产品认证", "知识产权", "税务风险"]},
            {"dimension": "跨境单位经济", "agent": "statistics", "framework": "UNIT_ECONOMICS",
             "queries": ["跨境利润测算", "售价 成本 物流 佣金 广告", "净利率", "盈亏平衡"]},
            {"dimension": "趋势风险", "agent": "industry", "framework": "TREND_RISK",
             "queries": ["跨境电商趋势", "政策变动", "竞争风险", "新兴市场"]},
        ],
        "经营诊断": [
            {"dimension": "店铺经营现状", "agent": "industry", "framework": "UNIT_ECONOMICS",
             "queries": ["跨境店铺平均", "毛利", "成本结构", "客单价"]},
            {"dimension": "竞品对比", "agent": "competition", "framework": "PORTER_FIVE",
             "queries": ["跨境竞品", "listing优化", "差异化"]},
            {"dimension": "用户反馈", "agent": "user", "framework": "STP",
             "queries": ["买家评价", "退货率", "差评", "复购"]},
        ],
        "竞品/趋势跟踪": [
            {"dimension": "平台动态", "agent": "competition", "framework": "PORTER_FIVE",
             "queries": ["亚马逊政策", "TikTok Shop", "平台规则变化"]},
            {"dimension": "品类趋势", "agent": "industry", "framework": "PEST",
             "queries": ["跨境趋势", "爆款", "新兴品类"]},
            {"dimension": "市场情绪", "agent": "user", "framework": "STP",
             "queries": ["买家情绪", "趋势", "讨论"]},
        ],
    },
}


def detect_skill(raw_question: str, objective: str = "") -> str:
    """根据提问内容挑选 Skill（换行业演示的核心）"""
    for keyword, skill in _SKILL_ROUTING.items():
        if keyword in raw_question:
            return skill
    return _DEFAULT


def get_skill_templates(skill: str = "") -> dict[str, list[dict[str, Any]]]:
    """取某 Skill 的维度模板（找不到回落默认）"""
    return _SKILL_TEMPLATES.get(skill) or _SKILL_TEMPLATES[_DEFAULT]


def framework_info(framework: str) -> dict[str, str]:
    """取分析框架的说明（方法论 + 检索提示）"""
    return FRAMEWORKS.get(framework, {"label": framework, "method": "", "query_hint": ""})


def skill_meta(skill: str = "") -> dict[str, Any]:
    """Skill 元信息（展示/材料用）"""
    if not skill:
        skill = _DEFAULT
    templates = get_skill_templates(skill)
    dims = templates.get("进入市场", [])
    framework_names = list({FRAMEWORKS.get(d.get("framework", ""), {}).get("label", "") for d in dims if d.get("framework")})
    return {
        "name": skill,
        "title": (
            "行业进入研究" if skill == "market-entry"
            else "宠物经济研究" if skill == "pet-economy"
            else "跨境电商研究"
        ),
        "dimensions": len(dims),
        "frameworks": framework_names,
        "description": (
            "专业方法论包：PEST/波特五力/STP/TAM-SAM-SOM/单位经济 等咨询级分析框架 "
            "+ L1-L5信源分级 + 决策SOP。换行业=换Skill，状态机复用。"
        ),
    }
