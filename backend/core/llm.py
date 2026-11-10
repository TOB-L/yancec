"""研策 · LLM 接入封装

统一管理大模型调用：
- 主模型：通义千问 qwen-plus（OpenAI 兼容 + 联网搜索 enable_search）
- 质检模型：更便宜的模型
- 多模态：Qwen-VL（图像/文档理解）

所有 Agent 通过这里拿模型实例，不直接依赖供应商 SDK。
数据源策略（MCP 层）：
  有 Qwen Key → 千问（联网搜索真实时）
  无 Qwen Key → 降级 DeepSeek（内置知识，诚实标注）
"""

from __future__ import annotations

import logging
import threading
from functools import lru_cache

from core.config import get_settings

logger = logging.getLogger(__name__)

# ============================================================
# Token 用量计量（v0.9.75）
# ============================================================
# 为什么必须有它：run_stats 账本里 `budget_spent` 恒为 0 —— 不是"没花钱"，
# 而是**从来没记过**。后果是两个：① 算账页一直无法回填，"多少钱一份"答不出来
#   （投资人必问，且 2026 年已有实证：超 40% 的 Agent 项目因"价值模糊、成本过高"
#    被终止，成本可控性本身就是一个卖点）；② 培训里点名建议带上台的
#   「成功结果成本」指标缺了唯一的分子。
# 设计：**只记 token（事实），钱按可配置单价算（假设）**。
# 没配单价时如实返回 None —— 绝不拍一个看起来专业的单价把成本"算"出来。
_USAGE_LOCK = threading.Lock()
_USAGE: dict = {"prompt": 0, "completion": 0, "calls": 0, "by_model": {}}
# 运行序号：每次 `reset_usage()` +1。用来**检测并发**——
# 计量器是进程级全局，两个任务同时跑会互相抹账（后开的 reset 清掉先开的累计）。
# 不去做"每任务一份计量器"的改造（那要动全部调用点），而是让**能读出来的那个数自己声明可信度**：
# 运行开始时记下序号，落账时若序号变了 → 说明本次期间另有任务重置过计量器 →
# 标记 `llm_shared`，看板据此**不把这一笔算进成本均值**。
# 「报一个混过的数」比「报不出来」更糟——前者会被当成实测值。
_RUN_SEQ = [0]


def reset_usage() -> int:
    """每次调研开始前清零（跨任务累计会让"单份成本"变成"历史总成本"）。

    **返回本次运行的序号**：调用方把它一直带到落账时，用来判断这次计量
    有没有被并发任务重置过（见 `usage_seq`）。旧调用方忽略返回值即可，行为不变。
    """
    with _USAGE_LOCK:
        _USAGE.update({"prompt": 0, "completion": 0, "calls": 0, "by_model": {}})
        _RUN_SEQ[0] += 1
        return _RUN_SEQ[0]


def usage_seq() -> int:
    """当前计量器序号。与运行开始时记下的序号不一致 → 本次计量**被别人重置过**。"""
    with _USAGE_LOCK:
        return _RUN_SEQ[0]


def usage_snapshot() -> dict:
    """当前累计用量快照（prompt/completion/calls + 按模型拆分）。"""
    with _USAGE_LOCK:
        return {
            "prompt_tokens": _USAGE["prompt"],
            "completion_tokens": _USAGE["completion"],
            "total_tokens": _USAGE["prompt"] + _USAGE["completion"],
            "calls": _USAGE["calls"],
            "by_model": {k: dict(v) for k, v in _USAGE["by_model"].items()},
        }


def estimate_cost_cny(snap: dict | None = None) -> float | None:
    """按配置单价估算本次调研成本（元）。**未配置单价 → None**，不猜。"""
    s = get_settings()
    pin = float(getattr(s, "llm_price_in_per_m", 0.0) or 0.0)
    pout = float(getattr(s, "llm_price_out_per_m", 0.0) or 0.0)
    if pin <= 0 and pout <= 0:
        return None
    u = snap or usage_snapshot()
    return round(u["prompt_tokens"] / 1_000_000 * pin + u["completion_tokens"] / 1_000_000 * pout, 4)


def record_external_usage(model: str, prompt_tokens: int, completion_tokens: int) -> None:
    """记一笔**不经过 LangChain 回调**的模型用量（如检索通道的 httpx 直连）。

    病案（2026-09-15 外部审计）：`mcp/search._qwen_search` 用 `httpx.post` **直连** DashScope
    （因为要拿 `enable_search` 的 `search_info`，LangChain 那条路拿不到），
    于是**不过 `_UsageMeter` 回调** → 检索模型的 token 从来没进过计量。

    后果有两层，第二层更隐蔽：
      ① `run_stats.llm_tokens` 一直被当作"这次跑了多少"的唯一读法 → K17 的
         「单份成本」**分母系统性偏小**；
      ② 实测 6 笔账本的 `by_model` **只有一个模型键**（`qwen3.7-plus`），
         而检索用的是 `qwen-plus` —— 也就是说"按模型分账"这个能力，
         数据上从来就没成立过（模型数恒为 1）。

    与 `on_llm_end` **共用同一个 `_USAGE` 与同一把锁**：一个计量器，两个入口。
    供应商没回用量时不记 0、也不猜（与回调同一条纪律）。
    """
    try:
        p, c = int(prompt_tokens or 0), int(completion_tokens or 0)
        if p <= 0 and c <= 0:
            return
        _name = str(model or "unknown")
        with _USAGE_LOCK:
            _USAGE["prompt"] += p
            _USAGE["completion"] += c
            _USAGE["calls"] += 1
            _cur = _USAGE["by_model"].setdefault(_name, {"prompt": 0, "completion": 0, "calls": 0})
            _cur["prompt"] += p
            _cur["completion"] += c
            _cur["calls"] += 1
    except Exception:
        pass  # 计量失败绝不影响主流程（与回调同）


class _UsageMeter:
    """LangChain 回调：每次模型调用结束时把 token 用量记进 `_USAGE`。

    刻意用**回调**而不是包装 invoke：调用点散布在 10+ 个模块里，
    逐个改既漏又易回归；回调挂在实例上，一处即全覆盖。
    """

    def on_llm_end(self, response, **kwargs):  # noqa: ANN001
        try:
            prompt = completion = 0
            # 形态 A：LLMResult.llm_output['token_usage']（langchain-openai 现行）
            _lo = getattr(response, "llm_output", None) or {}
            _tu = _lo.get("token_usage") or _lo.get("usage") or {}
            if _tu:
                prompt = int(_tu.get("prompt_tokens") or 0)
                completion = int(_tu.get("completion_tokens") or 0)
            # 形态 B：落在 message.usage_metadata 上（新版本逐步迁移到这里）
            if not (prompt or completion):
                for g in getattr(response, "generations", None) or []:
                    for item in g or []:
                        _m = getattr(item, "message", None)
                        _um = getattr(_m, "usage_metadata", None) or {}
                        prompt += int(_um.get("input_tokens") or 0)
                        completion += int(_um.get("output_tokens") or 0)
            if not (prompt or completion):
                return  # 供应商没回用量：**不记 0，也不记猜测值**，静默跳过
            _name = str(_lo.get("model_name") or _lo.get("model") or "unknown")
            with _USAGE_LOCK:
                _USAGE["prompt"] += prompt
                _USAGE["completion"] += completion
                _USAGE["calls"] += 1
                _cur = _USAGE["by_model"].setdefault(_name, {"prompt": 0, "completion": 0, "calls": 0})
                _cur["prompt"] += prompt
                _cur["completion"] += completion
                _cur["calls"] += 1
        except Exception:
            pass  # 计量失败绝不能影响主流程


_METER: list = []


def _callbacks() -> list:
    """惰性构造回调列表（langchain_core 缺失时返回空表，不阻断）。"""
    if _METER:
        return _METER
    try:
        from langchain_core.callbacks import BaseCallbackHandler

        # **基类顺序不能反**：写成 (_H 继承 BaseCallbackHandler, _UsageMeter) 时，
        # MRO 会先命中 BaseCallbackHandler 的空 on_llm_end，我们的实现永远不会被调用
        # —— 计量静默失效、而且看起来一切正常。_UsageMeter 必须在前面。
        class _H(_UsageMeter, BaseCallbackHandler):
            pass

        _METER.append(_H())
    except Exception:
        logger.warning("token 用量计量不可用（langchain_core 回调缺失）——成本将无法核算")
    return _METER


def _use_qwen() -> bool:
    """主模型是否切到千问（有 dashscope key 即启用）"""
    s = get_settings()
    return bool(s.dashscope_api_key or s.qwen_api_key)


def _main_base_url() -> str:
    s = get_settings()
    return s.qwen_base_url if _use_qwen() else s.deepseek_base_url


def _main_api_key() -> str:
    s = get_settings()
    if _use_qwen():
        return s.dashscope_api_key or s.qwen_api_key
    return s.deepseek_api_key


def _main_model() -> str:
    s = get_settings()
    return s.qwen_model if _use_qwen() else s.deepseek_model


@lru_cache
def get_main_llm(temperature: float = 0.2, enable_search: bool = False, max_tokens: int = 2000):
    """主模型：任务理解/规划/整合/报告生成 + 可选联网搜索

    千问 enable_search=True 时走真实联网（返回带来源的实时信息）；
    关闭思考链 enable_thinking=False 提速（qwen3 默认 reasoning 慢）。
    DeepSeek 降级时忽略 enable_search（其 API 不支持，保持诚实）。
    """
    from langchain_openai import ChatOpenAI

    if _use_qwen():
        # 千问原生参数经 extra_body 传递（enable_search 仅联网时传，避免参数报错）
        extra_body = {"enable_thinking": False}
        if enable_search:
            extra_body["enable_search"] = True
        return ChatOpenAI(
            model=_main_model(),
            api_key=_main_api_key(),
            base_url=_main_base_url(),
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body=extra_body,
            callbacks=_callbacks(),
        )
    return ChatOpenAI(
        model=_main_model(),
        api_key=_main_api_key(),
        base_url=_main_base_url(),
        temperature=temperature,
        max_tokens=max_tokens,
        callbacks=_callbacks(),
    )


@lru_cache
def get_quality_llm(temperature: float = 0.0):
    """质检模型：用更便宜的模型做事实核查/幻觉检测，控制成本"""
    from langchain_openai import ChatOpenAI

    settings = get_settings()
    extra_body = {"enable_thinking": False} if _use_qwen() else {}
    return ChatOpenAI(
        model=settings.quality_model,
        api_key=_main_api_key(),
        base_url=_main_base_url(),
        temperature=temperature,
        extra_body=extra_body or None,
        callbacks=_callbacks(),
    )


@lru_cache
def get_vlm_llm(temperature: float = 0.0):
    """多模态模型：Qwen-VL（价目表/产品图/截图解析）"""
    from langchain_openai import ChatOpenAI

    settings = get_settings()
    return ChatOpenAI(
        model=settings.qwen_vl_model,
        api_key=_main_api_key(),
        base_url=_main_base_url(),
        temperature=temperature,
        extra_body={"enable_thinking": False},
        callbacks=_callbacks(),
    )


def has_main_llm() -> bool:
    """是否已配置任一主模型 API Key"""
    return bool(_main_api_key())


def llm_provider_name() -> str:
    """当前数据源供应商名（展示/材料用：'通义千问' 或 'DeepSeek'）"""
    return "通义千问" if _use_qwen() else "DeepSeek"
