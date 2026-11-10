"""Router 主 Agent —— 任务理解 + 需求澄清 + 研究计划生成

赛题能力映射：
- 任务理解：把模糊提问拆成结构化研究计划
- 自主规划：拆子任务、排依赖、估预算

双模式：
- Real 模式：调用大模型做意图解析 + 计划生成（有 API Key 时）
- Fallback 模式：规则兜底（无 Key 或离线演示时），保证开发可测、演示不翻车

对应 W1-D2 验收标准：模糊问题 → 计划 JSON
"""

from __future__ import annotations
import logging

import json
import re
from typing import Any

from core.models import (
    DepthLevel,
    ResearchPlan,
    SubTask,
)
from core.llm import get_main_llm, has_main_llm
from core.skills import detect_skill, get_skill_templates, skill_meta

# ============================================================
# 需求澄清（顾问式体验：先问清楚再干活）
# ============================================================
# 澄清问题模板：根据提问内容判断是否需要追问
# 注：预算档已移到"设置"面板选择，不在澄清里问（用户指定）
_CLARIFICATION_POOL = [
    {
        "key": "objective",
        "question": "您这次调研的目标是？",
        "options": ["进入市场", "经营诊断", "竞品/趋势跟踪"],
    },
    {
        "key": "scope",
        "question": "关注的地域范围？",
        "options": ["全国", "特定城市", "仅线上"],
    },
]


def needs_clarification(raw_question: str) -> bool:
    """判断是否值得先澄清：问题模糊或关键信息缺失时返回 True"""
    has_region = re.search(r"(杭州|上海|北京|广州|深圳|全国|城市|本地|二三线|一线)", raw_question)
    has_action = re.search(r"(能不能做|机会|进入|开家|开一|开个|开咖啡|做不|入局|值得|想买|卖|做|调研|看看|了解)", raw_question)
    # 跨境/出海/进口等歧义词 → 对象定义不清，必须澄清（消灭 P3 任务漂移）
    has_cross_border = bool(
        re.search(r"(跨境|出海|进口|出口|海外|外贸|亚马逊|TikTok|Shopee|独立站)", raw_question, re.I)
    )
    return has_cross_border or not (has_region and has_action)


def _pick_clarification_questions(raw_question: str) -> list[dict[str, str]]:
    """挑选最相关的澄清问题（最多3个，避免打扰）"""
    picked = []
    if not re.search(r"(能不能做|机会|进入|入局|值得|做不)", raw_question):
        picked.append(_CLARIFICATION_POOL[0])
    if not re.search(r"(全国|杭州|上海|北京|广州|深圳|城市|本地)", raw_question):
        picked.append(_CLARIFICATION_POOL[1])
    # 跨境/出海/进口：对象定义有歧义（国内品牌出海？进口引入？跨境平台品类？）
    if re.search(r"(跨境|出海|进口|出口|海外|外贸|亚马逊|TikTok|Shopee|独立站)", raw_question, re.I):
        picked.append(
            {
                "key": "cross_border_scope",
                "question": "研究的'跨境'具体指哪种？",
                "options": ["国内品牌出海销售", "进口商品引入国内", "跨境平台上的该品类（如亚马逊/TikTok）"],
            }
        )
    return picked[:3]


# ============================================================
# 维度与子任务模板（Skill 方法论：market-entry 的骨架）
# ============================================================
DIMENSION_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "进入市场": [
        {"dimension": "市场全景", "agent": "industry", "queries": ["市场规模", "增速", "趋势", "行业报告"]},
        {"dimension": "竞争格局", "agent": "competition", "queries": ["主要玩家", "市场份额", "价格带", "门店数量"]},
        {"dimension": "消费人群", "agent": "user", "queries": ["消费者画像", "需求", "口碑", "评价"]},
        {"dimension": "进入壁垒", "agent": "industry", "queries": ["门槛", "资质", "成本", "供应链"]},
        {"dimension": "进入策略", "agent": "statistics", "queries": ["开店成本", "测算", "存活率"]},
    ],
    "经营诊断": [
        {"dimension": "经营现状", "agent": "industry", "queries": ["行业平均", "毛利", "成本结构"]},
        {"dimension": "竞品对比", "agent": "competition", "queries": ["竞品", "差异化", "对标"]},
        {"dimension": "用户反馈", "agent": "user", "queries": ["评价", "投诉", "复购"]},
    ],
    "竞品/趋势跟踪": [
        {"dimension": "竞品动态", "agent": "competition", "queries": ["竞品", "新品", "动作"]},
        {"dimension": "行业趋势", "agent": "industry", "queries": ["趋势", "报告", "政策"]},
        {"dimension": "用户情绪", "agent": "user", "queries": ["热点", "情绪", "讨论"]},
    ],
}

# 深度档 → 价格（商业卡片定价逻辑）
# 档位 → 预估消耗。**单位是「档位积分」，不是人民币。**
# 病案（2026-09-15 外部审计）：这里原本是**自己抄的一份价目** 0/39/99/299 元
# （早期人民币定价），而 `core/billing.py` 的 `MODE_POINTS` 实扣 **10/50/150/400 积分**，
# 口径表 K17 写的也是这一组。于是同一次运行里 `budget_estimate=39`（被当钱）、
# 实际扣费 50 积分、台账 `budget_spent` 又是第三个数——**三套刻度两种单位**，
# 交叉核对当场对不上。
#
# 修法不是"把数字改对"（那还会再漂一次），而是**消灭第二张表**：
# 价目唯一来源就是计费引擎本身，`billing` 改一次这里自动跟着改。
from core.billing import MODE_ALIAS as _MODE_ALIAS
from core.billing import MODE_POINTS as _MODE_POINTS

_DEPTH_PRICE: dict[DepthLevel, float] = {
    DepthLevel(_k): float(_MODE_POINTS[v]) for _k, v in _MODE_ALIAS.items()
}


# ============================================================
# 研究计划生成（LLM 提示词 + Fallback 兜底）
# ============================================================
# ⚠️ **本串里有字面花括号（JSON 示例），所以绝不许过 `.format()`。**
# 填充占位符一律走下面的 `_render_plan_system_prompt()`（用 `replace`）。
#
# 病案链（2026-09-18 审计 + 当晚实跑；2026-09-19 收官修复，一条链上三个缺陷）：
#
#   ① `_PLAN_SYSTEM_PROMPT.format(...)` 撞上示例里的 `{\n  "dimensions"`，
#      把它当占位符名 → **每次都抛 `KeyError`** → LLM 规划分支从未成功过；
#   ② 转义花括号后，流程往下走一步就死在本模块 `_build_plan_prompt` 里一行
#      **谁都没用过的死代码**上（`"、".join(字典列表)` → TypeError）；
#   ③ 再修掉 ②，模型终于产出计划了——**但它引入的是本产品最严重的历史缺陷**：
#      模型写的子任务描述里基础轮**不含 `『维度名』`**，而 `graph._chapter_key`
#      靠 `『』` 取章节键 → 基础轮拿到一个自由句当键、深入轮拿到维度名当键 →
#      **同一个维度出两章**（一章标题是"检索三四线城市咖啡消费相关的政策支持…"，
#      一章是"宏观环境分析"），也就是"同一指标两套数"。
#
# —— **2026-09-19 收官：三个缺陷一次改完，路径已打开并且已由一次真实运行验证。**
# 三处对应改动（缺一不可，只改 ① 就是把"死于 KeyError"换成"死于两章"）：
#   · ① → 不再用 `.format()`：新增 `_render_plan_system_prompt()`，`replace` 只替换
#     显式命名的占位符，示例里的花括号原样保留。**格式串从此不可被误用。**
#   · ② → `_build_plan_prompt` 里那行没人用的死代码已删（行内注释留了病案）。
#   · ③ → **维度由规划层显式声明，不再从描述文本里猜**：`SubTask.dimension` 是
#     结构化字段，`graph._chapter_key` 优先取它；LLM 返回的每条子任务**必须**带
#     `dimension` 且必须是 `dimensions` 数组里的值，**违反即判整份计划不合格 →
#     回落规则兜底**（fail-closed：宁可退到今天这条已知可用的路，也不冒"两章"的险）。
#     解析器还会按维度归并并**确定性指定唯一出章者**（深入/定制档取第 2 轮，
#     其余取第 1 轮）——出章者由规则定，不由模型的描述措辞定。
#
# **验收标准是可执行的、而且随包提交**：`_fulltest.py` 第 94 项是这把锁——
# 它用**行为探针**判断这条路是开是关（渲染一次提示词，看 JSON 示例有没有被吃掉、
# 每个子任务有没有拿到维度键），而不是去看有没有人"转义了花括号"。按它改，别按记忆改。
#
# 记住这条教训：**失败路径的修复必须验到"成功分支真的走通"，不能只验"错误信息变了"。**
# 只换一种死法的缺陷，比原来那个更难发现——因为日志里那行 warning 还在，
# 看起来"问题已经被看见了"。
_PLAN_SYSTEM_PROMPT = """你是"研策"的市场调研主管。你的职责是把用户的一句话调研需求，转化为结构化的研究计划。

输入：用户的模糊提问 + 需求澄清结果（含深度档 depth）。
输出：JSON，格式如下——
{
  "dimensions": ["维度1", "维度2", ...],
  "subtasks": [
    {"agent_type": "industry|competition|user|statistics", "dimension": "必须是 dimensions 数组里的某一项，逐字一致", "description": "子任务描述", "search_queries": ["检索词1", "检索词2"]}
  ],
  "depth": "free|standard|deep|custom",
  "price_estimate": 0.0
}

深度档决定详细度（必须遵守）：
- free（免费）：2-3 个维度，每个维度 1 个子任务，只做市场概览
- standard（标准）：4-5 个维度，每个维度 1 个子任务，覆盖市场/竞争/用户/趋势
- deep（深入）：6-8 个维度，每个维度 2 个子任务，含细分市场/价格带/渠道等
- custom（定制）：8-10 个维度，每个维度 2-3 个子任务，含决策建议/风险/成本测算

已匹配的 Skill：{skill_name}
Skill 维度骨架（必须按此方法论拆，可在其基础上扩展，不要偏离主题）：{skill_dims}

专业维度规范（维度名请用方法论术语，而不是笼统的"市场全景"）：
- 宏观环境分析（PEST：政策/经济/社会/技术）
- 市场规模测算（TAM/SAM/SOM：总市场→可服务→可获取，用人数×客单价交叉验证）
- 竞争格局分析（波特五力：现有竞争/新进入者/替代品/供应商/买方议价）
- 目标市场定位（STP：细分→目标→定位，输出核心客群画像）
- 进入壁垒与竞争优势（资金/技术/牌照/渠道/品牌）
- 单位经济模型（客单价/毛利率/盈亏平衡点）
- 趋势与风险扫描

规则：
1. dimensions 和 subtasks 数量按深度档扩展，档位越高越详细
2. subtasks 每个维度至少拆一个子任务，agent_type 匹配维度类型
3. price_estimate 是**本档位的档位积分消耗**（免费10 / 标准50 / 深入150 / 定制400），
   与计费引擎 `core/billing.MODE_POINTS` 同一口径——**不是人民币金额**，不要据此写钱数
4. 只输出 JSON，不要其他文字
5. **每条 subtask 必须带 `dimension` 字段，值必须与 `dimensions` 数组里的写法逐字一致。**
   系统靠这个字段把子任务归到章节上；**缺失或对不上，整份计划会被判不合格并退回模板**。
   不要靠描述文字表达归属——描述怎么写都行，归属只认 `dimension` 字段。
6. 【同维度只出一章·硬约束】报告里**一个维度只能有一个章节**：
   - standard/free 档：每个维度**只能有 1 个子任务**，禁止为同一维度再拆"深入/补充/细化"子任务
     （拆了就会变成同一维度两章、同一指标两套数——这是本产品最严重的历史缺陷）。
   - deep/custom 档：同一维度的第 2 个子任务描述**必须**以"深入『同一维度名』"开头，
     它是该维度的**唯一写作者**（第 1 个只做检索、不出章、其资料会并入它），
     因此它的描述里要写"产出该维度完整最终版"，而不是"只追加增量"。
   - 两条 subtask 的 `dimension` 必须**完全相同**，否则会被判成两个不同维度。
"""


def _render_plan_system_prompt(skill_name: str, skill_dims: str) -> str:
    """把 Skill 名与维度骨架填进规划系统提示词。

    ⚠️ **这里绝不能用 `str.format()`**，也绝不许有人把它改回去：
    提示词里那段 JSON 示例的花括号是**字面**的，`.format()` 会把
    `{\\n  "dimensions"` 当成占位符名抛 `KeyError` —— 历史上正是这一行让
    「模型自主拆解」整整一个版本周期一次都没跑成，而且**日志里只有一行 warning**，
    计划照常生成、报告照常交付，没有任何地方看得出来。

    改用 `replace`：只替换两个显式命名的占位符，其余花括号一律原样保留。
    行为由 `_fulltest.py` 第 94 项锁住（渲染一次，断言 JSON 示例逐字还在）。
    """
    return (
        _PLAN_SYSTEM_PROMPT.replace("{skill_name}", str(skill_name))
        .replace("{skill_dims}", str(skill_dims))
    )


def _resolve_subtask_dimensions(subtasks: list[dict], dimensions: list[str]) -> list[str]:
    """把 LLM 返回的每条子任务**确定性地**归到一个维度上（收口"同维度出两章"）。

    返回与 `subtasks` 等长的维度列表；**只要有一条归不上，就抛 `ValueError`**，
    由调用方整体回落到规则兜底计划。

    为什么要"归不上就整份退回"而不是"归不上就当它自成一章"：
    后者正是历史缺陷本身——基础轮描述是一句自由文本，它自成一章，
    深入轮又按维度名出一章，**同一个维度两章、同一指标两套数**。
    宁可退回到已知可用的模板计划（今天的现状），也不冒这个险。

    三级判定，逐级收紧：
      ① 子任务自带的 `dimension` 字段，且**逐字**等于 dimensions 里某一项 → 采用；
      ② 字段缺失/对不上：从描述里取 `『X』`/`「X」`，X 对得上 → 采用（容忍模型漏填）；
      ③ 仍不成：描述里出现某个维度名（去空白后包含）→ 采用；
      ④ 都不成 → 抛错。
    """
    _dim_set = [str(d).strip() for d in (dimensions or []) if str(d).strip()]
    resolved: list[str] = []
    for item in subtasks:
        raw = str(item.get("dimension") or "").strip()
        hit = raw if raw in _dim_set else ""
        if not hit:
            m = re.search(r"[『「]([^』」]+)[』」]", str(item.get("description") or ""))
            if m and m.group(1).strip() in _dim_set:
                hit = m.group(1).strip()
        if not hit:
            desc = re.sub(r"\s+", "", str(item.get("description") or ""))
            for d in _dim_set:
                if d and re.sub(r"\s+", "", d) in desc:
                    hit = d
                    break
        if not hit:
            raise ValueError(
                f"子任务未声明可识别的维度（dimension={raw!r}），"
                f"declared={_dim_set!r}——违反规划提示词规则 5，整份计划退回模板兜底"
            )
        resolved.append(hit)
    return resolved


def _normalize_subtasks(
    subtasks: list[SubTask], dims: list[str], depth: DepthLevel
) -> list[SubTask]:
    """按维度归并，并**确定性指定唯一出章者**——不依赖模型怎么写描述。

    与 `graph._final_subtask_ids` 的规则同源：出章者的 `id` 以 `_deep` 结尾
    （深入/定制档的第 2 轮）或描述含 `『维度名』`（其余档的唯一一轮）。
    这里直接把 id 摆正，于是**出章者由规则决定，不由模型的措辞决定**：
      · 深入/定制档：同维度多条 → 末条为出章者，id 记成 `st_<k>_deep`；
      · 其余档：同维度多条 → 首条为出章者，其余 id 记成 `st_<k>_aux`
        （不带 `_deep` 后缀 → `_final_subtask_ids` 会挑首条，正是我们要的）。
    """
    order: list[str] = []
    groups: dict[str, list[SubTask]] = {}
    for st, d in zip(subtasks, dims):
        if d not in groups:
            groups[d] = []
            order.append(d)
        groups[d].append(st)

    _deep_ok = depth in (DepthLevel.DEEP, DepthLevel.CUSTOM)
    out: list[SubTask] = []
    for k, d in enumerate(order):
        items = groups[d]
        if len(items) == 1:
            items[0].id = f"st_{k}"
            out.append(items[0])
            continue
        writer = items[-1] if _deep_ok else items[0]
        for i, st in enumerate(items):
            if st is writer:
                st.id = f"st_{k}_deep" if _deep_ok else f"st_{k}"
            else:
                st.id = f"st_{k}_aux{i}"
            out.append(st)
    return out


def _build_plan_prompt(raw_question: str, context: dict[str, str]) -> str:
    context = context or {}
    skill = detect_skill(raw_question, context.get("objective", ""))
    templates = get_skill_templates(skill)
    # ⚠️ 这里原本还有一行 `dims = "、".join(next(iter(templates.values()), []))`——
    # `templates` 的值是**字典列表**（{dimension, agent, framework, queries}），
    # 拿它直接 join 会抛 `TypeError: sequence item 0: expected str instance, dict found`，
    # 而 `dims` **一次都没被用过**（下一行就重算成正确的 `skill_names` 了）。
    #
    # 2026-09-18 实跑发现：这一行被它上面那句 `_PLAN_SYSTEM_PROMPT.format(...)` 的
    # KeyError **挡了整整一个版本周期**——花括号一转义，流程走到本函数就死在这里，
    # 于是「模型自主拆解」从"死于 KeyError"变成"死于 TypeError"，**依然一次没跑成**。
    # 如果只看日志里那行 warning 就说"修好了"，会把一个**换了死法**的缺陷当成已修复。
    # 教训：**失败路径的修复必须验到"成功分支真的走通"，不能只验"错误信息变了"。**
    skill_names = [d["dimension"] for d in next(iter(templates.values()), [])]
    return (
        f"用户提问：{raw_question}\n"
        f"需求澄清：{json.dumps(context, ensure_ascii=False)}\n"
        f"已匹配Skill：{skill_meta(skill)['title']}\n"
        f"Skill维度骨架：{'；'.join(skill_names)}\n"
        "请生成研究计划JSON。"
    )


def _fallback_plan(raw_question: str, context: dict[str, str]) -> ResearchPlan:
    """规则兜底：无 LLM 时也能生成计划（离线演示/开发测试）

    基于提问中的关键词匹配维度和子任务模板，保证演示不翻车。
    Skill 体系：按提问行业自动选方法论包（换行业=换 Skill）。
    """
    context = context or {}
    objective = context.get("objective", "进入市场")
    # 预算档优先读 context["depth"]（设置面板选择），兼容旧的 "budget" 字段
    depth_str = context.get("depth") or context.get("budget") or "免费"
    depth = {
        "免费": DepthLevel.FREE,
        "标准": DepthLevel.STANDARD,
        "深入": DepthLevel.DEEP,
        "定制": DepthLevel.CUSTOM,
    }.get(depth_str, DepthLevel.STANDARD)

    # Skill 自动路由：宠物 → pet-economy，其余 → market-entry
    skill = detect_skill(raw_question, objective)
    template = get_skill_templates(skill).get(objective, get_skill_templates(skill)["进入市场"])
    # 深度档决定子任务数量：免费=前2个维度，标准=前5个，深入=全量+细分，定制=全量+决策
    depth_limit = {
        DepthLevel.FREE: 2,
        DepthLevel.STANDARD: 5,
        DepthLevel.DEEP: len(template),
        DepthLevel.CUSTOM: len(template),
    }
    limit = depth_limit[depth]
    dimensions = []
    subtasks = []
    from core.skills import framework_info

    for i, item in enumerate(template[:limit]):
        dimensions.append(item["dimension"])
        fw = framework_info(item.get("framework", ""))
        # 子任务描述带上分析框架的方法论，让子Agent按咨询标准调研
        fw_hint = f"（方法论：{fw['label']}，{fw['method']}）" if fw.get("method") else ""
        subtasks.append(
            SubTask(
                id=f"st_{i}",
                agent_type=item["agent"],
                description=f"调研『{item['dimension']}』{fw_hint}：{raw_question}",
                search_queries=item["queries"],
                budget_estimate=0,  # 实际执行时由预算控制节点填充
                dimension=item["dimension"],
            )
        )
    # 深入/定制：给每个维度加一个"深入补充"子任务
    # 关键：描述里原始问题必须紧跟维度名（检索词的行业锚），写作指令放【】里且不进检索词
    # 同维度只出一章（方案 A）：这一轮是该维度的**唯一写作者**，前序轮次只做检索、资料并入本轮，
    # 所以它要产出**完整章**而不是"补充片段"——否则基础轮不出章、本章又只写增量，章节会残缺。
    if depth in (DepthLevel.DEEP, DepthLevel.CUSTOM):
        for i, item in enumerate(template[:limit]):
            subtasks.append(
                SubTask(
                    id=f"st_{i}_deep",
                    agent_type=item["agent"],
                    description=(
                        f"深入『{item['dimension']}』：{raw_question}。"
                        f"【写作要求：本维度全报告**只出一章**，你就是这一章的唯一作者（基础轮只做检索、"
                        f"其资料已并入你手上的资料池）。请写出该维度的完整最终版：①细分市场/分区域/分渠道数据 "
                        f"②关键数字的一手验证路径（问卷/访谈/实地）③反方证据 ④最新数据更新。"
                        f"同一指标全章只允许一个数值，禁止两个口径并存而不说明】"
                    ),
                    search_queries=[f"{item['dimension']} 细分数据 最新 分区域 分渠道" for q in item["queries"]],
                    budget_estimate=0,
                    dimension=item["dimension"],
                )
            )
    # 定制：加决策建议子任务
    if depth == DepthLevel.CUSTOM:
        subtasks.append(
            SubTask(
                id="st_decision",
                agent_type="statistics",
                description=f"生成『{raw_question[:20]}』的决策建议与成本测算",
                search_queries=["决策建议", "成本测算"],
                budget_estimate=0,
                # 它不属于模板里的任何一个调研维度，是定制档独有的一章。
                # **键要写死**：从前 `_chapter_key` 会从描述里抠 `『提问前20字』` 当键，
                # 于是同一份计划换一次提问就换一个键——章节键不该由用户输入决定。
                dimension="决策建议与成本测算",
            )
        )

    return ResearchPlan(
        id="",
        raw_question=raw_question,
        clarified_context=context,
        dimensions=dimensions,
        depth=depth,
        price_estimate=_DEPTH_PRICE[depth],
        subtasks=subtasks,
        status="draft",
        # 这条路 = 规则模板兜底。**必须如实标出来**，否则报告会替"模型自主拆解"背书。
        plan_source="fallback",
    )


def generate_plan(raw_question: str, context: dict[str, str]) -> ResearchPlan:
    """生成研究计划（真实模式优先，Fallback 兜底）+ MECE 问题树"""
    context = context or {}
    if has_main_llm():
        try:
            llm = get_main_llm(temperature=0.2)
            skill = detect_skill(raw_question, context.get("objective", ""))
            templates = get_skill_templates(skill)
            skill_dims = "；".join(
                item["dimension"] for item in next(iter(templates.values()), [])
            )
            system = _render_plan_system_prompt(
                skill_meta(skill)["title"], skill_dims
            )
            resp = llm.invoke(
                [
                    ("system", system),
                    ("human", _build_plan_prompt(raw_question, context)),
                ]
            )
            data = json.loads(resp.content.strip().removeprefix("```json").removesuffix("```"))
            _dims = [str(d).strip() for d in (data.get("dimensions") or []) if str(d).strip()]
            if not _dims:
                raise ValueError("计划未给出 dimensions")
            _raw_subs = list(data.get("subtasks") or [])
            if not _raw_subs:
                raise ValueError("计划未给出 subtasks")
            # **维度归口**：逐条确定性归到某个维度上；归不上就抛 → 整份退回模板。
            # 这是打开 LLM 路径的**前置条件**（花括号只是一行语法，这个才是那个真缺陷）。
            _resolved = _resolve_subtask_dimensions(_raw_subs, _dims)
            _stubs = [
                SubTask(
                    id=f"st_{i}",
                    agent_type=s["agent_type"],
                    description=s["description"],
                    search_queries=s.get("search_queries", []),
                    dimension=_resolved[i],
                )
                for i, s in enumerate(_raw_subs)
            ]
            _depth_llm = DepthLevel(data["depth"])
            plan = ResearchPlan(
                raw_question=raw_question,
                clarified_context=context,
                dimensions=_dims,
                depth=_depth_llm,
                price_estimate=data.get("price_estimate", _DEPTH_PRICE[_depth_llm]),
                # 归并 + 指定唯一出章者（出章者由规则定，不由模型的措辞定）
                subtasks=_normalize_subtasks(_stubs, _resolved, _depth_llm),
                status="draft",
                plan_source="llm",
            )
        except Exception as _plan_err:
            # LLM 规划失败 / 计划不合格 → 降级到规则兜底（保证不翻车）。
            #
            # ⚠️ **这一分支此前是裸 `except Exception:`，一个字都不留**（2026-09-17 改）。
            # 它当时**每一次都会走**：上面 `_PLAN_SYSTEM_PROMPT.format(...)` 撞上
            # 提示词里那段**字面 JSON 示例**的花括号，每次都抛 KeyError。
            # 后果不是「偶尔降级」，而是
            # **「模型自主规划 / 自主拆解」这条能力在线上从未发生过**——
            # 每一次计划都来自 `_SKILL_TEMPLATES` 的静态骨架按档位截断，
            # 而计划照常生成、报告照常交付、**没有任何地方能看出来**。
            # 这正是本项目公理六「降级必须告警，不许悄悄兜底」要防的形态，
            # 也是"记录→消费断链"家族里最隐蔽的一种：链断在异常处理里。
            #
            # —— 2026-09-19 收官：占位符改用 `replace`（见 `_render_plan_system_prompt`），
            # 三个缺陷一次改完，路径已打开并已由一次真实运行验证。
            # **本行现在是真的"出了问题才响"**：正常运行时不该再出现。
            # 它可能因四类原因响，四类都会在文里点名：
            #   ① 没有可用模型（`has_main_llm()` 为假时走 else 分支，不经这里）；
            #   ② 模型返回的不是合法 JSON；
            #   ③ 计划没给 dimensions/subtasks；
            #   ④ **子任务没有可识别的维度**（违反提示词规则 5）——这是刻意设的
            #      fail-closed：宁可退回已知可用的模板计划，也不冒"同维度出两章"的险。
            logging.getLogger(__name__).warning(
                "LLM 规划路径失败，已回落规则兜底（本次计划来自 _SKILL_TEMPLATES，非模型自主拆解）: %s",
                _plan_err,
            )
            plan = _fallback_plan(raw_question, context)
    else:
        plan = _fallback_plan(raw_question, context)
    # 兜底路径**一律**再确认一次来源标记（`_fallback_plan` 已写 `fallback`，
    # 这里只是防"将来有人新加一条兜底路径却忘了标"——两条路都标上，读写同源）。
    if not plan.plan_source:
        plan.plan_source = "fallback"
    # MECE 问题树：把子任务分类到 需求/供给/资源/财务 分支（咨询级自主规划的可视结构）
    try:
        plan.problem_tree = _build_mece_tree(plan)
    except Exception:
        pass
    # 假设驱动·按问题类型分流：只有因果类问题生成 H1-H3（其余走议题树/统计描述）
    try:
        plan.question_type = _detect_question_type(raw_question)
        if plan.question_type == "causal":
            plan.hypotheses = _build_hypotheses(raw_question)
    except Exception:
        pass
    return plan


# ============================================================
# MECE 问题树（咨询级自主规划：互相独立、合并穷尽）
# ============================================================
# 市场分析的四个穷尽分支：需求侧 / 供给侧 / 资源侧 / 财务侧
_MECE_BRANCHES: list[dict[str, Any]] = [
    {
        "branch": "需求侧",
        "icon": "📈",
        "question": "市场有没有足够的需求？",
        "keys": ["市场规模", "目标市场", "目标市场与容量", "消费", "用户", "品类机会", "需求", "容量", "人群", "PEST", "宏观", "全景", "概览", "综述", "概况", "规模", "空间"],
    },
    {
        "branch": "供给侧",
        "icon": "🏪",
        "question": "竞争有多激烈？",
        "keys": ["竞争", "竞品", "平台", "渠道", "格局", "替代", "价格带", "玩家", "集中度", "市场份额", "主要玩家"],
    },
    {
        "branch": "资源侧",
        "icon": "🔑",
        "question": "我有没有能力/资源做？",
        "keys": ["壁垒", "门槛", "供应链", "物流", "成本", "资质", "合规", "政策", "物流与成本", "资源", "能力"],
    },
    {
        "branch": "财务侧",
        "icon": "💰",
        "question": "能不能赚钱？",
        "keys": ["单位经济", "测算", "盈亏", "利润", "投资", "回本", "决策", "风险", "趋势", "存活", "开店成本", "毛利"],
    },
]


def _mece_scope(desc: str, raw_question: str) -> str:
    """MECE 分类只看"子任务真正的任务名"，排除污染源：

    1. 离线/模板子任务会把整句用户问题拼在描述末尾（每行都一样）→ 剔除，否则
       "市场规模+竞争格局"式双关键词问题会让每个子任务都命中同一分支、树塌成 1 支；
    2. 优先取『』/「」内的维度名（LLM/模板都带），干净且互斥；
    3. 方法论括号（PEST/TAM…）与本行无关的分类词剔除，避免串分支。
    """
    d = (desc or "")
    d = d.replace(raw_question or "", "")  # 剥离每行重复的用户原问题
    m = re.search(r"[『「]([^』」]+)[』」]", d)
    if m:
        return m.group(1)
    d = re.sub(r"（[^）]*）", "", d)  # 去掉方法论括号
    for pre in ("调研", "深入", "分析", "评估", "生成"):
        d = d.replace(pre, "")
    return d.strip()


def _build_mece_tree(plan: ResearchPlan) -> list[dict[str, Any]]:
    """把计划子任务分类到 MECE 分支（需求/供给/资源/财务），每个子任务只归一个分支。

    规则：按【子任务真正的任务名】关键词命中分支（剔除每行重复的用户原问题，防
    双关键词问题塌树）；未命中 → 归入"财务侧"（决策导向兜底）。
    """
    raw_q = getattr(plan, "raw_question", "") or ""
    branches = [
        {"branch": b["branch"], "icon": b["icon"], "question": b["question"], "subtask_ids": []}
        for b in _MECE_BRANCHES
    ]
    for st in plan.subtasks or []:
        scope = _mece_scope(st.description, raw_q)
        qs = " ".join(st.search_queries or [])
        hay = f"{scope} {qs}"
        target = None
        for b in _MECE_BRANCHES:
            if any(k in hay for k in b["keys"]):
                target = b
                break
        if target is None:
            target = _MECE_BRANCHES[-1]  # 兜底：财务侧
        for br in branches:
            if br["branch"] == target["branch"]:
                br["subtask_ids"].append(st.id)
                break
    # 过滤空分支（没有子任务的分支不显示，保持紧凑）
    return [br for br in branches if br["subtask_ids"]]


# ============================================================
# 假设驱动·按问题类型分流（② 不全报告强制 H1-H3）
# ============================================================
# 因果词（影响/导致/是否有效/为什么）→ 假设检验模式
_CAUSAL_WORDS = ("影响", "导致", "是否有效", "为什么", "因为", "因果", "会不会", "相关性", "有关系", "效应", "溢价", "降不降价", "涨价")
# 描述词（多大/多少/格局/规模）→ 统计描述 + TAM/SAM/SOM
_DESCRIPTIVE_WORDS = ("多大", "多少", "规模", "格局", "现状", "趋势", "如何", "是什么", "增长", "占比", "分布", "画像")


def _detect_question_type(raw_question: str) -> str:
    """研究问题类型：causal（假设检验）/ descriptive（统计描述）/ exploratory（议题树）"""
    q = raw_question or ""
    if any(w in q for w in _CAUSAL_WORDS):
        return "causal"
    if any(w in q for w in _DESCRIPTIVE_WORDS):
        return "descriptive"
    return "exploratory"


def _subject_of(raw_question: str, limit: int = 32) -> str:
    """从整句提问里取出**主语短语**，供模板句使用。

    2026-09-17 实跑病案：原写法是把整句问题裸截 24 字再插进模板 ——
        `H1：『{topic}』的需求规模是进入/决策的核心变量（待验证）`
    输入「星巴克等咖啡品牌下沉三四线城市该不该做？下沉门店增速为什么放缓？」
    渲染成了：
        H1：『**星巴克等咖啡品牌下沉三四线城市该不该做？下沉门店**』的需求规模是…
    断在问号中间、还带着"该不该做"这个**疑问尾缀**——**一句话里出现了两个谓语残缺的片段**。
    这一行会印进报告与说明材料，是教授复核者一眼可见的那种病句。

    修法：① 截到第一个问号（问题的**主干在问号之前**，问号后是第二个子问题）；
    ② 剥掉"该不该做/能不能做/要不要做/值不值得做/该不该进入"这类尾缀；
    ③ 最后才按长度截断，且**不切断连续的拉丁字母/数字**（避免"瑞幸2024Q"这种半截 token）。
    """
    s = (raw_question or "").strip()
    # ① 第一个问号之前是主问题
    for sep in ("？", "?", "。", "\n"):
        i = s.find(sep)
        if i > 0:
            s = s[:i]
            break
    # ② 剥疑问尾缀（可叠加）。两类：
    #    ②a 决策型尾缀（"…该不该做"）
    #    ②b 疑问短语尾缀（"…有什么机会""…是多少""…怎么样"）——
    #       不剥的话主语会是个问句（"『社区养老服务市场有什么机会』对应的需求规模…"），
    #       读起来仍不通。**只剥尾部的疑问套话，不动中间的实词。**
    for _ in range(4):
        _before = s
        # ⚠️ 动词要写成**选择分支**不能写成字符类：`[做进入去…]` 只匹配**一个字**，
        # 而"要不要**进入**"是两个字的动词——第一版就栽在这（`低空经济赛道要不要进入` 原样留下）。
        s = re.sub(
            r"(该不该|能不能|要不要|值不值得|可不可以|是否有必要)(做|进入|进去|去|上|干|开|扩|建|投|买|搞|弄)?$",
            "", s,
        ).strip()
        s = re.sub(
            r"(有|存在)?(什么|啥|哪些)?(机会|前景|空间|风险|问题|痛点|趋势|玩法|细分行业|细分赛道)?$"
            r"|(是|有)?多少$|多大$|怎么样$|如何$|是什么$|在哪$|是谁$|(有没有|是否)(影响|机会|必要|前景|有效|可行|成立|划算|值得)$"
            r"|该(进|做|上|去|入)$|(为什么|为何)(波动|放缓|下降|上升|变化)$",
            "", s,
        ).strip()
        s = s.rstrip("，,、：:；; 的")
        if s == _before:
            break
    if not s:
        s = (raw_question or "").strip()
    if len(s) <= limit:
        return s
    cut = s[:limit]
    # ③ 别把连续的拉丁字母/数字拦腰截断（"瑞幸2024Q1" → 不要切出"瑞幸2024Q"）
    m = re.search(r"[A-Za-z0-9]+$", cut)
    if m and m.start() > 0:
        cut = cut[: m.start()]
    return cut.rstrip("，,、：:；; ")


def _build_hypotheses(raw_question: str) -> list[str]:
    """因果类问题生成研究假设（模板；LLM 可扩展为按行业定制）。

    假设用于报告"假设验证"章节：每条假设 找支持证据 + 找反证 + 判断强度。
    """
    topic = _subject_of(raw_question)
    return [
        f"H1：『{topic}』对应的需求规模与支付意愿，足以支撑进入决策（待验证）",
        f"H2：竞争强度与进入壁垒显著影响新进入者存活概率（待验证）",
        f"H3：价格/促销/广告等营销动作对用户转化存在因果影响，需因果方法（DML/uplift）验证",
    ]


# ============================================================
# 顶层接口：需求澄清 + 计划生成 一站式
# ============================================================
def router_ingest(raw_question: str, context: dict | None = None) -> dict[str, Any]:
    """Router 入口：返回"需要澄清的问题"或"研究计划"

    返回值：
    - 需要澄清: {"need_clarification": True, "questions": [...]}
    - 已明确:   {"need_clarification": False, "plan": ResearchPlan}
    """
    context = context or {}
    if needs_clarification(raw_question):
        questions = _pick_clarification_questions(raw_question)
        if questions:
            return {"need_clarification": True, "questions": questions}
    return {"need_clarification": False, "plan": generate_plan(raw_question, context)}


def confirm_plan(raw_question: str, context: dict[str, str]) -> ResearchPlan:
    """用户回答澄清问题后，正式生成计划"""
    return generate_plan(raw_question, context)
