"""研策 · 积分计量引擎（档位+积分：模式=员工档位，积分=工作量计量）

设计与用户确认的方向：
- 模式档位保留（免费/标准/深入/定制）= "雇佣强度"，决定单次任务消耗积分
- 积分作计量货币：开始显示"预计消耗X积分·余额Y"，结束显示账单
- 充值买积分，档位越高单次越便宜（量大从优）；新用户送体验积分
- 内存账户（演示期），正式可用 SQLite 替换（接口不变）

⚠️ **演示账户的初始余额必须覆盖最贵的一档**（2026-09-19 改）：
从前 `_balance = 100`，而深入档单次 150、定制档 400 —— 于是**演示深入档时
界面会稳定打出「（积分不足，本次体验价）」**。那不是业务状态，是初始值没配够：
演示账户只演示一两次，却按"新用户只有 100 分"发币。现在给 2000 分（≈ 13 次深入档），
于是"不足"这个状态在正常演示里**结构上到不了**——而不是靠把提示藏起来。
"""

from __future__ import annotations

# 模式 → 单次任务积分（越深消耗越快，对应"员工档位"）
MODE_POINTS: dict[str, int] = {
    "免费": 10,
    "标准": 50,
    "深入": 150,
    "定制": 400,
}
DEFAULT_MODE = "标准"

# 充值阶梯（元 → 积分 + 赠送），档位越高单次成本越低
RECHARGE_TIERS: list[dict] = [
    {"amount": 30, "points": 300, "bonus": "赠 30 积分"},
    {"amount": 99, "points": 1200, "bonus": "赠 200 积分"},
    {"amount": 299, "points": 4000, "bonus": "赠 800 积分"},
]

# 模式英文码 → 中文（与前端 depth 状态对齐）
MODE_ALIAS: dict[str, str] = {
    "free": "免费",
    "standard": "标准",
    "deep": "深入",
    "custom": "定制",
}

# 新用户赠送体验积分。
# **必须 ≥ 最贵档位（定制 400）**，否则演示贵档时界面会稳定打「积分不足」——
# 那不是业务状态，是初始值没配够。2000 ≈ 13 次深入档 / 5 次定制档。
DEMO_START_BALANCE = 2000
_balance = DEMO_START_BALANCE
_ledger: list[dict] = []


def get_balance() -> int:
    return _balance


def task_cost(depth: str = DEFAULT_MODE) -> int:
    return MODE_POINTS.get(depth or "", MODE_POINTS[DEFAULT_MODE])


def preview(depth: str = DEFAULT_MODE) -> dict:
    """本次任务积分预览（预计消耗 + 余额 + 是否足够）"""
    cost = task_cost(depth)
    return {
        "mode": depth or DEFAULT_MODE,
        "cost": cost,
        "balance": _balance,
        "enough": _balance >= cost,
        "short": max(cost - _balance, 0),
    }


def consume(depth: str = DEFAULT_MODE) -> dict:
    """任务开始扣积分（余额不足不阻断演示，但预览明确提示不足）"""
    global _balance
    cost = task_cost(depth)
    if _balance >= cost:
        _balance -= cost
        _ledger.append({"mode": depth or DEFAULT_MODE, "cost": cost, "balance_after": _balance})
    return preview(depth)


def settle_rework(depth: str = DEFAULT_MODE, rework_count: int = 0) -> dict:
    """质检返工追加消耗：每轮返工 +20% 档位积分（"越难越贵"的透明计量）"""
    global _balance
    if rework_count > 0:
        extra = round(task_cost(depth) * 0.2 * int(rework_count))
        if _balance >= extra:
            _balance -= extra
            _ledger.append(
                {
                    "mode": depth or DEFAULT_MODE,
                    "rework": int(rework_count),
                    "extra": extra,
                    "balance_after": _balance,
                }
            )
    return preview(depth)


def recharge(amount: float) -> dict:
    """演示账户充值：按 `RECHARGE_TIERS` 的档位加积分。

    **为什么必须有这个函数**（2026-09-19 修）：`RECHARGE_TIERS` 一直躺在
    `/api/billing` 的返回里被送到前端，而**全仓没有任何一处能加积分**——
    界面写着"充值买积分"、接口返回三档价目，却没有一个能改变余额的入口。
    这是本仓库反复出现的「实现了 ≠ 能被用上」：**字段有、消费者有、
    承重的那个动作没有**。要么把价目表撤掉，要么把它接通——选了后者。

    真实支付通道**没有接，也不假装接了**：这是单机演示账户，充值即时到账、
    不产生任何支付行为，返回里 `"simulated": true` 如实标出，前端照此措辞。
    """
    global _balance
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "金额必须是数字", "billing": billing_info()}
    tier = next((t for t in RECHARGE_TIERS if abs(float(t["amount"]) - amt) < 0.01), None)
    if tier is None:
        return {
            "ok": False,
            "reason": f"没有 ¥{amount} 这个档位（可选："
                      f"{'、'.join('¥%g' % t['amount'] for t in RECHARGE_TIERS)}）",
            "billing": billing_info(),
        }
    _balance += int(tier["points"])
    _ledger.append(
        {
            "mode": "充值",
            "amount_cny": tier["amount"],
            "points": int(tier["points"]),
            "bonus": tier.get("bonus", ""),
            "balance_after": _balance,
        }
    )
    return {"ok": True, "simulated": True, "added": int(tier["points"]), "billing": billing_info()}


def billing_info() -> dict:
    """余额 + 充值档位 + 模式成本表（供前端头部与模式菜单展示）"""
    return {
        "balance": _balance,
        # 初始余额一并出去：界面上那句"重启回到初始值"要说得出**初始值是多少**，
        # 否则它是一句没法核对的承诺。
        "start_balance": DEMO_START_BALANCE,
        "tiers": RECHARGE_TIERS,
        "mode_points": MODE_POINTS,
        "default_mode": DEFAULT_MODE,
        "ledger": _ledger[-20:],
    }
