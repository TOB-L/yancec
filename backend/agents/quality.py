"""质检 Agent：事实核查 / 幻觉检测 / 信源完整度

规则（写进质检Agent，可展示给复核者）：
1. 每个关键数字 ≥2 独立来源
2. L4 数据必须交叉验证
3. L5 推测必须明确标注
4. 冲突信息必须并列展示或仲裁，不替用户下结论

返工控制：graph.py 中 route_after_quality 限制最多2次。
"""

from __future__ import annotations

import re
from typing import Any

from core.models import Evidence, SourceLevel
from core.llm import get_quality_llm, has_main_llm

# 关键数字模式（估算价/增速/数量/占比）
_NUMBER_PATTERN = re.compile(
    r"(约|超|达|近|仅|增|降)?\s?(\d+(?:\.\d+)?)\s*(%|万|亿|家|元|人|杯|户|单|元/杯)"
)
# 推测词
_SPECULATION_WORDS = ["预计", "推测", "可能", "估计", "约", "大概", "或许", "或达"]


def _has_real_url(source) -> bool:
    """判断来源是否有真实可点开的URL（http链接），占位符不算"""
    url = (getattr(source, "url", "") or "").strip()
    if not url or url in ("DeepSeek联网搜索", "资产库", "无"):
        return False
    return url.startswith("http")


def _rule_check(report: str, evidences: list[Evidence]) -> list[str]:
    """规则质检：不依赖LLM的硬性检查（评审可逐条核对的硬标准）"""
    issues = []

    # 关键数字（供两条规则共用）
    #
    # ⚠️ **两处都是 2026-09-16 全量审计修的**：
    #   ① 原先 `[:10]` —— **只查正文前 10 个数字**，而这条规则对外叫
    #      "每个关键数字必须有 ≥2 个独立来源"。前 10 个通常落在开头，
    #      真正要命的测算/对比数字在几百行之后，**从来没被查过**。
    #      现在核 60 个（成本仍是 O(证据 × 数字)，很小），并把覆盖面**写进结论**。
    #   ② 原先按**单位**匹配来源（`unit in ev.claim`）：只要某条证据提到过"元"，
    #      报告里任何一个"元"的数字都算它有来源——**一个域名为所有同单位数字背书**。
    #      这是"检查看起来更严、实际更松"的那一类：它把不达标的数字报成达标。
    #      改成按**数字本身**匹配（千分位归一 + 数字边界），这才是"这个数有来源"的定义。
    _MAX_NUM_CHECK = 60
    _all_numbers = _NUMBER_PATTERN.findall(report)
    numbers = _all_numbers[:_MAX_NUM_CHECK]
    _cover_note = (
        f"（规则层本次核对正文前 {_MAX_NUM_CHECK} 个数字，正文共 {len(_all_numbers)} 个，"
        f"其余 **{len(_all_numbers) - _MAX_NUM_CHECK} 个未被这条规则覆盖**）"
        if len(_all_numbers) > _MAX_NUM_CHECK
        else ""
    )

    # 1b. 【硬溯源 + 多源交叉】关键数字必须有 ≥2 个独立来源域名支撑，否则"无据断言/单源结论"。
    #     （防幻觉硬标准：每个数字能点开真实来源；关键论断多源交叉验证 = P0 护城河）
    import re as _re
    from urllib.parse import urlparse as _urlparse

    def _norm_digits(x: str) -> str:
        return _re.sub(r"[,，\s]", "", str(x or ""))

    _domains_by_num: dict[str, set[str]] = {}
    for ev in evidences:
        _domains = {_urlparse(s.url).netloc for s in ev.sources if _has_real_url(s)}
        if not _domains:
            continue
        _hay = _norm_digits(
            " ".join([ev.claim or ""] + [str(getattr(s, "excerpt", "") or "") for s in ev.sources])
        )
        for _, num, _u in numbers:
            _k = _norm_digits(num)
            # 数字边界：`72` 不许命中 `172`（单位匹配时代没有这层，等于放大误判）
            if _k and _re.search(r"(?<!\d)" + _re.escape(_k) + r"(?!\d)", _hay):
                _domains_by_num.setdefault(_k, set()).update(_domains)
    _no_src_nums: list[str] = []
    _single_src_nums: list[str] = []
    for _, num, unit in numbers:
        d = _domains_by_num.get(_norm_digits(num), set())
        if not d:
            _no_src_nums.append(f"{num}{unit}")
        elif len(d) < 2:
            _single_src_nums.append(f"{num}{unit}({len(d)}个域名)")
    def _fmt_nums(xs: list[str]) -> str:
        """列前 5 个，**多了要说出来**（本仓库纪律：静默截断会被读成"就这 5 个"）。"""
        _s = "、".join(xs[:5])
        return _s + (f" 等 {len(xs)} 个" if len(xs) > 5 else "")

    if _no_src_nums:
        issues.append(
            f"数字「{_fmt_nums(_no_src_nums)}」无真实URL来源支撑 → 须标注'估算'或补源{_cover_note}"
        )
    if _single_src_nums:
        issues.append(
            f"数字「{_fmt_nums(_single_src_nums)}」独立来源域名不足2个（单源结论）"
            f"→ 置信度上限为中，建议补第二来源交叉验证{_cover_note}"
        )

    # 2. L5 推测必须明确标注
    for word in _SPECULATION_WORDS:
        idx = report.find(word)
        if idx >= 0:
            # 推测词后60字内若没有"推测/标注/依据" → 提示
            seg = report[idx : idx + 60]
            if not any(m in seg for m in ["推测", "L5", "标注", "依据"]):
                issues.append(f"推测表述「{word}」附近未明确标注为L5推测")

    # 2b. 测算类章节：假设参数（渗透率/频次/客单价/转化率）是 L5 估算，
    #     全文必须出现 L5 标注，否则质检遗漏（硬标准，防"估算当事实"）
    if re.search(r"TAM|SAM|SOM|渗透率|客单价|测算|假设|转化率", report):
        if not re.search(r"L5", report):
            issues.append(
                "测算类章节含假设参数（渗透率/频次/客单价等），但全文无任何 L5 标注 → 估算参数必须标'(L5估算)'"
            )

    # 3. 冲突信息必须并列/仲裁
    if "矛盾" in report or "冲突" in report or "口径" in report:
        if "并列" not in report and "仲裁" not in report and "两种说法" not in report:
            issues.append("存在冲突表述但未并列展示或仲裁")

    return issues


# LLM 质检的窗口参数
# ------------------------------------------------------------
# 病案（2026-09-15 外部审计）：`_llm_check` 此前把整份报告截断成 `report[:3000]`
# 再交给质检模型。实测一份 20,572 字的报告 → **LLM 只看到前 14.6%**，
# 而规则层（`_rule_check`）是全文扫描的。于是「双轨质检」实际上是
# 「规则层全审 + LLM 层看个头」——而对外说的是"LLM 事实核查"。
# 这属于本项目反复出现的那一类：**看起来在工作，实际只做了一小部分。**
#
# 修法：按窗口切分、带重叠、并发跑，并把**真实覆盖率**记进质检结果（可被下游披露）。
# 重叠是为了不把一句话或一张表从中间切开——切开了两边都判不准。
_QA_WINDOW = 3000
_QA_OVERLAP = 300
_QA_MAX_CHUNKS = 10


def _qa_windows(n: int) -> list[tuple[int, int]]:
    """把长度为 n 的正文切成带重叠的窗口区间。单段够短时只有一段（行为与旧实现一致）。"""
    if n <= 0:
        return []
    if n <= _QA_WINDOW:
        return [(0, n)]
    out: list[tuple[int, int]] = []
    step = _QA_WINDOW - _QA_OVERLAP
    a = 0
    while a < n and len(out) < _QA_MAX_CHUNKS:
        b = min(a + _QA_WINDOW, n)
        out.append((a, b))
        if b >= n:
            break
        a += step
    return out


def llm_coverage(report: str) -> dict[str, Any]:
    """LLM 质检的**正文覆盖率**——用于如实披露，不是自夸。

    报告长到超出窗口上限时，覆盖率会小于 1.0；这个数必须跟着质检结论一起走，
    否则会出现"质检 PASS"被读成"全文都查过了"——而实际只查了前几段。
    """
    _n = len(report or "")
    if not _n:
        return {"chars": 0, "windows": 0, "covered_chars": 0, "coverage": 0.0}
    _wins = _qa_windows(_n)
    # 求区间**并集**长度（重叠部分不能重复计）
    _cov = 0
    _cur_a = _cur_b = -1
    for a, b in _wins:
        if a > _cur_b:
            _cov += max(0, _cur_b - _cur_a)
            _cur_a, _cur_b = a, b
        else:
            _cur_b = max(_cur_b, b)
    _cov += max(0, _cur_b - _cur_a)
    return {
        "chars": _n,
        "windows": len(_wins),
        "covered_chars": _cov,
        "coverage": round(_cov / _n, 3),
    }


def _llm_check(report: str, evidences: list[Evidence]) -> list[str]:
    """LLM 质检：事实核查 + 幻觉检测（真实模式）

    **全文覆盖**：按 `_QA_WINDOW` 切窗、并发跑、合并去重。
    返回值上限从 5 提到 8——覆盖变宽了，检出本来就会变多；
    用旧的 5 条上限会把"多查出来的问题"又压回去。
    """
    if not has_main_llm():
        return []
    try:
        from concurrent.futures import ThreadPoolExecutor

        llm = get_quality_llm()
        evidence_summary = "\n".join(
            f"- {ev.claim} [来源: {', '.join(s.level.value for s in ev.sources)}]"
            for ev in evidences[:10]
        )
        _sys = (
            "你是质检员。检查报告是否有：1)证据无法支撑的结论 2)来源缺失的断言 3)与证据矛盾的内容。"
            "只列问题，每条一行，没有则输出'PASS'。"
        )
        _wins = _qa_windows(len(report)) or [(0, len(report))]
        # **失败的窗口必须记账**（2026-09-16 修）。
        # 病案：`_one` 在异常时 `return []`——与"这段查了、没问题"**完全无法区分**。
        # 而 `llm_coverage` 算的是**窗口几何覆盖**（哪些字符区间被分配给了窗口），
        # 不是"模型真的回了话"。两者相乘的后果是：
        # **模型全线挂掉时，报告照旧印「LLM 语义复核覆盖正文 X / X 字（100%，分 N 段）」，
        # 且一条问题都没有** —— 一次没跑成的复核，被印成了"全文查过、干净"。
        # 这与本仓库已修过多次的同型病完全一致（未取得≠没变化 / 未执行≠FPR 0 / 全 skip≠0.0%）。
        _failed: list[int] = []

        def _one(idx_rng: tuple[int, tuple[int, int]]) -> list[str]:
            _idx, rng = idx_rng
            _a, _b = rng
            try:
                resp = llm.invoke([
                    ("system", _sys),
                    ("human", f"报告:\n{report[_a:_b]}\n\n可用证据:\n{evidence_summary}"),
                ])
                _c = (getattr(resp, "content", "") or "").strip()
                if _c.upper() == "PASS":
                    return []
                return [l.strip() for l in _c.split("\n") if l.strip()]
            except Exception:
                _failed.append(_idx)
                return []  # 单段失败不影响其他段（**所以更要把这次失败说出来**）

        if len(_wins) == 1:
            _parts = [_one((0, _wins[0]))]
        else:
            with ThreadPoolExecutor(max_workers=min(4, len(_wins))) as _ex:
                _parts = list(_ex.map(_one, enumerate(_wins)))

        # 合并去重：不同窗口可能对同一处报同一条，归一化后只留一条
        _out: list[str] = []
        _seen: set[str] = set()
        for _p in _parts:
            for _x in _p:
                _k = re.sub(r"[\s\d\.\-、，,）)（(【】\[\]]+", "", _x)[:40]
                if _k and _k not in _seen:
                    _seen.add(_k)
                    _out.append(_x)
        if _failed:
            # **放在最前**：`_out[:8]` 会截尾，而这一条比任何一条具体检出都重要
            # ——它决定了"这次复核的结论能不能被采信"。
            _seg = "、".join(f"第 {i + 1}" for i in _failed[:6])
            return [
                f"⚠️ LLM 语义复核**有 {len(_failed)}/{len(_wins)} 段未能完成**"
                f"（{_seg} 段，模型调用失败）——**这些段落未经语义复核**。"
                "上面的覆盖率是窗口分配比例，不代表这些段落被查过；"
                "本次质检的『未发现问题』不覆盖它们。"
            ] + _out[:8]
        return _out[:8]
    except Exception:
        return []


def _check_population_conflict(report: str) -> list[str]:
    """人口口径检查（FLAG 级，不 BLOCK）：只对"疑似国家/区域级人口口径"报警。

    关键：合法细分人群（如"咖啡消费者3.4亿人"）不能被误判；
    官方三口径（总14.1/城镇9.3/农村4.8）允许共存。
    只有当出现 **≥5亿** 且不在官方口径内的"X 亿人"（如 12亿、15亿）时，
    才提示可能是错误的总人口口径。
    """
    _ALLOWED = {14.1, 9.3, 4.8}
    found = [
        round(float(n), 1)
        for n in re.findall(r"(\d+(?:\.\d+)?)\s*亿(?:人|人口)?(?!元)", report)
        if float(n) >= 5 and round(float(n), 1) not in _ALLOWED
    ]
    if found:
        return [
            f"人口口径提示：报告出现 {sorted(set(found))} 亿人（疑似国家/区域总人口口径），"
            "应核对是否与官方口径一致（总人口14.1亿 / 城镇9.3亿 / 农村4.8亿）"
        ]
    return []


def _fatal_check(report: str) -> list[str]:
    """致命项质检：命中即 BLOCK（阻断交付/触发返工）。

    与 FLAG 的区别：FLAG 是"提示可交付"，BLOCK 是"硬伤必须返工"。
    规则设计遵循"确定性下沉"——只把**可自动判定的硬矛盾**判为致命：
    1. 测算自洽（SAM > TAM）
    2. 人口口径前后冲突
    （L5 缺失、缺 URL 等属于"标注/溯源"问题，判 FLAG 在报告末尾披露，
    不硬拦截——否则正常报告因偶尔漏标 L5 就反复返工，既慢又易翻车。）
    """
    fatal = []

    # 1. 测算自洽：TAM/SAM/SOM 若同现，SAM 必须 ≤ TAM（口径矛盾 = 硬伤）
    #    兼容"TAM（潜在总市场）≈25,110 亿元" / "TAM 约 2.5 万亿" 等写法，
    #    剔除千分位逗号；f-string 中正则量词 {0,8} 必须写成 {{0,8}}
    def _extract_amount(label: str) -> tuple[float, str] | None:
        # 兼容 "TAM（潜在总市场）≈25,110 亿元" / "TAM 约 2.5 万亿" / 表格行 "TAM\t约 4,096 亿元"。
        # 关键坑：正文 "TAM：2024年…" 等年份会被当成数值、无单位默认"元"→ TAM=2024.0元 →
        # 与真实 SAM 比对时误报"SAM>TAM"。对策：
        #   a) 不跨行匹配（换行断开，避免抓到表格/标题行）；
        #   b) 裸数字后紧跟"年"→ 判为年份，跳过；
        #   c) 全文中取"带显式单位(万亿/亿>万>千/元)"优先级最高的候选，而非第一个命中。
        scale_rank = {"万亿": 0, "亿": 0, "万": 1, "千": 2, "元": 3}
        best: tuple[float, str, int] | None = None
        for m in re.finditer(
            rf"{label}[^\d\n]{{0,14}}?([\d,]+(?:\.\d+)?)\s*(万亿|亿|万|千|元)?",
            report,
            re.I,
        ):
            try:
                num = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            unit = m.group(2)
            # 只认带显式货币单位(万亿/亿/万/千/元)的候选——绝不给裸数默认"元"：
            # 否则 "TAM：2024年…"→2024.0元、"TAM…渗透率32%"→32.0元 全被误当 TAM 值，
            # 与真实 SAM 比出"SAM>TAM"假矛盾。不带单位的 TAM/SAM 本身无意义，跳过。
            if not unit:
                continue
            rank = scale_rank.get(unit, 3)
            if best is None or rank < best[2]:
                best = (num, unit, rank)
        return (best[0], best[1]) if best else None

    tam = _extract_amount("TAM")
    sam = _extract_amount("SAM")
    if tam and sam:
        # 统一换算成"元"再比较：千=1e3 万=1e4 亿=1e8 万亿=1e12
        scale = {"千": 1e3, "万": 1e4, "亿": 1e8, "万亿": 1e12, "元": 1.0}
        tam_yuan = tam[0] * scale.get(tam[1], 1.0)
        sam_yuan = sam[0] * scale.get(sam[1], 1.0)
        if sam_yuan > tam_yuan:
            fatal.append(
                f"测算自洽错误：SAM({sam[0]}{sam[1]}) 大于 TAM({tam[0]}{tam[1]})，"
                "市场测算口径矛盾 → 必须返工修正参数"
            )

    # 人口口径仅作 FLAG 提示（报告末尾披露），不 BLOCK——
    # 合法细分人群（咖啡消费者3.4亿）与官方三口径共存都可能正常，避免误杀正常报告
    return fatal


def quality_check(report: str, evidences: list[Evidence]) -> dict[str, Any]:
    """质检入口：规则 + LLM 双重检查，输出分级（PASS / FLAG / BLOCK）。

    分级语义（对应框架改造 §3.6）：
    - PASS  无问题，直接交付
    - FLAG  有提示项（缺链接/未标注等），交付但在报告末尾"诚实披露"
    - BLOCK 有致命项（测算矛盾/人口口径冲突/L5缺失），触发返工或拒绝交付
    """
    issues = _rule_check(report, evidences)
    issues += _check_population_conflict(report)
    llm_issues = _llm_check(report, evidences)
    all_issues = issues + llm_issues

    fatal_issues = _fatal_check(report)  # 硬闸门：命中即 BLOCK，触发返工
    if fatal_issues:
        level = "BLOCK"
    elif all_issues:
        level = "FLAG"
    else:
        level = "PASS"

    return {
        "level": level,
        "passed": level != "BLOCK",
        "issues": all_issues,
        "rule_issues": issues,
        "fatal_issues": fatal_issues,
        "llm_issues": llm_issues,
        "evidence_count": len(evidences),
        "missing_source": len([i for i in all_issues if "来源" in i]),
        # LLM 质检的**正文覆盖率**：必须跟着结论一起走。
        # 不披露覆盖率，"质检 PASS" 会被读成"全文都查过了"——
        # 而这份报告可能长到超出窗口上限，实际只查了前几段。
        "llm_coverage": llm_coverage(report),
    }


def build_stop_card(
    report: str,
    evidences: list[Evidence],
    fatal_issues: list[str],
    budget_spent: float = 0.0,
) -> dict[str, Any]:
    """构建"人工接管卡片"数据（培训信号：主动停止=产品能力，不是失败）

    对齐培训的 Human Escalation + Agent Escalation Policy：
    为什么停 / 已知道什么 / 不知道什么 / 需要用户做什么 / 继续会消耗什么 / 怎么恢复 / 风险等级
    """
    # Risk Class：按致命项类型分类处置（对齐 Escalation Policy）
    fatal_txt = "；".join(fatal_issues or [])
    if "SAM" in fatal_txt and "TAM" in fatal_txt:
        risk_class, disposition = "参数矛盾", "回退重测（修正测算参数后重新计算）"
    elif "人口" in fatal_txt:
        risk_class, disposition = "参数矛盾", "回退重测（统一人口口径后重新计算）"
    elif "演示" in fatal_txt:
        risk_class, disposition = "演示拦截", "人工确认后继续返工（演示模式）"
    else:
        risk_class, disposition = "证据不足", "人工补充/确认数据后继续"
    return {
        "reason": "自动质检发现致命问题，需人工确认后才能继续交付",
        "why_stop": fatal_issues[:6],
        "risk_class": risk_class,
        "disposition": disposition,
        "known": f"已完成 {len(evidences)} 条证据检索与报告初稿",
        "unknown": "上述致命项对应的数据口径/来源/L5标注尚未确认",
        "need_user": "请选择：① 补充/更正数据源后重测　② 接受当前值并手动标注L5　③ 终止本次调研",
        # 单位是**档位积分**（与 `budget_spent` / `core.billing.MODE_POINTS` 同源），不是人民币。
        # 2026-09-15 前这里印「元」，而全仓未配模型单价、`cost_cny` 恒为 None——
        # 即"拒绝报钱的地方旁边"在报钱，违反本仓库 K17。
        "cost_if_continue": f"预计新增约 {budget_spent * 0.5:.1f} 档位积分（重跑相关子Agent与复检）",
        "how_to_resume": "点击『继续』以人工接管并下发返工，或点击『终止』结束本次任务",
    }
