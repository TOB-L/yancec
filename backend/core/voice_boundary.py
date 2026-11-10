"""研策 · 用户声音章节的**边界声明**（一手 / 二手的界线，必须印在章节里）

## 病案（2026-09-15 深访实跑，三份外部评审同时点名）

报告里有一章叫「**下沉市场用户真实反馈**」，而同一份报告的缺口披露写着：

> 「消费者声音」：无一手调研（问卷/访谈）→ 只能给**二手舆情线索**，
> 报告中必须显式声明未经一手验证。

**两个说法对不上，而章节标题是读者唯一会记住的那句。** GPT 那份评审把风险说得很直白：

> 一个复核者如果看到"下沉市场用户真实反馈"，然后发现下面是模拟数据，
> 很容易得出："你是不是在包装不存在的一手数据？"

对参赛作品而言，这一条的杀伤力和"技术被比下去"不是一个量级——
它伤的是**诚实**这个人设，而我们整套产品的立论恰恰建在那上面。

## 为什么不能只改标题

改标题治不了根：标题是模型写的，下一次可能写成"用户怎么说""消费者态度"……
**词表是治不完的**（本仓库在遗漏层的章节判定上已经吃过一次同样的教训）。
所以判据要放在**结构**上：这一章是不是**基于一手源**产出的。

## 判据（可判定，不猜）

看 `subtask_results["_routes"]` 里「消费者声音」类维度的路由结果：
`required_layer` 含 L5 且 `satisfied=False` → **该维度没有拿到一手源** → 加边界声明。
拿到了（satisfied=True）→ **一个字不加**。

这条纪律比"一律加声明"重要得多：**无条件贴"二手"标签，等于把真做过一手调研的那些报告说成没做**——
那是另一种不诚实，而且是往自贬方向偏的那种。
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 用户声音类章节的标题特征（模型写出来的各种叫法）
_VOICE_HEAD = re.compile(
    r"^#{2,4}\s*[^\n#]{0,24}?(用户|消费者|客群|人群)[^\n#]{0,16}?"
    r"(反馈|声音|评价|口碑|态度|怎么看|调研|洞察|画像)"
)

_BOUNDARY = (
    "> 🚧 **本节边界（一手 / 二手）**：本节内容**来自公开二手来源**（媒体报道、行业报告、"
    "公开讨论），**不是**本产品执行的一手调研（问卷/访谈/实地）。"
    "因此：**不代表总体用户偏好、不可用于直接估计市场比例**，仅作方向性线索。"
    "本报告未执行一手采集——需要一手数据时，请按文末『一手验证方案』实际执行后再引用。"
)
_MARK = "本节边界（一手 / 二手）"


def has_firsthand_user_source(routes: dict[str, Any] | None) -> bool:
    """本run 是否真的拿到了**一手用户声音源**（L5 且路由 satisfied）。"""
    for r in (routes or {}).values():
        if not isinstance(r, dict):
            continue
        lab = str(r.get("required_label") or "") + str(r.get("claim_label") or "")
        if "声音" not in lab and "用户" not in lab and "消费者" not in lab:
            continue
        if r.get("satisfied"):
            return True
    return False


def stamp_voice_boundary(md: str, routes: dict[str, Any] | None) -> str:
    """给用户声音章节加边界声明。**拿到一手源就一个字不加。**

    幂等（已有标记则跳过）、异常返回原文、只动章节内、不删任何原文。
    """
    text = md or ""
    if not text:
        return text
    try:
        if has_firsthand_user_source(routes):
            return text  # 有真一手源 → 不许贴"二手"标签（那是另一种不诚实）
        lines = text.split("\n")
        out: list[str] = []
        i = 0
        stamped = 0
        while i < len(lines):
            ln = lines[i]
            out.append(ln)
            if _VOICE_HEAD.match(ln.strip()):
                # 往后看 6 行：已有标记则跳过（幂等——同一节只会被加一次）
                _ahead = "\n".join(lines[i + 1:i + 7])
                if _MARK not in _ahead:
                    out.append("")
                    out.append(_BOUNDARY)
                    out.append("")
                    stamped += 1
            i += 1
        if not stamped:
            return text
        logger.info("用户声音边界声明已加 %d 处（本次未取得一手用户源）", stamped)
        return "\n".join(out)
    except Exception as e:
        logger.warning("用户声音边界声明失败（报告原样交付）: %s", e)
        return text


def describe() -> str:
    return (
        "用户声音章节边界声明：当『消费者声音』维度**未取得一手源**时，"
        "在其章节内印明『本节来自公开二手来源、非一手调研、不代表总体偏好』。"
        "判据走源路由结果（结构），不走标题词表；**已取得一手源时不加任何声明**——"
        "无条件贴『二手』标签等于把真做过一手调研的报告说成没做。"
    )
