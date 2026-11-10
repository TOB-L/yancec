"""研策 · 决策视角锁定（决策主体一致性，v0.9.42）

病案：同一份报告"执行摘要=高端品牌的口吻、各章节=新进入者视角"——摘要与每个章节由不同 LLM 调用
各自从正文"猜"谁在决策，猜不一致 → 评审一眼看穿立场漂移。
根因：决策主体从未被显式定义，也没有在生成环节之间传递。
修法：入口用规则把"决策主体视角"定成一段话（不新增 LLM 调用），随上下文注入执行摘要与每个章节
的生成指令，全文自始至终统一主体。

用法：viewpoint_block(raw_question, clarified_context) → 一段指令文本；两个消费方（graph 的执行摘要、
researchers 的章节整合）各自拼进自己的 system prompt。本模块只依赖纯字符串规则，无网络/无 LLM。

2026-09-07 复核修复：经营者只认强锚，避免把"评估…经营…的可行性"这类**进入者**问题误判成
"现有经营者"（裸"经营"一词不作数）；剔除"已经开"式悬空子串（会命中"已经开放/已经开展"）。
"""

from __future__ import annotations

# 已知行业词（把"决策主体"说得具体，避免通篇"该行业"）
_CATEGORY_WORDS = (
    "咖啡", "奶茶", "茶饮", "新茶饮", "宠物", "零食", "零食折扣", "美妆", "护肤",
    "服饰", "母婴", "数码", "家电", "餐饮", "健身", "教育", "预制菜",
    "跨境", "低空经济", "无人机", "新能源", "白酒", "啤酒", "方便面",
    "眼镜", "珠宝", "保健品", "小酒馆",
)

# 经营者强锚：显式"诊断/我的店/现有/已在经营"。刻意不含裸"经营"——"评估经营X的可行性"是进入者。
_OPERATOR_KW = (
    "经营诊断", "经营状况", "经营现状", "经营情况", "门店经营",
    "我的店", "我的门店", "我的品牌", "我们的店", "我们的门店", "我们的品牌",
    "现有门店", "现有品牌", "已经在经营", "在经营的店",
)
# 新进入者强锚（成对出现更稳，避免"进入/机会"类裸词命中政策/描述句）
_ENTRANT_KW = (
    "开一家", "开店", "开个店", "能不能开", "能开吗", "还能不能开",
    "想开", "加盟", "创业", "入局", "能不能做", "值不值得", "想不想做",
    "可不可以做", "做不做", "适合做", "进入这个市场", "进入该市场",
    "要不要进入", "要不要做", "有没有机会", "值不值得进", "要不要进",
)
# 观察者（竞品/趋势/跟踪/对比）——若同时出现进入词，进入者优先（上文顺序）
_TRACK_KW = ("竞品", "竞对", "趋势", "跟踪", "对比", "测评", "排行", "格局")


def _cat(raw: str) -> str:
    for w in _CATEGORY_WORDS:
        if w in (raw or ""):
            return w
    return ""


def _has(text: str, kws) -> bool:
    return any(k in text for k in kws)


def infer_subject(raw_question: str = "", context: dict | None = None) -> str:
    """返回决策主体角色：operator / entrant / observer / neutral

    依据 = 需求澄清里的目标(objective) + 提问句式。优先级：经营者强锚 > 新进入者强锚 > 观察者。
    全部无信号时回 neutral，绝不强行把"纯描述题"写成进入者/经营者。
    """
    ctx = context or {}
    obj = str(ctx.get("objective") or ctx.get("目标") or "")
    t = raw_question or ""
    if _has(obj, _OPERATOR_KW) or _has(t, _OPERATOR_KW):
        return "operator"
    if ("进入市场" in obj) or ("可行性" in obj) or _has(t, _ENTRANT_KW):
        return "entrant"
    if ("竞品" in obj) or ("趋势" in obj) or ("跟踪" in obj) or _has(t, _TRACK_KW):
        return "observer"
    return "neutral"


def viewpoint_block(raw_question: str = "", context: dict | None = None) -> str:
    """生成"决策视角锁定"指令块（执行摘要与各章节共用同一主体，防漂移）"""
    role = infer_subject(raw_question, context)
    cat = _cat(raw_question)
    tail = f"{cat}市场" if cat else "该品类/该行业"
    if role == "operator":
        head = f"一位正在经营{tail}的现有经营者/品牌方（提问者本人或其经营主体）"
    elif role == "entrant":
        head = f"一位正在考虑进入{tail}的新进入者/创业者（提问者本人）"
    elif role == "observer":
        head = f"第三方观察者视角（客观评估{tail}的现状、走势与玩家对比）"
    else:
        head = "中立研究视角（纯描述/对比型问题，不代入任何经营主体的立场）"
    return (
        f"- 决策主体：{head}。\n"
        "- 执行摘要、每个章节、决策清单必须自始至终以这一主体视角陈述与下结论；"
        "禁止在执行摘要或章节里把研究主体悄悄换写成另一类决策者"
        "（例：面向新进入者的研究，不得用'已在场的头部品牌/高端连锁'的立场代它下结论）。"
    )
