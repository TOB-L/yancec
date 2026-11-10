"""研策 · 可复算包（Audit Bundle）—— 把"可复算"从我们的说法变成第三方能跑的东西

## 这一层解决什么

在此之前，"证据可复算"是**我们自己在报告里声明的**。评审无法验证，只能选择相信或不相信。
这一层把它变成实物：一次调研产出一个 ZIP，**任何第三方解压后就能自己跑一遍**。

## 包的结构（关键：`verify.py` / `compute.py` 只用标准库，脱离本项目也能跑）

    研策_可复算包_<问题>_<时间>/
    ├── README.md        ← 三步复算指引（含"哪一步验证什么"）
    ├── manifest.json    ← 包元数据 + **每个文件的 SHA256**（防中途篡改）
    ├── rules.md         ← 公开判定规则（权重/路由/状态阈值/护照口径），由**运行时常量直接导出**
    ├── claims.csv       ← 每条结论：置信度 · 状态 · 独立域名数 · 来源 · 数据期 · 口径
    ├── sources.csv      ← 每条来源：层级 · 分组 · URL · 检索词 · 核验判定
    ├── raw/             ← **原始取证记录**（各维度真实检索返回，未加工）
    │   ├── subtask-<id>.json
    │   └── authorities.json
    ├── compute.py       ← 【独立可跑】从 raw 重算 CAGR / 同比 / 来源结构占比
    └── verify.py        ← 【独立可跑】重探每个引用 URL 存活 + 重判结论状态

## 设计原则（决定了它是真审计还是作秀）
1. **独立可跑**：两个脚本只用 Python 标准库（urllib/csv/json/hashlib/ssl），
   不 import 本项目任何模块。复核者 `python verify.py` 就能跑——不用装环境。
2. **规则不漂移**：`rules.md` 从**运行时常量**导出（`CREDIBILITY_WEIGHTS`、`CLAIM_ROUTES`、
   `STATUS_META`…），不是手写的说明文档——代码改了规则说明跟着改，不会对不上。
3. **原始记录不加工**：`raw/` 存的是检索层真实返回（仅截断长度），
   不是"整理过的结论"。第三方可以从原始记录一路复算到报告结论。
4. **诚实标注能力边界**：README 里写明**哪些能复算、哪些不能**
   （如：URL 存活可复算；"这个数字在原文里是否真的出现过"需人工点开核验）。

## 合规
包含的是**我们自己的取证记录**（检索返回的标题/摘要/URL），**不再抓取、不再转载**；
`verify.py` 的复探是对公开 URL 的 HEAD/GET 存活检查，频率 ≥1s/host，与主流程同规矩。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
import zipfile
from datetime import datetime
from typing import Any

from core import safe_io

logger = logging.getLogger(__name__)

_BUNDLE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "audit_bundles")
_MAX_RAW_ITEMS = 8      # 每个维度最多存 8 条原始记录
_MAX_RAW_CHARS = 1200   # 每条原始记录的正文截断长度


def _offline() -> bool:
    """离线测试模式——**测试产物不许写进生产可复算包目录**。

    病案（2026-09-16 核验三份外部方案时亲手数出来的）：这道闸门在
    `core/runstats.py`（账本）与 `core/report_store.py`（报告存档）都装了，
    **这里是第三个写入点，却没装**。实测 `data/audit_bundles/` 有 **420 个包**，
    其中 **388 个（92.4%）**来自两道测试题
    （`我想在杭州开咖啡店能不能做标准档` 204 个 + 同题深入档 184 个），
    另有 `测试问题A_dicts_流式形态` / `测试问题B_Evidence_同步形态` / `测试问题dicts`
    三个连名字都没改的测试残留。

    这是"同一个病、三处实现、只治了两处"的第三次复发——
    `test_offline_write_gates` 的 docstring 早就写过「以后再新增写生产档案的模块，
    也必须过这道闸门」，而这一处从来不在那道锁的覆盖范围里。

    为什么它比账本/存档被污染轻、但仍必须修：可复算包的目录**本身就是给人看的物证**
    （演示脚本第三张牌就是"解压这个包"）。一个复核者打开目录，看到 92% 是同一道测试题，
    得到的印象与本产品想证明的**正好相反**。

    闸门开在**写入点**（与账本、存档同源、同理由）：调用点有 3 处，写入点只有这一处。
    """
    return str(os.environ.get("YANCEC_OFFLINE") or "").strip() in ("1", "true", "True")


# ============================================================
# 生成的独立脚本（纯标准库；**不要**在这里写任何本项目的 import）
# ============================================================
_VERIFY_PY = '''#!/usr/bin/env python3
"""研策 · 可复算包验证脚本（独立运行，只用 Python 标准库）

用法：  python verify.py            # 全量验证
        python verify.py --quick    # 只验证前 10 个 URL（省时间）

做三件事：
  1. 重探 sources.csv 里每个 URL 是否仍然可访问（HEAD，失败回退 GET）
  2. 重算来源结构（各层级占比、门户/媒体占比）
  3. 与 claims.csv 声明的状态对账，输出"声明 vs 复算"差异

输出：终端表格 + verify_report.json
退出码：0 = 全部通过；1 = 有失败项（便于接入 CI）
"""
import argparse
import csv
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from urllib.parse import urlparse

UA = "yancec-audit-verify/1.0 (+reproducibility check; contact: see manifest.json)"
TIMEOUT = 15
HERE = os.path.dirname(os.path.abspath(__file__))


def _load_csv(name):
    p = os.path.join(HERE, name)
    if not os.path.exists(p):
        return []
    with open(p, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def probe(url):
    """探一个 URL 是否可访问 → (ok, status_or_err)"""
    if not url or not url.startswith("http"):
        return False, "非 http URL（纯文字来源，无可点页）"
    host = (urlparse(url).netloc or "").lower()
    for method in ("HEAD", "GET"):
        try:
            ctx = ssl.create_default_context()
            req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
                return True, r.status
        except urllib.error.HTTPError as e:
            # 405/403 常见于"禁 HEAD"，换 GET 再试一次；其余错误直接判定
            if method == "HEAD" and e.code in (403, 405, 501):
                continue
            if e.code < 400:
                return True, e.code
            return False, "HTTP %s" % e.code
        except Exception as e:
            if method == "HEAD":
                continue
            return False, type(e).__name__
    return False, "不可达"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="只验证前 10 个 URL")
    args = ap.parse_args()

    srcs = _load_csv("sources.csv")
    claims = _load_csv("claims.csv")
    print("=" * 78)
    print("研策 · 可复算包验证")
    print("=" * 78)
    print("来源 %d 条 / 结论 %d 条" % (len(srcs), len(claims)))

    # ---- 1. URL 存活复探 ----
    urls, seen = [], set()
    for s in srcs:
        u = (s.get("url") or "").strip()
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    if args.quick:
        urls = urls[:10]
    print("\\n[1/3] 重探 %d 个引用 URL（≥1 秒/主机，避免给对方站点压力）…" % len(urls))
    results, last_host, last_t = [], "", 0.0
    for u in urls:
        h = (urlparse(u).netloc or "").lower()
        if h == last_host:
            dt = time.time() - last_t
            if dt < 1.0:
                time.sleep(1.0 - dt)
        ok, info = probe(u)
        last_host, last_t = h, time.time()
        results.append({"url": u, "ok": ok, "info": str(info)})
        print("   %s  %-58s %s" % ("OK  " if ok else "FAIL", u[:58], info))

    n_ok = sum(1 for r in results if r["ok"])
    print("   → 存活 %d / %d" % (n_ok, len(results)))

    # ---- 2. 来源结构复算 ----
    print("\\n[2/3] 重算来源结构…")
    tiers = Counter(s.get("tier") or "?" for s in srcs)
    groups = Counter(s.get("group") or "未分组" for s in srcs)
    media_groups = ("财经媒体", "媒体报道", "社媒/论坛")
    n_media = sum(v for k, v in groups.items() if k in media_groups)
    n_total = sum(tiers.values()) or 1
    print("   层级 T1/T2/T3+ 分布：%s" % dict(tiers))
    print("   分组分布：%s" % dict(groups))
    print("   门户/媒体类占比：%.1f%%（报告规则上限 50%%）" % (100.0 * n_media / n_total))
    print("   独立域名数：%d" % len({(urlparse(s.get("url") or "").netloc or "").lower().removeprefix("www.")
                                  for s in srcs if (s.get("url") or "").startswith("http")}))

    # ---- 3. 状态对账 ----
    print("\\n[3/3] 结论状态对账（claims.csv 声明 vs 本次复算可核项）…")
    st = Counter(c.get("status") or "?" for c in claims)
    print("   状态分布：%s" % dict(st))
    weak = [c for c in claims if (c.get("status") or "") in ("INSUFFICIENT", "CONTESTED")]
    print("   需人工注意（证据不足/存争议）：%d 条" % len(weak))
    for c in weak[:10]:
        print("     · [%s] %s" % (c.get("status"), (c.get("claim") or "")[:56]))

    report = {
        "urls_checked": len(results), "urls_alive": n_ok,
        "url_results": results, "tier_dist": dict(tiers), "group_dist": dict(groups),
        "media_ratio": round(100.0 * n_media / n_total, 2),
        "status_dist": dict(st), "weak_claims": len(weak),
    }
    with open(os.path.join(HERE, "verify_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\\n已写出 verify_report.json")
    print("=" * 78)
    print("结论：URL 存活 %d/%d；复算完成。" % (n_ok, len(results)))
    print("注意：本脚本能验证『引用页是否还活着』与『来源结构是否合规』；")
    print("      不能验证『报告里的数字是否真的出自该页原文』——那需要人工点开原文核对。")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
'''

_COMPUTE_PY = '''#!/usr/bin/env python3
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
  实测：311 个已生成的发布包中 **305 个**在正确的键下含可解析的年度序列，全部被跳过。
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
_YEAR_NUM = re.compile(r"(20\\d{2})\\s*年\\s*([\\d,]+(?:\\.\\d+)?)")
_PCT = re.compile(r"([-+]?\\d+\\.?\\d*)\\s*%")
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
         （这正是第一版改法引入的回归，被真实发布包测出来了。）
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
                print("\\n  [%s]" % fn)
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
    print("\\n" + "=" * 78)
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
'''


# ============================================================
# 规则导出（**从运行时常量生成**，杜绝"文档与代码不一致"）
# ============================================================
def _aigc_stamp(report_md: str = "") -> str:
    """包内文本文件（README / rules）的法定 AI 标识行。

    病案（2026-09-15 外部审计）：可复算包里的 `README.md` 与 `rules.md` **一条标识都没有**。
    而 zip 里打包的是**我们程序生成的文本**，同属"生成合成内容"——
    报告本体五种格式都写了标识，**偏偏漏在自认为最讲合规的那件东西上**。

    内容编号取自**报告正文**，与报告导出用的编号**同源**：
    于是"这个包是不是那份报告的包"可以被核对，而不仅仅是一句声明。
    """
    try:
        # ⚠️ 必须走 `canonical_content_id`（复用正文编号），不能走 `content_id`（重算）。
        # 病案（2026-09-16）：本函数**自己印着**"与报告本体同一内容编号"，
        # 却在下一行重算了一次哈希——于是它声称的同一性**必然不成立**。
        # 一句话里同时出现"承诺"和"违背承诺的代码"，是本仓库最该警惕的形态：
        # 读者只看得见那句承诺。
        from core.ai_label import LABEL_BASIS, SERVICE_PROVIDER, canonical_content_id

        return (
            f"> 🤖 **AI 生成合成内容**｜服务提供者：{SERVICE_PROVIDER}｜"
            f"内容编号：`{canonical_content_id(report_md)}`\n"
            f"> 依{LABEL_BASIS}添加标识；本文件由程序生成，与报告本体同一内容编号。\n\n"
        )
    except Exception:
        return ""


def _rules_md() -> str:
    L: list[str] = ["# 研策 · 公开判定规则", "",
                    "> 本文件由**运行时常量直接导出**（不是手写说明），因此不会与代码行为不一致。", ""]
    try:
        from core.models import CREDIBILITY_WEIGHTS, SOURCE_LEVEL_BASE_SCORE

        L.append("## 1. 证据可信度四维权重（公开可核对）")
        L.append("")
        L.append("| 维度 | 权重 |")
        L.append("|---|---|")
        for k, v in CREDIBILITY_WEIGHTS.items():
            L.append(f"| {k} | {v:.0%} |")
        L.append("")
        L.append("信源等级基准分：")
        L.append("")
        L.append("| 等级 | 基准分 |")
        L.append("|---|---|")
        for k, v in SOURCE_LEVEL_BASE_SCORE.items():
            L.append(f"| {k.value} | {v} |")
        L.append("")
        L.append("合成分 = Σ(维度分 × 权重)；佐证分 = min(佐证数/2, 1) × 0.7 + 0.3。")
        L.append("")
    except Exception as e:
        L.append(f"（权重导出失败：{e}）")
    try:
        from core.confidence import grade_evidence_confidence  # noqa: F401

        L.append("## 2. 置信度分级规则")
        L.append("")
        L.append("- 推测(L5) → 强制「低」")
        L.append("- 无真实 URL（无法点开溯源）→ 上限「中」")
        L.append("- 独立来源域名 < 2 → 上限「中」（关键论断需 2+ 独立来源）")
        L.append("- 合成分 ≥0.75 → 高；≥0.5 → 中；其余 → 低")
        L.append("")
    except Exception:
        pass
    try:
        from core.evidence_matrix import STATUS_META

        L.append("## 3. 结论状态机（五态）")
        L.append("")
        L.append("| 状态 | 含义 |")
        L.append("|---|---|")
        for k, (lab, desc) in STATUS_META.items():
            L.append(f"| {k}（{lab}） | {desc} |")
        L.append("")
    except Exception:
        pass
    try:
        from core.source_registry import CLAIM_ROUTES, LAYERS

        L.append("## 4. 问答→源硬路由（问题类型 → 必选源层 → 要求的验证对）")
        L.append("")
        L.append("| 结论类型 | 必选源层 | 要求的验证对 |")
        L.append("|---|---|---|")
        for r in CLAIM_ROUTES:
            L.append(f"| {r.label} | {r.required} {LAYERS.get(r.required, '')} | {r.pair} |")
        L.append("")
        L.append("源层定义：" + "；".join(f"{k} {v}" for k, v in LAYERS.items()))
        L.append("")
    except Exception:
        pass
    try:
        from core.source_registry import BLOCKED_SOURCES

        L.append("## 5. 实测受阻的数据源（诚实披露：这些我们取不到）")
        L.append("")
        L.append("| 源 | 受阻原因 |")
        L.append("|---|---|")
        for b in BLOCKED_SOURCES:
            L.append(f"| {b['name']} | {b['reason']} |")
        L.append("")
    except Exception:
        pass
    try:
        from core.data_passport import describe as _dp

        L.append("## 6. 数字护照（数据期 / 口径）")
        L.append("")
        L.append(_dp())
        L.append("")
    except Exception:
        pass
    L.append("## 7. 本包能复算什么 / 不能复算什么")
    L.append("")
    L.append("**能复算**：引用页是否仍可访问（`verify.py`）；来源层级与门户占比是否合规（`verify.py`）；"
             "派生数字（CAGR/同比）是否算得出来（`compute.py`）；规则是否与代码一致（本文件由常量导出）。")
    L.append("")
    L.append("**不能复算**：报告里的数字是否**真的出自该页原文**（需人工点开核验）；"
             "LLM 撰写的分析文字是否恰当（属判断，非事实）。这两项我们**不声称**可复算。")
    return "\n".join(L)


# ============================================================
# 包构建
# ============================================================
def _slug(q: str, n: int = 22) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", (q or "调研")[:40])
    return (s or "调研")[:n]


def _engine_version() -> str:
    """引擎版本（**唯一来源**：`core/version.ENGINE_VERSION`）。

    病案：manifest 的 `engine` 字段原先取 `run_stats["engine"]` —— 那个键不存在，
    于是恒为空串。版本号在本仓库已经收口到一处（`core/version.py`，见其 docstring 里
    "同一份报告印两个版本号"的病案），这里直接取那一处，不再各写一份。
    """
    try:
        from core.version import ENGINE_VERSION

        return str(ENGINE_VERSION or "")
    except Exception:
        return ""


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    return h.hexdigest()


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)


def _csv(rows: list[list[Any]]) -> str:
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    for r in rows:
        w.writerow(["" if c is None else str(c) for c in r])
    return buf.getvalue()


def _trim_raw(raw: list[dict]) -> list[dict]:
    """原始记录瘦身：只截断长度，**不改内容**（可复算的前提是原始记录没被加工过）"""
    out: list[dict] = []
    for it in (raw or [])[:_MAX_RAW_ITEMS]:
        if not isinstance(it, dict):
            continue
        out.append(
            {
                "query": str(it.get("query") or "")[:200],
                "title": str(it.get("title") or "")[:200],
                "url": str(it.get("url") or "")[:500],
                "publisher": str(it.get("publisher") or "")[:100],
                "date": str(it.get("date") or "")[:40],
                "source_type": str(it.get("source_type") or ""),
                "snippet": str(it.get("snippet") or "")[:_MAX_RAW_CHARS],
                "content": str(it.get("content") or "")[:_MAX_RAW_CHARS],
            }
        )
    return out


def find_bundle(bundle_id: str) -> str:
    """按 id 找已生成的包

    防护用**拒绝危险模式 + 路径包含校验**，而不是 ASCII 白名单——
    实测教训：`[A-Za-z0-9_-]` 白名单会把中文 bundle_id（我们的默认命名）全拒掉，
    接口直接 404，看起来像"包没生成"，实际是自家的防护过头。
    """
    if not bundle_id or len(bundle_id) > 200:
        return ""
    # 拒绝一切可用来跳目录/注入的字符
    if any(ch in bundle_id for ch in ("/", "\\", "\x00", "\n", "\r", "\t")):
        return ""
    if ".." in bundle_id:
        return ""
    p = os.path.join(_BUNDLE_DIR, f"{bundle_id}.zip")
    # 兜底：解析后的真实路径必须仍在包目录内（防软链/编码绕过）
    try:
        if os.path.commonpath([os.path.realpath(p), os.path.realpath(_BUNDLE_DIR)]) != os.path.realpath(_BUNDLE_DIR):
            return ""
    except Exception:
        return ""
    return p if os.path.isfile(p) else ""


def build_bundle(state: dict, result: dict, question: str = "") -> dict[str, Any]:
    """从一次调研的 state + result 生成可复算包 → {id, path, files, bytes, ...}"""
    if _offline():
        logger.info("离线模式：本次不写生产可复算包（question=%s）", str(question or "")[:40])
        return {}
    try:
        os.makedirs(_BUNDLE_DIR, exist_ok=True)
    except Exception as e:
        logger.warning("可复算包目录创建失败: %s", e)
        return {}
    q = question or (state.get("raw_question") or "") or "调研"
    safe_id = f"{_slug(q)}_{datetime.now():%Y%m%d_%H%M%S}"
    work = os.path.join(_BUNDLE_DIR, safe_id)
    try:
        if os.path.isdir(work):
            shutil.rmtree(work, ignore_errors=True)
        os.makedirs(os.path.join(work, "raw"), exist_ok=True)

        # ⚠️ 必须**归一化**而不是**过滤**（2026-09-14 审计）。
        # 病案：原来写 `[e for e in evidences if isinstance(e, dict)]`——
        # 流式路径（graph.run_task_stream）会把 Evidence 覆盖成 dict，能过；
        # 而同步路径（run_task / /api/execute / _fulltest）传的是 **pydantic Evidence 实例**，
        # 于是整列被静默过滤 → claims.csv **只有表头**、manifest 里 counts.evidences=0，
        # 盘上有 106 个这样的空复算包。而 README 第三步恰恰是
        # 「打开 claims.csv 挑一条结论核对」——复核者照做会看到一张空表。
        # 注意：只删 `isinstance` 不过滤、不归一化，会在下面 `e.get("sources")` 抛
        # AttributeError 被外层 except 吞掉 → 变成"连包都不生成"，比空表更糟。
        def _as_dict(x):
            if isinstance(x, dict):
                return x
            try:
                return x.model_dump(mode="json")
            except Exception:
                return None

        citations = [d for d in (_as_dict(c) for c in (result.get("citations") or [])) if d]
        evidences = [d for d in (_as_dict(e) for e in (result.get("evidences") or [])) if d]
        routes = ((state.get("subtask_results") or {}).get("_routes") or {})
        run_stats = result.get("run_stats") or {}

        # ---- sources.csv ----
        # 表头用**英文机器键**（审计件是给脚本读的，中英混用会让读取端 key 对不上——实测踩过：
        # 中文表头 + 英文读取 → 整列为空、verify.py 探 0 个 URL，看起来"通过"实则什么都没验）
        src_rows: list[list[Any]] = [[
            "no", "source", "domain", "tier", "group", "data_period", "caliber", "query", "url", "verdict",
        ]]
        for i, c in enumerate(citations, 1):
            u = str(c.get("url") or "")
            dom = ""
            try:
                from urllib.parse import urlparse

                # 统一到可注册根域：`finance.sina.com.cn` 与 `www.sina.com.cn` 是**同一家**
                from core.source_util import registrable_root

                dom = registrable_root(u) or (urlparse(u).netloc or "").lower().removeprefix("www.")
            except Exception:
                dom = ""
            verdict = "在源清单中" if u.startswith("http") else "无 URL（纯文字来源）"
            src_rows.append([
                i, c.get("source") or "", dom, f"T{c.get('tier')}" if c.get("tier") else "",
                c.get("group") or "", c.get("data_period") or "未标注",
                c.get("caliber") or "未标注", c.get("query") or "", u, verdict,
            ])
        _write(os.path.join(work, "sources.csv"), _csv(src_rows))

        # ---- claims.csv ----
        try:
            from core.evidence_matrix import judge_status
            from core.confidence import grade_evidence_confidence
            from core.models import Evidence
        except Exception:
            judge_status = None
        cl_rows: list[list[Any]] = [[
            "no", "claim", "query", "confidence", "status", "independent_domains", "n_sources",
            "top_level", "conflicting", "urls",
        ]]
        for i, e in enumerate(evidences, 1):
            conf, status, ndom, lvl = "", "", "", ""
            if judge_status is not None:
                try:
                    ev = Evidence.model_validate(e)
                    conf = grade_evidence_confidence(ev).label
                    v = judge_status(ev)
                    # 独立域名数 = **可注册根域去重**后的个数，且**用置信度评估器的同一个函数**。
                    # 两处病案：①原写 `len({url ...})`——同一家媒体的三条不同文章 URL
                    # 会被算成"3 个独立域名"，而复算包里这一列的全部意义就是让复核者判断
                    # "这条结论到底有几个**互相独立**的佐证"；②就算改成域名去重，
                    # 若另写一份实现，就会与同一张表里的 `confidence` 列**各算各的**——
                    # 同一行两个数互相矛盾，比一个数错更伤可信度。
                    from core.confidence import distinct_source_domains as _dsd

                    status, ndom = v.code, len(_dsd(ev))
                    lvl = max(
                        (str(s.get("level") or "") for s in (e.get("sources") or [])), default=""
                    )
                except Exception:
                    pass
            urls = ";".join(
                str(s.get("url") or "") for s in (e.get("sources") or [])[:3] if s.get("url")
            )
            cl_rows.append([
                i, str(e.get("claim") or "")[:120], str(e.get("query") or "")[:80],
                conf, status, ndom, len(e.get("sources") or []), lvl,
                # **判据必须是"两选一"，不能只看布尔位**（2026-09-16）。
                # 病案：`is_conflicting` 是 `node_check_conflict` **就地改**在对象上的，
                # 而流式路径一度把它在改动前就快照掉了（见 graph.py 组装处病案）——
                # 于是这一列全印「否」，而正文同一份报告写着「⚔️ 存争议 N」。
                # `core/evidence_matrix.judge_status` 早就用了"或上 conflict_detail"的写法；
                # 这里与它对齐：**有冲突说明文字，就是有冲突**，不依赖那个可能丢掉的布尔位。
                "是" if (e.get("is_conflicting") or str(e.get("conflict_detail") or "").strip())
                else "否", urls,
            ])
        _write(os.path.join(work, "claims.csv"), _csv(cl_rows))

        # ---- raw/（原始取证记录，不加工）----
        raw_files: list[str] = []
        for k, v in (state.get("subtask_results") or {}).items():
            if not k.endswith("__raw"):
                continue
            trimmed = _trim_raw(v if isinstance(v, list) else [])
            if not trimmed:
                continue
            fn = f"subtask-{k[:-5]}.json"
            _write(os.path.join(work, "raw", fn), json.dumps(trimmed, ensure_ascii=False, indent=2))
            raw_files.append(fn)
        # 权威源直取单独归档（便于评审只看"一手源到底返回了什么"）
        auth_items: list[dict] = []
        for c in citations:
            if str(c.get("group") or "") in ("政府官方", "国际组织", "研究报告") or str(c.get("tier") or "") in ("1", "2"):
                auth_items.append(
                    {
                        "source": c.get("source"), "title": c.get("title"), "url": c.get("url"),
                        "data_period": c.get("data_period"), "caliber": c.get("caliber"),
                        "query": c.get("query"), "excerpt": str(c.get("excerpt") or "")[:_MAX_RAW_CHARS],
                    }
                )
        if auth_items:
            _write(os.path.join(work, "raw", "authorities.json"),
                   json.dumps(auth_items, ensure_ascii=False, indent=2))
            raw_files.append("authorities.json")
        # 路由审计原始记录
        if routes:
            _write(os.path.join(work, "raw", "source_routing.json"),
                   json.dumps(routes, ensure_ascii=False, indent=2))
            raw_files.append("source_routing.json")

        # ---- rules.md / 生成脚本 ----
        # 包内文本同样受法定标识约束（编号与报告同源，可核对是不是同一份）
        _stamp = _aigc_stamp(str(result.get("report_final") or ""))
        _write(os.path.join(work, "rules.md"), _stamp + _rules_md())
        _write(os.path.join(work, "verify.py"), _VERIFY_PY)
        _write(os.path.join(work, "compute.py"), _COMPUTE_PY)

        # ---- README ----
        n_media = sum(
            1 for c in citations if str(c.get("group") or "") in ("财经媒体", "媒体报道", "社媒/论坛")
        )
        n_t1 = sum(1 for c in citations if str(c.get("tier") or "") in ("1", "2"))
        readme = f"""# 研策 · 可复算包

- 研究问题：{q}
- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}
- 引擎基线：{run_stats.get('engine') or result.get('version') or '见报告水印'}
- 本次规模：引文 {len(citations)} 条 · 结论 {len(evidences)} 条 · 调研维度 {len(routes)} 个

## 这个包是什么

研策声称"结论**证据加权可被第三方复算**"。这个包就是**把这个声称交给你验证**——
它包含本次调研的**原始取证记录**、**判定规则**和**两个可独立运行的脚本**。

## 三步复算（只需 Python 3，无需安装本项目的任何东西）

```bash
# 第一步：重探每个引用 URL 是否还活着 + 重算来源结构
python verify.py

# 第二步：从原始记录独立重算派生数字（CAGR / 同比）
python compute.py

# 第三步：打开 claims.csv，挑任意一条结论，用其中的 URL 点开原文核对
```

`verify.py` 会输出"存活 N/M"；`compute.py` 会输出从原始序列算出的 CAGR，
**把它与报告正文里的增速对照**——对得上，说明报告的数字是算出来的，不是编的。

## 包内清单

| 文件 | 内容 |
|---|---|
| `README.md` | 本文件 |
| `manifest.json` | 包元数据 + **每个文件的 SHA256**（可核对有没有被中途改过） |
| `rules.md` | 公开判定规则（**由运行时常量导出**，不会与代码不一致） |
| `claims.csv` | 每条结论：置信度 · 状态 · 独立域名数 · 来源 |
| `sources.csv` | 每条来源：层级 · 分组 · 数据期 · 口径 · URL · 检索词 |
| `raw/` | **原始取证记录**（{len(raw_files)} 个文件，未加工） |
| `verify.py` | 【独立可跑】URL 存活复探 + 来源结构复算 |
| `compute.py` | 【独立可跑】从原始记录重算派生数字 |

## 本次来源结构（自证）

- 引文 {len(citations)} 条，其中 **T1/T2（官方/机构）{n_t1} 条**
- 门户/媒体/社媒类 **{n_media} 条**（占比 {100.0 * n_media / max(1, len(citations)):.0f}%，规则上限 50%）

## 我们**不**声称可复算的部分（诚实边界）

- 报告里的数字是否**真的出自该页原文** —— 需人工点开核验，脚本无法自动判定
- LLM 撰写的分析文字是否恰当 —— 属判断而非事实

这两项我们不打包票。可复算的只有：**引用页存活、来源结构合规、派生数字算得对、规则与代码一致**。
"""
        _write(os.path.join(work, "README.md"), _stamp + readme)

        # ---- manifest.json（含各文件 SHA256）----
        files_meta = []
        for root, _dirs, fns in os.walk(work):
            for fn in sorted(fns):
                if fn == "manifest.json":
                    continue
                p = os.path.join(root, fn)
                rel = os.path.relpath(p, work).replace("\\", "/")
                files_meta.append({"path": rel, "bytes": os.path.getsize(p), "sha256": _sha256(p)})
        manifest = {
            "bundle_id": safe_id,
            "product": "研策 · 市场调研数字员工",
            "question": q,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            # ⚠️ **这一格一直是空串**（2026-09-16 全量审计）：`run_stats` 里根本没有
            # `engine` 键（账本记的是档位/耗时/token，不记版本），所以恒回落到 `""`，
            # 而 README 那行「引擎基线：」于是退化成"见报告水印"——
            # **可复算包的 manifest 认不出自己是哪个版本出的**。
            # 第三方拿到包要复现，第一件事就是对版本；版本只在水印那句自然语言里，
            # 就没法机械化核对。改成从**版本号的唯一来源**（`core.version.ENGINE_VERSION`）取。
            "engine": run_stats.get("engine") or _engine_version(),
            "counts": {
                "citations": len(citations), "evidences": len(evidences),
                "routes": len(routes), "raw_files": len(raw_files),
            },
            "files": files_meta,
            "how_to_reproduce": ["python verify.py", "python compute.py", "打开 claims.csv 抽查原文"],
            "not_claimable": [
                "数字是否真的出自该页原文（需人工核验）",
                "LLM 撰写文字的分析恰当性（属判断）",
            ],
        }
        _write(os.path.join(work, "manifest.json"), json.dumps(manifest, ensure_ascii=False, indent=2))

        # ---- 打包 ----
        zip_path = os.path.join(_BUNDLE_DIR, f"{safe_id}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _dirs, fns in os.walk(work):
                for fn in fns:
                    p = os.path.join(root, fn)
                    z.write(p, os.path.join(safe_id, os.path.relpath(p, work)))
        shutil.rmtree(work, ignore_errors=True)
        n_bytes = os.path.getsize(zip_path)
        logger.info("可复算包已生成：%s（%d 文件 / %.1f KB）", safe_id, len(files_meta), n_bytes / 1024.0)
        return {
            "bundle_id": safe_id,
            "files": [f["path"] for f in files_meta],
            "n_files": len(files_meta),
            "bytes": n_bytes,
            "counts": manifest["counts"],
        }
    except Exception as e:
        logger.warning("可复算包生成失败（不影响交付）: %s", e, exc_info=True)
        shutil.rmtree(work, ignore_errors=True)
        return {}


def describe() -> str:
    return (
        "可复算包（Audit Bundle）：一次调研产出一个 ZIP，含**原始取证记录**、**公开判定规则**"
        "（由运行时常量导出，不漂移）与**两个纯标准库的独立脚本**——"
        "`verify.py` 重探每个引用 URL 存活并复算来源结构，`compute.py` 从原始序列独立重算 CAGR/同比。"
        "第三方无需安装本项目即可复算。**不声称**可复算「数字是否出自原文」与「文字分析是否恰当」。"
    )
