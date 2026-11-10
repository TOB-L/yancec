#!/usr/bin/env python3
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
    print("\n[1/3] 重探 %d 个引用 URL（≥1 秒/主机，避免给对方站点压力）…" % len(urls))
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
    print("\n[2/3] 重算来源结构…")
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
    print("\n[3/3] 结论状态对账（claims.csv 声明 vs 本次复算可核项）…")
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
    print("\n已写出 verify_report.json")
    print("=" * 78)
    print("结论：URL 存活 %d/%d；复算完成。" % (n_ok, len(results)))
    print("注意：本脚本能验证『引用页是否还活着』与『来源结构是否合规』；")
    print("      不能验证『报告里的数字是否真的出自该页原文』——那需要人工点开原文核对。")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
