"""研策本地检索工具层（mcp.search / mcp.vision …）

⚠️ 这个文件是**必需的，不是装饰**——删掉它整个检索层会静默失效。

病案（2026-09-11，v0.9.60 实跑）：本目录原先**没有 `__init__.py`**，于是它是一个
「命名空间包」。Python 的导入规则里 **常规包优先于命名空间包**：只要解释器的
site-packages 里装了一个常规的 `mcp` 包（PyPI 上的 Model Context Protocol SDK
就叫 `mcp`，且带 `__init__.py`），它就会**盖过**本目录——哪怕本目录排在 sys.path 更前面。

后果：`from mcp.search import search_with_llm` 抛 `No module named 'mcp.search'`，
**检索层整体失效** → 证据 0 条、引文 0 条、每个章节只剩「（检索失败: …）」。

为什么之前一直没暴露：项目的 venv 里没装那个 `mcp` 包，所以只有"用系统 Python
启动后端"这一种姿势会踩中——换解释器启动是极常见的操作，这种坑不该靠运气避开。
加上 `__init__.py`（常规包）之后，本目录在 sys.path 里靠前就必定胜出。
"""
