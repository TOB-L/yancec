"""研策 · 国际组织开放统计（World Bank / IMF）—— 宏观时序 + 预测锚

## 为什么单列一层

前面接的巨潮/SEC/统计局给的是**中国境内**的一手数据。但"出海/跨境/国际对比"类问题
（claude 场景包里的「出海选品」）拿不到对照口径——而这类问题恰恰要靠**别国官方数字**说话。

世界银行与 IMF 把统计库作为**免 key 公开 JSON API** 发布（2026-09-11 本机实测可达），
给的是**结构化长时序**，还有 IMF 独有的**未来预测值**：
- World Bank：200+ 经济体 × 上千指标的年序列（含 `lastupdated`，可判新鲜度）
- IMF DataMapper：各国 GDP 等，**含未来若干年预测** → 研究里的"预测锚"不再只能引二手研报

## 合规
- 两站均为官方公开 API，**免 key、免登录**；robots 实测无声明（World Bank）/ 目标路径未被
  Disallow 覆盖（IMF `/external/datamapper/`）。
- 频率沿用 `authority_fetch` 的 per-host 令牌桶 ≥1s；只取聚合年度值，不批量翻全库。
- 总开关同 `AUTHORITY_FETCH_ENABLED`；失败一律静默降级返回 []。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_WB = "https://api.worldbank.org/v2/country/{country}/indicator/{ind}?format=json&per_page=60&date={y0}:{y1}"
_IMF = "https://www.imf.org/external/datamapper/api/v1/{ind}"

# 高频宏观指标（World Bank 指标码 → 中文名）
# 指标码 → (中文名, 单位)。**单位必须是真的**——早先统一写"原始单位"占位，
# 拆包时看到「ChinaGDP（World Bank，单位：原始单位）」直接暴露给评审，属硬伤。
_WB_IND: tuple[tuple[str, str, str], ...] = (
    ("SP.POP.TOTL", "总人口", "人"),
    ("NY.GDP.MKTP.CD", "GDP（现价美元）", "美元"),
    ("NY.GDP.PCAP.CD", "人均GDP（现价美元）", "美元"),
    ("NY.GDP.MKTP.KD.ZG", "GDP年增速", "%"),
    ("SP.URB.TOTL.IN.ZS", "城镇化率", "%"),
    ("FP.CPI.TOTL.ZG", "通胀率（CPI年%）", "%"),
    ("NV.SRV.TOTL.ZS", "服务业增加值占GDP", "%"),
    ("IT.NET.USER.ZS", "互联网普及率", "%"),
)
# IMF DataMapper 指标（含预测值）——**必须带真单位**
# 病案（2026-09-14 审计）：这里原来是二元组，`_imf_one` 里单位写死成 `"十亿美元"`，
# 四个指标共用一个 → 报告里印出「中国 人口（含预测）（IMF WEO，单位：十亿美元）——
# 2024年 1,409」（实为 14.09 亿人）、「实际GDP增速 … 单位：十亿美元 —— 4.8」（实为 %）。
# 触发面极宽：`_IND_TRIGGERS` 把 人口/人均/增速 全指向这个函数，
# 而入口闸 `intl_wanted(...) or any(k in text for k in _MACRO_HINT)` 里含
# 「市场规模/行业规模/增速/增长率/人均/总额/渗透率/价格/消费/经济」——**一份常规市场调研几乎必中**。
# 挂的还是 L1「国际组织」身份，比没有来源更伤。
_IMF_IND: tuple[tuple[str, str, str], ...] = (
    ("NGDPD", "GDP（现价美元，含预测）", "十亿美元"),
    ("NGDP_RPCH", "实际GDP增速（含预测）", "%"),
    ("NGDPDPC", "人均GDP（现价美元，含预测）", "美元"),
    ("LP", "人口（含预测）", "百万人"),
)
# 指标码 → 单位。**查不到必须是空串**，绝不回落到"十亿美元"
# ——回落等于用一个看起来合理、其实张冠李戴的单位去印每一个新指标。
_IMF_UNITS: dict[str, str] = {k: u for k, _n, u in _IMF_IND}

# 国家/地区名 → ISO3（World Bank）/ ISO（IMF）
_COUNTRY: dict[str, str] = {
    "中国": "CHN", "中国大陆": "CHN", "全国": "CHN",
    "美国": "USA", "日本": "JPN", "韩国": "KOR", "印度": "IND", "德国": "DEU",
    "英国": "GBR", "法国": "FRA", "意大利": "ITA", "西班牙": "ESP", "加拿大": "CAN",
    "澳大利亚": "AUS", "巴西": "BRA", "俄罗斯": "RUS", "墨西哥": "MEX",
    "越南": "VNM", "泰国": "THA", "印尼": "IDN", "印度尼西亚": "IDN",
    "马来西亚": "MYS", "新加坡": "SGP", "菲律宾": "PHL", "沙特": "SAU",
    "全球": "WLD", "世界": "WLD",
}

# 触发词：命中即认为该问题需要国际对照口径
_INTL_TRIGGERS = (
    "出海", "海外", "国际市场", "跨境", "出口", "全球化", "各国", "全球",
    "东南亚", "欧美", "国际化", "世界", "国际对比", "对标",
)

# 指标触发词 → 该取哪些指标（避免把 8 个指标全拉一遍）
_IND_TRIGGERS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("人口", "人口规模", "老龄化", "劳动力"), ("SP.POP.TOTL", "LP")),
    (("人均", "人均收入", "人均GDP", "消费能力"), ("NY.GDP.PCAP.CD", "NGDPDPC")),
    (("城镇化", "城市化"), ("SP.URB.TOTL.IN.ZS",)),
    (("增速", "增长率", "增长", "CAGR"), ("NY.GDP.MKTP.KD.ZG", "NGDP_RPCH")),
    (("通胀", "物价", "CPI"), ("FP.CPI.TOTL.ZG",)),
    (("GDP", "经济规模", "经济体量", "市场规模"), ("NY.GDP.MKTP.CD", "NGDPD")),
)


def _pick_country(text: str) -> str:
    """问题里出现的国家 → ISO3；无则默认中国（研究主体通常在中国）"""
    for k, v in _COUNTRY.items():
        if k in (text or ""):
            return v
    return "CHN"


def _pick_indicators(text: str, intl: bool) -> list[str]:
    """按问题里的指标词挑要取的指标码（挑不中就给 GDP + 人口两个默认锚）"""
    want: list[str] = []
    for kws, inds in _IND_TRIGGERS:
        if any(k in (text or "") for k in kws):
            want.extend(inds)
    if not want:
        want = ["NY.GDP.MKTP.CD", "SP.POP.TOTL"] if not intl else ["NGDPD"]
    # 去重保序
    seen: set[str] = set()
    out = [x for x in want if not (x in seen or seen.add(x))]
    return out[:3]


def _fmt_num(v: float) -> str:
    """人类可读的数值（世界银行 GDP 是 1.9e13 量级，直接打印会变科学计数法）"""
    a = abs(v)
    if a >= 1e12:
        return f"{v / 1e12:,.2f}万亿"
    if a >= 1e8:
        return f"{v / 1e8:,.2f}亿"
    if a >= 1e4:
        return f"{v:,.0f}"
    return f"{v:,.4g}"


def _fmt_series(pairs: list[tuple[str, float]], unit: str, name: str, src: str) -> str:
    body = "；".join(f"{y}年 {_fmt_num(v)}" for y, v in pairs)
    return f"{name}（{src}，单位：{unit}）—— {body}"


def fetch_worldbank(country: str = "CHN", indicators: list[str] | None = None, years: int = 6) -> list[dict[str, Any]]:
    """World Bank 指标年序列（结构化，可直接算增速/对锚）"""
    from core.authority_fetch import _cached, _WB_CACHE, _get, enabled
    from datetime import date as _date

    if not enabled():
        return []
    inds = indicators or ["NY.GDP.MKTP.CD"]
    y1 = _date.today().year
    y0 = y1 - max(3, int(years))
    out: list[dict[str, Any]] = []
    _names = {k: (v, u) for k, v, u in _WB_IND}
    for ind in inds[:2]:
        key = f"wb|{country}|{ind}|{y0}"
        _cn, _unit = _names.get(ind, (ind, ""))
        items = _cached(_WB_CACHE, key, lambda i=ind, c=_cn, u=_unit: _wb_one(country, i, y0, y1, c, u))
        out.extend(items)
    return out


def _wb_one(country: str, ind: str, y0: int, y1: int, cn: str, unit: str = "") -> list[dict[str, Any]]:
    from core.authority_fetch import _get

    try:
        r = _get(_WB.format(country=country, ind=ind, y0=y0, y1=y1))
        if r.status_code != 200:
            return []
        j = r.json() or []
        if not isinstance(j, list) or len(j) < 2:
            return []
        meta, rows = (j[0] or {}), (j[1] or [])
        pairs: list[tuple[str, float]] = []
        for row in rows:
            v = row.get("value")
            if v is None:
                continue
            try:
                pairs.append((str(row.get("date") or ""), float(v)))
            except Exception:
                continue
        if not pairs:
            return []
        pairs.sort(key=lambda kv: kv[0])
        pairs = pairs[-6:]
        cname = ((rows[0] or {}).get("country") or {}).get("value") or country
        url = _WB.format(country=country, ind=ind, y0=y0, y1=y1).replace("&format=json", "&format=json")
        _u = unit or "（原始单位）"
        txt = _fmt_series(pairs, _u, f"{cname} {cn}", "World Bank")
        # 派生增速：**判据在 `core/series_math.py`，三处实现共用同一条规则。**
        # 病案（2026-09-15）：这里原先把水平值公式无条件套到所有序列上。
        # 本函数的入参里有一半指标本身就是**比率**（`_WB_IND` 里 5/8 的单位是 `%`：
        # GDP年增速/城镇化率/通胀率/服务业占比/互联网普及率），于是
        # 「中国 GDP 年增速 2.34→4.96」被算成 CAGR≈16.2%（正确约 4.9%），
        # 并带着"（本地计算）"的权威标签印进报告。
        # 更糟的是可复算包 `compute.py` 用的是同一个错公式，会**复现**这个错数 →
        # 第三方跑完得到"一致"，**验证机制为错误背书**。
        cagr = ""
        try:
            from core.series_math import derive_growth

            _label, _g = derive_growth(pairs, cn, _u if unit else "")
            if _g is not None:
                cagr = f"；{_label}≈{_g:.1f}%（本地计算）"
        except Exception:
            cagr = ""
        return [
            {
                "title": f"{cname} {cn}（World Bank 年度时序）"[:150],
                "url": url,
                "date": pairs[-1][0],
                "publisher": "World Bank",
                "snippet": _fmt_series(pairs, _u, f"{cname}{cn}", "World Bank")[:200],
                "content": txt + cagr + f"。数据源：世界银行公开 API（库更新 {meta.get('lastupdated', '见源站')}）。",
                "source_type": "authority",
            }
        ]
    except Exception as e:
        logger.info("权威源·World Bank 失败(降级)：%s", e)
        return []


def fetch_imf(country: str = "CHN", indicators: list[str] | None = None) -> list[dict[str, Any]]:
    """IMF DataMapper 指标（**含未来预测值**——研究里的预测锚）"""
    from core.authority_fetch import _cached, _WB_CACHE, _get, enabled

    if not enabled():
        return []
    inds = indicators or ["NGDPD"]
    _names = {k: n for k, n, _u in _IMF_IND}
    out: list[dict[str, Any]] = []
    for ind in inds[:2]:
        if ind not in _names:
            continue
        out.extend(
            _cached(_WB_CACHE, f"imf|{country}|{ind}", lambda i=ind: _imf_one(country, i, _names[i]))
        )
    return out


def _imf_one(country: str, ind: str, cn: str) -> list[dict[str, Any]]:
    from core.authority_fetch import _get
    from datetime import date as _date

    try:
        r = _get(_IMF.format(ind=ind))
        if r.status_code != 200:
            return []
        vals = ((r.json() or {}).get("values") or {}).get(ind) or {}
        series = vals.get(country) or {}
        if not series:
            return []
        pairs: list[tuple[str, float]] = []
        for y, v in series.items():
            try:
                if v is None:
                    continue
                pairs.append((str(y), float(v)))
            except Exception:
                continue
        if len(pairs) < 2:
            return []
        pairs.sort(key=lambda kv: kv[0])
        cur = _date.today().year
        hist = [p for p in pairs if int(p[0]) <= cur][-5:]
        fut = [p for p in pairs if int(p[0]) > cur][:5]
        cname = next((k for k, v in _COUNTRY.items() if v == country), country)
        url = _IMF.format(ind=ind)
        # 单位**按指标取**，查不到给空串——回落成"十亿美元"等于给每个新指标
        # 都印一个看似合理、实则张冠李戴的单位（人口/增速都这么被印错过）。
        _unit = _IMF_UNITS.get(ind, "")
        body = _fmt_series(hist or pairs[-5:], _unit, f"{cname} {cn}", "IMF WEO")
        if fut:
            body += "。**预测值**：" + "；".join(f"{y}年 {_fmt_num(v)}" for y, v in fut)
        return [
            {
                "title": f"{cname} {cn}（IMF WEO，含预测）"[:150],
                "url": url,
                "date": (hist or pairs)[-1][0],
                "publisher": "IMF",
                "snippet": body[:200],
                "content": body + "。数据源：IMF World Economic Outlook 公开 API（含官方预测值，可作预测锚）。",
                "source_type": "authority",
            }
        ]
    except Exception as e:
        logger.info("权威源·IMF 失败(降级)：%s", e)
        return []


def intl_wanted(text: str) -> bool:
    """该问题是否需要国际对照口径（出海/跨境/各国对比）"""
    return any(t in (text or "") for t in _INTL_TRIGGERS)


def fetch_intl(question: str, subtask_desc: str = "", queries: list[str] | None = None) -> list[dict[str, Any]]:
    """国际组织统计调度：挑国家 + 挑指标 → World Bank + IMF（含预测）"""
    text = f"{question or ''} {subtask_desc or ''} " + " ".join(queries or [])
    intl = intl_wanted(text)
    country = _pick_country(text)
    inds = _pick_indicators(text, intl)
    wb = [i for i in inds if i.startswith(("NY.", "SP.", "FP.", "NV.", "IT."))]
    imf = [i for i in inds if i in _IMF_UNITS]
    out: list[dict[str, Any]] = []
    out.extend(fetch_worldbank(country, wb or ["NY.GDP.MKTP.CD"], years=6))
    out.extend(fetch_imf(country, imf or ["NGDPD"]))
    return out


def describe() -> str:
    return (
        "国际组织开放统计：**World Bank**（200+ 经济体宏观年序列，含库更新时间）+ "
        "**IMF WEO**（含**官方未来预测值**，作研究预测锚）。两站均免 key 公开 API、"
        "robots 无禁；沿用 per-host ≥1s 限速，只取聚合年度值。"
        "给「出海/跨境/国际对比」类问题提供别国官方口径——此前只能引二手研报。"
    )
