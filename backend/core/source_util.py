"""研策 · 信源 URL 规范键 / 可注册根域 / 引用配额（来源多样性子系统）

数据源"整篇只剩 3 个域名 / 同一篇文章重复 5 次 / 只有品牌官网"的构造性解药：
全链路去重只认**规范键**（canonical_key），不认原始 URL 字符串——
- m.163.com 与 www.163.com 的同一篇文章 → 同一规范键，只占 1 个名额；
- sohu 带不同 pvid / 163 带不同 utm 的同文双链 → 同一规范键；
- 同域**不同文章**（路径不同）→ 不同规范键，保留（配额在配额层控制）。

所有"产出可点来源列表 / 引文 / 资产"的入口去重都用 canonical_key，
否则同文镜像会白白吃掉面板名额，把多机构挤掉 → 观众看到"只有三个官网"。
"""
from __future__ import annotations

from urllib.parse import urlparse, urlunparse

# 可注册根域后缀（覆盖中国大陆站点主体：www/m.163.com→163.com；*.starbucks.com.cn→starbucks.com.cn）
_MULTI_TLD = (
    "com.cn", "org.cn", "net.cn", "gov.cn", "edu.cn", "ac.cn",
    "com.hk", "com.tw", "co.jp", "com.sg", "com.my", "co.kr", "cn.com",
)
# 纯追踪/渠道参数：query 若只由这些组成 → 整串丢弃（sohu 不同 pvid = 同文双链的元凶）
_TRACK_ONLY = {
    "pvid", "from", "spm", "sa", "source", "src", "origin", "token", "ivk_sa",
    "weixin", "wx_fmt", "scene", "refer_scene", "isappinstalled", "app",
    "wappos", "callback", "nsukey", "abtest", "rand", "clicktime", "t", "k",
    "spm_id_from", "dt_dapp", "dt_platform", "idfa", "share_uid", "share_token",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "vd_source", "searchword", "keyword", "openid", "user_id", "channel",
}
# 即使其余都是追踪键，这些"文章标识"键也必须保留（如 iresearch Report Detail?id=4528）
_ALWAYS_KEEP = {
    "id", "pid", "type", "page", "pn", "articleid", "docid", "sid", "nid",
    "article_id", "news_id", "report_id",
}


def registrable_root(url: str) -> str:
    """可注册根域：www/m.163.com → 163.com；www.stats.gov.cn → stats.gov.cn；
    *.starbucks.com.cn → starbucks.com.cn。无法解析返回空串。"""
    try:
        h = (urlparse(url or "").hostname or "").lower().split(":")[0]
    except Exception:
        return ""
    h = h.split("@")[-1].removeprefix("www.")
    if not h or "." not in h:
        return h
    parts = h.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_TLD:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def _is_mobile_host(url: str) -> bool:
    """m.xxx 移动镜像（mof/mon…开头的官方域名不算）"""
    try:
        h = (urlparse(url or "").netloc or "").lower().split(":")[0].split("@")[-1]
    except Exception:
        return False
    return h.startswith("m.") and not h.startswith("mof") and not h.startswith("mo")

def prefers_host(old_url: str, new_url: str) -> bool:
    """同规范键下二选一：desktop/www 优先于 m. 移动镜像"""
    return _is_mobile_host(old_url) and not _is_mobile_host(new_url)


def canonical_url(url: str) -> str:
    """规范 URL：去 fragment；host 去 www、小写；query 若全为追踪/渠道键则整串丢弃。

    例子：
      m.163.com/dy/article/X.html  vs  www.163.com/dy/article/X.html      → 同
      www.sohu.com/a/7?pvid=000115_3w_a  vs  www.sohu.com/a/7?pvid=b      → 同
      www.iresearch.com.cn/Report/Detail?id=4528                          → 保留 id
    """
    if not url:
        return ""
    try:
        p = urlparse((url or "").strip())
    except Exception:
        return ""
    if not p.scheme or not p.netloc:
        return ""
    host = (p.netloc or "").split("@")[-1].split(":")[0].lower().removeprefix("www.")
    keep_q = ""
    if p.query:
        keys = [kv.split("=", 1)[0].lower() for kv in p.query.split("&") if kv]
        if any(k in _ALWAYS_KEEP for k in keys) or not all(k in _TRACK_ONLY for k in keys):
            keep_q = p.query
    path = (p.path or "").rstrip("/") or "/"
    return urlunparse((p.scheme, host, path, "", keep_q, ""))


def canonical_key(url: str) -> tuple[str, str]:
    """去重键 = (可注册根域, 规范路径[+保留query])。

    同根域同路径（镜像/渠道差异）→ 同一键；同根域不同文章 → 不同键（由配额层限流）。
    """
    root = registrable_root(url)
    if not root:
        return ("", "")
    cu = canonical_url(url)
    path = "/"
    if cu:
        try:
            pp = urlparse(cu)
            path = (pp.path or "/") + ("?" + pp.query if pp.query else "")
        except Exception:
            path = "/"
    return (root, path)
