"""事实参数知识库（只读）：人口基数等宏观事实，LLM 只能选键，值由代码注入。

原则（对应框架改造 §3.1）：
- 事实是"查得到、不该编"的。让 LLM 现场回忆人口基数 = 把事实当幻觉生成。
- 本库对未知键抛错，倒逼 LLM"只选库里的"，从机制上杜绝编造（消灭 P1）。
- 每个键带 source/level/as_of，报告引用时由知识库统一背书，天然一致（消灭 P4）。
- 数值的单位转换在代码里做，LLM 不碰单位。
"""

# 人口基数（值统一"亿人"，以国家统计局为准，年度更新）
POPULATIONS: dict[str, dict] = {
    "中国_总人口": {"value": 14.1, "unit": "亿人", "source": "国家统计局", "level": "L1", "as_of": "2024"},
    "中国_城镇人口": {"value": 9.3, "unit": "亿人", "source": "国家统计局", "level": "L1", "as_of": "2024"},
    "中国_农村人口": {"value": 4.8, "unit": "亿人", "source": "国家统计局", "level": "L1", "as_of": "2024"},
}

# 人口键 → 正文口径名（锁定口径。实测病案：引擎注入"城镇人口9.3亿"，LLM 行文却写成
# "全国总人口9.3亿"——数值是城镇的、标签写成了总人口，被 RedTeam 判高危。显式口径词根治。）
_POPULATION_LABELS: dict[str, str] = {
    "中国_总人口": "全国总人口",
    "中国_城镇人口": "全国城镇人口",
    "中国_农村人口": "全国农村人口",
}

# 宏观事实（单位各异，代码统一处理）
MACRO_FACTS: dict[str, dict] = {
    "中国_居民人均可支配收入_2024": {"value": 41314, "unit": "元", "source": "国家统计局", "level": "L1", "as_of": "2024"},
    "中国_社会消费品零售总额_2024": {"value": 487900, "unit": "亿元", "source": "国家统计局", "level": "L1", "as_of": "2024"},
    "中国_网上零售额_2024": {"value": 155000, "unit": "亿元", "source": "国家统计局", "level": "L1", "as_of": "2024"},
    "中国_快递业务量_2024": {"value": 1751, "unit": "亿件", "source": "国家邮政局", "level": "L1", "as_of": "2024"},
}

FACTS_KB: dict[str, dict] = {**POPULATIONS, **MACRO_FACTS}


def get_fact(key: str) -> dict:
    """查询事实；未命中直接抛错——绝不让 LLM 现场编。"""
    if key not in FACTS_KB:
        raise KeyError(f"事实参数不存在: {key}，请从知识库选择，不要自行编造")
    return FACTS_KB[key]


def population_keys() -> list[str]:
    """可选用的人口键（供 LLM 选择）"""
    return list(POPULATIONS.keys())


def fact_label(key: str) -> str:
    """事实键的中文口径名（正文引用该事实时必须锁定的口径词）。

    总/城镇/农村三个口径极易在行文中混写（曾实测城镇9.3亿被写成"全国总人口9.3亿"），
    人口键走显式映射；宏观键（带年份后缀）退化到键中段；未知键原样返回——绝不让调用方静默猜错。
    """
    if key in _POPULATION_LABELS:
        return _POPULATION_LABELS[key]
    parts = key.split("_")
    if len(parts) >= 3 and parts[-1].isdigit():
        return parts[1]
    return parts[1] if len(parts) > 1 else key


def format_fact(key: str) -> str:
    """把事实格式化为可写进报告的字符串（口径名+值+单位+来源+级别+年份）"""
    f = get_fact(key)
    return f"{fact_label(key)} {f['value']:g}{f['unit']}（{f['source']}{f['as_of']}，{f['level']}）"
