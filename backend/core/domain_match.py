"""域名匹配：把"来源白名单"从**子串包含**改成**域名后缀**语义

## 病案（2026-09-11 自查）

`core/source_router.route_source` 用的是 `any(d in url for d in TIER1_DOMAINS)`
——在**整个 URL 字符串**上做子串匹配，而不是在**域名**上做后缀匹配。

两个后果，前者是真问题：

① **误升级**：`https://www.baidu.com/link?url=...aHR0cHM6...` 这类跳转链接本身不含，
   但形如 `https://xxx.com/redirect?to=https://www.gov.cn/...` 的**跳转/引用链接**，
   query 里带了 `gov.cn` 字样，就会被直接判成 `Tier1 政府官方`。
   数据源面板上于是出现一条"政府官方"，点进去是别人的跳转页——
   **这正是"来源虚高"最典型、也最难被一眼看穿的形态**。
② **误降级**：低质农场域若出现在别人 URL 的 query 里（`?ref=chinairn.com`），
   也会被莫名降档。

## 语义

- 模式 `gov.cn`      → 命中 `gov.cn` 本身与其**任意子域**（`tjj.zj.gov.cn`）
- 模式 `.gov`（带点）→ 视为**顶级域后缀**，命中 `x.gov`、`a.b.gov`（裸 `gov` 不算）
- 一律**不做子串匹配**：`evil.com/gov.cn` 不命中

之所以要单独一个模块：这套语义在 `source_router` / `source_quality` /
`region_stat` 三处都要用，而三处**原来各写了一遍子串版**，一起错。
"""
from __future__ import annotations

from urllib.parse import urlparse


def host_of(url: str) -> str:
    """URL → 小写主机名（去掉前导点与端口）。取不到返回空串。

    刻意**不**用 `netloc` 直接返回：`netloc` 带端口与 userinfo
    （`user@host:8080`），拿去做后缀匹配会漏。
    """
    try:
        p = urlparse(str(url or "").strip())
    except Exception:
        return ""
    h = (p.hostname or "").lower().strip(".")
    return h


def host_hit(host: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """主机名是否命中任一模式（后缀语义，非子串）"""
    h = (host or "").lower().strip(".")
    if not h:
        return False
    for raw in patterns or ():
        p = str(raw or "").strip().lower()
        if not p:
            continue
        if p.startswith("."):
            # `.gov` / `.edu` —— 顶级域后缀：必须**完整**落在标签边界上
            # （`x.gov`、`a.b.gov` 命中；`notgov` 不命中）
            if h.endswith(p) and len(h) > len(p):
                return True
        else:
            p = p.lstrip(".")
            if h == p or h.endswith("." + p):
                return True
    return False


def domain_hit(url: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """URL 是否命中任一域名模式（**先取主机名，再做后缀匹配**）"""
    return host_hit(host_of(url), patterns)


def host_match_any(url: str, groups: tuple[tuple[str, ...], ...]) -> tuple[int, str] | None:
    """按序命中多组模式 → (组序号, 命中的模式)。

    给"先判 Tier1、再判 Tier2、最后兜底"这类**有序**分级用：
    顺序即优先级，返回第一个命中的组，调用方据此决定档位。
    """
    h = host_of(url)
    if not h:
        return None
    for i, pats in enumerate(groups):
        for raw in pats or ():
            p = str(raw or "").strip().lower()
            if not p:
                continue
            if p.startswith("."):
                if h.endswith(p) and len(h) > len(p):
                    return i, p
            else:
                p = p.lstrip(".")
                if h == p or h.endswith("." + p):
                    return i, p
    return None


def describe() -> str:
    return (
        "域名匹配采用**后缀语义**（在主机名上匹配，不在 URL 全串上做子串包含）："
        "模式 `gov.cn` 命中其本身与任意子域（`tjj.zj.gov.cn`）；带点模式 `.gov` 视为顶级域后缀"
        "（`x.gov` 命中、裸 `gov` 不命中）。这样 `xxx.com/redirect?to=gov.cn` 这类"
        "**跳转/引用链接**不会再被误升级成「政府官方」——来源分级虚高最典型的形态。"
    )
