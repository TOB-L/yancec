"""研策 · 提示词指纹（Prompt Registry）—— 把「提示词改没改」从一个说法变成可核对的数

## 为什么必须做这一层：它补的是**我们已经声称、却无法检查**的一件事

`core/calibration.py` 的存档里写着：

> 「核验官是模型，**换模型或改判定提示词后本结果即作废**（有效期 45 天）」

这是一个**承诺**：判定提示词一改，那份校准结果立即失效。
但在此之前，**没有任何东西能检查它**——45 天 TTL 只是"提示词没变"的一个粗糙代理：

    提示词今天改了，那份存档在 45 天内**仍会被 `load()` 读出来**，
    并原样印进每一份交付报告附录。

也就是说，**我们承诺了一件做不到的事**，而它就印在每份报告里。
这与本仓库反复出现的"声称但没接线"是同一个病，只不过这次声称的是一个**否定式**的保证
（"改了就会作废"）——**否定式的保证最难被发现是空的**，因为没人会去改提示词试试。

## 这一层做什么（四步）

1. **登记**：把仓库里的提示词常量按 `模块::常量名` 登记成一张表；
2. **指纹**：每个提示词取正文 SHA-256 前 8 位（内容寻址，与 AIGC 内容编号同一套纪律）；
3. **整集指纹**：全部指纹再哈希一次 = `prompt_set`，落账 + 印在报告附录；
4. **判废**：校准存档落盘时记下**判定提示词**的指纹，读取时比对——
   不一致即视为过期（与 TTL 是**与**关系：任一不满足就作废）。

## 三条纪律

- **只写指纹，不写正文**。提示词正文是资产，指纹是证据；账本与报告里出现正文，
  等于把可被抄袭的东西随包发出去。
- **登记表漏一个，这一层就漏保护一个**。所以配一条机械锁：
  全仓 `*PROMPT*` 形态的模块级常量数**必须等于**登记表条数——多一个少一个都报。
  没有这条锁，这一层会随着"新加一个提示词"而静默退化。
- **指纹变化不改写历史**。它只让"这份校准还算不算数"变成一个可判定的问题，
  不追溯改任何已落盘的结果。
"""
from __future__ import annotations

import hashlib
import importlib
import logging

logger = logging.getLogger(__name__)

# 登记表：`键 = (模块名, 常量名)`。
# 键是**稳定标识**（进账本、进报告），所以用语义名而不是变量名——
# 变量名可以在重构时改，账本里的历史值不能。
PROMPTS: dict[str, tuple[str, str]] = {
    "router.plan": ("agents.router", "_PLAN_SYSTEM_PROMPT"),
    "researchers.integrate": ("agents.researchers", "_INTEGRATION_PROMPT"),
    "researchers.calc": ("agents.researchers", "_CALC_EXTRACT_PROMPT"),
    "researchers.calc_unit": ("agents.researchers", "_UNIT_CALC_EXTRACT_PROMPT"),
    "decision.sop": ("agents.decision", "_SOP_SYSTEM_PROMPT"),
    "summary.metrics": ("graph", "_SUMMARY_METRICS_PROMPT"),
    "entailment.judge": ("core.entailment", "_JUDGE_PROMPT"),
    "adversarial": ("core.adversarial", "_ADVERSARIAL_PROMPT"),
    "debate.skeptic": ("core.debate", "_SKEPTIC"),
    "debate.analyst": ("core.debate", "_ANALYST"),
    "debate.judge": ("core.debate", "_JUDGE"),
    "replan.propose": ("core.replan", "_PROPOSE_PROMPT"),
    "tool_select.pick": ("core.tool_select", "_PICK_PROMPT"),
    "industry_kb.card": ("core.industry_kb", "_CARD_PROMPT"),
    "outcome_track.judge": ("core.outcome_track", "_JUDGE"),
}

# 全仓提示词常量的**机械判据**——锁用它数一遍，与登记表条数比对。
# 覆盖两种形态：`*PROMPT*` 与三个辩论角色常量（它们不以 PROMPT 命名，
# 但同样是喂给模型的指令正文 —— **按名字找会漏掉它们，正是"只收一种词形"**）。
PROMPT_CONST_RE = (
    r"^(_?[A-Z][A-Z_]*PROMPT[A-Z_]*|_SKEPTIC|_ANALYST|_JUDGE)\s*[:=]"
)
# 本模块自己不算提示词（`CALIBRATION_PROMPT` 是一个**名字**，不是正文）。
PROMPT_SCAN_EXCLUDE = ("core/prompt_registry.py",)

# 判定提示词——**校准结果作废判据只认这一个**。
# 为什么只认它：校准测的是"这个核验官判得准不准"，
# 而核验官 = 模型 + 判定提示词。其它提示词变了，校准依然有效。
CALIBRATION_PROMPT = "entailment.judge"

_MISSING = "missing"


def _fingerprint(text: str) -> str:
    """指纹 = 正文 SHA-256 前 8 位。与 AIGC 内容编号同一套内容寻址纪律。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:8]


def fingerprint(name: str) -> str:
    """取单个提示词的指纹。**取不到返回 `"missing"` 而不是抛异常。**

    为什么 fail-soft 而不是 fail-closed：这一层是**旁证**，不是主链路。
    某个提示词被重命名/挪走时，让整份报告生成不出来，代价远大于少一个指纹。
    但**必须留下可分辨的痕迹**——`"missing"` 与真指纹长得完全不同，
    而 `prompt_set()` 会把"有没有 missing"一并算进去，所以静默退化会被看见。
    """
    spec = PROMPTS.get(name)
    if not spec:
        return _MISSING
    mod_name, attr = spec
    try:
        mod = importlib.import_module(mod_name)
    except Exception as e:  # pragma: no cover - 导入失败只可能是重构改了模块名
        logger.debug("提示词登记表指向的模块导入失败 %s: %s", mod_name, e)
        return _MISSING
    val = getattr(mod, attr, None)
    if not isinstance(val, str) or not val.strip():
        return _MISSING
    return _fingerprint(val)


def snapshot() -> dict[str, str]:
    """全部提示词指纹。`missing` 项**原样保留**——它就是要被看见的那个信号。"""
    return {k: fingerprint(k) for k in PROMPTS}


def prompt_set() -> str:
    """整集指纹：键 + 各自指纹排序后拼接再哈希。

    为什么把**键也算进去**：只哈希指纹集合的话，两个提示词互换内容会得到同一集合。
    键进哈希，等价于"哪个提示词是什么内容"被整体钉住。
    `missing` 会作为普通值参与哈希——于是**任何一个提示词取不到，
    整集指纹就会变**，这正是我们要的：退化会改变读数，而不是悄悄通过。
    """
    snap = snapshot()
    blob = "|".join(f"{k}={v}" for k, v in sorted(snap.items()))
    return _fingerprint(blob)


def calibration_fingerprint() -> str:
    """判定提示词的指纹——**校准存档的作废判据**。"""
    return fingerprint(CALIBRATION_PROMPT)


def missing_names() -> list[str]:
    """取不到指纹的提示词名。供自检与报告附注使用。"""
    return [k for k, v in snapshot().items() if v == _MISSING]


def describe() -> str:
    return (
        "提示词指纹：把仓库里的提示词常量登记成表并取内容指纹（SHA-256 前 8 位），"
        "整集指纹落账并印在报告附录；判定提示词的指纹进校准存档，"
        "**指纹不一致即判校准过期**——把「换提示词则校准作废」从一句承诺变成可判定的比对。"
    )
