"""研策 · 时序派生数字的**唯一算法出处**

## 为什么必须单独一个模块

同一个"复合增速"公式在本仓库出现在**三个地方**，而它们的输入类型并不相同：

| 位置 | 用途 | 能否 import 本模块 |
| :--- | :--- | :--- |
| `core/authority_intl.py` | World Bank 年序列 | ✅ |
| `core/authority_fetch.py` | SEC XBRL 年序列 | ✅ |
| `core/audit_bundle.py` | 可复算包 `compute.py`（第三方独立跑） | ❌ 必须自带副本 |

第三处要能**脱离本项目运行**，所以不能 import。它的副本由
`_fulltest.py` 里的一致性用例锁住：同一组序列在两处必须给出同一个数。

## 核心规则：增速序列不是水平序列

`(末/初)^(1/n)-1` 是**水平（存量）序列**的复合增速公式。

把它套到**增速序列**上会得到荒谬的数——实测（2026-09-15）：

> World Bank「中国 GDP 年增速」= 2.34, 8.57, 3.134, 5.416, 4.958, 4.96（单位 %）
> 被算成 **CAGR≈16.2%**；而正确的几何平均约 **4.9%**。

**错在输入类型，不在精度。** 序列本身是"每年的增长率"，不是"每年的经济规模"。
对增长率序列做复合，正确做法是各年**增长因子**的几何平均：

    g = (∏ (1 + rᵢ/100))^(1/k) − 1

## 为什么这条线特别危险

三个地方用的是**同一个错公式**，所以可复算包会**复现**这个错数，
第三方跑完得到"两边一致"——**验证机制为错误背书**。
这比"报告里有一个错数"严重得多：它把错误变成了"已被独立验证"。

## 判据用单位与指标名，不靠猜

- 指标名含 **变化型** 词（增速/增长率/同比/环比/涨幅/通胀率…）→ 增速序列
- 指标名含 **水平型** 词（率/占比/比例/份额/渗透率/覆盖率…）→ 水平序列
- 单位是「个百分点」→ 增速序列；单位是货币/人/家/杯 → 水平序列
- 单位是 `%` 而指标名给不出线索 → **判不出，返回 None，不算**

⚠️ **`%` 本身不是"这是增速"的证据。** 本条曾写反（2026-09-16 修正）：
`城镇化率 63.9%` 是**水平**（六成三是城镇人口），不是"增长了 63.9%"。
把它当增速序列做几何平均，实测得出「城镇化率区间年均增速≈50%」——
**而这个错数会被可复算包复现，于是"验证机制为错误背书"。**
"""
from __future__ import annotations

# 单位里出现这些 → 该序列是"变化量"，不是存量。
# ⚠️ **`%` 不在此列**：百分号只说明"这个数是百分数"，不说明"这个数在增长"。
# 见 `_LEVEL_PCT_NAME_HINTS` 与 `classify_series` 的说明。
_RATE_UNITS = ("个百分点", "pp", "ppt")

# 值本身是**变化率**的指标名。只有这一组适用几何平均。
_RATE_NAME_HINTS = (
    "增速",
    "增长率",
    "复合增长",
    "同比",
    "环比",
    "涨幅",
    "通胀率",
    "cpi",
    "cagr",
)

# 值本身是**以 % 表示的水平/占比**的指标名。**这一组不是增速序列。**
#
# 病案（2026-09-16 全量审计，本模块自己的判据写反了）：
# 原 `_RATE_NAME_HINTS` 里混进了「城镇化率 / 普及率 / 占比 / 比率」——它们是
# **水平**，不是变化：`城镇化率 = 63.9%` 的含义是"六成三是城镇人口"，
# **不是**"城镇化率增长了 63.9%"。把 `(1+v/100)` 连乘再开方，
# 得到的是「城镇化率区间年均增速≈50%」——**一年涨 50 个百分点的城镇化率只有算错才出得来**。
#
# 触发面（`core/authority_intl.py` 的 `_WB_IND` 八个指标里**三个**是这一类）：
#   城镇化率（SP.URB.TOTL.IN.ZS）· 服务业增加值占GDP（NV.SRV.TOTL.ZS）· 互联网普及率（IT.NET.USER.ZS）
# 三个都带 `%` 单位、都走这条路，且都挂着"（本地计算）"的权威标签印进报告。
#
# 处理方式：走**水平序列**的 CAGR（比率本身的年复合增速），标签也随之不同。
_LEVEL_PCT_NAME_HINTS = (
    "城镇化率",
    "普及率",
    "渗透率",
    "覆盖率",
    "占有率",
    "占比",
    "占",          # 「服务业增加值占GDP」这类——「占X」在中文指标名里就是"份额"的意思
    "比例",
    "份额",
    "比率",
    "率",          # 兜底：中文里以「率」结尾的绝大多数是水平（毛利/失业/留存/转化…）。
                   # 变化型的「增长率/通胀率」在上面的 `_RATE_NAME_HINTS` 里**先判**，
                   # 所以不会被这一条吃掉。顺序即优先级。
)


def classify_series(name: str = "", unit: str = "") -> str:
    """该时序是 `"rate"`（值本身是变化率）/ `"level"`（值本身是水平）/ `"unknown"`。

    **这是三处实现必须一致的那个判断**（本模块 · 可复算包 `compute.py` 的副本 ·
    由 `_fulltest` 的一致性用例锁住）。判据顺序即优先级：

      1. 单位是「个百分点」→ 变化量，无可争议；
      2. 指标名含"变化型"词（增速/增长率/同比/通胀率…）→ rate；
      3. 指标名含"水平型"词（率/占比/比例/份额…）→ level；
      4. 单位是 `%` 但指标名给不出任何线索 → **判不出**；
      5. 其余 → level（金额、人数、门店数是绝大多数情况）。

    ⚠️ 第 4 条**不给默认值**：`%` 既可能是增速也可能是水平，蒙一个的代价是
    一个"看起来算过了"的错数。本模块的纪律与产品同一条——**宁可不给数**。
    """
    _n = (name or "").strip()
    _low = _n.lower()
    _u = (unit or "").strip()
    if _u in _RATE_UNITS:
        return "rate"
    if any(h in _low for h in _RATE_NAME_HINTS):
        return "rate"
    if any(h in _n for h in _LEVEL_PCT_NAME_HINTS):
        return "level"
    if _u in ("%", "％"):
        return "unknown"
    if not _n and not _u:
        return "unknown"
    return "level"


def is_rate_series(name: str = "", unit: str = "") -> bool:
    """该时序是**比率/增速**序列（True）还是**水平/存量**序列（False）。

    保留布尔签名（历史调用方按布尔用），但判据已收归 `classify_series`——
    **"判不出"在这一层按"水平"处理**（水平的 CAGR 对水平序列永远成立，
    对增速序列会得出 16.2% 那种数；真正会用到"判不出"的地方是
    `derive_growth`，它在那一档上**直接弃权**）。
    """
    return classify_series(name, unit) == "rate"


def geo_mean_rate(values: list[float]) -> float | None:
    """**增速序列**的区间年均增速 = 各年增长因子的几何平均（%）。

    输入是百分数（如 2.34 表示 +2.34%）。少于 2 个点、或出现 ≤ -100% 的
    不可解释值时返回 None（**不返回 0**——0 会被读成"年均零增长"，那是个结论，不是缺席）。
    """
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return None
    prod = 1.0
    for v in vals:
        f = 1.0 + v / 100.0
        if f <= 0:
            return None
        prod *= f
    return (prod ** (1.0 / len(vals)) - 1.0) * 100.0


def cagr_level(first: float, last: float, years: int) -> float | None:
    """**水平序列**的区间年复合增速 CAGR（%）。years 为**年数间隔**（末-初）。"""
    try:
        if years <= 0 or first <= 0 or last <= 0:
            return None
        return ((last / first) ** (1.0 / years) - 1.0) * 100.0
    except Exception:
        return None


def derive_growth(pairs: list[tuple[str, float]], name: str = "", unit: str = "") -> tuple[str, float | None]:
    """时序 → (标签, 数值%)。**这是三处实现必须一致的那个判断。**

    返回的标签直接印进报告，所以它同时承担"说清这是什么数"的责任：
    - 增速序列 → `("区间年均增速（几何平均）", g)`
    - 水平序列 → `("区间年复合增速 CAGR", g)`
    - **判不出** → `(说明, None)`——调用方**不得**把 None 印成 0，
      也不得在标签里假装算过（"区间增速"四个字配一个错公式就是本模块要防的那件事）。
    """
    _kind = classify_series(name, unit)
    if _kind == "rate":
        return "区间年均增速（几何平均）", geo_mean_rate([v for _y, v in pairs])
    if _kind == "unknown":
        # **弃权，而不是猜。** 单位是 %、指标名又给不出线索时，
        # 两条公式都可能把它算成一个"看起来算过了"的错数。
        return "区间增速（序列类型判不出，本次不出数）", None
    if len(pairs) < 2:
        return "区间年复合增速 CAGR", None
    try:
        n = int(pairs[-1][0]) - int(pairs[0][0])
    except Exception:
        return "区间年复合增速 CAGR", None
    return "区间年复合增速 CAGR", cagr_level(float(pairs[0][1]), float(pairs[-1][1]), n)
