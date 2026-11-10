"""研策后端 · core 包

## 这个文件为什么存在（两件事，都别再删）

### ① 把 `core` 从**命名空间包**变成**常规包**
病案（2026-09-11，`mcp/` 上真实发生过）：一个**没有 `__init__.py`** 的目录是"命名空间包"，
而 Python 的规则是——**常规包优先于命名空间包，即使本地目录在 sys.path 更前面**。
当时 `mcp/` 缺 `__init__.py`，用系统 Python 启动时 PyPI 上的 `mcp` 包把本地的
`mcp/search.py` 整个遮掉，`No module named 'mcp.search'` → 每一章都变成"（检索失败）"、
0 引文、报告 23KB。**症状看起来像"检索挂了"，根因是包解析**。
`core` 与 `agents` 同样是裸目录，同样有这个风险 —— 这一个文件把它关掉。

### ② 让 `.env` 在**任何** `core.*` import 时进入 `os.environ`
详见 `core/config.py` 顶部病案：`.env` 原本只被读进 pydantic `Settings`，从不写
`os.environ`，于是 `AUTHORITY_FETCH_ENABLED`（总开关）/ `SEC_UA_CONTACT`（SEC 合规）
/ `CITATION_MEDIA_RATIO` 等**直接用 `os.environ` 读的键全部静默取默认值**。
放在这里而不是各模块各写一行：**七个调用点各写一遍，就一定会漏一个**，
而漏掉的那个（比如"总开关关不掉"）不会报错，只会安静地不生效。
"""
from __future__ import annotations

try:  # 容错：没有 dotenv 时不影响任何业务（`Settings` 那条路仍可用）
    from core.config import ENV_FILE  # noqa: F401  —— 副作用即加载 .env，见 core/config.py
except Exception:  # pragma: no cover
    pass
