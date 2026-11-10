"""研策 · 权威源直取（绕过通用搜索，直接向权威源公开接口取证）

为什么要有这一层：通用搜索引擎（含博查）的排序会被 SEO"行业报告农场"占据
（实测：`site:` 限定被博查忽略，返回报告大厅/华经/产业调研网等内容农场）。
要真正提高**来源权威性**，只能绕开通用搜索，直接向权威源的**公开发布页**取数。

## 已接入（2026-09-10 本机实测可用）
1. **巨潮资讯 cninfo.com.cn** —— 上市公司**法定信息披露**平台（证监会指定）。
   两步链：`topSearch/query`（名称→code+orgId，**orgId 必填**）→ `hisAnnouncement/query`（公告列表）。
   实测 HTTPS 200 / JSON / 无需 cookie/签名/Referer。PDF 直链 `static.cninfo.com.cn` 可下。
2. **国家统计局 www.stats.gov.cn** —— `/sj/zxfb/`（数据发布）/`/sj/sjjd/`（数据解读）**静态列表页**。
   实测 200、静态含 45 条倒序文章链接。**不用** `data.stats.gov.cn/easyquery.htm`（本机被 WAF 403）。
3. **SEC EDGAR**（`efts.sec.gov` / `data.sec.gov`）—— 美股法定申报，**补巨潮的 A 股边界**
   （星巴克 SBUX / 瑞幸 LKNCY 等）。只走官方公开机器接口，**绝不碰 `www.sec.gov/cgi-bin`（robots Disallow）**。
   UA 须带联系方式（`SEC_UA_CONTACT`），fair-access ≤10 req/s（本模块 per-host ≥1s 远低于）。

## 合规红线（代码内可读，勿越；改本文件前先读）
- 只取**公开发布**内容（政府数据发布 / 上市公司法定披露）；**摘要引用，不全文转载**。
- **守 robots**：cninfo/stats 两站 robots.txt 实测 404（无声明）；SEC 的 `/cgi-bin` **明令 Disallow → 不访问**。
  一律不触碰任何 Disallow 路径。
- **不绕过**验证码/风控/登录/付费墙；遇 403/412/挑战页 → 立即降级返回空，不做 cookie/无头绕过。
- **不伪造 Referer/Origin**；**不接**未授权的未公开内部接口（如艾瑞前端逆向接口）。
- **频率**：全进程 per-host 令牌桶 ≥1s，串行请求；不分页翻历史。
- **总开关**：环境变量 `AUTHORITY_FETCH_ENABLED`（默认开；置 0 一键关闭，接入点静默降级）。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) yancec-research-bot/1.0"


def enabled() -> bool:
    """总开关（可一键关闭整个权威源层）"""
    return str(os.environ.get("AUTHORITY_FETCH_ENABLED", "1")).strip() not in ("0", "false", "False", "")


# ---------------- per-host 令牌桶（≥1s/次，串行） ----------------
_LOCK = threading.Lock()
_LAST: dict[str, float] = {}
_MIN_INTERVAL = 1.0


def _throttle(url: str) -> None:
    """per-host 令牌桶：同一主机两次请求间隔 ≥ `_MIN_INTERVAL`

    ⚠️ **sleep 必须在锁外**（2026-09-14 审计）。原实现把 `time.sleep(wait)` 写在
    `with _LOCK:` 里面，于是它不是"per-host 限速"，而是**全进程串行点**——
    线程为 A 主机等待时，B/C/D 主机的线程也一起被挡在锁外。
    交付期引文核验是并发多路、跨多个主机，这个串行点会把最坏墙钟时间
    按主机数线性放大（而它看起来"只是限速"，很难被怀疑）。

    并发正确性：`检查 + 写 _LAST` 仍在同一把锁内完成，所以同一主机只会有一个线程
    拿到 `wait <= 0`；其余线程算出正等待，出锁后再睡。
    """
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        host = ""
    if not host:
        return
    while True:
        with _LOCK:
            now = time.monotonic()
            wait = _MIN_INTERVAL - (now - _LAST.get(host, 0.0))
            if wait <= 0:
                _LAST[host] = now
                return
        time.sleep(min(wait, _MIN_INTERVAL))


def _get(url: str, timeout: float = 12.0):
    if not _robots_allows(url):
        return _BlockedResponse(url)
    _throttle(url)
    import httpx

    return httpx.get(url, timeout=timeout, headers={"User-Agent": _UA}, follow_redirects=True)


def _post(url: str, data: dict, timeout: float = 15.0):
    if not _robots_allows(url):
        return _BlockedResponse(url)
    _throttle(url)
    import httpx

    # 显式 UTF-8：curl --data 中文会按 GBK 发出导致空结果（实测），httpx 传 dict 走 UTF-8 无此问题
    return httpx.post(
        url,
        data=data,
        timeout=timeout,
        headers={"User-Agent": _UA, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        follow_redirects=True,
    )


# ---------------- robots.txt 门（per-host 缓存，进程内每主机只取一次） ----------------
# 病案（2026-09-11 自查）：`守 robots` 此前**只对 SEC 生效**（`verification._ROBOTS_DENY`
# 是一条硬编码的 sec.gov 规则），其余源——统计局、协会、巨潮——从来没查过 robots.txt。
# 也就是说"我们守 robots"这句在文档里写了很久，在代码里只覆盖了一个站。
#
# 现在改成**通用门**：任何经 `_get`/`_post` 发出的权威源请求，先过该主机 robots.txt 的
# `can_fetch(我们的 UA, 目标 URL)`。缓存按主机存，一个进程内每主机最多一次额外请求。
#
# 三种"取不到 robots"的处理，都按**不禁止**走（这是 robots 协议本身的口径：
# 无 robots.txt = 无限制），但都记 reason，便于审计时区分"问过了说可以"与"没问到"。
_ROBOTS_CACHE: dict[str, tuple[bool, str]] = {}
_ROBOTS_LOCK = threading.Lock()


def _robots_allows(url: str) -> bool:
    """目标 URL 是否被该主机 robots.txt 允许（结果进程内缓存）"""
    try:
        p = urlparse(url)
        host = (p.netloc or "").lower()
        if not host or p.scheme not in ("http", "https"):
            return True
        with _ROBOTS_LOCK:
            hit = _ROBOTS_CACHE.get(host)
        if hit is not None:
            return hit[0]
        root = f"{p.scheme}://{host}"
        allow, _reason = _fetch_robots(root, url)
        with _ROBOTS_LOCK:
            _ROBOTS_CACHE[host] = (allow, _reason)
        return allow
    except Exception:
        return True


def _fetch_robots(root: str, target: str) -> tuple[bool, str]:
    import httpx

    try:
        _throttle(root + "/robots.txt")
        r = httpx.get(root + "/robots.txt", timeout=8.0, headers={"User-Agent": _UA}, follow_redirects=True)
    except Exception as e:
        return True, f"robots 未取到（{type(e).__name__}）→ 按未限制处理"
    if r.status_code != 200:
        return True, f"robots 无（HTTP {r.status_code}）→ 按未限制处理"
    try:
        import urllib.robotparser as _rp

        parser = _rp.RobotFileParser()
        parser.parse((r.text or "").splitlines())
        ok = bool(parser.can_fetch(_UA, target))
    except Exception:
        return True, "robots 解析失败 → 按未限制处理"
    return ok, ("robots 允许" if ok else "robots 明令 Disallow")


def robots_reason(url: str) -> str:
    """取该 URL 所主机 robots 的判定理由（供审计/巡检展示）"""
    _robots_allows(url)
    try:
        host = (urlparse(url).netloc or "").lower()
        with _ROBOTS_LOCK:
            return _ROBOTS_CACHE.get(host, (True, ""))[1]
    except Exception:
        return ""


class _BlockedResponse:
    """被 robots 拦下时的**空响应替身**：让调用方沿原有"非 200 → 降级返回空"的路径走，
    不必在每个取数函数里各写一遍 robots 分支。"""

    status_code = 999
    text = ""
    content = b""
    headers: dict[str, str] = {}
    reason = "robots-disallowed"

    def __init__(self, url: str = "") -> None:
        self.url = url

    def json(self):
        return {}


# ============================================================
# 源 1 · 巨潮资讯（上市公司法定披露）
# ============================================================
_CNINFO_BASE = "https://www.cninfo.com.cn"          # HTTPS（核验修正：明文 HTTP 有中间人风险）
_CNINFO_PDF = "https://static.cninfo.com.cn/"


def _cninfo_column(code: str) -> str:
    """按证券代码推断市场：6=沪(sse) / 0,3=深(szse) / 4,8=北(bj)"""
    c = (code or "").strip()[:1]
    if c == "6":
        return "sse"
    if c in ("0", "3"):
        return "szse"
    if c in ("4", "8"):
        return "bj"
    return "szse"


def _cninfo_query(code: str, org: str, limit: int, year: str = "", category: str = "") -> list[dict[str, Any]]:
    """一次 hisAnnouncement 查询 → 公告条目列表"""
    se = f"{year}-01-01~{year}-12-31" if year else ""
    r2 = _post(
        f"{_CNINFO_BASE}/new/hisAnnouncement/query",
        {
            "pageNum": 1,
            "pageSize": max(1, min(int(limit), 30)),  # pageSize≤30（克制）
            "column": _cninfo_column(code),
            "tabName": "fulltext",
            "stock": f"{code},{org}",
            "searchkey": "",
            "category": category,
            "seDate": se,
            "isHLtitle": "true",
        },
    )
    if r2.status_code != 200:
        return []
    anns = (r2.json() or {}).get("announcements") or []
    out: list[dict[str, Any]] = []
    for a in anns:
        try:
            ts = int(a.get("announcementTime") or 0) / 1000
            date = time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""
        except Exception:
            date = ""
        title = re.sub(r"</?em>", "", str(a.get("announcementTitle") or "")).strip()
        adj = str(a.get("adjunctUrl") or "").lstrip("/")
        url = (_CNINFO_PDF + adj) if adj else ""
        if not title or not url:
            continue
        out.append({"title": title, "url": url, "date": date})
    return out


def fetch_cninfo(company: str, limit: int = 5, year: str = "") -> list[dict[str, Any]]:
    """给定公司名/简称 → 该司公告列表（标题/日期/PDF 直链）。

    两步链：topSearch（名称→code+orgId，**orgId 必填**，缺则本条链失败）→ hisAnnouncement。

    **优先定期报告**（2026-09-11 修）：此前不带 `category` → 取回的是"最新几条公告"，
    实测比亚迪返回的是「暂停办理过户登记」「临时股东会通告」这类**治理公告**——
    对市场调研近乎无用。而 `category=category_ndbg_szsh`（年度报告）/
    `category_bndbg_szsh`（半年度报告）拿到的才是**装着营收/门店/毛利数据的定期报告**。
    故先按"年报 → 半年报"取；两类都没有（非上市公司/新上市）再退回最新公告，保证不空手。
    """
    name = (company or "").strip()
    if not name or not enabled():
        return []
    try:
        r1 = _post(f"{_CNINFO_BASE}/new/information/topSearch/query", {"keyWord": name, "maxNum": 10})
        if r1.status_code != 200:
            logger.info("巨潮 topSearch 非 200(%s)：%s", r1.status_code, name[:20])
            return []
        hits = r1.json() or []
        if not hits:
            return []
        h0 = hits[0]
        code, org = str(h0.get("code") or ""), str(h0.get("orgId") or "")
        if not code or not org:
            logger.info("巨潮 topSearch 缺 code/orgId，跳过：%s", name[:20])
            return []
        sec = str(h0.get("zwjc") or name)
        # **主体校验**（2026-09-14 审计）：topSearch 会**模糊命中**——查询词越通用，
        # 越容易返回一个名字里恰好含它的无关公司（实测 `keyWord=连锁` → 红旗连锁）。
        # 不校验就等于把一家无关上市公司的年报当 L1 一手源注进报告，
        # 还会抽出它的营收/总资产数值——正是本文件 `_COMPANY_STOP` 注释里那句
        # "串台的一手源比没有一手源危险得多（会被当成权威证据引用）"。
        # 对照 `fetch_sse_edc` 早就有同类主体门，只有巨潮这条最硬的链漏了。
        if not _same_entity(name, sec):
            logger.warning("巨潮主体不符，丢弃（query=%r 命中 zwjc=%r code=%s）", name[:20], sec[:20], code)
            return []
        # ① 定期报告优先（年报 → 半年报）
        # pageSize 取 2×limit：年报常与"年报摘要"成对出现，去重后数量会减半（实测踩过）
        rows: list[dict[str, Any]] = []
        for cat in ("category_ndbg_szsh", "category_bndbg_szsh"):
            rows = _cninfo_query(code, org, max(limit * 2, 6), year, cat)
            if rows:
                break
        # ② 退回最新公告（宁给治理公告，也不空手——但内容说明会写明它是什么）
        _periodic = bool(rows)
        if not rows:
            rows = _cninfo_query(code, org, max(limit * 2, 6), year, "")
        # 摘要去重：全文与摘要同在时只留全文（标题去掉"摘要"后同名即视为重复）
        _full = {r["title"] for r in rows if "摘要" not in r["title"]}
        rows = [r for r in rows if ("摘要" not in r["title"]) or (r["title"].replace("摘要", "") not in _full)]
        out: list[dict[str, Any]] = []
        for a in rows[:limit]:
            title, url, date = a["title"], a["url"], a["date"]
            _kind = "定期报告" if _periodic else "公告"
            out.append(
                {
                    "title": f"{sec}：{title}"[:150],
                    "url": url,
                    "date": date,
                    "publisher": f"巨潮资讯·{sec}",
                    "snippet": f"{sec}（{code}）法定披露{_kind}：{title}（{date}）",
                    "content": (
                        f"{sec}（{code}）在证监会指定披露平台巨潮资讯发布的**{_kind}**：{title}，"
                        f"披露日期 {date}。原文 PDF：{url}"
                    ),
                    "source_type": "authority",
                }
            )
        logger.info("权威源·巨潮命中 %d 条(%s)：%s", len(out), "定期报告" if _periodic else "最新公告", name[:20])
        return out
    except Exception as e:
        logger.info("权威源·巨潮失败(降级)：%s", e)
        return []


# ============================================================
# 源 1b · 巨潮年报**正文数字抽取**（把"给年报链接"升级成"给年报里的数"）
# ------------------------------------------------------------
# 为什么必须做：巨潮此前只给到"比亚迪：2025年年度报告 + PDF 链接"——
# 报告正文里引用营收/净利时，仍然只能转述媒体，一手数字**没真正拿到**。
# 年报 PDF 的「主要会计数据和财务指标」章是法定结构化披露，抽出来就是可直接引用的数字，
# 并且能**本地算同比**（算术在代码里，不交给 LLM）。
#
# 合规：只取巨潮公开披露的 PDF；只读前若干页（主要会计数据章）；限体积；结果进程内缓存。
# ============================================================
_PDF_MAX_BYTES = 30 * 1024 * 1024   # 超 30MB 不下载（避免拖垮主流程）
_PDF_PAGES = 28                     # 只读前 28 页（主要会计数据章通常在前 12 页内）
_FIN_LABELS: tuple[tuple[str, str], ...] = (
    ("营业收入", "营业收入"),
    ("归属于上市公司股东的净利润", "归母净利润"),
    ("归属于母公司股东的净利润", "归母净利润"),
    ("归属于上市公司股东的扣除非经常性损益的净利润", "扣非归母净利润"),
    ("经营活动产生的现金流量净额", "经营活动现金流净额"),
    ("归属于上市公司股东的净资产", "归母净资产"),
    ("总资产", "总资产"),
)
_NUM_RE = re.compile(r"(-?[\d]{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+\.\d{2})")


def _pdf_text(url: str, pages: int = _PDF_PAGES) -> str:
    """下载并抽取 PDF 前若干页文本（失败返回 ''）"""
    if not _robots_allows(url):
        return ""
    _throttle(url)
    import httpx

    try:
        with httpx.stream(
            "GET", url, timeout=40.0, follow_redirects=True,
            headers={"User-Agent": _UA},
        ) as r:
            if r.status_code != 200:
                return ""
            cl = r.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > _PDF_MAX_BYTES:
                logger.info("巨潮年报 PDF 过大(%sMB)，跳过正文抽取", round(int(cl) / 1e6, 1))
                return ""
            buf = bytearray()
            for chunk in r.iter_bytes(65536):
                buf.extend(chunk)
                if len(buf) > _PDF_MAX_BYTES:
                    return ""
    except Exception as e:
        logger.info("巨潮年报 PDF 下载失败(降级)：%s", e)
        return ""
    try:
        import io

        from pypdf import PdfReader

        rd = PdfReader(io.BytesIO(bytes(buf)))
        out: list[str] = []
        for i, pg in enumerate(rd.pages):
            if i >= pages:
                break
            try:
                out.append(pg.extract_text() or "")
            except Exception:
                continue
        return "\n".join(out)
    except Exception as e:
        logger.info("巨潮年报 PDF 解析失败(降级)：%s", e)
        return ""


def _extract_fin_numbers(text: str) -> list[dict[str, Any]]:
    """从年报正文里抽「主要会计数据」：标签 → 本期/上期数值（元）+ 本地算同比"""
    if not text:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for label, cn in _FIN_LABELS:
        if cn in seen:
            continue
        idx = text.find(label)
        if idx < 0:
            continue
        window = text[idx + len(label) : idx + len(label) + 160]
        nums = _NUM_RE.findall(window)[:3]
        vals: list[float] = []
        for n in nums:
            try:
                v = float(n.replace(",", ""))
            except Exception:
                continue
            if abs(v) < 1e6:  # 主要会计数据都是百万级以上（元）；小于此的多为股数/比例噪声
                continue
            vals.append(v)
            if len(vals) >= 2:
                break
        if not vals:
            continue
        seen.add(cn)
        cur = vals[0]
        yoy = ""
        if len(vals) >= 2 and vals[1] > 0:
            yoy = f"，同比 {(cur / vals[1] - 1) * 100:+.1f}%（本地计算）"
        item = {
            "label": cn,
            "cur": cur,
            "yoy": yoy,
            "text": f"{cn} {cur / 1e8:,.2f} 亿元{yoy}",
        }
        out.append(item)
    return out


def fetch_cninfo_financials(company: str, limit: int = 2) -> list[dict[str, Any]]:
    """给定公司 → 从其**年报 PDF 正文**抽取主要财务数据（营收/归母净利/现金流/总资产）

    只在拿到定期报告时才有意义；抽不到就返回空（诚实降级，不猜数）。
    """
    if not company or not enabled():
        return []
    rows = fetch_cninfo(company, limit=max(1, limit))
    # 只对**定期报告**做正文抽取（公告多为治理事项，无主要会计数据章）
    row = next((r for r in rows if "年度报告" in r["title"] or "半年度报告" in r["title"]), None)
    if not row:
        return []
    return _cninfo_report_items(row, company)


def _cninfo_report_items(row: dict[str, Any], company: str) -> list[dict[str, Any]]:
    """**一次性下载**年报 PDF → 同时抽「主要会计数据」与「行业情况」两块，**合并成一条来源**

    两块内容在同一份 PDF 里，各下各的等于把同一份年报拉两遍——对源站是多余的负担，
    而这正是我们反复强调"不做无谓请求"的地方。

    也**必须合并成一条**：`fetch_authority` 末尾按 URL 去重，同一份 PDF 产出两条时
    后一条会被静默丢掉（实测：行业情况条目就是这么消失的，而且不报错——
    表面上"有年报数据"，其实最值钱的行业章节一次都没进过报告）。
    """
    text = _pdf_text(row["url"])
    if not text:
        return []
    parts: list[str] = []
    fin = _extract_fin_numbers(text)
    if fin:
        parts.append(
            "**【主要会计数据和财务指标】**" + "；".join(x["text"] for x in fin)
        )
    # 年报里的「行业情况」章节：**经审计的一手行业数据**（市占率、行业规模、门店数、竞争格局），
    # 由上市公司自己披露、由会计师事务所审计，比任何第三方研报都硬。
    # 此前我们只读前 28 页抽"主要会计数据"，等于把金矿当石头。
    seg, inums = "", []
    try:
        seg, inums = _extract_industry_section(text)
    except Exception as e:
        logger.info("年报行业章节抽取失败(降级)：%s", e)
    if seg:
        _num_txt = "；".join(
            f"{n['indicator']}（{n['kind']}）{n['value']}{n['unit']}" for n in inums[:12]
        )
        parts.append("**【行业情况/经营讨论·原文摘录】**" + seg + (f"（其中数值：{_num_txt}）" if _num_txt else ""))
    if not parts:
        return []
    logger.info(
        "权威源·巨潮年报正文抽取成功：%s（财务 %d 项 / 行业 %d 字 %d 数值）",
        company[:16], len(fin), len(seg), len(inums),
    )
    body = "。".join(parts)
    tags = " + ".join([x for x in ("主要财务数据" if fin else "", "行业情况" if seg else "") if x])
    return [
        {
            "title": f"{row['publisher']}·{row['title']}（年报正文抽取：{tags}）"[:150],
            "url": row["url"],
            "date": row["date"],
            "publisher": row["publisher"],
            "snippet": body[:200],
            "content": (
                f"{row['title']}（{row['date']}）年报 PDF **正文抽取**：{body}。"
                f"数据来源：巨潮资讯法定披露年报 PDF 原文（{row['url']}）；"
                "财务同比为本地计算，口径以年报原文为准。"
                "**行业情况章节是上市公司自己披露、经审计的行业口径**——它与第三方机构测算"
                "（艾瑞/欧睿等）口径通常不同，并列引用时须写明各自来源，不可直接比较。"
            ),
            "source_type": "authority",
        }
    ]


# ------------------------------------------------------------
# 年报里的「行业情况」章节定位
# ------------------------------------------------------------
# 为什么值得单独做：A 股年报「第三节 管理层讨论与分析」里，
# 公司会披露**行业规模、市占率、门店数、竞争格局**——而且是**经审计**的。
# 这是市场调研能拿到的、最硬的一类行业数据（此前只被当成"财报 PDF"整个跳过）。
_INDUSTRY_HEADS = (
    "行业情况", "行业格局", "行业发展", "行业趋势", "所处行业", "行业地位",
    "市场竞争格局", "竞争格局", "经营情况讨论与分析", "管理层讨论与分析",
)
# 目录页里这些标题也会出现（后面跟一串 `……` 或页码）→ 要跳过，否则摘录到的是目录
_TOC_LEAD = re.compile(r"[…\.·]{3,}|\s\d{1,3}\s*$")


# 目录页特征：`.......` 引导符 / `目 录` 二字
# 实测伊利股份 2025 年报：第一次命中"管理层讨论与分析"是在**交叉引用**里
# （"…讨论与分析"中"可能面对的风险"部分的内容"），紧跟其后就是目录页；
# 只看标题后 60 字判不出来，得看**更长的窗口**。
_TOC_NOISE = re.compile(r"(?:[…\.·]{4,})|目\s*录")
# 交叉引用特征：标题前面出现"详见/参见…"的，是引用不是章节起点
_XREF_PRE = ("详见", "参见", "见本", "中“", "中「", "部分的内容")


def _extract_industry_section(text: str, max_chars: int = 1400) -> tuple[str, list[dict[str, Any]]]:
    """年报正文 → (行业情况章节原文摘录, 其中抽到的数值)

    定位策略：在**所有**行业类标题命中里，跳过①目录行②交叉引用，取**最早的**有效起点。
    全都无效就返回空——不猜、不硬凑。
    """
    t = str(text or "")
    if len(t) < 500:
        return "", []
    best = -1
    for h in _INDUSTRY_HEADS:
        start = 0
        while True:
            i = t.find(h, start)
            if i < 0:
                break
            pre = t[max(0, i - 26) : i]
            win = t[i : i + 420]
            bad = (
                bool(_TOC_LEAD.search(t[i + len(h) : i + len(h) + 60]))
                or bool(_TOC_NOISE.search(win))
                or any(k in pre for k in _XREF_PRE)
            )
            if not bad:
                if best < 0 or i < best:
                    best = i
                break
            start = i + len(h)
    if best < 0:
        return "", []
    seg = t[best : best + max_chars]
    # 收尾：尽量停在句号/分号处，避免摘录以半句结束
    for stop in ("。", "；", "\n"):
        j = seg.rfind(stop, int(max_chars * 0.6))
        if j > 0:
            seg = seg[: j + 1]
            break
    seg = re.sub(r"\s+", " ", seg).strip()
    try:
        nums = extract_attachment_numbers(seg)
    except Exception:
        nums = []
    return seg, nums


# ============================================================
# 源 2 · 国家统计局（数据发布/解读 静态列表页）
# ============================================================
_NBS_PAGES = (
    "https://www.stats.gov.cn/sj/zxfb/",      # 数据发布
    "https://www.stats.gov.cn/sj/sjjd/",      # 数据解读
)
# 只认这些栏目的文章路径（核验修正：站点已把文章迁到 /xxgk/sjfb/，旧的 /sj/zxfb/ 过滤会漏最新一条）
_NBS_PATH_OK = ("/sj/zxfb/", "/sj/sjjd/", "/sj/zxfbhjd/", "/xxgk/sjfb/", "/xxgk/")
_NBS_LINK = re.compile(r'href="\.?/?((?:\d{6})/t(?:\d{8})_\d+\.html)"[^>]*>([^<]{4,80})</a>')


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（滚动数组，O(len(a)*len(b))，输入都很短）。

    为什么要它：只做"整串包含"匹配时，"居民人均可支配收入"这类**换说法的指标名**
    永远匹配不到页面标题"居民收入和消费情况" → 统计局直取实际一条都取不回（实测）。
    改成长公共子串≥4 字即算同一指标族（"可支配收入"vs"居民收入"仍有 2 字重叠，
    4 字门限保证不放行泛词），宁可少不可错。
    """
    if not a or not b:
        return 0
    if a in b:
        return len(a)
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _nbs_match(title: str, toks: list[str]) -> bool:
    """标题 ↔ 关键词 是否同一指标族：整串包含 或 最长公共子串≥4 字"""
    if not toks:
        return True
    for t in toks:
        if t in title:
            return True
        if _lcs_len(t, title) >= 4:
            return True
    return False


def fetch_nbs(keyword: str, limit: int = 3) -> list[dict[str, Any]]:
    """给定指标/主题关键词 → 统计局数据发布/解读里**最新的相关文章**（标题含数值 + 官方链接）。

    走静态列表页（最稳，无需 JS）；不做分页翻历史。
    """
    kw = (keyword or "").strip()
    if not kw or not enabled():
        return []
    # 关键词切分：中文连续串按空格/标点拆，取长度≥2 的片段做"标题需包含"判定
    toks = [t for t in re.split(r"[\s，,。、;；/]+", kw) if len(t) >= 2][:4]
    out: list[dict[str, Any]] = []
    try:
        for page in _NBS_PAGES:
            if len(out) >= limit:
                break
            r = _get(page)
            if r.status_code != 200:
                continue
            html = r.text or ""
            for href, title in _NBS_LINK.findall(html):
                # 列表页 href 是相对路径（./202609/t….html）→ 必须相对**页面 URL** 解析，
                # 否则丢掉栏目前缀、路径白名单判定失效（实测踩过）。
                url = urljoin(page, href)
                if not any(p in url for p in _NBS_PATH_OK):
                    continue
                t = re.sub(r"\s+", " ", title).strip()
                if not _nbs_match(t, toks):
                    continue
                if any(o["url"] == url for o in out):
                    continue
                m = re.search(r"t(\d{8})_", href)
                date = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}" if m else ""
                out.append(
                    {
                        "title": t,
                        "url": url,
                        "date": date,
                        "publisher": "国家统计局",
                        "snippet": f"国家统计局数据发布：{t}（{date}）",
                        "content": f"国家统计局公开发布：{t}，发布日期 {date}。原文：{url}",
                        "source_type": "authority",
                    }
                )
                if len(out) >= limit:
                    break
        if out:
            logger.info("权威源·统计局命中 %d 条：%s", len(out), kw[:20])
    except Exception as e:
        logger.info("权威源·统计局失败(降级)：%s", e)
    return out[:limit]


# ------------------------------------------------------------
# 源 2b · 统计局**文章正文数值抽取**（把"给链接"变成"给数字"）
# ------------------------------------------------------------
# 病案：此前 NBS 只给到「2026年8月份居民消费价格同比上涨0.8% + 链接」——
# 报告里要用 CPI 数字时仍得转述媒体，**一手数字没真正拿到**。
# 实测正文结构（2026-09-11 抓真实文章）：
#   · 数字与单位之间**带空格**（"同比上涨 0.8%"）→ 正则一律用 \s* 吸收
#   · 同一段正文在页面里**出现两次**（视频块/移动版）→ 必须去重，否则同句进两遍
#   · 关键句三种：主指标（同比/环比）、累计（1—8月平均）、分项（食品/非食品…）
#   · 另有 `<table>` 数据表 —— 这是最"可计算"的一块
# 只做**正则级确定性提取**，不做语义推测；抽不到返回空，不猜。
# ------------------------------------------------------------
# 空白类里必须收全角与各种 Unicode 空格：`&ensp;`(U+2002)/`&emsp;`/`&thinsp;` 解码后
# 是 U+2000–U+200B，不在 `[ \t\xa0]` 里——不收的话表头会变成 `指 　　　 标` 这种带隐形字符的串，
# 后面按名字匹配指标就永远匹配不上（实测四川那张主要指标表就踩了这个）。
_NBS_WS = re.compile("[ \t\u3000\xa0\u2000-\u200b\r\n]+")
_NBS_BLOCK = re.compile(r"</(?:p|div|tr|br)>", re.I)
_IND_WORD = r"[一-龥]{2,14}"
_NBS_P_MAIN = re.compile(
    r"(" + _IND_WORD + r")\s*(同比|环比|比上年同期|比上月)\s*(上涨|下降|增长|减少|持平)\s*([\d.]+)?\s*%"
)
_NBS_P_CUM = re.compile(
    r"(\d{1,2})\s*[—\-~]\s*(\d{1,2})\s*月(?:平均)?[，,]\s*(" + _IND_WORD + r")\s*(?:比上年同期|同比)?\s*(上涨|下降|增长|减少)\s*([\d.]+)\s*%"
)
_NBS_P_SUB = re.compile(
    r"(" + _IND_WORD + r")价格\s*(?:分别)?\s*(上涨|下降|持平)\s*([\d.]+)?\s*%"
)
# 金额式：`1—7月份，社会消费品零售总额 287744 亿元，同比增长 1.2%`
# **这是社零/GDP/投资/产量类文章的主导句式**，只认 % 形式会整篇抽不出东西（实测踩过）。
# 同时抓「金额」与「同比」，并把 **累计(1—7月) 与 当月(7月)** 分开标注——两者数值差一个量级，混用是硬伤。
_NBS_PERIOD = r"(\d{1,2}\s*[—\-~]\s*\d{1,2}|\d{1,2})\s*月(?:份)?"
_NBS_TEST = r"([\d,]+(?:\.\d+)?)\s*(亿元|万亿元|万人|亿吨|万台|万辆|亿千瓦时|万千瓦)"
_NBS_P_AMT = re.compile(
    _NBS_PERIOD
    + r"[，,]\s*("
    + _IND_WORD
    + r")\s*"
    + _NBS_TEST
    + r"\s*[，,]?\s*(?:同比)?\s*(增长|下降|上涨|减少)\s*([\d.]+)\s*%"
)
# 通用金额式（年报行业章节/研报正文的主导句式）：`指标 + 金额 + 币种单位 [+ 增长X%]`
# 指标名不允许含数字（含数字说明前面还有别的数，切出来会是残句）。
_GEN_AMT = re.compile(
    r"([一-龥A-Za-z]{3,18})\s*"
    r"([\d,]+(?:\.\d+)?)\s*(万亿元|亿元|万美元|亿美元|万元|亿美元|亿)"
    r"(?:\s*[，,]\s*(?:比上年同期|比上年|同比|环比|较上年)?\s*"
    r"(增长|下降|上升|减少|回落)\s*([\d.]+)\s*%)?"
)
_NBS_TABLE = re.compile(r"(?is)<table.*?</table>")
_NBS_CELL = re.compile(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>")
_NBS_ROW = re.compile(r"(?is)<tr[^>]*>(.*?)</tr>")


def _nbs_clean(html: str) -> str:
    """HTML → 规整正文（去脚本样式、块级标签转换行、压缩空白；数字与单位间空格留给正则 \\s* 吸收）

    实体解码走 `html.unescape` 而不是手工 replace 几个：`&ensp;`/`&emsp;`/`&thinsp;`/
    `&#12288;` 这些在政府站正文里到处都是（用来给"指标"两个字加字距），
    手工 replace 挂一漏万——漏掉的那个会变成**指标名里的隐形字符**，后面按名字匹配永远匹配不上。
    """
    import html as _html

    t = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", html or "")
    t = _NBS_BLOCK.sub("\n", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = _html.unescape(t)
    return _NBS_WS.sub(" ", t)


def _nbs_dedupe_lines(text: str) -> list[str]:
    """去重正文（同一段在页面出现两次），保序"""
    seen: set[str] = set()
    out: list[str] = []
    for ln in (text or "").split("\n"):
        s = ln.strip()
        if len(s) < 10:
            continue
        key = s[:80]
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _nbs_tables(html: str, max_rows: int = 12) -> list[list[list[str]]]:
    """解析 `<table>` → 每表若干行的单元格文本（数据表是这里最"可计算"的一块）

    ⚠️ 必须过掉**导航/面包屑表**：政府站整站布局常用 `<table>` 排版，
    实测杭州统计局页面的第一张"表"表头是 `当前位置： 首页 | > 统计数据 | > 进度数据`。
    不滤掉的话，这段导航会被当成"数据表"写进报告来源摘录里——
    **读者看到的是"本研究提取到的数据表：首页 > 统计数据"**，比没有更糟。

    判据只用高精度信号（宁可漏滤，不可误杀真数据表）：
      · 首行任一格含 `当前位置` / `您现在的位置`
      · 非空格子里 `>`/`>>` 占比 ≥ 一半（面包屑分隔符）
      · 整表所有格子加起来没有出现任何阿拉伯数字（数据表不可能一个数字都没有）
    """
    import html as _html

    out: list[list[list[str]]] = []
    for tb in _NBS_TABLE.findall(html or "")[:4]:
        rows: list[list[str]] = []
        for tr in _NBS_ROW.findall(tb):
            cells = [
                _NBS_WS.sub(" ", _html.unescape(re.sub(r"(?s)<[^>]+>", " ", c))).strip()
                for c in _NBS_CELL.findall(tr)
            ]
            cells = [c for c in cells if c]
            if len(cells) >= 2:
                rows.append(cells)
            if len(rows) >= max_rows:
                break
        if len(rows) < 2:
            continue
        if _is_nav_table(rows):
            continue
        out.append(rows)
    return out


def _is_nav_table(rows: list[list[str]]) -> bool:
    """这张表是不是"用 table 排版的导航"（而非数据表）"""
    try:
        head = " ".join(rows[0]) if rows else ""
        if "当前位置" in head or "您现在的位置" in head:
            return True
        flat = [c for row in rows for c in row if c]
        if not flat:
            return True
        seps = sum(1 for c in flat if c.strip("&gt; \t") in ("", ">", ">>", ">>>") or c.count(">") >= 1)
        if seps * 2 >= len(flat):
            return True
        if not any(re.search(r"\d", c) for c in flat):
            return True
        if _is_meta_table(flat):
            return True
    except Exception:
        return False
    return False


# 政府站**信息公开属性表**的字段名（索引号/发文字号/主题分类/成文日期…）。
# 实测重庆市统计局：页面上一张 `<table>` 排的是这篇文章的公开属性元数据，
# 结构上"像数据表"（多行多列、有数字——索引号里就有），导航判据一个都拦不住，
# 于是被当成"数据表1"写进来源摘录。**读者看到的会是"本报告提取的数据表：索引号 | 发文字号"**。
_META_MARKERS = (
    "索引号", "发文字号", "主题分类", "成文日期", "发布日期", "有效性",
    "废止日期", "公开方式", "公开时限", "公开范围", "责任部门", "发文机关",
    "信息来源", "浏览次数", "字体", "分享到", "打印本页", "机构名称",
)


def _is_meta_table(flat: list[str]) -> bool:
    """整表是"信息公开属性"→ 不是数据（≥2 个字段名命中即判）"""
    blob = " ".join(flat)
    hits = sum(1 for m in _META_MARKERS if m in blob)
    return hits >= 2


def extract_nbs_numbers(html: str) -> list[dict[str, Any]]:
    """统计局**网页**文章 → 结构化数值（确定性抽取，不含推测）

    产出三类：主指标（同比/环比）、累计（1—8月）、分项（食品/非食品…）。单位一律 %。

    关键正确性（实测踩出来的）：
    · 指标名要**剥掉尾部的口径词**——`[一-龥]{2,14}` 贪婪会把"比上年同期"吃进指标名，
      产出「居民消费价格比上年同期」这种不存在的指标。
    · 分项必须**带上同比/环比**。同篇文章里「食品价格」同时有 同比 -1.4% 与 环比 +0.4%，
      不标口径就是两个长得一样的值——引用时必然出错。口径按**离它最近的前文标记**判定。
    """
    return _extract_numbers("\n".join(_nbs_dedupe_lines(_nbs_clean(html))))


def extract_text_numbers(text: str) -> list[dict[str, Any]]:
    """**附件正文**（PDF/Word 抽出的纯文本）→ 结构化数值（与网页同一套规则）

    为什么需要单独一个入口：很多统计局的"月报/公报"**正文根本不在网页上**——
    网页只有一个附件下载链接（实测杭州市统计局的 7 月月报：页面 8KB 只有一句
    "发布日期/来源/浏览次数"，全部数据在一份 106KB 的 PDF 里）。
    只抓网页的话，这类站永远只贡献一个标题。

    与网页版的差别只在**切行方式**：PDF 文本没有 HTML 块级标签，
    靠句号/分号切句——`_nbs_clean` 会把换行一起压掉，直接复用会让整篇文章变成一行。
    """
    t = re.sub(r"[ \t　\xa0 -​]+", " ", text or "")
    t = re.sub(r"([。；;])", r"\1\n", t)
    return _extract_numbers("\n".join(_nbs_dedupe_lines(t)))


# ------------------------------------------------------------
# 附件正文里的**表格**抽取（PDF 文本的第二种形态）
# ------------------------------------------------------------
# 为什么散文抽取器不够用：政府统计月报 PDF 抽出来的是一张**纯文本表**——
#   ```
#   指标 1-6月（亿元） 同比±% 占GDP比重±%
#   *地区生产总值 12013 5.3 -
#     第一产业 163 4.1 1.3
#   ```
# 行里**没有动词**（没有"增长/上涨"），散文式的 `X同比增长Y%` 正则一条都匹配不到。
# 实测杭州统计局的统计月报就是这样：`extract_text_numbers` 返回 0 条，
# 而数据明明全在里面（GDP/数字经济/规上工业/高新技术产业…）。
#
# 切法：**从右往左**收集末尾的数值 token，剩下的就是指标名。
# 之所以不从左往右取"第一个 token 当名字"——中文指标名可能含空格
# （「其中：高新技术产业」「战略性新兴产业」），从左切会把名字切碎。
_NUM_TOK = re.compile(r"^-?[\d,]+(?:\.\d+)?%?$")


def _split_label_values(line: str) -> tuple[str, list[str]]:
    """一行表格文本 → (指标名, 数值列表)。从右往左剥数值，剥不动了剩下的就是名字"""
    toks = [t for t in re.split(r"\s+", (line or "").strip()) if t]
    vals: list[str] = []
    i = len(toks)
    while i > 0:
        t = toks[i - 1]
        if t in ("-", "—", "–", "…", "...", "－"):
            i -= 1          # 占位符：本期无数据，跳过（**不要**当成 0）
            continue
        if not _NUM_TOK.match(t):
            break
        vals.insert(0, t)
        i -= 1
    return " ".join(toks[:i]).strip(), vals


def _parse_table_header(line: str) -> list[tuple[str, str, str]] | None:
    """表头行 → [(列名, 期间, 口径)]；不是表头返回 None

    例：`指标 1-6月（亿元） 同比±% 占GDP比重±%`
      → [("1-6月","亿元","值"), ("同比","%","同比"), ("占GDP比重","%","占比")]
    """
    toks = [t for t in re.split(r"\s+", (line or "").strip()) if t]
    if len(toks) < 2 or "指标" not in toks[0]:
        return None
    cols: list[tuple[str, str, str]] = []
    for c in toks[1:]:
        m = re.search(r"[（(]([^）)]+)[）)]", c)
        unit = m.group(1) if m else ("%" if ("%" in c or "±" in c) else "")
        period = ""
        pm = re.match(r"^(\d{1,2}\s*[-—~]\s*\d{1,2}\s*月|\d{1,2}\s*月|\d{4}年)", c)
        if pm:
            period = re.sub(r"\s+", "", pm.group(1))
        name = re.sub(r"[（(][^）)]*[）)]", "", c).replace("±", "").strip()
        if "同比" in name:
            basis = "同比"
        elif "环比" in name:
            basis = "环比"
        elif "比重" in name or "占比" in name or "占比" in c:
            basis = "占比"
        else:
            basis = "值"
        cols.append((period, unit, basis))
    return cols or None


def extract_table_numbers(text: str) -> list[dict[str, Any]]:
    """**附件正文里的表格** → 结构化数值（确定性抽取，不含推测）

    产出与散文抽取器同样的 schema（indicator / kind / value / unit），
    只是 `kind` 由**列头**决定（"1-6月·同比"），而不是由行文动词决定。
    """
    lines = [ln for ln in (text or "").splitlines()]
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    cols: list[tuple[str, str, str]] | None = None
    for ln in lines:
        raw = ln.rstrip()
        if not raw.strip():
            continue
        hdr = _parse_table_header(raw)
        if hdr:
            cols = hdr
            continue
        if cols is None:
            continue
        if re.match(r"^\s*(注|数据来源|资料来源)[：:]", raw):
            cols = None       # 注释行 = 该表结束（下一张表会重新给表头）
            continue
        label, vals = _split_label_values(raw)
        label = label.lstrip("*＊· ").strip()
        if not label or not vals or len(label) > 22:
            continue
        if "指标" in label or "单位" in label:
            continue
        for j, v in enumerate(vals[: len(cols)]):
            period, unit, basis = cols[j]
            # kind 的取法与散文抽取器一致：**口径优先，没口径就用期间**。
            # 指标名里**不带**口径/期间——`地区生产总值(同比%)` 这种名字看着像指标、
            # 其实是"指标+口径"糊在一起，跨报告对账时会被当成一个新指标。
            ind = label
            kind = basis if basis != "值" else (period or "值")
            key = (ind, kind, v)
            if key in seen:
                continue
            seen.add(key)
            out.append({"indicator": ind, "kind": kind, "value": v.replace(",", ""), "unit": unit or "%"})
    return out


def extract_attachment_numbers(text: str) -> list[dict[str, Any]]:
    """附件正文 → 结构化数值（**散文式 + 表格式都试**，合并去重）

    两类形态在同一份 PDF 里可能同时出现：前面是文字表述、后面附数据表。
    只取其中一类会漏掉另一半。
    """
    a = extract_text_numbers(text)
    b = extract_table_numbers(text)
    seen = {(x["indicator"], x["kind"], x["value"]) for x in a}
    for x in b:
        k = (x["indicator"], x["kind"], x["value"])
        if k not in seen:
            seen.add(k)
            a.append(x)
    return a


# 页面上的**附件**链接：两种真实形态
#   ① 直链：`…/xxx.pdf`
#   ② JPaaS 政府站平台的下载网关：`…/document/download?fileUrl=<编码串>`
#      （杭州/浙江/宁波这一批站都用它；**它就在页面 href 里**，
#        访客点“2026年7月月报”拿到的正是这个——顺着公开链接走，不是逆向接口）
_ATTACH_HREF = re.compile(r'href="([^"]+(?:\.pdf|\.PDF|download\?fileUrl=[^"]+))"')
_PDF_TEXT_CACHE: dict[str, str] = {}


def _numbers_from_attachment(page_url: str, html: str, max_files: int = 1) -> tuple[list[dict[str, Any]], str]:
    """网页正文没有数值时 → 顺着页面上的**公开附件链接**下 PDF 再抽 → (数值, 附件地址)

    只认 PDF：xls/xlsx/doc 没有现成解析器，**宁可留空也不半解**——
    半解出来的表格比不抽更危险（行列错位后数字还在，但含义全变了）。
    """
    if not enabled():
        return [], ""
    tried = 0
    for m in _ATTACH_HREF.finditer(html or ""):
        if tried >= max_files:
            break
        href = m.group(1).replace("&amp;", "&")
        au = urljoin(page_url, href)
        if not au.startswith("http"):
            continue
        tried += 1
        try:
            txt = _PDF_TEXT_CACHE.get(au)
            if txt is None:
                txt = _pdf_text(au, pages=20)
                _PDF_TEXT_CACHE[au] = txt
        except Exception:
            continue
        if not txt or len(txt) < 200:
            continue
        nums = extract_attachment_numbers(txt)
        if nums:
            logger.info("权威源·统计局**附件**数值抽取成功：%s（%d 数值）", au[-48:], len(nums))
            return nums, au
    return [], ""


def _extract_numbers(body: str) -> list[dict[str, Any]]:
    """数值抽取核心（body 已是规整正文，保留换行）——网页版与附件版共用这一份规则"""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    seen_iv: set[tuple[str, str]] = set()   # (指标, 数值) 去同值重复：同句会被"比上年同期"与"累计"两路各抓一次
    _TAIL = re.compile(r"(?:比上年同期|比上月|同比|环比|平均|累计)+$")

    def _add(ind: str, kind: str, val: str, unit: str = "%") -> None:
        # 只剥真正的行文噪声词。**不剥「其他」**——「其他用品及服务」是 CPI 八大类的
        # 正式名称（实测剥掉会变成不存在的「用品及服务」）。
        ind = re.sub(r"^(?:全国|其中|月份|年|的|月)+", "", ind or "").strip()
        ind = _TAIL.sub("", ind).strip()
        if not (2 <= len(ind) <= 16):
            return
        k = (ind, kind, val, unit)
        if k in seen:
            return
        iv = (ind, val, unit)
        if iv in seen_iv:
            return          # 同一指标同一数值只留先出现的那条（口径两说时以先出现的为准）
        seen.add(k)
        seen_iv.add(iv)
        out.append({"indicator": ind, "kind": kind, "value": val, "unit": unit})

    def _basis_at(pos: int) -> str:
        """取该位置之前最近出现的同比/环比作为口径（分项句本身通常不重复写口径）"""
        a, b = body.rfind("同比", 0, pos), body.rfind("环比", 0, pos)
        if a < 0 and b < 0:
            return "同比"
        return "同比" if a > b else "环比"

    for m in _NBS_P_MAIN.finditer(body):
        ind, basis, verb, val = m.group(1), m.group(2), m.group(3), m.group(4)
        if verb == "持平":
            _add(ind, basis, "0.0")
        elif val:
            _add(ind, basis, ("-" if verb in ("下降", "减少") else "+") + val)
    for m in _NBS_P_CUM.finditer(body):
        _m1, m2, ind, verb, val = m.groups()
        _add(ind, f"1—{m2}月累计", ("-" if verb in ("下降", "减少") else "+") + val)
    for m in _NBS_P_SUB.finditer(body):
        ind, verb, val = m.group(1), m.group(2), m.group(3)
        basis = _basis_at(m.start())
        if verb == "持平":
            _add(f"{ind}价格", f"分项·{basis}", "0.0")
        elif val:
            _add(f"{ind}价格", f"分项·{basis}", ("-" if verb == "下降" else "+") + val)
    # 金额式（社零/GDP/投资/产量类文章的主导句式）：金额与同比**都抽**，
    # 并把累计（1—7月）与当月（7月）分开标注——两者差一个量级，混用即硬伤。
    for m in _NBS_P_AMT.finditer(body):
        period, ind, amt, unit, verb, pct = m.groups()
        p = re.sub(r"\s+", "", period) + "月"
        kind_p = f"{p}累计" if ("—" in p or "-" in p or "~" in p) else p
        _add(ind, kind_p, amt.replace(",", ""), unit)
        _add(ind, f"{kind_p}·同比", ("-" if verb in ("下降", "减少") else "+") + pct)
    # **通用金额式**（最后跑）：`指标 + 金额 + 币种单位 [+ 增长X%]`。
    # 年报「行业情况」章节、研报正文用的大多是这种句式，而它**不写成"1—7月，…"**
    # （实测永辉超市年报：「2025年，社会消费品零售总额501,202亿元，比上年增长3.7%」）——
    # 上面那几条统计局专用句式一条都匹配不到，整段行业数据抽出来是 0 条。
    # 只认**币种单位**（亿元/万亿元/万元/亿美元…）：带"个/家/只"会让"覆盖24个省市""共计403家门店"
    # 这类句子产出「覆盖 24 个」这种不存在的指标；而金额式的指标名几乎不会误伤。
    # 放在最后跑：与上面重叠时由 `seen_iv` 去重，**保留统计局专用句式给出的更精确口径标签**。
    for m in _GEN_AMT.finditer(body):
        ind, amt, unit, verb, pct = m.groups()
        _add(ind, "值", amt.replace(",", ""), unit)
        if verb and pct:
            _add(ind, "同比", ("-" if verb in ("下降", "减少") else "+") + pct)
    return out


def fetch_nbs_detail(url: str, title: str = "", max_tables: int = 1) -> list[dict[str, Any]]:
    """国家统计局一篇文章的正文 → 结构化数值 + 数据表（`fetch_stat_detail` 的薄包装）"""
    return fetch_stat_detail(url, title, publisher="国家统计局", max_tables=max_tables)


def fetch_stat_detail(
    url: str,
    title: str = "",
    publisher: str = "国家统计局",
    date_hint: str = "",
    max_tables: int = 1,
) -> list[dict[str, Any]]:
    """**任一统计局**（国家 / 省 / 市）的一篇文章正文 → 结构化数值 + 数据表（失败静默返回 []）

    为什么把国家统计局专用的抽取器推广成通用的：
    上一轮把地域源从 0 做到 16 个站，但它们贡献的是**标题**——
    "杭州市统计局：2026年7月月报" —— **那不是数据，那是链接**。
    而市县统计局的文章写的是**同一套官方文体**（"1—7月，全省规模以上工业增加值同比增长7.5%"），
    实测抽取器直接可用：上海 19 条、河南 20 条、四川 11 条 + 一张主要指标表。

    所以这一层的价值不在"又接了几个站"，而在**把已有站里的话读出来**。

    `date_hint`：列表页已知的日期。有的站文章路径只含年份（浙江/杭州 `/art/2026/art_…`），
    正文页也拿不到更精确的日期，就用列表页那个（"宁可精度低，也不编一个"）。
    """
    if not url or not enabled():
        return []
    try:
        r = _get(url)
        if r.status_code != 200:
            return []
        html = r.text or ""
        nums = extract_nbs_numbers(html)
        tables = _nbs_tables(html)[: max(0, max_tables)]
        _attach = ""
        if not nums and not tables:
            # 网页正文没数字 → 顺着页面上的**公开附件**（PDF）再试一次。
            # 实测杭州市统计局的《统计月报》就是这样：网页只有一句"发布日期/来源"，
            # 数据全在 106KB 的 PDF 附件里。不做这一步，杭州这个源永远只贡献一个标题。
            try:
                nums, _attach = _numbers_from_attachment(url, html)
            except Exception as _e:
                logger.info("附件数值抽取失败(降级)：%s", _e)
        if not nums and not tables:
            return []
        parts: list[str] = []
        if nums:
            parts.append(
                "**正文数值抽取**："
                + "；".join(f"{n['indicator']}（{n['kind']}）{n['value']}{n['unit']}" for n in nums[:14])
            )
        for i, tb in enumerate(tables, 1):
            head = " | ".join(tb[0][:6]) if tb else ""
            body_rows = [" | ".join(rw[:6]) for rw in tb[1:8]]
            parts.append(f"**数据表{i}**（{head}）：" + " ／ ".join(body_rows))
        m = re.search(r"t(\d{8})_", url)
        _d = _date_from_match(m) if m else ""
        date = _d if len(_d) >= len(date_hint or "") else (date_hint or "")
        _from = f"（数值来自页面附件：{_attach}）" if _attach else ""
        logger.info("权威源·统计局正文抽取成功：%s（%d 数值 / %d 表）", (title or url)[:24], len(nums), len(tables))
        return [
            {
                "title": f"{publisher}·{title or '数据发布'}（正文数值抽取）"[:150],
                "url": url,
                "date": date,
                "publisher": publisher,
                "snippet": (parts[0] if parts else "")[:200],
                "content": (
                    f"{publisher}公开发布原文《{title}》（{date or '日期见原文'}）结构化抽取结果{_from}："
                    + "。".join(parts)
                    + f"。数据来源：{publisher}公开发布（{url}）；以上为**原文数值的确定性抽取**，口径以原文为准；"
                    "同名指标在不同来源间可能口径不同（如「全省」与「全国」），引用时请以原文标注为准。"
                ),
                "source_type": "authority",
            }
        ]
    except Exception as e:
        logger.info("权威源·统计局正文抽取失败(降级)：%s", e)
        return []


# ============================================================
# 源 3 · SEC EDGAR（美股法定申报，补巨潮的 A 股边界）
# ------------------------------------------------------------
# 为什么接它：巨潮只覆盖 A 股。用户题库里的星巴克(SBUX)、瑞幸(LKNCY) 这类
# **美股主体**此前拿不到一手披露，只能靠媒体转述——这正是最难修的一类病。
# SEC 把 EDGAR 全文检索与结构化申报作为**公开机器接口**提供（www.sec.gov/developer），
# 只要求：① UA 里带真实联系方式 ② fair-access ≤10 请求/秒。本模块 per-host ≥1s 远低于上限。
#
# 合规红线（同全文件；此处额外两条）
# - **只走 `efts.sec.gov`（全文检索）与 `data.sec.gov`（结构化申报）**；
#   `www.sec.gov/cgi-bin/*` 被 www.sec.gov 的 robots 明令 `Disallow: /cgi-bin` → **绝不访问**。
# - 不猜、不逆向检索参数；取不到就降级返回空。
# ============================================================
_SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
_SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
_SEC_CONCEPT = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json"
_SEC_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

# XBRL 财务概念 → 中文指标名（**可计算数据**：拿到的是数值时序，不是文章标题）
# 这是"从搜索能力变成 Research Data Layer"的分界线——有了时序就能**本地算 CAGR/增速**，
# 而不是让 LLM 从文字里猜算术（幻觉高发区）。
_SEC_CONCEPTS: tuple[tuple[str, str], ...] = (
    ("RevenueFromContractWithCustomerExcludingAssessedTax", "营业收入"),
    ("Revenues", "营业收入"),
    ("GrossProfit", "毛利"),
    ("OperatingIncomeLoss", "营业利润"),
    ("NetIncomeLoss", "净利润"),
)

# 中文主体名 → EDGAR 注册名/代码片段（中文名无法直接匹配 EDGAR 的英文 title）
# 只覆盖题库高频的港美股消费/科技主体；匹配不到就**如实返回空**，不猜。
_SEC_ALIAS: dict[str, str] = {
    "星巴克": "STARBUCKS", "瑞幸": "LUCKIN", "苹果": "APPLE", "特斯拉": "TESLA",
    "耐克": "NIKE", "麦当劳": "MCDONALD", "可口可乐": "COCA COLA", "百胜": "YUM BRANDS",
    "肯德基": "YUM BRANDS", "必胜客": "YUM BRANDS", "亚马逊": "AMAZON",
    "谷歌": "ALPHABET", "微软": "MICROSOFT", "英伟达": "NVIDIA", "Meta": "META PLATFORMS",
    "奈飞": "NETFLIX", "沃尔玛": "WALMART", "开市客": "COSTCO", "好市多": "COSTCO",
    "宝洁": "PROCTER", "联合利华": "UNILEVER", "百事": "PEPSICO", "露露乐蒙": "LULULEMON",
    "希尔顿": "HILTON", "万豪": "MARRIOTT", "波音": "BOEING", "英特尔": "INTEL",
    "高通": "QUALCOMM", "amd": "ADVANCED MICRO", "AMD": "ADVANCED MICRO",
}

_SEC_FORMS = ("10-K", "10-Q", "20-F", "6-K", "8-K")
_SEC_TICKERS_CACHE: tuple[float, dict[str, int]] = (0.0, {})


class SecUAUnconfigured(RuntimeError):
    """`SEC_UA_CONTACT` 未配置 → **主动不发请求**（fail-closed）。

    病案（2026-09-14 端到端体检）：`_sec_ua()` 在联系方式为空时回落到字面量
    `contact-not-configured` 并照常发请求。这个串是我们自己编的——SEC 在异常流量时
    **联系不到任何人**，fair-access 政策要的"声明身份与联系方式"一个字都没兑现。
    更难看的是它和我们对外材料里的原话直接打架：材料写"声明式 UA（不伪装 Chrome）·
    守规矩、可被联系"，代码却在拿占位联系方式敲一个政府接口的门。

    所以这一层改成 fail-closed：**宁可少一个数据源，也不拿假联系方式敲门**。
    未配置时如实降级为"SEC 通道未启用"，并把原因带进日志与受阻台账——
    这是"够不着就公示、不绕"，不是故障。
    """

    def __init__(self, reason: str = "") -> None:
        super().__init__(
            reason
            or "SEC_UA_CONTACT 未配置：按 SEC fair-access 合规姿态主动不发请求（不拿占位联系方式敲门）"
        )
        self.reason = str(self)


def sec_contact_configured() -> bool:
    """SEC UA 的联系方式是否已真实配置（唯一判据，勿在别处另写一份）"""
    return bool((os.environ.get("SEC_UA_CONTACT") or "").strip())


def _sec_ua() -> str:
    """SEC 要求 UA 声明身份与联系方式；用 SEC_UA_CONTACT 配真实邮箱更稳

    调用前请先过 `sec_contact_configured()`——本函数**不再回落到占位串**，
    未配置时直接抛 `SecUAUnconfigured`，避免"编译得过、跑得动、但违规"的状态。
    """
    contact = (os.environ.get("SEC_UA_CONTACT") or "").strip()
    if not contact:
        raise SecUAUnconfigured()
    return f"yancec-research-bot/1.0 ({contact})"


def _sec_get(url: str, timeout: float = 15.0):
    if not sec_contact_configured():
        raise SecUAUnconfigured()
    _throttle(url)  # per-host ≥1s（SEC fair-access 上限 10/s，远低于）
    import httpx

    return httpx.get(
        url,
        timeout=timeout,
        headers={"User-Agent": _sec_ua(), "Accept-Encoding": "gzip, deflate", "Accept": "application/json"},
        follow_redirects=True,
    )


def _sec_ticker_map() -> dict[str, int]:
    """EDGAR 全量 title/ticker → CIK 映射（进程内缓存 1 小时，文件约 1MB 只取一次）"""
    global _SEC_TICKERS_CACHE
    ts, m = _SEC_TICKERS_CACHE
    if m and (time.monotonic() - ts) < 3600.0:
        return m
    out: dict[str, int] = {}
    try:
        r = _sec_get(_SEC_TICKERS, timeout=20.0)
        if r.status_code == 200:
            for _, v in (r.json() or {}).items():
                try:
                    t = str(v.get("title") or "").strip().upper()
                    tk = str(v.get("ticker") or "").strip().upper()
                    cik = int(v.get("cik_str") or 0)
                except Exception:
                    continue
                if not cik:
                    continue
                if t:
                    out.setdefault(t, cik)
                if tk:
                    out.setdefault(tk, cik)
    except Exception as e:
        logger.info("权威源·SEC 代码表获取失败(降级)：%s", e)
        return {}
    if out:
        _SEC_TICKERS_CACHE = (time.monotonic(), out)
    return out


def _sec_resolve(company: str) -> tuple[int, str]:
    """主体名 → (CIK, 显示名)。支持中文别名、英文名片段、ticker；查不到返回 (0, "")"""
    name = (company or "").strip()
    if not name:
        return 0, ""
    key = _SEC_ALIAS.get(name, name).strip().upper()
    if len(key) < 2:
        return 0, ""
    m = _sec_ticker_map()
    if not m:
        return 0, ""
    # 精确（ticker 或完整 title）优先，其次 title 前缀，最后子串
    if key in m:
        return m[key], key
    for k, cik in m.items():
        if k.startswith(key + " ") or k.startswith(key + ","):
            return cik, k
    for k, cik in m.items():
        if key in k:
            return cik, k
    return 0, ""


def fetch_sec(company: str, limit: int = 3, forms: tuple[str, ...] = _SEC_FORMS) -> list[dict[str, Any]]:
    """给定（中文/英文）主体名 → 该司在 SEC 的**近期法定申报**（表单类型/申报日/原文链接）

    走 data.sec.gov/submissions/CIK##########.json（结构化，官方公开接口）。
    """
    if not company or not enabled():
        return []
    try:
        cik, shown = _sec_resolve(company)
        if not cik:
            return []
        r = _sec_get(_SEC_SUBMISSIONS.format(cik=f"{cik:010d}"))
        if r.status_code != 200:
            logger.info("权威源·SEC 申报非 200(%s)：%s", r.status_code, company[:20])
            return []
        rec = ((r.json() or {}).get("filings") or {}).get("recent") or {}
        accs = rec.get("accessionNumber") or []
        dates = rec.get("filingDate") or []
        fs = rec.get("form") or []
        docs = rec.get("primaryDocument") or []
        descs = rec.get("primaryDocDescription") or []
        out: list[dict[str, Any]] = []
        for i, acc in enumerate(accs):
            if len(out) >= limit:
                break
            form = str(fs[i] if i < len(fs) else "")
            if forms and form not in forms:
                continue
            acc_s = str(acc)
            doc = str(docs[i] if i < len(docs) else "")
            date = str(dates[i] if i < len(dates) else "")
            desc = str(descs[i] if i < len(descs) else "") or form
            if not acc_s:
                continue
            url = _SEC_ARCHIVE.format(cik=cik, acc=acc_s.replace("-", ""), doc=doc)
            out.append(
                {
                    "title": f"{shown}：{form} {desc}（{date}）"[:150],
                    "url": url,
                    "date": date,
                    "publisher": f"SEC EDGAR·{shown}",
                    "snippet": f"{shown}（CIK {cik:010d}）SEC 法定申报：{form}，申报日 {date}",
                    # **本条是"登记线索"，不是"数据"**（2026-09-14 引用蕴涵核验的实测结论）。
                    # 病案：原先这里写的是"{shown} 向 SEC 提交了 {form}"——一句话里没有任何实质内容，
                    # 却被下游当成一条"证据主张"贴进报告（"竞争格局：STARBUCKS CORP 提交了 10-Q"）。
                    # 引用蕴涵核验逐条比对时，8/8 判为"不相关"：摘录仅为登记元数据，
                    # 不含任何关于该维度的实质内容。**核验官是对的**——这不是引用错了，
                    # 是取证层把"索引条目"当成了"结论依据"。
                    # 现在把这件事写在条目里（并在 entailment 层单列一类、不计入通过率），
                    # 让它在报告里读起来就是"线索"，而不是伪装成一条发现。
                    "content": (
                        f"[登记线索·未抽取正文] {shown}（CIK {cik:010d}）SEC 法定申报索引："
                        f"{form}，申报日 {date}。**本条只证明该申报存在，未抽取申报正文，"
                        f"不能作为任何指标的数值依据**；需要数值请走同一主体的 XBRL 财务时序"
                        f"或巨潮年报正文抽取。原文（EDGAR 归档）：{url}"
                    ),
                    "source_type": "authority",
                }
            )
        if out:
            logger.info("权威源·SEC 命中 %d 条：%s", len(out), company[:20])
        return out
    except Exception as e:
        logger.info("权威源·SEC 失败(降级)：%s", e)
        return []


def fetch_sec_financials(company: str, years: int = 5) -> list[dict[str, Any]]:
    """给定主体 → 其**结构化财务时序**（营收/毛利/营业利润/净利润，年度值）

    走 data.sec.gov XBRL companyconcept（官方公开 JSON）。这是权威源里少见的
    **可直接计算**的一类：拿到数值后增速/CAGR 本地算，不依赖 LLM 算术。

    诚实边界：XBRL 概念名各司不同（同一指标可能叫 Revenues 或
    RevenueFromContractWithCustomerExcludingAssessedTax）→ 按候选表**逐个试**，
    都没有就返回空，**不拿近似概念冒充**。
    """
    if not company or not enabled():
        return []
    try:
        cik, shown = _sec_resolve(company)
        if not cik:
            return []
        for tag, cn in _SEC_CONCEPTS:
            r = _sec_get(_SEC_CONCEPT.format(cik=f"{cik:010d}", tag=tag))
            if r.status_code != 200:
                continue
            units = ((r.json() or {}).get("units") or {}).get("USD") or []
            # 只取年度值（10-K / FY）；同一年度多份申报（原始+修订）取最后申报的那条
            by_year: dict[str, dict[str, Any]] = {}
            for u in units:
                try:
                    if str(u.get("form") or "") not in ("10-K", "20-F"):
                        continue
                    if str(u.get("fp") or "") not in ("FY", ""):
                        continue
                    fy = int(u.get("fy") or 0)
                    if not fy:
                        continue
                    by_year[str(fy)] = u
                except Exception:
                    continue
            if not by_year:
                continue
            picks = sorted(by_year.items(), key=lambda kv: kv[0])[-max(2, int(years)) :]
            rows = []
            for fy, u in picks:
                val = u.get("val")
                if val is None:
                    continue
                try:
                    v = float(val)
                except Exception:
                    continue
                # 换算成"亿元"便于中文报告直接引用（原始为美元）
                rows.append((fy, v, f"{v / 1e8:,.1f} 亿美元"))
            if len(rows) < 2:
                continue
            series = "；".join(f"{fy}财年 {txt}" for fy, _v, txt in rows)
            # 本地算 CAGR（可计算数据的意义所在：算术在代码里，不交给 LLM）
            # **判据统一收口在 `core/series_math.py`**（2026-09-15）：
            # SEC XBRL 目前取到的都是金额概念（存量），走水平值公式是对的；
            # 但同一份错公式曾在本仓库另外两处把"增速序列"算成 CAGR≈16.2%，
            # 所以这里不再各写一份，避免第三次踩同一个坑。
            cagr = ""
            try:
                from core.series_math import derive_growth

                _label, _c = derive_growth([(str(_y), float(_v)) for _y, _v, _ in rows], cn, "美元")
                if _c is not None:
                    # **标签由 `series_math` 给，这里不再自己拼一个口径词。**
                    # 病案（2026-09-16）：这里原先写死前缀「区间年复合增速 CAGR≈」再把
                    # `_label`括在括号里，于是同一句话里印两遍口径
                    # （`…CAGR≈5.4%（区间年复合增速 CAGR）`）。同一事实两处词形，
                    # 一旦判据改成"增速序列"就会印出「CAGR≈4.9%（区间年均增速（几何平均））」
                    # 这种自相矛盾的句子。
                    cagr = f"{_label}≈{_c:.1f}%（本地计算）"
            except Exception:
                cagr = ""
            url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json"
            out_txt = f"{shown} {cn}（{tag}，SEC XBRL 结构化数据，单位美元）——{series}"
            if cagr:
                out_txt += f"；{cagr}"
            logger.info("权威源·SEC XBRL 命中 %s：%s %d 期", company[:16], cn, len(rows))
            return [
                {
                    "title": f"{shown}：{cn}（SEC XBRL 年度时序）"[:150],
                    "url": url,
                    "date": str(rows[-1][0]),
                    "publisher": f"SEC EDGAR·{shown}",
                    "snippet": f"{shown} {cn}：" + "，".join(f"{fy} {txt}" for fy, _v, txt in rows),
                    "content": out_txt,
                    "source_type": "authority",
                }
            ]
        return []
    except Exception as e:
        logger.info("权威源·SEC XBRL 失败(降级)：%s", e)
        return []


# ============================================================
# 源 4-7 · 行业机构 / 政府采购 / 投资者互动（HTML 静态列表页）
# ------------------------------------------------------------
# 2026-09-11 本机实测可用（无 robots 禁、免登录、无验证码、纯静态 HTML），
# 补的是**行业解释层**：官方统计给"总量"，上市公司给"个体"，行业协会给"为什么"。
# 全部**按关键词触发**（命中才取）：不做无谓请求、不让无关源污染报告来源结构。
# ============================================================
@dataclass(frozen=True)
class _ListSite:
    sid: str
    name: str
    pages: tuple[str, ...]      # 列表页（可多页，取到 limit 即止）
    link_re: str                # 文章链接正则（必须含 href 组 + 标题组）
    date_in: str                # 从 href 里提日期的正则（可选，无则空）
    triggers: tuple[str, ...]   # 命中任一即取该源（空 = 不按关键词触发）
    note: str


_LIST_SITES: tuple[_ListSite, ...] = (
    _ListSite(
        sid="ccgp",
        name="中国政府采购网",
        pages=("http://www.ccgp.gov.cn/cggg/zygg/",),
        # 实测形如 ./gkzb/202609/t20260911_27310452.htm —— 路径自带年月，可确定性解析
        link_re=r'href="\.?/?([^"]*?/(\d{6})/t(\d{8})_\d+\.htm)"[^>]*>\s*([^<]{6,90}?)</a>',
        date_in=r"t(\d{8})_",
        triggers=("采购", "招标", "中标", "政府购买", "公共资源", "投标", "供应商"),
        note="财政部指定发布媒体；采购中标与预算口径 = 大客户/渠道侧需求的硬证据",
    ),
    _ListSite(
        sid="caam",
        name="中国汽车工业协会",
        pages=("http://www.caam.org.cn/chn/1/cate_1/list_1.html",),
        link_re=r'href="([^"]*?con_\d+\.html)"[^>]*>\s*([^<]{6,90}?)</a>',
        date_in="",
        triggers=("汽车", "新能源车", "整车", "乘用车", "商用车", "车企", "电动车", "电车", "4S"),
        note="汽车行业月度产销/运行口径（行业机构层）",
    ),
    _ListSite(
        sid="ccfa",
        name="中国连锁经营协会",
        pages=("http://www.ccfa.org.cn/portal/cn/index.jsp",),
        link_re=r'href="([^"]*?xiangxi\.jsp\?id=\d+&(?:amp;)?type=\d+)"[^>]*>\s*([^<]{6,90}?)</a>',
        date_in="",
        triggers=("连锁", "加盟", "便利店", "超市", "餐饮连锁", "特许", "门店"),
        note="连锁零售/餐饮百强榜、门店与加盟口径",
    ),
)

_SSE_EDC = "https://sns.sseinfo.com/ajax/feeds.do"
_SSE_EDC_TRIGGERS = ("经营", "表态", "投资者", "问询", "订单", "产能", "分红", "股价", "回应")

# 导航/栏目噪声词：静态列表页常把"常务理事单位/关于我们/更多"这类**非文章条目**混在链接里
# （实测 CAAM 列表页抓出"常务理事单位""普通会员单位"= 会员名录，不是行业动态）。
_NAV_WORDS = (
    "更多", "首页", "关于我们", "联系我们", "会员单位", "理事单位", "常务理事",
    "加入我们", "网站地图", "版权", "意见反馈", "人才招聘", "English", "登录", "注册",
)


def _is_nav_noise(title: str) -> bool:
    return any(w in (title or "") for w in _NAV_WORDS)


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def _date_from_match(dm: "re.Match[str] | None", text_fallback: str = "", text_re: str = "") -> str:
    """从链接/标题里抽出日期（**多形态**，抽不到就留空，绝不编一个日期出来）

    支持四种形态，都是实测遇到的：
      · 3 组 `(年)(月)(日)` → `YYYY-MM-DD`（江苏 `/art/2026/8/24/`、河南 `/2026/08-19/`、四川 `/2026/8/18/`）
      · 8 位单组 `20260910` → `YYYY-MM-DD`（政府站最常见的 `/202609/t20260910_...`）
      · 6 位单组 `202608`   → `YYYY-MM`（苏州 `/sjfbyjd/202608/`，路径只到月）
      · 4 位单组 `2026`     → `YYYY`（浙江/杭州/宁波 `/art/2026/art_...`，路径**只含年份**）

    宁可给出**精度更低的真实日期**（"2026"），也不给一个看起来完整、实际是补零编出来的
    `2026-01-01`——后者会污染时效判断，也会在报告里印出一条我们并不知道的日期。
    """
    if not dm:
        return ""
    try:
        g = [x for x in (dm.groups() or ()) if x is not None]
    except Exception:
        g = []
    if len(g) >= 3:
        try:
            return f"{int(g[0]):04d}-{int(g[1]):02d}-{int(g[2]):02d}"
        except Exception:
            return ""
    if g:
        d = re.sub(r"\D", "", str(g[0]))
        if len(d) >= 8:
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        if len(d) == 6:
            return f"{d[:4]}-{d[4:6]}"
        if len(d) == 4:
            return d
    # 链接里没有日期 → 再看**锚文本**（杭州的发布日期只写在标题里：「2026年7月月报2026-08-28」）
    if text_re and text_fallback:
        tm = re.search(text_re, text_fallback)
        if tm:
            return _date_from_match(tm)
    return ""


def _pick_date(href: str, title: str, date_in: str, text_re: str) -> str:
    """取**精度更高**的那个日期（同一条目可能两处都有日期，且精度不同）

    实测杭州：链接里只有年份（`/art/2026/art_…` → "2026"），
    而列表页标题里写着完整发布日期（「2026年7月月报**2026-08-28**」）。
    只认 href 会把一条有确切发布日的条目降级成"年份不详"——
    而时效判断正是靠这个日期。取更长的那个即可，不用猜哪个"更权威"。
    """
    d_href = _date_from_match(re.search(date_in, href) if date_in else None)
    d_text = _date_from_match(re.search(text_re, title) if text_re else None)
    return d_text if len(d_text) > len(d_href) else d_href


def fetch_list_site(site: _ListSite, keyword: str = "", limit: int = 3) -> list[dict[str, Any]]:
    """抓一个行业机构/政府站点的**静态列表页** → 最新相关文章条目

    相对路径一律 `urljoin(page, href)` 解析（统计局踩过：字符串拼接会丢栏目前缀）。
    `site` 既接受 `_ListSite`，也接受 `core.region_stat.RegionSite`（字段名对齐，鸭子类型）。
    """
    if not enabled():
        return []
    toks = [t for t in re.split(r"[\s，,。、;；/]+", keyword or "") if len(t) >= 2][:4]
    _text_re = getattr(site, "text_date_re", "") or ""
    _title_re = getattr(site, "title_re", "") or ""
    out: list[dict[str, Any]] = []
    try:
        for page in site.pages:
            if len(out) >= limit:
                break
            r = _get(page)
            if r.status_code != 200:
                continue
            html = r.text or ""
            for m in re.finditer(site.link_re, html):
                if len(out) >= limit:
                    break
                href, title = m.group(1), _strip_html(m.group(4) if m.lastindex and m.lastindex >= 4 else m.group(2))
                if not href or len(title) < 6:
                    continue
                if _is_nav_noise(title):
                    continue
                # 正向标题门（地域站专用）：条目必须**像一条统计发布**才准进。
                # 统计局首页常把"数据发布"和"局内新闻/党建/慰问"混在一条流里，
                # 栏目对了、条目无关——实测江苏省局头三条是《走访慰问老党员》《政绩观教育》。
                if _title_re and not re.search(_title_re, title):
                    continue
                # 关键词门：站点栏目混杂，只留与本问题相关的条目
                if toks and not any(t in title for t in toks):
                    continue
                url = urljoin(page, href.replace("&amp;", "&"))
                if any(o["url"] == url for o in out):
                    continue
                date = _pick_date(href, title, site.date_in or "", _text_re)
                out.append(
                    {
                        "title": f"{site.name}：{title}"[:150],
                        "url": url,
                        "date": date,
                        "publisher": site.name,
                        "snippet": f"{site.name}公开发布：{title}（{date or '日期见原文'}）",
                        "content": f"{site.name}公开发布：{title}。用途：{site.note}。原文：{url}",
                        "source_type": "authority",
                    }
                )
        if out:
            logger.info("权威源·%s 命中 %d 条：%s", site.name, len(out), (keyword or "")[:18])
    except Exception as e:
        logger.info("权威源·%s 失败(降级)：%s", site.name, e)
    return out[:limit]


def fetch_sse_edc(company: str = "", limit: int = 3) -> list[dict[str, Any]]:
    """上证e互动（沪市投资者关系问答）—— 实测免登录、robots 无声明、返回 HTML 片段

    补的是巨潮覆盖不到的那一层：**经营表态 / 投资者关切**（法定披露只讲结果，不讲回应）。
    边界：只覆盖**沪市**；深市互动易是 JS 壳+私有接口，**不做**。

    **必须按主体过滤**（2026-09-11 修）：接口返回的是**全站最新问答**，不过滤就会出现
    "问比亚迪、答山鹰国际"这种串台污染（实测踩过）。故逐条抽取 公司名+代码，
    与该主体匹配不上的一律丢弃；一个都不匹配 → 返回空（诚实空手，不塞无关内容）。
    """
    name = (company or "").strip()
    if not name or not enabled():
        return []
    try:
        r = _get(f"{_SSE_EDC}?type=10&show=1&pageSize=50&lastid=-1&page=1")
        if r.status_code != 200:
            return []
        html = r.text or ""
        # 形如 <div class="m_feed_txt" id="m_feed_txt-1788445"> ... <a href='user.do?uid=..'>:珀莱雅(603605)</a>正文
        out: list[dict[str, Any]] = []
        for m in re.finditer(r'id="m_feed_txt-(\d+)"[^>]*>(.*?)</div>', html, re.S):
            if len(out) >= limit:
                break
            mid, body = m.group(1), m.group(2)
            text = _strip_html(body)
            if len(text) < 10:
                continue
            # 主体门：从条目里抽 公司名(代码) → 与请求主体比对，不匹配直接丢
            #
            # ⚠️ **这里以前是 fail-open，而且是一处很隐蔽的 fail-open**（2026-09-16 全量审计）：
            # 原写法是
            #     item_name = cm.group(1) if cm else ""
            #     if name not in item_name and item_name not in name: continue
            # 正则**不命中**时 `item_name` 是空串，而 `"" not in name` 恒为 **False**
            # （空串是任何字符串的子串）→ 整个条件为假 → **这一条不但没被丢掉，反而留下了**。
            # 后果：一条无法归属主体的问答（可能是**别家公司**的）被挂上
            # `publisher="上证e互动·"`（名字是空的）、`source_type="authority"`，
            # 以 L2 权威身份进入本次调研的报告。**这是最坏的一类错**：
            # 假来源比没有来源更伤，而它还被标成了"官方互动平台"。
            # 现在 fail-closed：抽不出主体就不收——**宁可少一条，也不要一条挂错主体的权威源**。
            cm = re.search(r"([一-龥A-Za-z0-9]{2,12})\s*[（(](\d{6})[)）]", text)
            if not cm:
                continue      # 无法归属主体 → 丢弃（不是"跳过比对"）
            item_name = (cm.group(1) or "").strip()
            if not item_name:
                continue
            if name not in item_name and item_name not in name:
                continue
            code = cm.group(2)
            url = f"https://sns.sseinfo.com/company.do?uid={mid}"
            out.append(
                {
                    "title": f"上证e互动·{item_name}：{text[:70]}"[:150],
                    "url": url,
                    "date": "",
                    "publisher": f"上证e互动·{item_name or code}",
                    "snippet": text[:160],
                    "content": (
                        f"上证e互动（上交所投资者关系互动平台）{item_name}（{code}）投资者提问/公司回应："
                        f"{text[:400]}。用途：上市公司经营表态与投资者关切（法定披露覆盖不到的一层）。"
                    ),
                    "source_type": "authority",
                }
            )
        if out:
            logger.info("权威源·上证e互动 命中 %d 条：%s", len(out), name[:16])
        return out
    except Exception as e:
        logger.info("权威源·上证e互动 失败(降级)：%s", e)
        return []


# ============================================================
# 调度与适配
# ============================================================
_COMPANY_HINT = re.compile(r"([一-龥A-Za-z]{2,12})(?:集团|股份|有限公司|公司)")

# 常见企业行业后缀：用于从问句里摔出"公司名候选"（如"比亚迪汽车""蜜雪冰城餐饮"）
_COMPANY_SUFFIX = re.compile(r"([一-龥]{2,6})(?:汽车|银行|科技|控股|电器|食品|乳业|医药|地产|能源|电器|集团|超市|餐饮)")
_MACRO_HINT = ("市场规模", "行业规模", "增速", "增长率", "人均", "总额", "渗透率", "价格", "消费", "经济", "政策")

# **开"宏观源"这道门用的词表，与上面那张不是同一张**（2026-09-18 实跑病案）。
#
# 病案：一份下沉咖啡报告里，`China GDP年增速（World Bank）` 这一条被同时挂进
# **进入壁垒 / 单位经济 / 趋势风险 / 下沉客群消费心理洞察** 四个章节，
# 引文核验逐条判 ❌ 不相关（"宏观GDP增速与企业微观层面的单位经济模型无直接对应关系，
# 属于张冠李戴"）——**28 对里 4 对（14%）是被机制自己造出来的假引用**。
#
# 两个叠加的根因：
#   ① **判据用的是全文**（`question + subtask_desc + queries`）——
#      用户的问句里几乎必然含"市场规模/消费/价格"，于是**每个子任务都开门**；
#   ② 词表本身太宽：`经济` 命中「单位**经济**」、`消费` 命中「**消费**心理洞察」——
#      那是**章节名**，不是"这是宏观题"的证据。
#
# 所以开门的判据改成两条**同时**满足：**只看本子任务自己的文本** + **只认真正的宏观指标词**
# （`价格/消费/经济` 从开门词表里去掉——它们是章节名里最常见的字，留着就等于门永远开着）。
# `_MACRO_HINT` 本身**保持不变**：第 1933 行用它从**问句**里抠宏观短语（那是它的正确用途），
# 开门与抠短语是两件事，用两张表。
_MACRO_SCOPE = (
    "市场规模", "行业规模", "宏观", "GDP", "gdp", "增速", "增长率", "复合增长",
    "人均", "总额", "渗透率", "人口", "城镇化", "通胀", "CPI", "社零", "消费品零售",
)

# 经营数字类触发词：命中才做年报 PDF 正文抽取（下载+解析约 5s，不做无谓开销）
_FIN_HINT = (
    "营收", "营业收入", "净利润", "归母", "毛利", "利润", "财务", "财报", "年报",
    "经营数据", "业绩", "盈利", "亏损", "现金流", "总资产", "净资产", "单位经济",
)
# 行业侧触发词：问"市场规模/份额/竞争格局"时也去读年报——
# 因为**年报「行业情况」章节里就有这些数字，而且是经审计的**。
# 只按 `_FIN_HINT` 触发的话，这类题永远只拿到第三方研报的估算口径。
_INDUSTRY_HINT = (
    "市场规模", "行业规模", "市场份额", "市占率", "占有率", "竞争格局", "集中度",
    "行业趋势", "行业情况", "行业地位", "门店数", "渗透率", "竞争对手", "格局",
)

# 主体名**停用词**：这些不是公司名，但中文无词边界时会被兜底逻辑当成句首实体取出来。
# 病案（实测）：「中国咖啡品牌出海东南亚的机会」→ 兜底取到 **"中国"** → 巨潮 topSearch("中国")
# 命中 **中国宝安** 的年报 → 把一家无关上市公司的财报注进咖啡出海报告里。
# 宁可少取，不可串台——串台的一手源比没有一手源危险得多（会被当成权威证据引用）。
_COMPANY_STOP = frozenset(
    (
        "中国", "全国", "我国", "国内", "全球", "世界", "国际", "海外", "出海",
        "行业", "市场", "产业", "领域", "赛道", "品类", "品牌", "企业", "公司",
        "消费", "用户", "客户", "人群", "需求", "供给", "趋势", "未来", "前景",
        "投资", "融资", "创业", "进入", "下沉", "一线", "二线", "三线", "城市",
        "农村", "县城", "乡镇", "线上", "线下", "电商", "渠道", "价格", "成本",
        "如何", "什么", "怎么", "是否", "可以", "应该", "值得", "分析", "研究", "报告",
    )
)


# 通用**行业词/业态词**：它们长得像公司名，其实是"做什么的"。
# 病案（2026-09-14 审计）：`_company_candidates("连锁餐饮加盟模式的门店数")`
# 实测返回 `['连锁','连锁餐饮','连锁餐']` → 巨潮 `keyWord=连锁` 命中 **红旗连锁**（便利店），
# 把一家无关上市公司的年报当 L1 一手源注进报告，还会抽出它的营收/总资产数值。
# 对照：本文件 `_COMPANY_STOP` 的注释早就写过同一个道理（"中国"→中国宝安），只是词表没收全。
_GENERIC_BIZ = (
    "连锁", "餐饮", "科技", "控股", "食品", "电器", "零售", "超市", "药业", "医药",
    "地产", "能源", "汽车", "传媒", "物流", "家居", "服装", "旅游", "酒店", "教育",
    "银行", "证券", "保险", "投资", "贸易", "实业", "商贸", "文化", "健康", "环保",
    "智能", "电子", "机械", "化工", "建材", "纺织", "农业", "牧业", "乳业", "酒业",
    "饮料", "珠宝", "百货", "商城", "电商", "网络", "信息", "软件", "数据", "通信",
    "航空", "海运", "港口", "电力", "燃气", "水务", "影视", "游戏", "母婴", "美妆",
    "火锅", "茶饮", "咖啡", "烘焙", "快消", "生鲜", "便利", "加盟", "直营", "品牌",
)
_ENTITY_NOISE = re.compile("|".join(_GENERIC_BIZ) + r"|集团|股份|有限公司|公司|股份有限")


def _same_entity(query: str, sec_name: str) -> bool:
    """查询词与巨潮返回的证券简称**是不是同一个主体**

    判据刻意不是"互相包含"：候选词常是**不完整的前缀**
    （`_COMPANY_HINT` 从"内蒙古伊利实业集团股份有限公司"切出的是"内蒙古伊利实业"），
    而 `zwjc` 是**证券简称**（"伊利股份"）——两者互不包含，用互相包含判会把
    这类**真命中全判成串台**（比误放更糟：直接丢掉最硬的一手源）。

    做法：两边都剥掉行业词与公司后缀，再要求**包含关系**且短的一侧 ≥2 字。
      · "连锁" → 剥完是空串 → **判否**（这正是要拦的）
      · "内蒙古伊利实业" vs "伊利股份" → "内蒙古伊利" / "伊利" → 包含 → **判是**
    """
    a = _ENTITY_NOISE.sub("", str(query or "")).strip()
    b = _ENTITY_NOISE.sub("", str(sec_name or "")).strip()
    if len(a) < 2 or len(b) < 2:
        return False
    return a in b or b in a


def _company_candidates(text: str) -> list[str]:
    """从问句里摔公司名候选（不追求精准，靠 cninfo topSearch / SEC 代码表校验：查不到就空手回来）

    中文没有词边界，句子开头的实体名会被后面的虚词粘住：
    "星巴克在中国的下沉…" 直接取 4 字会得到 **"星巴克在"**（实测踩过，导致 cninfo/SEC 双双查不到）。
    所以句首兜底**同时给出 2/3/4 字三种前缀**，逐个交给源侧校验——查得到的那个自然胜出。
    同时过 `_COMPANY_STOP`：国名/行业词等**不是主体名**，放进去只会命中无关公司（实测"中国"→中国宝安）。
    """
    out: list[str] = []
    m = _COMPANY_HINT.search(text or "")
    if m:
        out.append(m.group(1))
    for m2 in _COMPANY_SUFFIX.finditer(text or ""):
        if m2.group(1) not in out:
            out.append(m2.group(1))
    # 句首兜底：2/3/4 字都试（实体名长度不定，源侧会告诉我们哪个对）
    head = re.match(r"^[\s【“\"']*([一-龥]{2,})", text or "")
    if head:
        s = head.group(1)
        for n in (4, 3, 2):
            if len(s) >= n and s[:n] not in out:
                out.append(s[:n])
    # 行业词/业态词在**候选阶段**就剔掉：它们进 topSearch 只会命中无关主体
    # （实测 `keyWord=连锁` → 红旗连锁）。词表见 `_GENERIC_BIZ`。
    return [c for c in out if c not in _COMPANY_STOP and c not in _GENERIC_BIZ][:4]


# 进程内结果缓存：分**两类**分别缓存（此前用 question 单一键 → 14 个子任务共享
# 第一次的 NBS 结果，等于"用子任务A的关键词给子任务B取证"= 维度错配）。
# 巨潮按公司候选缓存（公司级，与子任务无关）；统计局按关键词缓存（维度级，各自命中）。
_CN_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_NBS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_NBS_DETAIL_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
# 地域统计局**文章正文**的抽取结果（按文章 URL 缓存；一篇月报会被多个子任务用到，
# 不缓存就会对同一个政府站重复抓正文）。与 `_LIST_CACHE` 分开：一个是列表页、一个是正文页。
_REGION_DETAIL_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_SEC_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_SEC_FIN_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_LIST_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CN_FIN_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_WB_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}   # 供 core/authority_intl.py 复用
_INTL_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CACHE_TTL = 600.0


# per-key 单飞（single-flight）：同一键并发只让**一个**线程真的去抓，其余等它。
# 病案（2026-09-14 审计）：`_cached` 是无锁 check-then-act —— 4 个并行子任务同时未命中
# 同一个键时，**四个线程都会去抓**。对"正文无数值、数据全在 30MB 附件 PDF"的地方统计局页面
# （注释里点名的杭州市统计局月报就是这类），就是 4 次 PDF 下载 + 4 次解析。
# 这恰好是这些缓存存在的唯一理由，也直接违反本项目"不做无谓请求"的纪律。
_CACHE_LOCKS: dict[str, threading.Lock] = {}
_CACHE_LOCKS_GUARD = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _CACHE_LOCKS_GUARD:
        lk = _CACHE_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _CACHE_LOCKS[key] = lk
    return lk


def _cached(cache: dict, key: str, fn) -> list[dict[str, Any]]:
    hit = cache.get(key)
    if hit and (time.monotonic() - hit[0]) < _CACHE_TTL:
        return hit[1]
    with _lock_for(key):
        # 双检：等锁期间别的线程可能已经把它填上了
        hit = cache.get(key)
        if hit and (time.monotonic() - hit[0]) < _CACHE_TTL:
            return hit[1]
        val = fn()
        cache[key] = (time.monotonic(), val)
        return val


def _sec_candidates(text: str) -> list[str]:
    """文本里出现的美股主体候选 —— **与 `_company_candidates` 相互独立**。

    为什么必须独立：一句话里同时有 A 股与美股主体时（"比亚迪和特斯拉谁更强"），
    旧逻辑是「巨潮命中一家就 break，`_got_cn=True` 后整段 SEC 跳过」→
    **美股那家永远不会被取**。而"中外对标"恰恰是出海/竞争类问题的常见形态。
    别名表是**显式**的（不是猜 NER），扫描成本为零、不会误伤别的词。
    """
    t = text or ""
    out: list[str] = [k for k in _SEC_ALIAS if k in t]
    for c in _company_candidates(t):
        if c not in out:
            out.append(c)
    return out[:3]


def _macro_kw(question: str, queries: list[str] | None, subtask_desc: str) -> list[str]:
    """给统计局检索用的关键词候选（按优先级）

    病案：「星巴克在中国的下沉还有空间吗？**咖啡市场规模**多大」——
    `_MACRO_HINT` 判定"这是宏观题"通过了，但原来拿去检索的是该子任务的检索词（星巴克/营收/门店），
    统计局当然搜不到：**门开了却拿错了钥匙**。
    所以除了子任务词，再从**问句里把宏观短语抠出来**（"咖啡市场规模"）作为第二把钥匙。
    """
    out: list[str] = []
    q = " ".join(queries or []).strip()
    if q:
        out.append(q[:32])
    # 从问句里抠"XX + 宏观词"的短语：**先按标点切子句**，再清洗疑问/程度尾巴。
    # （直接取"宏观词前 8 字"会切出 `还有空间吗？咖啡市场规模` 这种带标点疑问词的串，永远匹配不到标题）
    _TAIL_Q = re.compile(r"(多大|多少|如何|怎么样|怎样|是什么|有哪些|吗|呢|吧|的|了)+$")
    _HEAD_Q = re.compile(r"^(那么|那|请问|想问|想|要|现在|目前)+")
    for hint in _MACRO_HINT:
        if hint not in (question or ""):
            continue
        for clause in re.split(r"[，。；？！,;?!\n、]", question or ""):
            if hint not in clause:
                continue
            seg = _TAIL_Q.sub("", _HEAD_Q.sub("", clause.strip()).strip()).strip()
            if 3 <= len(seg) <= 32 and seg not in out:
                out.append(seg)
            break
        break
    d = (subtask_desc or "").strip()
    if d and d[:32] not in out:
        out.append(d[:32])
    return out[:3]


def want_tool(sid: str, only_tools: list[str] | None) -> bool:
    """本次是否打这个源族。`only_tools is None` → 全打（fail-open）。

    做成**模块级函数**而不是 `fetch_authority` 里的闭包：闭包只能靠执行整个函数来间接验证，
    而这条判定恰恰是最需要单测的一条（它判错一次就会静默关掉整层源）。

    **别名归并（实跑踩中）**：注册表里地域统计源记的是**一条聚合项 `region_stats`**
    （面板上显示为一个源），而取证层按 `tjj_*` 逐个判定（杭州/南京/宁波…各自独立）。
    于是模型选了 `region_stats`——一个最自然的选择——`want_tool("tjj_hangzhou", ...)`
    却返回 False，**16 个地方统计局被全体跳过**。
    这一条不加，工具选择就不是"少打几个源"，而是"把整个地域源层静默关掉"。
    """
    if only_tools is None:
        return True
    if sid in only_tools:
        return True
    # 聚合项 ↔ 成员项互认（地域统计：region_stats ↔ tjj_*）
    if sid.startswith("tjj_") and "region_stats" in only_tools:
        return True
    if sid == "region_stats" and any(str(t).startswith("tjj_") for t in only_tools):
        return True
    # 国际组织在注册表里是两条（worldbank / imf），历史上台账记过一条 `intl`；
    # 若模型给出 `intl`（不该有，但防脏输入）则两条都放行
    return sid in ("worldbank", "imf") and "intl" in only_tools


def _layer_of(sid: str) -> str:
    """源 id → **注册表里的**层级（取不到返回 ''，**不猜、不在调用点补一个**）。

    2026-09-18 第十跑病案（外部审计第 ⑨ 对：一个源在同一份报告里被标成三层）：
    列表站那一族在调用点被硬编码成 `"L1"`，而 CCFA / CAAM 在 `source_registry` 里
    都是 `L3 · 行业机构/协会`、`source_health.Probe` 也写 L3。后果实测：
      数据源体系表（注册表渲染）= L3 ／ 本次调用台账（这里）= L1 ／ 置信度台账 = L2
    ——**同一个源，一份报告，三个层级，而三个都是"系统说的"。**
    层级只有一个源（注册表），调用点再写一个就是第二个事实源。
    """
    try:
        from core.source_registry import layer_of_source

        return layer_of_source(sid) or ""
    except Exception:
        return ""


def fetch_authority(
    question: str,
    subtask_desc: str = "",
    limit: int = 3,
    queries: list[str] | None = None,
    trace: list[dict[str, Any]] | None = None,
    only_tools: list[str] | None = None,
) -> list[dict[str, Any]]:
    """按问题类型挑权威源（都可失败降级为 []）：
    - 命中"公司名候选" → 巨潮法定披露（公告/年报）
    - 命中宏观/规模/增速类 → 统计局数据发布（用**本次子任务自己的**关键词去匹配）
    - 命中地域名 → 该地统计局（省/市）数据发布栏目

    queries：本子任务的检索词（维度级），比整题问句更能命中对应指标的统计局文章。
    trace：本次调研的**调用台账**（外部传入的 list，本函数往里追加"哪个源回了几条"）。
           它是"这次报告真的用了什么源"的唯一依据——比"我们支持 15 个源"硬得多。
    only_tools：工具自主选择（`core/tool_select.py`）给出的本次应调用的源 id 名单。
           **None = 未选择 → 全打（fail-open）**；给了名单就只打名单里的源族，
           被跳过的源族会在台账里留一行"工具计划未选中"，以免把「主动不查」
           误读成「查了没查到」。
    """
    if not enabled():
        return []
    try:
        from core.source_health import record as _rec
    except Exception:  # pragma: no cover
        def _rec(*_a, **_k):  # type: ignore
            return None
    text = f"{question or ''} {subtask_desc or ''} " + " ".join(queries or [])
    # **本子任务自己的文本**（描述 + 检索词），不含用户问句。
    # 开"宏观源"这道门只用它——理由见 `_MACRO_SCOPE` 的病案：
    # 把问句算进来，等于"问句里出现过的宏观词，对所有子任务都开门"。
    _local = f"{subtask_desc or ''} " + " ".join(queries or [])
    # 按**源族**收集，最后轮转分配名额（而不是边取边拼）。
    # 病案：早先"先到先得"的拼接顺序下，排在前面的源族会把整个 out 配额吃光——实测
    #   "比亚迪和特斯拉谁更强"被 8 份年报填满、SEC 一条进不来；
    #   "星巴克…咖啡市场规模"被 SEC 的 4 条占满、统计局一条进不来。
    # 轮转后保证：**每个源族在被取第二条之前，都先贡献过第一条**。
    _fam: list[list[dict[str, Any]]] = []
    _trace = trace

    def _add(sid: str, name: str, items: list[dict[str, Any]] | None, layer: str = "") -> None:
        """挂一个源族 + 记一条**本次调用台账**（两件事必须同步，所以合成一个动作）

        台账是"这次报告真的向哪些源发了请求、各回来几条"的唯一依据。
        之所以合成一个动作而不是各写一行：分开写必然会漏——漏掉的那次调用
        在报告里就等于"这个源没用过"，而我们其实用过。
        """
        it = list(items or [])
        _fam.append(it)
        try:
            _rec(_trace, sid, name, it, layer=layer)
        except Exception:
            pass

    def _want(sid: str) -> bool:
        """见模块级 `want_tool`（别名归并/聚合项互认都在那里，且可单测）。"""
        return want_tool(sid, only_tools)

    def _skip(sid: str, name: str, layer: str = "") -> None:
        """被工具计划跳过的源族 —— **必须留痕**。

        不记的话，台账里"这个源没出现"会被读成"这个源死了/回空了"，
        而真相是"这次判断它用不上，主动没打"。**主动不查**和**查了没查到**是两件事。
        """
        try:
            _rec(_trace, sid, name, [], note="工具计划未选中，本次跳过（非回空）", layer=layer)
        except Exception:
            pass

    # 巨潮：公司候选逐个试，命中一家即止（公司级缓存，跨子任务复用）；
    # **巨潮取不到才试 SEC**（A 股优先——同一个主体不会两边都有，避免无谓跨境请求）
    # 单一源条数上限：巨潮一次能返回 N 份年报，若不设上限会**吃光整个 out 配额**，
    # 把同题里的美股主体/统计局/国际源全挤掉（实测："比亚迪和特斯拉谁更强" 被 8 份年报填满，
    # SEC 一条都进不来）。有价值的只是「最新财报的数字 + 最近几期报告」，历史列表无边际价值。
    _cn_cap = max(1, min(int(limit), 2))
    _cands = _company_candidates(question or "")
    _got_cn = False
    if not _want("cninfo"):
        _skip("cninfo", "巨潮资讯（A股法定披露）", "L2")
    for cand in (_cands if _want("cninfo") else []):
        got = _cached(_CN_CACHE, f"{cand}|{_cn_cap}", lambda c=cand: fetch_cninfo(c, limit=_cn_cap))
        if got:
            # 问的是经营数字时才做**年报正文抽取**（+~5s 下载解析）；只要链接不要数字就不花这个时间
            if any(k in text for k in _FIN_HINT) or any(k in text for k in _INDUSTRY_HINT):
                _add(
                    "cninfo", "巨潮资讯（A股法定披露）",
                    _cached(_CN_FIN_CACHE, cand, lambda c=cand: fetch_cninfo_financials(c, limit=2)),
                )
            _add("cninfo", "巨潮资讯（A股法定披露）", got)
            _got_cn = True
            break
    # SEC：**独立于巨潮是否命中**（见 `_sec_candidates` 说明）——
    # "比亚迪和特斯拉谁更强"这类中外对标题，A 股那家命中后旧逻辑会整段跳过 SEC，美股那家就丢了。
    if not _want("sec_edgar"):
        _skip("sec_edgar", "SEC EDGAR（美股法定申报）", "L2")
    elif not sec_contact_configured():
        # **通道停用也必须留痕**（2026-09-15 外部审计）。
        # 病案：`fetch_sec` 内部把 `SecUAUnconfigured` 吞掉并 `return []`，
        # 于是台账里 sec_edgar 这一行**直接消失**——与"关键词没触发""回空""网络异常"
        # **完全不可区分**；而同一份报告的附录里还印着"已接入 SEC EDGAR"。
        # **主动停用**和**查了没有**是两件事，台账必须能把它们分开。
        try:
            _rec(
                _trace, "sec_edgar", "SEC EDGAR（美股法定申报）", [],
                note="通道停用（`SEC_UA_CONTACT` 未配置 → fail-closed）——本次**未发起任何请求**",
                layer="L2",
            )
        except Exception:
            pass
    for cand in (_sec_candidates(text) if _want("sec_edgar") else []):
        if _got_cn and cand == (_cands[0] if _cands else ""):
            continue  # 同一主体巨潮已拿到，不必再去 SEC 试一遍
        got = _cached(_SEC_CACHE, f"{cand}|3", lambda c=cand: fetch_sec(c, limit=3))
        if got:
            # 结构化财务时序**前置**：它是可直接引用的数值（还能算增速），
            # 比"一篇 8-K 的链接"对报告的价值高得多；limit 截断时不该被挤掉。
            _add("sec_edgar", "SEC EDGAR（美股法定申报）",
                 _cached(_SEC_FIN_CACHE, cand, lambda c=cand: fetch_sec_financials(c)))
            _add("sec_edgar", "SEC EDGAR（美股法定申报）", got)
            break

    # 统计局：只用子任务自己的关键词（维度级缓存），不掺整题问句——否则每个维度都检索同一串词
    if not _want("nbs"):
        _skip("nbs", "国家统计局", "L1")
    if _want("nbs") and any(k in _local for k in _MACRO_SCOPE):
        # 关键词候选按序试（子任务词 → 问句里的宏观短语 → 子任务描述），
        # 命中即止：治"门开了却拿错钥匙"（判定是宏观题、却拿公司词去检索）
        _hits: list[dict[str, Any]] = []
        for _kw in _macro_kw(question, queries, subtask_desc):
            if not _kw:
                continue
            _hits = _cached(_NBS_CACHE, _kw, lambda k=_kw: fetch_nbs(k, limit=max(1, min(int(limit), 3))))
            if _hits:
                break
        # 统计局正文数值抽取：把命中的**首篇**文章正文抽成结构化数值（+1 次请求 ~1-2s）。
        # 这是"国家统计局从给链接到给数字"的关键一步；缓存按文章 URL，跨维度复用。
        # ⚠️ 必须**前置**于 `_hits`：两者 URL 相同，末尾去重是"先到先得"——
        #    顺序写反会把带数字的那条挤掉，只留一个光标题（实测踩过，且表面看"有命中"不易发现）。
        if _hits:
            _u0 = _hits[0].get("url") or ""
            _t0 = str(_hits[0].get("title") or "")
            if _u0:
                _add("nbs", "国家统计局",
                     _cached(_NBS_DETAIL_CACHE, _u0, lambda u=_u0, t=_t0: fetch_nbs_detail(u, t)))
        _add("nbs", "国家统计局", _hits)

    # 地域统计源（省/市统计局）：**问题里明确提到了某地**才取。
    # 抽不到地域词 → 一个请求都不发（本层的既有纪律：不做无谓请求）。
    # 上限 2 个地域，且省市同时命中时只留市（市级更具体，省级数不能冒充市级）。
    #
    # ⚠️ 这里**必须**配正文数值抽取，否则地域源贡献的只是一行标题
    # （"杭州市统计局：2026年7月月报"）——**那不是数据，是链接**。
    # 只对**首条**做抽取（+1 次请求/地域），与 NBS 同一策略：首条通常是最新一期，
    # 边际价值最高；列表里其余条目保持"给链接"的角色。
    try:
        from core.region_stat import sites_for
        for _rs in sites_for(text, limit=2):
            # 地域源同理：跳过的要留痕（16 个地方统计局不可能每个子任务都打一遍）
            if not _want(_rs.sid):
                _skip(_rs.sid, _rs.bureau, "L1")
                continue
            _hits = _cached(_LIST_CACHE, _rs.sid, lambda s=_rs: fetch_list_site(s, "", limit=2))
            if _hits:
                _u0 = _hits[0].get("url") or ""
                _t0 = str(_hits[0].get("title") or "")
                _d0 = str(_hits[0].get("date") or "")
                if _u0:
                    _add(
                        _rs.sid, _rs.bureau,
                        _cached(
                            _REGION_DETAIL_CACHE, _u0,
                            lambda u=_u0, t=_t0, d=_d0, s=_rs: fetch_stat_detail(
                                u, t, publisher=s.bureau, date_hint=d
                            ),
                        ),
                        layer="L1",
                    )
            _add(_rs.sid, _rs.bureau, _hits, layer="L1")
    except Exception as e:
        logger.info("地域统计源跳过（降级）: %s", e)

    # 行业机构/政府采购（关键词触发，最多 2 个站点——按需取，不做无谓请求）
    # **不做标题二次过滤**：这类垂类专业站本身就是过滤器（连锁协会发的就是连锁内容）。
    # 教训：曾用整句子任务词去过滤标题 → 中文无分隔被当成**一个长 token**，永远匹配不到 → 静默 0 条。
    # 现在取该站最新条目，并由数字护照/结论状态如实标注它是什么（不冒充"针对本问题的数据"）。
    _hit_sites = 0
    for _site in _LIST_SITES:
        if _hit_sites >= 2:
            break
        if not any(t in text for t in _site.triggers):
            continue
        if not _want(_site.sid):
            _skip(_site.sid, _site.name, _layer_of(_site.sid))
            continue
        _hit_sites += 1
        # 层级取自注册表（见 `_layer_of`）：原先这里 `_add(...)` 根本不传 layer，
        # 而 `_skip(...)` 硬编码 "L1" —— 同一族源"调用时没有层级、跳过时是 L1"。
        _add(_site.sid, _site.name,
             _cached(_LIST_CACHE, _site.sid, lambda s=_site: fetch_list_site(s, "", limit=2)),
             layer=_layer_of(_site.sid))
    # 上证e互动：触发词命中且能定位到 A 股主体时才取（只覆盖沪市）
    if not _want("sse_edc"):
        _skip("sse_edc", "上证e互动", "L2")
    if _want("sse_edc") and any(t in text for t in _SSE_EDC_TRIGGERS):
        _c0 = (_cands[0] if _cands else "") or ""
        if _c0:
            _add("sse_edc", "上证e互动", _cached(_LIST_CACHE, f"sse_edc|{_c0}", lambda c=_c0: fetch_sse_edc(c, limit=2)))

    # 国际组织开放统计（World Bank / IMF）：仅当问题涉及出海/跨境/国际对比，
    # 或宏观指标类（规模/人口/人均/增速）需要**官方对照口径**时取——否则白跑跨境请求。
    try:
        from core.authority_intl import fetch_intl, intl_wanted

        if not (_want("worldbank") or _want("imf")):
            _skip("worldbank", "世界银行开放数据", "L1")
            _skip("imf", "IMF WEO", "L1")
        elif intl_wanted(text) or any(k in _local for k in _MACRO_SCOPE):
            # ⚠️ **缓存键必须覆盖整段文本，不能用前 40 字**（2026-09-16 全量审计）。
            # 病案：原写法 `f"intl|{text[:40]}"` —— 子任务的检索文本**前 40 字常常是同一句**
            # （同一份调研里各维度共享问题主干），于是问句稍长就**所有子任务共用一个缓存键**：
            # 第一个子任务拿到的世界银行/IMF 数据被原样喂给后面每一个维度。
            # 这不是"省了一次请求"，是**跨维度串数据**：用户维度拿着竞争维度的宏观口径去写结论，
            # 而两个维度在报告里是两章——**同一个数出现在两章、各自声称支持不同的论断**。
            # 改用全文本的短哈希：稳定、不随前缀撞车、也不把长问句塞进键里。
            import hashlib as _hl

            _ck = "intl|" + _hl.sha1((text or "").encode("utf-8")).hexdigest()[:16]
            _intl = _cached(_INTL_CACHE, _ck, lambda: fetch_intl(question, subtask_desc, queries))
            # **按域名拆成两个源分别记账**（2026-09-14 修）。
            # 病案：原先记的是 `_add("intl", "国际组织开放统计（世界银行/IMF）", ...)`，
            # 而 `intl` **不是注册表里的源 id**（注册表里是 worldbank / imf 两条）。
            # 后果：调用台账里出现了一个注册表没有的"幽灵源"（渲染时层级为空、显示"—"），
            # 而"已接入 16 个源"与"台账里的源"对不上——评审只要对一眼就能发现。
            # 更要紧的是：工具自主选择按注册表 id 挑源，永远挑不中 `intl`，
            # 于是国际源会在有工具计划时被静默跳过。拆开记账，两个问题一起消失。
            _wb = [x for x in (_intl or []) if "worldbank" in str(x.get("url") or "")]
            _im = [x for x in (_intl or []) if "imf" in str(x.get("url") or "")]
            _other = [x for x in (_intl or []) if x not in _wb and x not in _im]
            if _want("worldbank"):
                _add("worldbank", "世界银行开放数据", _wb + _other, layer="L1")
            if _want("imf"):
                _add("imf", "IMF WEO", _im, layer="L1")
    except Exception as e:
        logger.info("权威源·国际统计跳过（降级）: %s", e)

    # 轮转分配：第 1 轮每个源族各取 1 条（按族优先级顺序），再第 2 轮……直到 limit。
    # 这样"上限"截断的是**每个族的第 N 条**，而不是**整个后面的族**。
    out: list[dict[str, Any]] = []
    _round = 0
    while len(out) < limit and any(len(f) > _round for f in _fam):
        for _f in _fam:
            if len(out) >= limit:
                break
            if len(_f) > _round:
                out.append(_f[_round])
        _round += 1

    # 去重 + 上限
    seen: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for it in out:
        u = it.get("url") or ""
        if u and u not in seen:
            seen.add(u)
            uniq.append(it)
    return uniq[:limit]


def to_raw(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """权威项 → `_search_mcp` 兼容的 raw 形态（直接进现有证据链/置信度/URL 核验管线）。

    注意：不伪造 `sources`（权威项本身即唯一来源页），`url` 就是可点原文。
    """
    raw: list[dict[str, Any]] = []
    for it in items or []:
        u = it.get("url") or ""
        if not u:
            continue
        raw.append(
            {
                "query": it.get("snippet", "")[:60],
                "title": it.get("title", ""),
                "url": u,
                "urls": [u],
                "sources": [
                    {
                        "site_name": it.get("publisher", ""),
                        "title": it.get("title", ""),
                        "url": u,
                        "date": it.get("date", ""),
                    }
                ],
                "content": it.get("content", ""),
                "publisher": it.get("publisher", ""),
                "date": it.get("date", ""),
                "source_type": "authority",
            }
        )
    return raw


def describe() -> str:
    return (
        "权威源直取：① 巨潮资讯（A 股法定披露，公告/年报 PDF 直链）"
        "② 国家统计局（数据发布/解读静态列表页，按**本子任务检索词**做最长公共子串≥4 字的指标族匹配）"
        "③ SEC EDGAR（美股法定申报 10-K/10-Q/20-F/6-K，**补巨潮的 A 股边界**）。"
        "只取公开发布内容、守 robots（SEC `/cgi-bin` 明令禁 → 不碰）、不绕风控、per-host ≥1s、摘要引用不转载；"
        "AUTHORITY_FETCH_ENABLED=0 可一键关闭。"
    )
