"""研策 · 跨平台中文字体解析（PDF 与图表共用一把尺）。

病案（2026-11-17，CI 第一次真跑 Linux 时抓到的）：
    PDF 导出的字体候选表**只写了 Windows 路径**（`C:\\Windows\\Fonts\\msyh.ttc`、
    `simhei.ttf`）。在 Ubuntu / Docker 里这两条**必然**都失败，于是代码静默退回
    `Helvetica` ——中文全部渲染成空白。而法定 AI 显式标识（GB 45438）
    恰恰是这份 PDF 里最该被看清的那一行。

    这个缺陷的性质：**代码看着完全正确，本地 Windows 上永远测不出来**。
    `selftest.py` 的字形用例是"真导出一次、把文字提取回来检查"，
    所以在 Windows 上一直是绿的，直到 CI 换成 Linux 才把它顶出来。

这里的判据只有一条：**字体文件真的存在，且真能画出常用汉字**。
不按平台分支猜、不按系统名猜——候选表逐条试，试不到就返回 `None`，
由调用方**告警或拒发**。绝不静默降级成"接口 200、内容是空白"。
"""

from __future__ import annotations

import os

# 显式指定字体时用这个环境变量（容器里挂载自有字体、或想统一字体时用）。
ENV_FONT = "YANCEC_CJK_FONT"

# 用来判"这个字体到底认不认中文"的最小样本：覆盖简体常用字与全角标点。
_CJK_PROBE = "中文报告标题注"

# 候选表：Windows / Linux（Debian·Ubuntu 系常见包）/ macOS。
# 顺序即优先级：先用系统自带的界面字体，再用思源/文泉驿这类确定含 CJK 的开源字体。
_CANDIDATES: tuple[str, ...] = (
    # ── Windows ──
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\Deng.ttf",
    # ── Linux：fonts-noto-cjk / fonts-wqy-zenhei / fonts-arphic-* ──
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/arphic/ukai.ttc",
    # ── macOS ──
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
)

# 兜底扫描的目录（各发行版路径并不统一，候选表不可能穷举）。
_FONT_DIRS: tuple[str, ...] = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
)

# 兜底扫描时只认名字里带这些片段的字体——避免把 DejaVu 这类无中文字形的字体当答案。
_NAME_HINTS: tuple[str, ...] = ("notosanscjk", "notoserifcjk", "wqy", "droidsansfallback", "sourcehansans")


def _is_cjk_capable(path: str) -> bool:
    """用 reportlab 真加载一次，查 cmap 里有没有 `_CJK_PROBE` 的每个字。

    判不了（reportlab 未安装、字体损坏、异常）一律当"不行"——
    在"能不能画出中文"这件事上，**不确定就是不行**（fail-closed）。
    """
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        name = "_cjk_probe"
        kwargs = {"subfontIndex": 0} if path.lower().endswith((".ttc", ".otc")) else {}
        try:
            pdfmetrics.registerFont(TTFont(name, path, **kwargs))
        except Exception:
            if kwargs:
                pdfmetrics.registerFont(TTFont(name, path))
            else:
                raise
        face = pdfmetrics.getFont(name).face
        return all(ord(ch) in face.charToGlyph for ch in _CJK_PROBE)
    except Exception:
        return False


def resolve_cjk_font() -> str | None:
    """→ 可用的中文字体路径；一个都没有则 `None`（调用方负责告警/拒发）。

    `YANCEC_CJK_FONT` 一旦设置就**只认它**：设置了却找不到，返回 `None` 而不是
    悄悄退回别的字体——显式配置写错了必须看得见。
    """
    env = (os.environ.get(ENV_FONT) or "").strip()
    if env:
        return env if os.path.isfile(env) else None

    for path in _CANDIDATES:
        if os.path.isfile(path) and _is_cjk_capable(path):
            return path

    for root in _FONT_DIRS:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                low = fn.lower()
                if not low.endswith((".ttf", ".ttc", ".otf", ".otc")):
                    continue
                if not any(h in low for h in _NAME_HINTS):
                    continue
                full = os.path.join(dirpath, fn)
                if _is_cjk_capable(full):
                    return full
    return None


def font_status() -> dict:
    """诊断用：字体解析结果与判据来源（健康检查、自检、报错信息都用它）。"""
    env = (os.environ.get(ENV_FONT) or "").strip()
    path = resolve_cjk_font()
    if env and env == path:
        how = f"env:{ENV_FONT}"
    elif path:
        how = "candidate"
    elif env:
        how = f"env:{ENV_FONT}（指定了但文件不存在或画不出中文）"
    else:
        how = "none"
    return {"path": path, "how": how, "ok": bool(path)}
