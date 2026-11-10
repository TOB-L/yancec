"""服务端报告存档：报告不该只有浏览器里那一份

病案（2026-09-11 用户实测）：跑完 12 分钟的报告，**刷新页面后整段消失**。
直接原因是前端把消息数组两次写进 localStorage、裸 `setItem` 被配额打满后
`catch {}` 静默吞掉（见前端 persistMessages 的容量阶梯）。但更根本的问题是
**架构**：一次调研唯一的实物交付物，只存在配额最小、最易失的那一层（浏览器），
清缓存 / 换浏览器 / 无痕 / 配额满，任何一条都能让它消失。

这里在服务端磁盘再落一份：
  · 页面刷新 → 前端凭 report_id 直接取回，不依赖 localStorage 是否写成功；
  · localStorage 写满 / 换设备 / 清缓存 → 报告仍在，可从历史报告列表找回；
  · 顺带成为"历史报告"的数据源——不依赖任何一次运行的进程内存。

只存前端**恢复报告卡所需**的字段；证据明细（evidences，几十条、体积占大头）
不进这里：报告卡不读它，可复算包里另有原始记录。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime
from typing import Any

from core import safe_io

logger = logging.getLogger(__name__)

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "reports")
_MAX_KEEP = 200         # 保留最近 N 份（每份约 100~300KB，200 份 ≈ 30MB，磁盘上完全划算）。
# 为什么从 40 提到 200：前端历史会话里旧报告的正文**只留预览 + 指针**，打开时来这里取全文。
# 存档留得太少，"打开旧会话"就会变成"报告找不回来了"——那正是我们要消灭的体验。
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{4,64}$")


def _offline() -> bool:
    """离线测试模式——**测试产物不许写进生产报告存档**。

    病案（2026-09-15 外部审计）：账本（`core/runstats.py`）早就装了这道闸门，
    报告存档却一直没有。实测 `data/reports` 实为 **199 份**，
    其中 **180 份（90%）**正文含 `_fulltest` 的 mock 标记「人均年消费12杯」。

    为什么这比"账本被污染"更严重：`_prune()` 按 mtime 倒序裁剪，
    而 `_MAX_KEEP=200` 已**恰好饱和**——**测试档正在把真实报告挤出保留窗**。
    复核者若说"打开一份你们真实跑的报告看看"，打开的很可能是一份 mock 报告。

    闸门开在**写入点**（与账本同源、同理由）：调用点有若干处，写入点只有一处。

    2026-09-16：判定实现已收归 `core.safe_io`；本函数保留为薄委托。
    """
    from core import safe_io

    return safe_io.is_offline()


def _ensure_dir() -> str:
    from core import safe_io

    safe_io.makedirs(_DIR)
    return _DIR


def _new_id(question: str) -> str:
    """ASCII 文件名（时间戳 + 问题短哈希）

    刻意用纯 ASCII：本仓库在"可复算包"上踩过一次坑——路径穿越防护写成 ASCII 白名单，
    结果把中文 bundle_id 全拒了、接口 404，看起来像"包没生成"。这里从源头就不产生
    非 ASCII 名字，防护和可读性都不必二选一。
    """
    h = hashlib.sha1((question or "").encode("utf-8")).hexdigest()[:6]
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{h}"


def save_report(question: str, result: dict[str, Any]) -> dict[str, Any] | None:
    """把一份报告落盘（失败不影响交付）→ {id, question, ts, verdict, chars}"""
    if _offline():
        logger.info("离线模式：本次报告不写生产存档（question=%s）", str(question or "")[:40])
        return None
    try:
        report = (result or {}).get("report_final") or ""
        if not report.strip():
            return None
        _ensure_dir()
        rid = _new_id(question or "")
        payload = {
            "id": rid,
            "question": question or "",
            "ts": datetime.now().isoformat(timespec="seconds"),
            "report": report,
            "citations": result.get("citations") or [],
            "metrics": result.get("metrics") or [],
            # 引用可信度两层读数：跑完即落档，否则刷新/换设备后交付物头部只剩"报告正文"，
            # 而"引用支撑率"恰恰是这一版最该被复现的那个数（存档里没有 = 找不回来重算）。
            "entailment": result.get("entailment") or {},
            "decision_chain": result.get("decision_chain") or [],
            # **不能用 `or 0.0`**：`pass_rate` 在"一条都没敲过门"时是 `None`（未判定），
            # `None or 0.0` 会把它变回 0.0——即"没测过"落盘成"0%"，正是本产品在批评的
            # 「把缺席当结论」。存档要能区分这两者，刷新后前端才画得出正确角标。
            "citation_pass_rate": result.get("citation_pass_rate"),
            "decision_verdict": result.get("decision_verdict") or "",
            # 闸门结论随报告一起落档（2026-09-16）：
            # 病案：闸门把判定行改写成了 `Validate`，而**存档里只有改写后的字符串**——
            # 于是"这份报告到底有没有被闸门拦过、拦的是哪几条"在存档上**无法判定**，
            # 只能去正则扫正文（脆弱且会随排版失效）。而录屏前的机检第 5 条
            # （"报告头出现闸门横幅且判定行已改写"）恰恰要判这一件事。
            # 存 dict 而不是布尔：level / blockers / downgraded_from / downgraded_to 都要能核。
            "integrity_gate": result.get("integrity_gate") or {},
            "validation_tasks": result.get("validation_tasks") or [],
            "monitor_conditions": result.get("monitor_conditions") or [],
            "audit_bundle": result.get("audit_bundle") or None,
            # ── 以下是 2026-09-16 补的：**恢复出来的报告卡此前会掉一大片读数** ──
            # 病案：这些字段在实时路径上都有、在 `reportMsgFromServer` 里也留了渲染位，
            # 唯独**存档里没有**。于是"刷新一下 / 从历史里打开"这份报告：
            #   · 头部印成「 条证据 ·  处仲裁」（两个数都空着）
            #   · 角标 tooltip 说「本次未产出冲突检测读数（**旧报告**）」——而它刚跑完
            #   · 坏链不再被拦（`badUrls` 丢了 → 死链照旧可点）
            # 一屏证据齐全的报告，刷新后变成一屏空读数。**存了不读的另一面：读的时候没存。**
            "evidence_count": len(result.get("evidences") or []),
            "conflicts": len(result.get("conflict_results") or []),
            # 仲裁分母（trunk_matched / pairs_compared）：**「0 处仲裁」没有分母时
            # 读不出"查过没有"还是"根本没查"**——这正是当初把它加进 result 的理由。
            "conflict_scan": result.get("conflict_scan") or {},
            "citation_report": result.get("citation_report"),
            "bad_urls": result.get("bad_urls") or [],
            "url_fixes": result.get("url_fixes") or {},
            # 决策清单降级标记：丢了它，从历史里打开的降级报告会被当成正常交付
            "sop_fallback": result.get("sop_fallback") or {},
            "stop_card": result.get("stop_card") or None,
        }
        with safe_io.open_prod(os.path.join(_DIR, f"{rid}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        _prune()
        return {
            "id": rid,
            "question": payload["question"][:80],
            "ts": payload["ts"],
            "verdict": payload["decision_verdict"],
            "chars": len(report),
        }
    except Exception as e:
        logger.warning("报告存档失败（不影响交付）: %s", e)
        return None


def _prune() -> None:
    """只留最近 _MAX_KEEP 份，防止无限增长"""
    try:
        files = sorted(
            (f for f in os.listdir(_DIR) if f.endswith(".json")),
            key=lambda f: os.path.getmtime(os.path.join(_DIR, f)),
            reverse=True,
        )
        for f in files[_MAX_KEEP:]:
            safe_io.remove(os.path.join(_DIR, f))
    except Exception:
        pass


def _path(rid: str) -> str | None:
    """id → 路径。拒绝危险模式 + 目录包含校验（不用 ASCII 白名单，见 _new_id 说明）"""
    if not rid or not _ID_RE.match(rid):
        return None
    p = os.path.abspath(os.path.join(_DIR, f"{rid}.json"))
    try:
        if os.path.commonpath([p, os.path.abspath(_DIR)]) != os.path.abspath(_DIR):
            return None
    except ValueError:
        return None
    return p if os.path.isfile(p) else None


def load_report(rid: str) -> dict[str, Any] | None:
    p = _path(rid)
    if not p:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("报告读取失败 %s: %s", rid, e)
        return None


def list_reports(limit: int = 20) -> list[dict[str, Any]]:
    """历史报告清单（**不含正文**——列表接口不该把几百 KB 正文全带上）"""
    try:
        if not os.path.isdir(_DIR):
            return []
        files = sorted(
            os.listdir(_DIR),
            key=lambda f: os.path.getmtime(os.path.join(_DIR, f)),
            reverse=True,
        )
        out: list[dict[str, Any]] = []
        for f in files:
            if not f.endswith(".json") or len(out) >= max(1, min(limit, 100)):
                continue
            try:
                with open(os.path.join(_DIR, f), encoding="utf-8") as fh:
                    d = json.load(fh)
                out.append(
                    {
                        "id": d.get("id") or f[:-5],
                        "question": (d.get("question") or "")[:80],
                        "ts": d.get("ts") or "",
                        "verdict": d.get("decision_verdict") or "",
                        "chars": len(d.get("report") or ""),
                    }
                )
            except Exception:
                continue
        return out
    except Exception as e:
        logger.warning("报告清单读取失败: %s", e)
        return []


def describe() -> dict[str, Any]:
    """自检用：存档目录 + 当前份数 + 最近一份"""
    _ensure_dir()
    items = list_reports(limit=100)
    return {"dir": _DIR, "count": len(items), "latest": items[0] if items else None}
