"""研策 · 配置模块

从 .env 读取环境变量，统一管理 API Key、模型、路径等。
.env 位于项目根目录（yancec/.env），无论从哪个目录运行都能定位。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录 = yancec/（本文件在 backend/core/，向上两级）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

# ============================================================
# 让 `.env` **同时**进入 `os.environ`
# ------------------------------------------------------------
# 病案（2026-09-14 端到端自查）：`.env` 一直只被读进 pydantic `Settings`，
# **从不写 `os.environ`**；而下面这些键是**直接用 `os.environ.get()` 读的**：
#   · `AUTHORITY_FETCH_ENABLED`（权威源层总开关）
#   · `SEC_UA_CONTACT`（SEC fair-access 要求 UA 带联系方式）
#   · `CITATION_MEDIA_RATIO` / `AUTH_QUOTA_PER_DIM` / `BING_SCRAPE_GAP`（三道闸门）
# 于是它们**全部静默取默认值**，配置形同虚设：
#   - 总开关置 `0` **关不掉**权威源层（这是个红线开关，必须真的能关）；
#   - `SEC_UA_CONTACT` 填了也没用，SEC 的 UA 永远是占位串——**违反对方的
#     fair-access 政策**；更糟的是启动 WARNING 在用户填好之后**仍然会响**，
#     让人以为是自己没配对，反复折腾一个其实已经生效的配置。
#
# 两种读法必须都有效，所以在这里显式 load 一次。
# `override=False`：**真实环境变量优先于 .env**（部署时用 shell 覆盖是常见做法，
# 不能被文件悄悄改回去）。
# ============================================================
try:
    from dotenv import load_dotenv

    load_dotenv(ENV_FILE, override=False)
except Exception:  # pragma: no cover - 没有 dotenv 时不影响 Settings 那条路
    pass


class Settings(BaseSettings):
    """应用配置（从环境变量 / .env 读取）"""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_file_encoding="utf-8", extra="ignore"
    )

    # 大模型
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    quality_model: str = "qwen-plus"

    # 通义千问（主模型：OpenAI 兼容 + 联网搜索 + 多模态）
    dashscope_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen3.7-plus"
    qwen_vl_model: str = "qwen3.7-plus"
    # 联网搜索专用模型：qwen-plus（经典模型 enable_search 稳定返回 search_info；
    # qwen3.x 系列的 enable_search 响应不含 search_info，导致数据源无真实URL）
    qwen_search_model: str = "qwen-plus"

    # 多模态（可选，兼容旧字段）
    qwen_api_key: str = ""

    # 成本核算单价（元 / 百万 token）——**留空则只记 token、不报钱**。
    # 刻意不做内置默认价：不同模型/不同时期价格差异大，写死一个数会让
    # "单份成本"变成看起来精确的猜测值（比"算不出"更糟）。填了才算钱。
    llm_price_in_per_m: float = 0.0
    llm_price_out_per_m: float = 0.0

    # 搜索（结构化搜索 API 主供应：千问 search_info 恒空时的真源替代）
    serpapi_key: str = ""
    baidu_api_key: str = ""
    baichat_api_key: str = ""
    # 博查 AI Web Search（中文 Agent 搜索，结构化返回 title/url/siteName）——优先
    bocha_api_key: str = ""
    # 微软 Bing Web Search API v7（免费层）——次选
    bing_search_api_key: str = ""

    # 应用
    debug: bool = True
    host: str = "127.0.0.1"
    port: int = 8000

    # 存储（路径固定为项目内绝对路径，避免 CWD 不同导致 Chroma 找不到/建错库）
    database_url: str = "sqlite:///./yancec.db"
    vector_db_path: str = str(PROJECT_ROOT / "backend" / "data" / "chroma")


@lru_cache
def get_settings() -> Settings:
    return Settings()
