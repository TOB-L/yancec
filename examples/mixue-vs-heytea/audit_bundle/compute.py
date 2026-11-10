#!/usr/bin/env python3
"""研策 · 可复算包计算脚本（独立运行，只用 Python 标准库）

用法：  python compute.py

做什么：从 raw/ 里的**原始取证记录**重算报告用到的派生数字——
  1. 权威源带出的年度序列 → 重算增速（报告里这些是"本地计算"，此处独立复现）
  2. 来源结构占比（政府/机构/媒体）→ 与 verify.py 交叉印证

为什么重要：报告里的"同比 +3.5%""CAGR 6.4%"如果只用 LLM 生成，第三方无法验证。
本脚本让评审**自己把同样的算术跑一遍**，对得上就是真的。

======================================================================
本脚本 2026-09-15 修的两个缺陷（都属"看起来在跑、其实什么也没做"）
======================================================================

**缺陷一：读错了键名，所以从来没算出过任何东西。**
  原先只读 `content` / `snippet` 两个键，而 `raw/authorities.json` 的 schema 是
  `source / title / url / data_period / caliber / query / excerpt`——**两个键都不存在**。
  后果：文本恒为空 → 每个包都"检出 0 条可复算时序"，而脚本**看起来跑通了**。
  实测：311 个已生成的送审包中 **305 个**在正确的键下含可解析的年度序列，全部被跳过。
  现在改为读多个候选键，并在结尾**打印本次实际用了哪些键、记录里真实存在哪些键**。

**缺陷二：对"增速序列"套了"水平序列"的公式。**
  `(末/初)^(1/n)-1` 是给存量序列（金额、人数、门店数）用的。
  本包的权威源里有一半指标本身就是比率——`国家 GDP 年增速` 的单位就是 `%`。
  把 2.34→8.57→3.134→5.416→4.958→4.96 当作存量序列算，得到 CAGR≈16.2%；
  正确做法是各年**增长因子**的几何平均，约 4.9%。
  **这两个缺陷互相掩盖**：键名错让脚本什么都算不出，所以公式错从没暴露；
  只修键名而不修公式，脚本会开始输出错数。所以两处必须同时修。

  ⚠️ 本脚本必须能脱离本项目独立运行，所以这里自带一份算法副本，
     而不是 import `core/series_math.py`。两份实现由项目的
     `_fulltest.py` 一致性用例锁住：同一组序列必须给出同一个数。
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")

# raw/authorities.json 的**实际**键（含历史别名，防 schema 再变时静默归零）
# ⚠️ 只取**第一个非空**的键作为正文，**不拼接**：`excerpt` 与 `title` 往往含有
#    同一段序列，拼起来会让每个年份出现两次 → 增长因子个数翻倍 → 增速算错。
#    （实测：15.00→19.50 万亿的 6 点序列被拼成 8 点，CAGR 从 5.4% 变成 17.6%。）
_BODY_KEYS = ("excerpt", "content", "snippet", "query")

# 与报告口径一致：从文本里抓"YYYY年 数值"序列
# ⚠️ 必须排除「2026年8月份」这类**日期**被误读成"2026 年的值是 8"——
#    实测踩过：一个月份把序列尾点污染成 8，算出 -53% 的假 CAGR。
#    审计包里出现这种错比不做审计更糟（会被当成"你们自己都算错"）。
_YEAR_NUM = re.compile(r"(20\d{2})\s*年\s*([\d,]+(?:\.\d+)?)")
_PCT = re.compile(r"([-+]?\d+\.?\d*)\s*%")
_DATE_TAIL = ("月", "日", "季度", "Q")

# 增速序列的判据（**逐字副本**，与 core/series_math.py 保持一致，
# 由 _fulltest 的一致性用例锁住——本文件要能脱离项目独立运行，所以不能 import）
_RATE_UNITS = ("个百分点", "pp", "ppt")
_RATE_HINTS = ("增速", "增长率", "复合增长", "同比", "环比", "涨幅", "通胀率", "cpi", "cagr")
_LEVEL_PCT_HINTS = ("城镇化率", "普及率", "渗透率", "覆盖率", "占有率",
                    "占比", "占", "比例", "份额", "比率", "率")


def classify_series(name="", unit=""):
    """值本身是变化率（"rate"）/ 水平（"level"）/ 判不出（"unknown"）。

    ⚠️ **`%` 不是"这是增速"的证据**：城镇化率 63.9% 是**水平**，不是"增长了 63.9%"。
    本条曾写反，实测把城镇化率算成「区间年均增速≈50%」——
    而那是个会被本脚本**复现**的错数，等于独立复算为错误背书。判据顺序即优先级。
    """
    _n = (name or "").strip()
    _low = _n.lower()
    _u = (unit or "").strip()
    if _u in _RATE_UNITS:
        return "rate"
    if any(h in _low for h in _RATE_HINTS):
        return "rate"
    if any(h in _n for h in _LEVEL_PCT_HINTS):
        return "level"
    if _u in ("%", "％"):
        return "unknown"
    if not _n and not _u:
        return "unknown"
    return "level"


def _num(s):
    try:
        return float(s.replace(",", ""))
    except Exception:
        return None


def _unit_declared(body):
    """正文里声明的单位（`（…，单位：%）`）。权威源每条都带，是最硬的那条判据。"""
    t = body or ""
    for u in ("%", "％", "个百分点"):
        if ("单位：" + u) in t or ("单位:" + u) in t:
            return u
    return ""


def _classify(title, body):
    """(标题, 正文) → "rate" / "level" / "unknown"。

    判据优先级（**测试驱动定下来的**，不是想当然）：
      1. **正文里的单位声明**：`（…，单位：%）` 之类，权威源每条都带，覆盖面最广。
      2. 指标名兜底：只在 **title** 里找线索。
         ⚠️ **不能在整个正文里找**——报告的 excerpt 里含有我们自己写的那句
         「区间年复合增速 CAGR≈5.4%（本地计算）」，全文搜索会把**存量序列**
         （GDP 现价美元）也判成增速序列，于是对金额做几何平均，得到 17.6% 这种数。
         （这正是第一版改法引入的回归，被真实送审包测出来了。）
      3. **判不出就判不出**——不蒙一个公式上去。
    """
    return classify_series(title or "", _unit_declared(body))


def _is_rate(title, body):
    """兼容旧签名：只在"确实是增速序列"时为 True（判不出按 False）。"""
    return _classify(title, body) == "rate"


def _series_from(body, keep_small=False):
    """从文本抓 (年份, 数值) 序列；排除日期式误匹配与裸 1-2 位整数

    keep_small=True 时**保留 1-2 位整数**——只在已判定为增速序列时使用：
    增速本来就是"5""9"这种小整数，用存量序列的过滤规则会把它们全丢掉。
    """
    out = []
    for m in _YEAR_NUM.finditer(body):
        tail = body[m.end():m.end() + 1]
        if tail in _DATE_TAIL:
            continue                      # 「2026年8月」是日期，不是"2026 年的值"
        raw = m.group(2)
        if not keep_small and "." not in raw and "," not in raw and len(raw) <= 2:
            continue                      # 裸 1-2 位整数多为月/序号，不是量级数据
        n = _num(raw)
        if n is not None:
            out.append((m.group(1), n))
    return out


def cagr_level(first, last, years):
    """**存量序列**的区间年复合增速（%）"""
    if years <= 0 or first <= 0 or last <= 0:
        return None
    return ((last / first) ** (1.0 / years) - 1) * 100.0


def geo_mean_rate(values):
    """**增速序列**的区间年均增速（%）= 各年增长因子的几何平均

    少于 2 个点、或出现 ≤ -100% 的不可解释值 → None。
    **不返回 0**——0 会被读成"年均零增长"，那是个结论，不是缺席。
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


def main():
    if not os.path.isdir(RAW):
        print("未找到 raw/ 目录"); return 1
    files = sorted(f for f in os.listdir(RAW) if f.endswith(".json"))
    print("=" * 78)
    print("研策 · 可复算包独立复算（原始记录 → 派生数字）")
    print("=" * 78)
    print("原始记录文件：%d 个" % len(files))
    n_series = 0
    n_records = 0
    seen_keys = set()
    for fn in files:
        try:
            with open(os.path.join(RAW, fn), "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print("  [跳过] %s（%s）" % (fn, e)); continue
        items = data if isinstance(data, list) else data.get("items") or []
        for it in items:
            if not isinstance(it, dict):
                continue
            n_records += 1
            seen_keys.update(it.keys())
            _title = str(it.get("title") or "")
            # 正文取**第一个非空**的候选键（不拼接，见 _BODY_KEYS 处的说明）
            body = ""
            for _k in _BODY_KEYS:
                _v = str(it.get(_k) or "").strip()
                if _v:
                    body = _v
                    break
            _rate = _is_rate(_title, body)
            _kind = _classify(_title, body)
            # 百分数序列的取值天然是 1–2 位数（2.34 / 36.2 / 8），必须 keep_small：
            # 否则「城镇化率 36 / 44 / 51」会被"裸 1-2 位整数多为月/序号"那条规则**整段丢掉**，
            # 于是水平-%序列在复算里表现为"没检出时序"——又是一次"没数被读成没有"。
            _keep_small = _rate or bool(_unit_declared(body))
            pairs = _series_from(body, keep_small=_keep_small)
            if len(pairs) >= 2:
                # 同年去重：同一年份在正文里可能出现两次（标题与正文各一遍，
                # 或表格里"指数值|增速"成对出现）。重复点会让增长因子个数翻倍、
                # 增速直接算错（实测 6 点被算成 8 点 → 5.4% 变 17.6%）。
                _by_year = {}
                for _y, _v in pairs:
                    _by_year[_y] = _v
                pairs = sorted(_by_year.items(), key=lambda kv: kv[0])
                (y0, v0), (y1, v1) = pairs[0], pairs[-1]
                try:
                    span = int(y1) - int(y0)
                except Exception:
                    span = 0
                n_series += 1
                print("\n  [%s]" % fn)
                print("    来源：%s" % (it.get("source") or it.get("publisher")
                                      or it.get("title") or "")[:60])
                print("    序列：%s" % "；".join("%s=%s" % (y, v) for y, v in pairs))
                if _kind == "unknown":
                    # **弃权要印出来**，不能悄悄滑进 CAGR 分支——那等于对一个
                    # "不知道是增速还是水平"的序列断言"它的年复合增速是 X%"。
                    print("    → **未能复算**：单位是 %，但指标名给不出"
                          "「这是增速序列还是水平序列」的线索。两条公式都可能把它算成"
                          "一个**看起来算过了**的错数（增速序列套水平公式 = 16.2% 那种），"
                          "故本次弃权——不给数，也不给 0。")
                elif _kind == "rate":
                    g = geo_mean_rate([v for _y, v in pairs])
                    if g is not None:
                        print("    → 独立复算 区间年均增速（几何平均）≈ %.1f%%"
                              "（%s→%s，%d 个增长因子）" % (g, y0, y1, len(pairs)))
                    else:
                        print("    → **未能复算**：该序列判为增速序列，但增长因子不可解释")
                else:
                    g = cagr_level(v0, v1, span)
                    if g is not None:
                        print("    → 独立复算 CAGR ≈ %.1f%%（%s→%s，%d 年）" % (g, y0, y1, span))
                    else:
                        print("    → **未能复算**：%s→%s 跨度 %d 年，端点非正或跨度为 0" % (y0, y1, span))
            # 报告里的同比是"本地计算"——此处把正文里的百分比重列出来供逐一核对
            for m in _PCT.findall(body)[:6]:
                print("    正文出现百分比：%s%%" % m)
    print("\n" + "=" * 78)
    print("独立复算完成：从 %d 条原始记录中检出 %d 条可复算时序。" % (n_records, n_series))
    if n_series == 0:
        # **「0 条」必须自己解释自己。** 本脚本曾经因为读错键名而常年输出 0，
        # 而输出长这样——"独立复算完成：检出 0 条"——读起来像"没有问题"。
        # 现在把原因直接印出来，让它不可能再被误读。
        print("")
        print("  ⚠ 未检出任何可复算时序。**这不等于没有问题**，它只说明本次没算出东西。")
        if n_records == 0:
            print("    原因：raw/ 里没有任何记录 —— 是「没有输入」，不是「算出来是 0」。")
        else:
            print("    原因：%d 条记录里，正文键下没有 >=2 个「YYYY年 数值」。" % n_records)
            print("    本次读取的键：%s" % "、".join(_BODY_KEYS))
            print("    记录里真实存在的键：%s" % ("、".join(sorted(seen_keys)) or "（无）"))
            _miss = [k for k in _BODY_KEYS if k not in seen_keys]
            if len(_miss) == len(_BODY_KEYS):
                print("    ★ 全部候选键都不存在 —— 说明 raw/ 的 schema 又变了，")
                print("      请把上面「记录里真实存在的键」加进本文件的 _BODY_KEYS。")
        print("    ⚠ 本包因此**不能**用来证明报告数字是算出来的。")
    else:
        print("把上面的增速与报告正文对照——一致即证明报告数字是算出来的，不是编的。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
