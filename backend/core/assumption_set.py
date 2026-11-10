"""假设集指纹 —— 让「哪一跑用了哪一套假设」从**不可见**变成**一眼可见**。

## 为什么需要它（2026-09-18 第十跑；外部审计指出、我方复核确认）

九跑与十跑都标 `引擎 v0.9.90`、`prompt_set` **都是 `72c514f9`** —— **判据是冻结的**。
但两跑的测算输入几乎全不同：

| 参数 | 九跑 | 十跑 |
|---|---|---|
| TAM 年均频次 | 35 杯 | **45 杯** |
| UE 日均杯量 | 300 | **315** |
| UE 客单价 | 15 元 | **16 元** |
| UE 月固定成本 | 65,000 | **72,000** |
| UE 毛利率 | 65% | **62%** |
| **BEP** | **222 杯** | **242 杯** |
| LTV / CAC | 16.2 | **33.5（+107%）** |
| TAM | 4,687 亿 | **6,026 亿（+28.6%）** |

**后果**：`v0.9.90` 这个版本号**不能标识一份可复现的报告**。
被问「我能不能复现你这个数」时，现答案是"不能"——而报告里**没有任何一处**
说得清"这一跑的假设和上一跑不是同一套"。

`prompt_set` 管的是**判据**（15 条指令正文有没有被改）；
本模块管的是**假设**（测算输入取值有没有变）。
**两者是两件事**——只冻结判据、不标识假设，跨跑可比性就有一半是瞎的。

## 它刻意不做什么

- **不改任何数字**：只读报告正文里**已经印出来**的参数行，规范化后取哈希。
- **不判定哪套假设对**：它只说"这次是这一套"，不说"这套好不好"。
  （判定是 R1/R13/闸门的事，不是它的。）
- **不改失败行为**：取不到参数就返回空串，那一段照常出，**不留半句假话**。
- **不是质量指标**：两组不同哈希之间**没有优劣**。同一个 `assumption_set`
  的两份报告才有资格做横比——这正是它存在的意义。
"""
from __future__ import annotations

import hashlib
import re

# 假设写在这两节里（参数行/参数表）。**只认这两节**：
# 正文别处也会出现数字（竞争格局、目标市场…），把它们算进来会让指纹
# 随"这跑多引了一条媒体数据"而变——那就不是**假设**集指纹了。
_SECTION_PREFIXES = ("市场规模测算", "单位经济")

# 表头/列名/图例，不是参数。
# ⚠️ 后四个（测算层级 / 测算维度 / 关键假设 / 情景设定）是**实测漏进来的**：
#   它们本身是那张表的**列名行**（`| 测算层级 | 定义说明 |`），而第一版词表只挡了"指标"类。
#   留下来的代价不是"错"，是**过敏**——模型把图例重写一句话，指纹就变，
#   而假设一个字没动。**一个会常态抖动的指纹比没有指纹更糟，因为它会教人忽略它。**
_SKIP_NAMES = (
    "指标", "指标项目", "指标维度", "数值", "金额", "情景类型", "备注", "说明", "来源",
    "测算层级", "测算维度", "关键假设", "情景设定",
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_SEP_CELL = re.compile(r"^[:\-\s]+$")


def _pairs(md: str) -> list[tuple[str, str]]:
    """从《市场规模测算》与《单位经济》两节的表格行里取 (参数名, 取值)。

    **只取表格行**，不取散文：散文里的数字排版每跑都可能不同（"约 315 杯" / "315 杯"），
    拿它做哈希会让指纹在不该变的时候变——**一个会常态抖动的指纹比没有指纹更糟**，
    因为它会教人忽略它。
    """
    out: list[tuple[str, str]] = []
    grab = False
    for ln in (md or "").splitlines():
        m = _HEADING_RE.match(ln)
        if m:
            # ⚠️ **只有 `##` 及更高级才算"换了一节"**（第一版没写这条，当场造出一个全零指纹）：
            # 《市场规模测算》下面全是 `### TAM：…` / `### SAM：…` 子标题，
            # 而 `"TAM：潜在总市场规模测算".startswith("市场规模测算")` 是 False ——
            # 每遇到一个子标题就把 `grab` 关掉，于是**整节一行都取不到，指纹恒为空串**。
            # 一个恒为空串的指纹，看起来像"这份报告没有假设"，比没有指纹更糟。
            if len(m.group(1)) <= 2:
                grab = any(m.group(2).startswith(p) for p in _SECTION_PREFIXES)
            continue
        if not grab or not ln.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        name, val = cells[0], cells[1]
        if not name or not val or _SEP_CELL.match(name) or _SEP_CELL.match(val):
            continue
        if any(k in name for k in _SKIP_NAMES):
            continue
        out.append((name, val))
    return out


def fingerprint(md: str) -> str:
    """本次假设集指纹（8 位十六进制）。**取不到 → 空串，不猜。**"""
    try:
        ps = _pairs(md)
        if not ps:
            return ""
        # 规范化：去空白（数字两侧加不加空格是排版差异，不是假设差异）、按参数名排序。
        # ⚠️ **`r'\s+'` 不是 `r'\\s+'`**（第一版写成后者，被自己的测试锁当场抓住）：
        # 原始字符串里 `\\s` 是"字面反斜杠 + s"，匹配不到任何空白 → **归一化静默失效**，
        # 于是"35 杯"和"35杯"给出两个指纹。它不报错、不崩，只是安静地不做事。
        _ws = re.compile(r"\s+")
        blob = "|".join(f"{_ws.sub('', n)}={_ws.sub('', v)}" for n, v in sorted(ps))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]
    except Exception:
        return ""


def latest_recorded(path: str | None = None) -> str:
    """账本(`data/run_stats.jsonl`)里**最近一笔**的假设集指纹。取不到 → 空串。

    **为什么要读账本，而不是只看本报告**：九跑与十跑的 `prompt_set` 都是 `72c514f9`，
    而 BEP 从 222 跳到 242、TAM 差 28.6% —— 但这只在我**把两份报告摆在一起**时才看得见。
    真实的读者只拿到**一份**。所以"变了"这件事必须在**这一份里**说出来，
    而说出来的唯一办法是：**本跑跟上一跑比**。
    读不到账本 → 返回空串，调用方就不印那一句（**不印"无法比较"的空话**）。
    """
    try:
        import json
        from pathlib import Path

        p = Path(path) if path else Path(__file__).resolve().parent.parent / "data" / "run_stats.jsonl"
        if not p.exists():
            return ""
        last = ""
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            v = r.get("assumption_set")
            if isinstance(v, str) and v:
                last = v          # 顺序扫，最后一个非空就是最近一笔
        return last
    except Exception:
        return ""


def describe(md: str, prev: str = "") -> str:
    """可印进报告的一段话（取不到 → 空串，**由调用方决定不印，而不是印一句空话**）。

    `prev`：账本里最近一笔的指纹。与本次不同时，**多印一句"变了"**——
    这是唯一能让只拿到一份报告的读者知道"这跑和上跑不是同一套假设"的办法。
    """
    fp = fingerprint(md)
    if not fp:
        return ""
    n = len(_pairs(md))
    out = (
        f"> **假设集指纹 `assumption_set = {fp}`**（本报告《市场规模测算》+《单位经济》"
        f"两节里 **{n} 行参数**规范化后的内容哈希）。"
        "**它管的是假设，`prompt_set` 管的是判据，两件事。**"
        "用同一版本重跑得到不同指纹，说明**测算是同一套算法、不同的输入**——"
        "那种情况下两份报告的测算数字**不可直接横比**。"
    )
    if prev and prev != fp:
        out += (
            f"\n>\n> ⚠️ **本跑的假设集与上一笔不同**（上一笔 `assumption_set = {prev}`）。"
            "**同一版本号下这是可能的，而且不是缺陷**——测算输入是每跑现定的估算参数，"
            "不是写死的配置。但它意味着：**本报告的测算数字与上一份不可直接横比**，"
            "要横比，先看两边的 `assumption_set` 是不是同一个。"
            "（这一行存在的全部目的，就是让「两跑假设不同」从**读者看不见**变成**印在纸上**。）"
        )
    return out
