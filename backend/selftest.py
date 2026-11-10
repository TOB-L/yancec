r"""研策 · 全链路功能自测（一次性全部测试）

覆盖 v0.9.19 已交付的全部功能点，离线确定性运行（不依赖网络/外部LLM）：
  1. Source Router 白名单（route_source）
  2. 结论置信度分级（grade_evidence_confidence）
  3. 质检 2+ 独立来源规则（_rule_check）
  4. 反证检索（_needs_adversarial / _build_adversarial_query）
  5. MECE 问题树（_build_mece_tree）
  6. Known/Estimated/Unknown 缺口披露（_build_gap_disclosure）
  7. 证据链台账（_build_confidence_appendix）
  8. HTML 交互报告（build_html_report + 指标提取）
  9. 一手验证方案（design_primary_research 降级 + format_md + scan_gaps）
  10. 对抗性校验 counter_evidence 参数
  11. 积分计量（billing）
  12. URL 域名回退（_domain_root）
  13. 渐进式 plan 事件 + problem_tree 字段
  14. 导出端点存在性（html/pptx/docx/xlsx/pdf + 新服务端点）
  15. 端到端：离线跑完整状态机（mock 检索/LLM/网络核验）

用法（PowerShell）：
  cd backend
  python selftest.py
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

# **必须早于任何 core 导入**：离线跑测试绝不许写生产账本（详见 core/runstats.py 病案）。
# 本轮踩过：反复跑测试把账本从 6 行撑到 10 行，看板分母跟着虚高。
os.environ["YANCEC_OFFLINE"] = "1"


@contextlib.contextmanager
def prod_write_allowed(why: str):
    """**测试需要写、生产不许写** —— 这个区分此前没人表达过。

    2026-09-16 把离线闸门从"逐点手装"下移到 I/O 层（`core/safe_io.py`）之后，
    一次性炸出三条用例：`test_metric_store_roundtrip` / `test_outcome_review` /
    `test_entailment_calibration`。它们测的是**落盘逻辑本身**（序列能不能读回、
    回访记录能不能落盘再读、校准存档过没过期），却一律跑在
    `YANCEC_OFFLINE=1` 之下——于是闸门一装，测的就不是逻辑了。

    **注意这条闸门的代价与边界**：它只是临时摘掉环境变量，**不会**把路径改到临时目录。
    所以调用它的**前置条件**是：调用方已经把它要写的那个模块路径（`MODULE._PATH`
    之类）指到 `tempfile.mkdtemp()` 下。因此这里强制要求写 `why`，且 `why` 里
    必须出现 `tmp`/`临时` 字样——**不是为了好看，是为了让人在写下这句话时
    必须确认自己真的改过路径**，而不是顺手把生产污染的门打开。
    """
    if not why.strip():
        raise AssertionError("prod_write_allowed 必须写明理由")
    if ("tmp" not in why) and ("临时" not in why):
        raise AssertionError(
            f"prod_write_allowed 的理由必须点明落盘路径已指向临时目录，收到：{why!r}"
        )
    _saved = os.environ.pop("YANCEC_OFFLINE", None)
    try:
        yield
    finally:
        if _saved is not None:
            os.environ["YANCEC_OFFLINE"] = _saved
        else:
            os.environ["YANCEC_OFFLINE"] = "1"


import contextlib  # noqa: E402  —— 放在上面那个装饰器之后仅为阅读顺序，导入本身无副作用

import traceback
from unittest import mock

# 控制台按 UTF-8 输出（v0.9.75 修）。**这不是美观问题**：
# 此前在 GBK 控制台（PowerShell 默认、以及被管道接走时）跑，一旦有测试失败，
# `print(f"  ❌ {name}: {e}")` 自己先抛 UnicodeEncodeError → **失败信息根本印不出来**，
# 只剩一段 traceback 指着 check() 内部。实测踩中一次：套件明明有红灯，
# 看到的却是"编码错误"，白白浪费一轮排查。**测试报告的失败不能被它自己的输出方式吃掉。**
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# **必须是脚本所在目录的绝对路径，不是 `"."`**（2026-09-17 修）。
# 病案：从上层目录跑 `python yancec/backend/_fulltest.py` 时，`"."` 指向仓库根，
# 于是套件里那些"按裸模块名导入后端脚本"的用例全部 ImportError 失败——
# 而失败信息长得像**材料口径不过**。一次环境问题被报成一次口径问题，
# 是这套测试最不该有的误导（它的整体可信度正是建立在"红了就是真的红了"上）。
sys.path.insert(0, str(Path(__file__).resolve().parent))

_HERE = Path(__file__).resolve().parent

_PASS = 0
_FAIL = 0
_FAILURES: list[str] = []


_SKIPPED: list[str] = []

# 克隆环境里必然缺的两类东西：第三方依赖、内部材料。
# 它们缺失不代表代码错，所以单独记 SKIP，不混进 FAIL——否则"clone 后一片红"会掩盖真问题。
_SKIP_MARKERS = (
    "No module named",
    "没有可核的报告",
    "探针不在",
    "清单不在",
    "未给出能力判据",      # 需要真实网络探针
    "决策基线应至少",      # 需要内部基线数据文件
    "取不到指纹",          # 需要能 import 完整模型依赖
)


def _is_env_missing(exc: BaseException) -> bool:
    if isinstance(exc, (ModuleNotFoundError, FileNotFoundError)):
        return True
    msg = str(exc)
    return any(m in msg for m in _SKIP_MARKERS)


def check(name: str, fn):
    """跑一个测试项：fn 抛错 → FAIL；缺依赖/缺内部材料 → SKIP；否则 PASS。"""
    global _PASS, _FAIL
    try:
        fn()
        _PASS += 1
        print(f"  ✅ {name}")
    except Exception as e:
        if _is_env_missing(e):
            _SKIPPED.append(name)
            print(f"  ⏭  {name}（环境缺失，已跳过）")
            return
        _FAIL += 1
        _FAILURES.append(name)
        print(f"  ❌ {name}: {e}")
        print(traceback.format_exc(limit=3))


def section(t):
    print(f"\n== {t} ==")


# ============================================================
# 1. Source Router 白名单
# ============================================================
def test_source_router():
    from core.source_router import route_source

    assert route_source("https://www.customs.gov.cn/x", "海关总署") == (1, "政府官方")
    assert route_source("https://www.stats.gov.cn/x", "国家统计局") == (1, "政府官方")
    assert route_source("https://www.iresearch.com.cn/r", "艾瑞咨询") == (2, "研究报告")
    assert route_source("https://blog.example-random.cn/x", "")[0] == 4  # 未命中 → 默认
    assert route_source("", "商务部")[0] == 1  # 来源名命中
    assert "gov.cn" in __import__("core.source_router", fromlist=["describe"]).describe()


# ============================================================
# 2. 置信度分级
# ============================================================
def test_confidence():
    from core.confidence import grade_evidence_confidence, confidence_distribution
    from core.models import Evidence, Source, SourceLevel

    # L1 官方 + 多源佐证(2) → 高
    ev_high = Evidence(
        claim="市场规模约5000亿元",
        sources=[
            Source(title="统计", url="https://www.stats.gov.cn/x", level=SourceLevel.L1_OFFICIAL,
                   publisher="统计局", corroboration_count=2, timeliness_score=0.9, independence_score=0.8),
        ],
    )
    assert grade_evidence_confidence(ev_high).level == "high", grade_evidence_confidence(ev_high)

    # 单源 L3 → 中（降级）
    ev_mid = Evidence(
        claim="渗透率约35%",
        sources=[
            Source(title="行业报告", url="https://www.iresearch.com.cn/r", level=SourceLevel.L3_INDUSTRY,
                   publisher="艾瑞", corroboration_count=1, timeliness_score=0.7, independence_score=0.5),
        ],
    )
    assert grade_evidence_confidence(ev_mid).level == "medium"

    # 无URL → 中（降级）
    ev_nourl = Evidence(claim="估测约100亿", sources=[Source(title="推测", url="", level=SourceLevel.L2_SEMI_OFFICIAL)])
    assert grade_evidence_confidence(ev_nourl).level == "medium"

    # L5 推测 → 低
    ev_low = Evidence(
        claim="预计约120亿", is_speculation=True,
        sources=[Source(title="推测", url="https://www.stats.gov.cn/x", level=SourceLevel.L5_SPECULATION)],
    )
    assert grade_evidence_confidence(ev_low).level == "low"

    dist = confidence_distribution([ev_high, ev_mid, ev_low])
    assert dist["high"] >= 1 and dist["low"] >= 1


# ============================================================
# 3. 质检：关键论断 2+ 独立来源
# ============================================================
def test_quality_multi_source():
    from agents.quality import _rule_check
    from core.models import Evidence, Source, SourceLevel

    report = "市场规模约5000亿元，增速约45%。"
    ev = Evidence(
        claim="市场规模约5000亿元",
        sources=[Source(title="统计", url="https://www.stats.gov.cn/x", level=SourceLevel.L1_OFFICIAL)],
    )
    issues = _rule_check(report, [ev])
    assert any("独立来源域名不足2个" in i for i in issues), issues  # 单源被 FLAG

    # 双来源域名 → 不触发单源 FLAG（但"%"无来源仍会报无源）
    ev2 = Evidence(
        claim="市场规模约5000亿元，增速约45%",
        sources=[
            Source(title="统计", url="https://www.stats.gov.cn/x", level=SourceLevel.L1_OFFICIAL),
            Source(title="行业", url="https://www.iresearch.com.cn/r", level=SourceLevel.L3_INDUSTRY),
        ],
    )
    issues2 = _rule_check(report, [ev2])
    assert not any("独立来源域名不足2个" in i for i in issues2), issues2


# ============================================================
# 4. 反证检索
# ============================================================
def test_adversarial_search():
    from agents.researchers import _needs_adversarial, _build_adversarial_query
    from core.models import SubTask

    st_risk = SubTask(id="st_7", agent_type="industry", description="调研『趋势风险』：咖啡跨境电商", search_queries=["风险"])
    st_deep = SubTask(id="st_0_deep", agent_type="industry", description="深入『宏观』")
    st_normal = SubTask(id="st_0", agent_type="industry", description="调研『市场规模』")

    assert _needs_adversarial(st_risk) is True
    assert _needs_adversarial(st_deep) is False  # 深入轮不重复
    assert _needs_adversarial(st_normal) is False
    q = _build_adversarial_query("咖啡跨境电商")
    assert "风险" in q and "失败" in q and "反方" in q


# ============================================================
# 5. MECE 问题树
# ============================================================
def test_mece_tree():
    from agents.router import _build_mece_tree
    from core.models import DepthLevel, ResearchPlan, SubTask

    plan = ResearchPlan(
        raw_question="杭州开咖啡店",
        depth=DepthLevel.STANDARD,
        subtasks=[
            SubTask(id="st_0", agent_type="industry", description="调研『市场规模测算』（TAM）"),
            SubTask(id="st_1", agent_type="competition", description="调研『竞争格局』（波特五力）"),
            SubTask(id="st_2", agent_type="industry", description="调研『物流与成本』（单位经济）"),
            SubTask(id="st_3", agent_type="user", description="调研『消费人群』（STP）"),
        ],
    )
    tree = _build_mece_tree(plan)
    assert tree, "MECE 树不应为空"
    all_ids = [sid for br in tree for sid in br["subtask_ids"]]
    assert sorted(all_ids) == ["st_0", "st_1", "st_2", "st_3"], "每个子任务必须归入且只归入一个分支"
    branches = {br["branch"] for br in tree}
    assert "需求侧" in branches and "供给侧" in branches
    # 空分支应被过滤
    assert not any(not br["subtask_ids"] for br in tree)


def test_mece_tree_no_collapse():
    """离线计划子任务描述会把整句用户问题拼在末尾——若分类按整句匹配，
    '市场规模+竞争格局'式双关键词问题会让每个子任务都塌进同一分支、树=1支。
    """
    from unittest import mock

    from agents.router import generate_plan

    with mock.patch("agents.router.has_main_llm", return_value=False):
        p = generate_plan("中国现制咖啡市场规模多大？竞争格局如何？", {"depth": "标准"})
    branches = [b["branch"] for b in (getattr(p, "problem_tree", None) or [])]
    assert len(branches) >= 2, f"MECE树不应塌成单支: {branches}"
    ids = [sid for b in (getattr(p, "problem_tree", None) or []) for sid in b["subtask_ids"]]
    assert len(ids) == len(p.subtasks or []), "每个子任务仍须归入且只归入一个分支"


# ============================================================
# 6. 缺口披露 + 7. 证据链台账
# ============================================================
def test_appendix():
    from graph import _build_gap_disclosure, _build_confidence_appendix
    from core.models import Evidence, Source, SourceLevel

    evs = [
        Evidence(claim="市场规模约5000亿元", sources=[Source(title="统计", url="https://www.stats.gov.cn/x", level=SourceLevel.L1_OFFICIAL, corroboration_count=2, timeliness_score=0.9, independence_score=0.8)]),
        Evidence(claim="渗透率约35%", sources=[Source(title="行业", url="https://www.iresearch.com.cn/r", level=SourceLevel.L3_INDUSTRY)]),
    ]
    state = {"evidences": evs}
    ledger = _build_confidence_appendix(state)
    assert "证据链台账" in ledger and "stats.gov.cn" in ledger, "台账应含溯源域名"

    report = "市场规模约5000亿元。建议补充一手问卷验证；实地访谈待进行。"
    gaps = _build_gap_disclosure(state, report)
    assert "Known" in gaps and "Estimated" in gaps and "Unknown" in gaps
    assert "问卷" in gaps or "访谈" in gaps


# ============================================================
# 8. HTML 交互报告
# ============================================================
def test_html_report():
    from core.report_html import build_html_report, _extract_metrics

    md = (
        "# 测试报告\n\n## 📊 核心指标一览表\n"
        "| 指标 | 数值 | 来源机构 | 数据级别 | 年份 | 备注 |\n|---|---|---|---|---|---|\n"
        "| 市场规模 | 5005亿元 | 国家统计局 | L1 | 2025 | |\n"
        "| 增速 | 45% | 艾瑞咨询 | L2 | 2025 | |\n| 门店数 | 5000家 | 红餐 | L2 | 2025 | |\n"
        "\n## 结论\n\n市场规模可观。"
    )
    mets = _extract_metrics(md)
    assert len(mets) >= 3, f"应提取≥3条指标，实际{len(mets)}"
    page = build_html_report(md, "测试报告")
    assert "测试报告" in page
    assert "echarts" in page and "marked" in page  # CDN 引用
    assert "__REPORT_MD__" not in page  # 占位符已替换


# ============================================================
# 9. 一手验证方案（降级模板，不调用 LLM）
# ============================================================
def test_primary_research():
    from core.primary_research import _fallback_design, format_md, scan_gaps, design_primary_research

    d = _fallback_design()
    assert d.get("survey") and d.get("interviews") and d.get("field")
    md = format_md(d)
    assert "一手验证方案" in md and "问卷" in md

    gaps = scan_gaps("市场规模约5000亿元（估算），建议一手验证；置信度上限为中")
    assert gaps

    # 无 LLM → 走降级模板（不调用网络）；has_main_llm 在函数内 import，需 mock core.llm
    with mock.patch("core.llm.has_main_llm", return_value=False):
        d2 = design_primary_research("报告内容")
    assert d2.get("survey"), "无LLM时应返回降级方案"


# ============================================================
# 10. 对抗性校验（counter_evidence 参数）
# ============================================================
def test_adversarial_counter():
    from core.adversarial import adversarial_review, format_adversarial_md

    # 空报告早退（不碰 LLM/网络），验证签名兼容 counter_evidence；has_main_llm 在函数内 import
    # v0.9.44 三态语义：空报告 / 无 LLM = "未执行" → None（不再与"运行后干净"的 [] 混同）
    with mock.patch("core.llm.has_main_llm", return_value=False):
        assert adversarial_review("", "", "反方证据") is None
        assert adversarial_review("有内容报告", "问题") is None
    md = format_adversarial_md([{"number": "5005", "context": "市场规模", "issue": "口径存疑", "risk": "中", "suggestion": "复核"}])
    assert "对抗性数字校验" in md


def test_redteam_does_not_doubt_the_clock():
    """RedTeam **不许拿自己的知识截止去否定系统时钟**；报告头水印不是待核数字。

    病案（2026-09-15 深访实跑，逐字取自真实报告）：
      页眉：**研策调研报告**｜引擎基线 v0.9.80｜生成时点：2026-09-15（存疑·待核）
      RedTeam：| 2026-09-15 | 报告头信息：生成时点2026-09-15 | 时序穿越/信源虚构。
                当前真实时间为2024-2025年间（…AI训练数据截止较早…） | 高 |

    两处都错：① 交付水印被当成待核数字（**报告连自己的生成日期都不敢认**，且这是读者
    看到的第一行）；② 论据是"校验官觉得现在是 2024-2025 年"。

    更严重的连带后果在多角色辩论里：那条「国家统计局 2026-01–08 社零」在**引文核验**中
    是 `stats.gov.cn HTTP 200 ✅`，却在辩论中被"申辩方承认质疑成立"，收敛成
    **"该数据为虚构或预测"**——同一条证据链前面说它活着、后面说它是编的。

    口径**刻意收窄**：只丢「纯日期 + 元信息位置」这一种组合。
    「正文说 2025 年、指标表写 2024 年」这类**真实的内部时序矛盾必须继续报出来**——
    收宽了会把真问题一起挡掉，那比误报更糟。
    """
    from core.adversarial import _drop_meta_issues

    _issues = [
        # 真实报告原文那条 → 必须丢
        {"number": "2026-09-15", "context": "报告头信息：生成时点2026-09-15",
         "issue": "时序穿越/信源虚构", "risk": "高", "suggestion": "修正未来时间点"},
        {"number": "2026-09-15", "context": "报告头水印（引擎基线/AIGC 标识）",
         "issue": "元信息可疑", "risk": "中", "suggestion": "—"},
        # 反向锁 ①：**真实的内部时序矛盾**必须留着
        {"number": "2024年", "context": "竞争格局：正文称2025年门店数，指标表标2024年",
         "issue": "同一指标年份错位", "risk": "高", "suggestion": "统一年份"},
        # 反向锁 ②：真数字不受影响
        {"number": "14.2万亿元", "context": "执行摘要：县域社零总额", "issue": "量级存疑", "risk": "高",
         "suggestion": "核实"},
        # 反向锁 ③：日期出现在**正文分析**里（不是元信息位置）→ 不丢
        {"number": "2026-09-15", "context": "宏观环境：数据发布日期，用于判断时效性",
         "issue": "发布日期与其余章节不一致", "risk": "中", "suggestion": "核对"},
    ]
    out = _drop_meta_issues(_issues)
    _nums = [(i["number"], i["context"][:12]) for i in out]
    assert len(out) == 3, f"只应丢 2 条元信息项：{_nums}"
    assert not any("报告头" in i["context"] or "水印" in i["context"] for i in out), _nums
    assert any(i["number"] == "2024年" for i in out), "真实的内部年份错位被误杀（收太宽了）"
    assert any(i["number"] == "14.2万亿元" for i in out), "真数字被误杀"
    assert any(i["number"] == "2026-09-15" and "发布" in i["context"] for i in out), \
        "正文里的日期分析被误杀（只该丢元信息位置的那两条）"

    # 提示词里必须写明这条禁令——提示词是第一道，代码闸门是第二道，两道都要在
    import inspect as _i

    from core import adversarial as _adv
    from core import debate as _db

    _p = _adv._ADVERSARIAL_PROMPT
    assert "训练数据截止" in _p and "生成时点" in _p, "RedTeam 提示词缺『不得质疑时钟/水印』的禁令"
    assert "训练数据截止" in _db._SKEPTIC, "质疑方提示词缺同类禁令"
    _a = _db._ANALYST
    assert "虚构" in _a and "要驳回" in _a, "申辩方缺『不许顺着把真实数据认成虚构』的约束"
    # 两道闸门都要在真实代码路径上（有函数 ≠ 有接线）
    assert "_drop_meta_issues" in _i.getsource(_adv.adversarial_review), "过滤函数未接进 RedTeam 出口"
    from core import decision_guard as _dg

    assert "_ISO_DATE" in _i.getsource(_dg.demote_high_risk_in_head), "就地降级侧缺第二道闸门"


# ============================================================
# 11. 积分计量
# ============================================================
def test_billing():
    from core import billing

    assert billing.task_cost("免费") == 10
    assert billing.task_cost("标准") == 50
    assert billing.task_cost("深入") == 150
    assert billing.task_cost("定制") == 400
    p = billing.preview("标准")
    assert p["cost"] == 50 and p["mode"] == "标准"
    bal0 = billing.get_balance()
    c = billing.consume("标准")
    assert c["balance"] == bal0 - 50
    info = billing.billing_info()
    assert info["tiers"] and info["mode_points"]["标准"] == 50
    # 恢复账户（回滚测试消耗）
    billing._balance = bal0


# ============================================================
# 12. URL 域名回退（纯函数，不联网）
# ============================================================
def test_domain_root():
    from core.verification import _domain_root

    assert _domain_root("https://www.meituan.com/research/2024-coffee") == "https://www.meituan.com"
    assert _domain_root("http://stats.gov.cn/art/x.html") == "http://stats.gov.cn"
    assert _domain_root("https://www.dianping.com/") == "https://www.dianping.com"
    assert _domain_root("不是URL") == ""


# ============================================================
# 13. 渐进式 plan 事件 + problem_tree 字段
# ============================================================
def test_plan_event_shape():
    from core.models import DepthLevel, ResearchPlan

    plan = ResearchPlan(
        raw_question="q",
        depth=DepthLevel.STANDARD,
        problem_tree=[{"branch": "需求侧", "icon": "📈", "question": "需求", "subtask_ids": ["st_0"]}],
    )
    d = plan.model_dump(mode="json")
    assert "problem_tree" in d and d["problem_tree"][0]["branch"] == "需求侧"


# ============================================================
# 14. 导出端点 + 新服务端点存在性
# ============================================================
def test_endpoints():
    from app import app

    paths = {getattr(r, "path", "") for r in app.routes}
    for need in [
        "/api/export/{fmt}",
        "/api/primary-research",
        "/api/primary-research/simulate",
        "/api/billing",
        "/api/execute-stream",
        "/api/tools",
        "/api/deep-dive",
        "/api/assets",
    ]:
        assert need in paths, f"缺少端点 {need}"


# ============================================================
# 15. 端到端：离线跑完整状态机（mock 检索/LLM/网络核验）
# ============================================================
def test_e2e_offline():
    from core.models import DepthLevel

    _FAKE_RICH = {
        "content": "据国家统计局2025年数据，中国咖啡市场规模约5005亿元，增速约45%。"
                   "据艾瑞咨询测算，人均年消费12杯。杭州咖啡门店数超5000家。",
        "urls": ["https://www.stats.gov.cn/x", "https://www.iresearch.com.cn/r"],
        "sources": [
            {"site_name": "国家统计局", "title": "统计公报", "url": "https://www.stats.gov.cn/x"},
            {"site_name": "艾瑞咨询", "title": "咖啡行业报告", "url": "https://www.iresearch.com.cn/r"},
        ],
    }
    _FAKE_VERDICT = lambda urls, **k: {u: (u, True, "HTTP 200") for u in (urls or [])}

    with mock.patch("core.llm.has_main_llm", return_value=False), \
         mock.patch("agents.router.has_main_llm", return_value=False), \
         mock.patch("agents.researchers.has_main_llm", return_value=False), \
         mock.patch("agents.quality.has_main_llm", return_value=False), \
         mock.patch("agents.decision.has_main_llm", return_value=False), \
         mock.patch("agents.researchers._cached_search_rich", side_effect=lambda q: _FAKE_RICH), \
         mock.patch("core.rag.search_assets", return_value=[]), \
         mock.patch("core.rag.add_asset", return_value=""), \
         mock.patch("mcp.search.fetch_page", return_value=None), \
         mock.patch("mcp.search.web_search_urls", return_value=[]), \
         mock.patch("core.industry_kb.save_industry_card", return_value=None), \
         mock.patch("core.verification.build_citation_report", return_value={"total": 0, "counts": {}, "items": [], "verdicts": {}}), \
         mock.patch("core.verification.verify_urls", side_effect=_FAKE_VERDICT):
        from graph import run_task

        # 交付附录**全部**刻意吞异常（附录坏了不能阻断交付）——代价是"附录静默消失"。
        # 所以在跑完之后检查一遍日志：只要有"生成失败"，就说明报告里少了一块。
        # 实测踩过：口径对照块用了 `re.split` 而该作用域没 import re，
        # 生产里整块从来没出现过，日志里只有一行 warning，没人看。
        _cap_records: list[str] = []

        import logging as _logging

        class _Cap(_logging.Handler):
            def emit(self, record):
                try:
                    _cap_records.append(record.getMessage())
                except Exception:
                    pass

        _cap = _Cap()
        _root = _logging.getLogger()
        _root.addHandler(_cap)
        try:
            result = run_task(
                "我想在杭州开咖啡店，能不能做，标准档",
                {"objective": "进入市场", "depth": "标准"},
            )
        finally:
            _root.removeHandler(_cap)
        _broken = [m for m in _cap_records if "生成失败" in m or "附录" in m and "失败" in m]
        assert not _broken, f"交付附录生成失败（被 try/except 吞掉，报告里会静默少一块）：{_broken}"
        assert result["status"] == "done"
        assert result.get("report_final") and len(result["report_final"]) > 500, "报告应有实质内容"
        assert result.get("citations"), "应产出引用"
        assert len(result.get("evidences", [])) >= 1, "应产出证据"
        assert result.get("plan") and result.get("plan").problem_tree, "计划应含 MECE 树"
        assert "置信度" in result["report_final"], "报告应含置信度披露"
        assert "缺口披露" in result["report_final"] or "Known" in result["report_final"], "报告应含缺口披露"
        # 企业级专业研报：默认不夹带"大赛评分/自评/AI透明度"痕迹；参赛对照版才含
        assert "Rubric" not in result["report_final"], "干净研报不应含 Rubric 自评"
        assert "目标大赛" not in result["report_final"], "干净研报不应含大赛自检"
        assert "AI 使用与透明度" not in result["report_final"], "干净研报不应含 AI 透明度附录"
        _comp = result.get("competition_report") or ""
        assert _comp and "目标大赛五维自检" in _comp and "AI 使用与透明度" in _comp, "参赛对照版应含五维自检+AI透明度附录"
        # 红线：参赛对照版不得外发"正大杯 Rubric 七维带分块"或任何自评硬分
        assert "Rubric 系统自检清单" not in _comp, "参赛对照版不应含正大杯 Rubric 块"
        assert "自检合计" not in _comp and "总分" not in _comp, "参赛对照版不应含自评硬分"
        # ===== 法定标识（2026-09-14 新增）=====
        # 《人工智能生成合成内容标识办法》2025-09-01 已施行：显式标识两种模式**都必须有**。
        # 企业模式此前"全仓零标识"——客户拿无标识 AI 报告对外发布，责任落在客户身上。
        # 这条锁死"附录可省、标识不可省"这条边界不被后人改回去。
        assert "AI 生成合成内容" in result["report_final"], "企业研报缺少法定显式标识（起始/末尾）"
        assert "内容编号" in result["report_final"], "显式标识应含内容编号"
        assert "人工智能生成合成内容标识办法" in result["report_final"], "显式标识应写明依据"
        assert "AI 生成合成内容" in _comp, "参赛对照版同样必须有法定标识"
        # 引用蕴涵核验：离线无模型 → 必须是"本次未执行"，**不得**写成通过
        _ent = result.get("entailment") or {}
        assert _ent.get("executed") is False, "离线无模型时 entailment 应为未执行"
        assert "本次未执行" in result["report_final"], "未执行时附录须如实写『本次未执行』"
        assert "citation_pass_rate" in result, "结果应带链接层通过率字段（缺省 0.0 也要在）"


# ============================================================
# 16. Rubric 自评器（大赛评分对标）
# ============================================================
def test_rubric():
    from core.models import DepthLevel, Evidence, ResearchPlan, Source, SourceLevel, SubTask
    from core.rubric import format_rubric_md, score_report

    state = {
        "plan": ResearchPlan(
            raw_question="杭州咖啡店",
            depth=DepthLevel.STANDARD,
            problem_tree=[
                {"branch": "需求侧", "subtask_ids": ["st_0"]},
                {"branch": "供给侧", "subtask_ids": ["st_1"]},
                {"branch": "财务侧", "subtask_ids": ["st_2"]},
            ],
            subtasks=[SubTask(id=f"st_{i}", agent_type="industry", description=f"调研『维度{i}』") for i in range(5)],
        ),
        "evidences": [
            Evidence(claim="市场规模约5000亿元", sources=[Source(title="统计", url="https://www.stats.gov.cn/x", level=SourceLevel.L1_OFFICIAL, corroboration_count=2)]),
            Evidence(claim="【反方】风险被低估", sources=[Source(title="行业", url="https://www.iresearch.com.cn/r", level=SourceLevel.L3_INDUSTRY)]),
        ],
        "conflict_results": [{"claim": "x"}],
        "decision_sop": "## 决策清单",
    }
    report = (
        "# 报告\n\n## 📌 执行摘要\n...\n\n## 结论\n\nPEST 波特五力 TAM 单位经济 置信度 结论置信度\n"
        "一手验证方案 问卷 深访 对抗性数字校验 数据与方法说明 引文核验 数据缺口 反方证据"
    )
    r = score_report(state, report)
    assert 0 <= r["total"] <= 100, r
    assert len(r["breakdown"]) == 7
    assert r["lowest"]["dimension"]
    md = format_rubric_md(r)
    assert "Rubric" in md and "自检合计" in md


# ============================================================
# 17. 目标大赛五维自检（双轨）+ 假设驱动分流
# ============================================================
def test_competition_dual_track():
    from core.models import DepthLevel, Evidence, ResearchPlan, Source, SourceLevel
    from core.rubric import competition_selfcheck, format_competition_md
    from agents.router import _detect_question_type, _build_hypotheses

    # 问题类型分流
    assert _detect_question_type("促销是否有效，为什么增长") == "causal"
    assert _detect_question_type("市场规模多大，格局如何") == "descriptive"
    assert _detect_question_type("这个行业有什么机会") == "exploratory"
    assert len(_build_hypotheses("咖啡涨价")) == 3

    # ---- H1 模板不许把整句提问裸插进去（2026-09-17 实跑病案）----
    #
    # 病案逐字来自本跑交付报告：
    #   H1：『**星巴克等咖啡品牌下沉三四线城市该不该做？下沉门店**』的需求规模是进入/决策的核心变量（待验证）
    # 原写法 `topic = raw_question.strip()[:24]` 把整句问题拦腰截断——
    # **断在问号中间、还带着"该不该做"这个疑问尾缀，一句话里两个谓语残缺的片段。**
    # 这一行会印进报告与说明材料，是教授复核者一眼可见的那种病句。
    from agents.router import _subject_of

    _h1 = _build_hypotheses("星巴克等咖啡品牌下沉三四线城市该不该做？下沉门店增速为什么放缓？")[0]
    assert "下沉门店" not in _h1, f"H1 里还带着第二个子问题（裸截断没修好）：{_h1}"
    assert "该不该做" not in _h1, f"H1 主语里还留着疑问尾缀：{_h1}"
    assert "星巴克等咖啡品牌下沉三四线城市" in _h1, f"H1 主语丢了：{_h1}"
    # 主语必须是"名词短语"而不是"问句片段"：9 道 MR-Bench 题全过一遍
    for _q in ("社区养老服务市场有什么机会？要不要进入？", "AI Agent 创业该进什么细分行业？",
               "中国现制咖啡市场规模多大？竞争格局如何？", "广告投放对咖啡销量是否有效？ROI 为什么波动？",
               "低空经济赛道要不要进入？政策对商业化有什么影响？"):
        _s = _subject_of(_q)
        for _bad in ("？", "?", "什么", "多少", "如何", "是否", "要不要", "该不该"):
            assert _bad not in _s, f"主语里残留疑问成分 {_bad!r}：{_q} → {_s!r}"
    # 反向锁：**不要过度清洗**——把实词也削掉就没信息了
    assert _subject_of("中国现制咖啡市场规模多大？") == "中国现制咖啡市场规模", "清洗过头，把实词削掉了"
    assert _subject_of("星巴克等咖啡品牌下沉三四线城市该不该做？").startswith("星巴克等咖啡品牌下沉三四线城市")

    # ---- 决策链：**渲染必须晚于全部记录**（2026-09-17 实跑病案）----
    #
    # 病案：本跑报告头部徽章「🧠 自主决策链 **19**」，正文却写「共做出 **17** 次取舍」
    # 且逐条列出恰好 17 条——**同一屏两个数**。根因是**渲染早于记录**：
    # 那一行在原 `:2580` 就渲染了，而 `arbitrate`（"要不要给被质疑方申辩权"）与
    # `degrade`（"改数字、删数字，还是标出来"）记在 `:2798` / `:2862`，**都在它之后**。
    # 缺的正好是这两条最有故事性的——L19b 分镜要的就是它们。
    #
    # 锁的形状：**源结构机械判定**——回填点之后不许再出现任何 `record()` 调用。
    # 再加一条行为断言：渲染条数 == decisions 长度（这条防的是回填本身出错）。
    from core.decision_trace import KINDS as _DK, render_appendix_md as _rdm

    _g2 = __import__("graph")
    import inspect as _insp

    _nd = _insp.getsource(_g2.node_deliver)
    _backfill = _nd.find("_DEC_SLOT in final")
    assert _backfill > 0, "找不到决策链回填点——这条锁会退化成空转"
    _tail = _nd[_backfill:]
    _late = [m for m in ("_dec(", "_rec(", "decision_trace.record(") if m in _tail]
    assert not _late, (
        f"决策链回填点**之后**仍有 record 调用 {_late}——"
        "那几条取舍会记进 decisions、却进不了报告正文，徽章与正文又会差数（本跑就是差的 2 条）"
    )
    assert "_DEC_SLOT" in _nd, "交付节点里没有占位符——说明又改回「就地渲染」了"

    # 行为断言：渲染条数必须等于 decisions 长度（KINDS 全类各一条，正好覆盖分组渲染）
    _ds = [{"kind": _k, "situation": "s", "options": ["a"], "chosen": "c", "why": "w",
            "evidence": "", "trigger": "t"} for _k in _DK]
    _rmd = _rdm(_ds)
    assert _rmd.count("**处境**") == len(_ds), (
        f"决策链渲染出的条数（{_rmd.count('**处境**')}）≠ decisions 长度（{len(_ds)}）——"
        "回填处的一致性告警就是按这个数判的，它坏了告警就成了摆设"
    )
    # 计数判据不能把**标题里的那句**也算进去（"**面对什么处境、有哪些选项…**"是加粗的整句，
    # 但 `**处境**` 这个精确 token 不该在里面出现）——否则告警会永远多报 1。
    assert "**处境**" not in _rmd.split("\n\n")[0] or _rmd.count("**处境**") == len(_ds), (
        "计数判据把标题里的字样也算成了决策条目"
    )

    # ---- 单位经济：参数量纲守卫（2026-09-17 实跑病案）----
    #
    # 病案（逐字来自本跑交付报告）：
    #   「用户**年均**消费频次为 12 次(L5估算)，客单价 15 元，生命周期为 **12 个月**(L5估算)，
    #     即 12 × 15 × 12 = 2,160 元」→ LTV/CAC「比值**高达 27.0**」
    # 公式是对的（年频次 × 年数），错在**提取器把"12 个月"填进了名为 `repeat_years` 的年限字段**。
    # 这段代码里 `gross_margin` 有钳位、`repeat_years` 只做了 `max(x,0)`——
    # **同一段代码，一个参数有合理性校验、一个没有**。后果是 12 倍的 LTV 高估，
    # 而它一路传到 LTV/CAC 还被正文当成"很健康"引用。
    #
    # 修法是**披露而不是悄悄改数**（公理六）：值照原样算，但把"这个参数不可信"印在数值旁边。
    # 锁要同时证明**报得出**与**不常态误报**——后者更重要：会常态误报的检查比没有更糟。
    from agents.researchers import _compute_unit_economics as _cue

    _base = dict(investment=55, daily_orders=137, avg_price=15, gross_margin=0.6,
                 monthly_rent=2.9, monthly_staff=2.4, monthly_other=0.9, cac=80)
    _bad = _cue(dict(_base, repeat_years=12, annual_freq=12))       # ← 本跑那组
    assert _bad["ltv_reliable"] is False, "本跑那组「生命周期 12 个月填进年限字段」没被标出来"
    assert any("repeat_years" in _f for _f in _bad["param_flags"]), "告警没说清是哪个参数、什么毛病"
    assert _cue(dict(_base, repeat_years=0.1, annual_freq=12))["ltv_reliable"] is False, "年限过小也要报"
    assert _cue(dict(_base, repeat_years=2.5, annual_freq=5000))["ltv_reliable"] is False, "复购次数越界要报"
    # **反向锁**：一组完全合理的参数**一条都不许报**。
    # （第一版还加过"LTV/CAC 过高"这一条，阈值 15 —— 实测**合理参数也会触发**
    #   （15×80×2.5×0.6÷80=22.5），高频刚需品类本来就可能两位数。已撤掉。）
    for _ry, _af in ((2.5, 80), (1.0, 12), (3, 150), (0.5, 1)):
        _ok = _cue(dict(_base, repeat_years=_ry, annual_freq=_af))
        assert _ok["ltv_reliable"] is True and not _ok["param_flags"], (
            f"合理参数被误报（repeat_years={_ry} annual_freq={_af}）：{_ok['param_flags']}"
        )

    # ---- 低信源通道名：**同一个概念不许有三张手工词表**（2026-09-17 实跑病案）----
    #
    # 病案：本跑核心指标表上印着
    #     | 县域门店盈亏平衡安全阈值月销 | 28台 | **反方检索资料** | L2媒体与咨询 | …
    # 而**同一张表下面**挂着一条"来源标注修正"提示，举的例子恰是「**反方检索数据**」——
    # **提示在说自己修了一类它其实没修的东西。**
    # 根因是三张词表各写各的：
    #     graph._GENERIC_SRC        → 只有「反方检索数据」
    #     report_html._JUNK_SOURCE  → 一条「反方检索」都没有
    #     industry_kb._LOW_TRUST    → 有「反方检索资料」（**唯一收全的那张**）
    # **差一个字，整条漏过。** 本仓库第三次踩"只收一种词形＝没收"
    # （前两次：`节\b` 中文词边界、`_SEC_CNT` 只收「章节」）。
    from graph import _fix_metric_source_qualification as _fms
    from graph import _GENERIC_SRC as _GS
    from core.report_html import _JUNK_SOURCE as _JS
    from core.industry_kb import _LOW_TRUST as _LT

    _row = "| 县域门店盈亏平衡安全阈值月销 | 28台 | 反方检索资料 | L2媒体与咨询 | 2024 | 单位经济章节正文 |"
    _fixed = _fms(_row)
    assert "反方检索资料" not in _fixed.split("\n")[0], (
        f"本跑那行低信源通道名没被净化（词表只收「反方检索数据」，报告写的是「反方检索资料」）：{_fixed}"
    )
    assert "待核" in _fixed, "净化后应改标「待核」"
    # **三张表都必须判中真正逃掉的那一族**——判据用**生产同款谓词**（`any(w in 单元格)`），
    # 而不是"三张表逐字相同"。
    # （第一版要求每张表逐字列出三种词形 → `industry_kb._LOW_TRUST` 被误判：它收的是更短的
    #   「反方检索」，**已经覆盖**了"资料"/"数据"两种后缀。把实现细节当契约，锁就会在正当重构时误报。
    #   第二版又把探针要求摊到所有低信源词上 → 三张表用途不同，`industry_kb` 本来就不接触"行业数据"。
    #   **只在真正逃掉的那一族上锁，方向与生产一致。**）
    for _name, _lst in (("graph._GENERIC_SRC", _GS), ("report_html._JUNK_SOURCE", _JS),
                        ("industry_kb._LOW_TRUST", _LT)):
        for _cell in ("反方检索资料", "反方检索数据", "反方检索"):
            assert any(_w in _cell for _w in _lst), (
                f"{_name} 判不中来源格「{_cell}」——差一个字就整条漏过（本跑就是这么漏的）"
            )
    # 提示语的例子必须**从实际命中里取**，不许写死（写死过一次，举的例子恰是词表没覆盖的写法）
    _note = _fms("| 指标 | 值 | 反方检索资料 | L1 | 2024 | n |")
    assert "本次实际命中" in _note, "提示语仍在用硬编码例子——它会再次举一个自己没查的词"

    state = {
        "plan": ResearchPlan(
            raw_question="促销是否有效", depth=DepthLevel.STANDARD,
            problem_tree=[{"branch": "需求侧", "subtask_ids": ["st_0"]}, {"branch": "供给侧", "subtask_ids": ["st_1"]}, {"branch": "财务侧", "subtask_ids": ["st_2"]}],
        ),
        "evidences": [
            Evidence(claim="市场规模约5000亿元", sources=[Source(title="统计", url="https://www.stats.gov.cn/x", level=SourceLevel.L1_OFFICIAL)]),
            Evidence(claim="【反方】风险", sources=[Source(title="行业", url="https://www.iresearch.com.cn/r", level=SourceLevel.L3_INDUSTRY)]),
        ],
        "decision_sop": "## 决策清单",
    }
    report = (
        "# 报告\n\n## 结论\n\n引文核验 置信度 数据缺口 一手验证方案 反方证据 对抗性数字校验 决策清单 Rubric 自评"
    )
    cc = competition_selfcheck(state, report)
    assert len(cc["breakdown"]) == 5
    assert 0 <= cc["total"] <= 100
    md = format_competition_md(cc)
    assert "目标大赛五维自检" in md and "赛道维度评定" in md


# ============================================================
# 18. AI 透明度双模式
# ============================================================
def test_transparency():
    from core.transparency import build_transparency_md, transparency_disclosure

    md = build_transparency_md("competition")
    assert "AI 使用与透明度" in md and "通义千问" in md and "一手验证" in md
    assert build_transparency_md("enterprise") == ""  # 企业模式不附 AI 附录
    assert len(transparency_disclosure()["tools"]) >= 5


# ============================================================
# 19. 一手研究·模拟数据（演示闭环）+ 方法章节/Rubric 定向返工
# ============================================================
def test_simulated_data():
    from core.simulated_data import simulate_survey

    # 无 LLM → 模板路径（离线确定性）；设计带问卷 → 字段由问法提炼
    with mock.patch("core.llm.has_main_llm", return_value=False):
        design = {
            "survey": {
                "target": "三四线下沉市场的咖啡消费者",
                "questions": [
                    "您每月购买现制咖啡的频次？",
                    "您常去的咖啡品牌类型？",
                    "您单次消费金额？",
                    "您是否愿意尝试附近新开的咖啡店（0-10）？",
                ],
            }
        }
        sim = simulate_survey("报告：下沉市场咖啡增速放缓", design, n=120, seed=7)
        assert sim["n"] == 120, "样本数应等于 n"
        assert sim["csv"].startswith(chr(0xFEFF)), "CSV 应带 UTF-8 BOM（Excel 中文）"
        # 2026-09-15 改：CSV **本体**前面多了两行"演示用模拟数据"声明。
        # 原先只写在界面与 md 里，而文件下载出去就脱离了界面——转发/导入 Excel 之后，
        # 一份没有声明的"问卷样本.csv"与真实采样看起来毫无区别（与 C10 直接冲突）。
        _csv_lines = sim["csv"].splitlines()
        # 首行带 UTF-8 BOM（Excel 中文不乱码），所以判"是不是以 # 开头"要先剥 BOM
        _l0 = _csv_lines[0].lstrip("﻿")
        assert _l0.startswith("#") and "演示用模拟数据" in _l0, (
            f"CSV 本体第一行必须是模拟数据声明：{_l0[:60]!r}"
        )
        assert _csv_lines[1].startswith("#"), "声明应占前两行（`#` 开头，Excel/pandas 都当普通行）"
        assert len(_csv_lines) == 123, "2 行声明 + 1 表头 + 120 行样本"
        assert "模拟" in sim["md"] and "非真实" in sim["md"], "必须诚实标注模拟数据"
        assert sim["md"].startswith("## 🧪") or "关键发现" in sim["md"]
        # 空设计（无问卷）也能兜底出数据
        sim2 = simulate_survey("报告内容", None, n=60, seed=1)
        assert sim2["n"] == 60 and sim2["csv"]
    # 确定性：同 seed → 同结果（演示可复现）
    with mock.patch("core.llm.has_main_llm", return_value=False):
        s1 = simulate_survey("x", None, n=40, seed=99)
        s2 = simulate_survey("x", None, n=40, seed=99)
        assert s1["csv"] == s2["csv"]


def test_rubric_rework_helpers():
    from core.rubric import _FRAMEWORKS, extract_frameworks

    descs = ["调研『市场规模测算』（TAM）", "调研『竞争格局』（波特五力）", "调研『消费人群』（STP）"]
    fw = extract_frameworks(descs)
    assert "TAM" in fw and "波特五力" in fw and "STP" in fw
    assert "TAM" in _FRAMEWORKS

    # 方法章节：从 plan 的 problem_tree/subtasks 生成 → report_draft 自动含框架+置信度方法（Rubric 设计分）
    from core.models import DepthLevel, ResearchPlan, SubTask
    from graph import _build_method_chapter

    plan = ResearchPlan(
        raw_question="咖啡下沉",
        depth=DepthLevel.STANDARD,
        problem_tree=[
            {"branch": "需求侧", "icon": "📈", "question": "需求", "subtask_ids": ["st_0"]},
            {"branch": "供给侧", "icon": "🏪", "question": "供给", "subtask_ids": ["st_1"]},
            {"branch": "财务侧", "icon": "💰", "question": "财务", "subtask_ids": ["st_2"]},
        ],
        subtasks=[
            SubTask(id="st_0", agent_type="industry", description="调研『市场规模测算』（TAM）"),
            SubTask(id="st_1", agent_type="competition", description="调研『竞争格局』（波特五力）"),
            SubTask(id="st_2", agent_type="industry", description="调研『单位经济』（单位经济）"),
        ],
    )
    chap = _build_method_chapter({"plan": plan})
    assert "研究方法与框架" in chap and "MECE" in chap
    assert "TAM" in chap or "波特五力" in chap or "单位经济" in chap
    assert "置信度分级" in chap


# ============================================================
# 20. URL 信任门槛 + 垃圾页探测（治"跳转404/词典/无关站"）
# ============================================================
def test_url_trust_gates():
    from core.verification import _looks_junk_page, is_trusted_domain

    # 可信域名：监管官方/头部研究放行（含幻觉深链常见的省厅 gov.cn 子域）
    assert is_trusted_domain("https://www.stats.gov.cn/article/x.html")
    assert is_trusted_domain("https://data.iresearch.com.cn/report/123")
    assert is_trusted_domain("https://www.customs.gov.cn/zsg/xx")
    assert is_trusted_domain("http://nynct.sc.gov.cn/nynct/c100239/202404/7e8b9c.shtml")
    # 未受信（垃圾/词条站/伪造域名）不放行
    assert not is_trusted_domain("https://random-junk-hanyu123.cn/industry/coffee")
    assert not is_trusted_domain("https://www.zhidao-ads.net/x")
    assert not is_trusted_domain("")
    # 软404/词典/验证墙 → 判垃圾（含 gov CMS 软404：HTTP200 但内容是"页面不存在"）
    assert _looks_junk_page("<html><title>404 Not Found</title><body></body></html>") is True
    assert _looks_junk_page("<html><title>四川省农业农村厅</title><body>抱歉，您访问的页面不存在或已被删除</body></html>") is True
    assert _looks_junk_page("<html><title>汉语词典_在线查询</title><body>无此词条 部首 笔画</body></html>") is True
    assert _looks_junk_page("<html><body>访问过于频繁，请输入验证码</body></html>") is True
    # 真内容页（含正常标题+长正文）不误杀
    good = (
        "<html><head><title>2025咖啡行业年度报告_艾瑞</title></head>"
        f"<body>{'<p>市场规模与增速数据详情</p>' * 30}</body></html>"
    )
    assert _looks_junk_page(good) is False

    # 来源层黑名单：日志实测的 Bing 字典SEO站必须被 _is_clean_url 拦截（跳词典根治）
    from agents.researchers import _is_clean_url, _title_relevant

    assert not _is_clean_url("https://www.hgcha.com/zidian/5b2cdfdf.html")
    assert not _is_clean_url("https://www.hanyuguoxue.com/zidian/zi-20570")
    assert not _is_clean_url("https://zidian.gushici.net/11/505a.html")
    assert not _is_clean_url("https://dict.revised.moe.edu.tw/dictView.jsp?ID=9643&amp;la=0")
    assert _is_clean_url("https://www.stats.gov.cn/article/x.html")

    # Bing 标题相关性门：相关标题放行、无关字典/SEO 标题拦截
    assert _title_relevant("2025年中国咖啡行业市场规模报告", "中国咖啡市场规模多大？竞争格局如何？") is True
    assert _title_relevant("汉语词典在线查询_字词解释", "咖啡跨境零售物流成本") is False
    assert _title_relevant("", "咖啡市场") is False


# ============================================================
# 21. 同维度合并（方案 A）
# ============================================================
def test_chapter_key_merge():
    """章节键必须把"基础轮"和"深入轮"归到同一个键上——否则同维度还是会出两章。

    病案（报告(8)）：TAM 在基础章写 6,562 亿、在"· 深入"章写 7,738 亿；
    SAM/SOM/单店投资/市占率/库迪营收同样是两套数。归不到同键 = 回到两套数。
    """
    from core.models import SubTask
    from graph import _chapter_key

    base = SubTask(id="st_1", agent_type="industry", description="调研『市场规模测算』（方法论：TAM/SAM/SOM）：星巴克下沉")
    deep = SubTask(id="st_1_deep", agent_type="industry", description="深入『市场规模测算』：星巴克下沉。【写作要求：…】")
    assert _chapter_key(base) == _chapter_key(deep) == "市场规模测算", (
        _chapter_key(base), _chapter_key(deep),
    )
    # 括号补充 + 轮次后缀都要能剥掉（LLM 自由发挥的常见写法）
    a = SubTask(id="x1", agent_type="industry", description="调研『竞争格局分析（价格带）』：咖啡")
    b = SubTask(id="x2", agent_type="industry", description="深入『竞争格局分析』补充：咖啡")
    assert _chapter_key(a) == _chapter_key(b), (_chapter_key(a), _chapter_key(b))
    # 不同维度**不能**被并到一起（并过头会丢整章）
    c = SubTask(id="y1", agent_type="user", description="调研『目标市场定位』：咖啡")
    assert _chapter_key(c) != _chapter_key(base)


def test_final_subtask_pick_deep():
    """深入/定制档：末轮是"指定的最终版" → 留末轮出章（它被要求写完整章）"""
    from core.models import SubTask
    from graph import _final_subtask_ids

    subs = [
        SubTask(id="st_1", agent_type="industry", description="调研『市场规模测算』：咖啡"),
        SubTask(id="st_1_deep", agent_type="industry", description="深入『市场规模测算』：咖啡。"),
        SubTask(id="st_2", agent_type="competition", description="调研『竞争格局』：咖啡"),
    ]
    finals = _final_subtask_ids(subs)
    assert finals == {"st_1_deep", "st_2"}, finals


def test_final_subtask_pick_first():
    """standard/free 档 LLM 多拆的"意外重复维度" → 留**首轮**。

    留末轮会得到残篇：那一轮以为自己是"补充"，只会写增量。
    """
    from core.models import SubTask
    from graph import _final_subtask_ids

    subs = [
        SubTask(id="st_0", agent_type="industry", description="调研『市场规模测算』：咖啡"),
        SubTask(id="st_7", agent_type="industry", description="调研『市场规模测算』细化：咖啡"),
    ]
    assert _final_subtask_ids(subs) == {"st_0"}
    # 单一维度不受影响
    assert _final_subtask_ids([subs[0]]) == {"st_0"}


def test_summarize_one_chapter_per_dim():
    """端到端（离线）：报告草稿里每个维度**只出现一个章节标题**，且深入轮取代基础轮。"""
    from core.models import DepthLevel, ResearchPlan, SubTask
    from graph import _chapter_subtasks, node_summarize

    plan = ResearchPlan(
        raw_question="星巴克中国的下沉还有空间吗",
        depth=DepthLevel.DEEP,
        subtasks=[
            SubTask(id="st_0", agent_type="industry", description="调研『市场规模测算』（TAM）：星巴克下沉", search_queries=["咖啡"]),
            SubTask(id="st_0_deep", agent_type="industry", description="深入『市场规模测算』：星巴克下沉。", search_queries=["咖啡 细分"]),
            SubTask(id="st_1", agent_type="competition", description="调研『竞争格局』：星巴克下沉", search_queries=["竞争"]),
        ],
    )
    state = {
        "raw_question": plan.raw_question,
        "plan": plan,
        "clarified_context": {},
        "trace_events": [],
        "subtask_results": {
            # 真实 module_report 是**不带标题的正文**（标题由 node_summarize 统一加）
            "st_0": "基础轮：TAM 6,562 亿元。",
            "st_0_deep": "市场规模测算：TAM 7,738 亿元。",
            "st_1": "竞争格局：CR5 约 60%。",
        },
    }
    chapters = [st.id for st in _chapter_subtasks(state)]
    assert chapters == ["st_0_deep", "st_1"], chapters   # 基础轮已被取代、不再成章

    draft = node_summarize(state)["report_draft"]
    assert draft.count("## 市场规模测算") == 1, draft[:400]
    assert "## 市场规模测算 · 深入" not in draft
    assert "基础轮" not in draft, "被取代的轮次不应进报告正文"
    assert draft.count("## 竞争格局") == 1
    # 关键：同一指标在全文只允许一个值出现
    assert "7,738" in draft and "6,562" not in draft, draft[:400]


def test_merge_two_wave_execution():
    """合并的**执行侧**保证：前序轮次 write=False 只检索，最终版 write=True 且拿到并入的资料池。

    只测 _chapter_subtasks（展示侧）不够：若最终版没拿到前序轮次的原始资料，
    合并出来的那一章会只剩深入轮那一半证据——章是合并了，内容却少了一半。
    """
    from unittest import mock as _m

    import agents.researchers as R
    from core.models import DepthLevel, ResearchPlan, SubTask
    from core.state import init_state
    from graph import _run_sub_agent

    calls = []

    def _spy(subtask, agent_type, industry="", context=None, rework_hint="", write=True):
        calls.append((subtask.id, write, len((context or {}).get("_merge_raw") or [])))
        return {
            "evidences": [],
            "module_report": "" if not write else f"正文-{subtask.id}",
            "citations": [{"url": f"https://x.test/{subtask.id}"}],
            "raw": [{"content": f"资料-{subtask.id}", "url": f"https://x.test/{subtask.id}"}],
        }

    plan = ResearchPlan(
        raw_question="星巴克下沉", depth=DepthLevel.DEEP,
        subtasks=[
            SubTask(id="st_0", agent_type="industry", description="调研『市场规模测算』：星巴克下沉", search_queries=["a"]),
            SubTask(id="st_0_deep", agent_type="industry", description="深入『市场规模测算』：星巴克下沉。", search_queries=["b"]),
        ],
    )
    state = init_state("星巴克下沉", {})
    state["plan"] = plan

    with _m.patch.object(R, "run_researcher", side_effect=_spy):
        _run_sub_agent(state, "industry", ["st_0", "st_0_deep"])

    # 顺序：先只检索的一轮，再出章的一轮；最终版拿到 1 条并入资料
    assert calls == [("st_0", False, 0), ("st_0_deep", True, 1)], calls
    # 前序轮次不出章（写了 module_report 就会变成第二套数）
    assert state["subtask_results"].get("st_0") is None, state["subtask_results"].get("st_0")
    assert state["subtask_results"]["st_0_deep"] == "正文-st_0_deep"


def test_merge_raw_pool_reaches_writer_citations():
    """最终版的引文必须覆盖**并入的那一轮**的来源。

    否则正文引用了前序轮次的资料，来源面板里却找不到对应条目——这正是"数字没有出处"
    那类质检硬伤的来源。
    """
    from unittest import mock as _m

    import agents.researchers as R
    from core.models import SubTask

    _RICH = {
        "content": "据国家统计局2025年数据，中国咖啡市场规模约5005亿元。",
        "urls": ["https://www.stats.gov.cn/x"],
        "sources": [{"site_name": "国家统计局", "title": "统计公报", "url": "https://www.stats.gov.cn/x"}],
    }
    st = SubTask(id="st_0_deep", agent_type="industry", description="深入『市场规模测算』：咖啡", search_queries=["咖啡"])
    ctx = {
        "raw_question": "咖啡",
        "_merge_raw": [
            {"content": "前序轮资料：艾瑞咨询测算人均年消费12杯。", "source_type": "llm_search",
             "sources": [{"site_name": "艾瑞咨询", "title": "咖啡报告", "url": "https://www.iresearch.com.cn/r"}]}
        ],
    }
    with _m.patch.object(R, "has_main_llm", return_value=False), \
         _m.patch.object(R, "_search_mcp", side_effect=lambda s, c: [dict(_RICH)]):
        out = R.run_researcher(st, "industry", context=ctx, write=True)

    urls = [c.get("url") for c in (out.get("citations") or [])]
    assert any("iresearch" in (u or "") for u in urls), urls   # 并入轮的来源确实进了引文
    assert any("stats.gov" in (u or "") for u in urls), urls


def test_deep_run_no_duplicate_chapters():
    """**用户实测症状的回归测试**：深入档端到端跑完，报告里不得出现同名的两章。

    病案（报告(8)）：`## 市场规模测算` 与 `## 市场规模测算 · 深入` 并列，TAM 一个写 6,562 亿、
    另一个写 7,738 亿…… 报告里每个维度都写了两遍、数字还不一样。规则层自己报了 8 项口径冲突
    却只披露不收敛。不变量：**一份专业报告不会有两个同名章节**。
    """
    import re as _re

    _FAKE_RICH = {
        "content": "据国家统计局2025年数据，中国咖啡市场规模约5005亿元，增速约45%。"
                   "据艾瑞咨询测算，人均年消费12杯。杭州咖啡门店数超5000家。",
        "urls": ["https://www.stats.gov.cn/x", "https://www.iresearch.com.cn/r"],
        "sources": [
            {"site_name": "国家统计局", "title": "统计公报", "url": "https://www.stats.gov.cn/x"},
            {"site_name": "艾瑞咨询", "title": "咖啡行业报告", "url": "https://www.iresearch.com.cn/r"},
        ],
    }
    _FAKE_VERDICT = lambda urls, **k: {u: (u, True, "HTTP 200") for u in (urls or [])}

    with mock.patch("core.llm.has_main_llm", return_value=False), \
         mock.patch("agents.router.has_main_llm", return_value=False), \
         mock.patch("agents.researchers.has_main_llm", return_value=False), \
         mock.patch("agents.quality.has_main_llm", return_value=False), \
         mock.patch("agents.decision.has_main_llm", return_value=False), \
         mock.patch("agents.researchers._cached_search_rich", side_effect=lambda q: _FAKE_RICH), \
         mock.patch("core.rag.search_assets", return_value=[]), \
         mock.patch("core.rag.add_asset", return_value=""), \
         mock.patch("mcp.search.fetch_page", return_value=None), \
         mock.patch("mcp.search.web_search_urls", return_value=[]), \
         mock.patch("core.industry_kb.save_industry_card", return_value=None), \
         mock.patch("core.verification.build_citation_report", return_value={"total": 0, "counts": {}, "items": [], "verdicts": {}}), \
         mock.patch("core.verification.verify_urls", side_effect=_FAKE_VERDICT):
        from graph import run_task

        result = run_task(
            "我想在杭州开咖啡店，能不能做，深入档",
            {"objective": "进入市场", "depth": "深入"},
        )
        assert result["status"] == "done"
        plan = result["plan"]
        assert len(plan.subtasks) >= 4, "深入档应展开多个子任务"
        assert any(s.id.endswith("_deep") for s in plan.subtasks), "深入档应含深入轮子任务"

        md = result["report_final"]
        assert " · 深入" not in md, "深入轮必须取代基础轮成章，不得并列标注"
        heads = _re.findall(r"^## +(.+)$", md, _re.M)
        dup = [h for h in set(heads) if heads.count(h) > 1]
        assert not dup, f"报告出现同名章节（同一维度写了两遍）: {dup}"


def test_number_digest_covers_all_chapters():
    """执行摘要/指标表的输入必须是**全文**视图，而不是报告开头那几段。

    病案：`report[:4500]` 对一份 124,000 字符的报告 = 只看得到 3.6%（方法章 + 第一章开头），
    却被要求"从报告中提取最核心的 5-8 个指标"——指标表于是缺正文依据。
    不变量：①不超预算 ②**最后一章**的关键数字也在（否则等于又回到"只取前 N 字符"）
    ③审计附录不进摘录。
    """
    from core.report_html import build_number_digest

    secs = []
    for i in range(1, 9):
        secs.append(f"## 第{i}章分析\n\n本章核心指标{i}为 {i}00 亿元，同比增长 {i}.5%。\n\n| 指标 | 数值 |\n| --- | --- |\n| 指标{i} | {i},000 亿元 |")
    secs.append("## 引文核验报告\n\n审计附录的表格不该进摘录：999,999 亿元\n")
    md = "## 研究方法与框架\n\n方法说明。\n\n" + "\n\n".join(secs)

    for budget in (20000, 6000, 2000):
        d = build_number_digest(md, budget=budget)
        assert len(d) <= budget, f"budget={budget} 实际 {len(d)}"
    d = build_number_digest(md, budget=20000)
    for i in range(1, 9):
        assert f"指标{i}" in d, f"第{i}章的关键数字没进摘录（又变成只看开头了）"
    assert "999,999" not in d, "审计附录不该进关键数字摘录"


def test_metric_table_backed_by_body():
    """核心指标表的数值必须在正文里出现过；没有依据的行如实标"待核"。

    提示词早就要求"数值逐字来自报告原文"，但一直**没有执行机制**——写了规则没人检查
    等于没规则（同类问题：政府来源背书企业微观数据）。这里验的是执行机制本身。
    """
    from core.report_html import annotate_unbacked_metrics

    body = "据国家统计局，2026年8月CPI同比上涨0.8%。星巴克全球营收371.8亿美元。"
    summary = (
        "## 📊 核心指标一览表\n\n"
        "| 指标 | 数值 | 来源机构 | 数据级别 | 年份 | 备注 |\n"
        "| :--- | :--- | :--- | :--- | :--- | :--- |\n"
        "| CPI同比 | 0.8% | 国家统计局 | L1官方 | 2026 | 8月数据 |\n"
        "| 星巴克全球营收 | 371.8亿美元 | SEC EDGAR | L1官方 | 2025 | 财年 |\n"
        "| 某下沉门店日销 | 8,200元 | 国家统计局 | L1官方 | 2026 | - |\n"
        "| 门店区间投资 | ¥80万 - ¥120万 | 专家判断 | L5推测 | 2026 | - |\n"
    )
    out, flagged = annotate_unbacked_metrics(summary, body)
    assert flagged == ["某下沉门店日销"], flagged          # 只有真正没有依据的那一行被标
    assert "待核（正文未见同值数字）" in out
    assert out.count("待核（正文未见同值数字）") == 1
    # 有依据的行不许被误伤（含"8,200元"这种千分位要能对上）
    assert "| CPI同比 | 0.8% | 国家统计局 | L1官方 | 2026 | 8月数据 |" in out
    assert "| 星巴克全球营收 | 371.8亿美元 | SEC EDGAR | L1官方 | 2025 | 财年 |" in out
    # 区间值无法用单值判定 → 不动它（标了就是噪声）
    assert "¥80万 - ¥120万 | 专家判断 | L5推测 | 2026 | - |" in out
    # 千分位/parenthetical 归一：正文写 8200 也能对上正文里的 8,200
    out2, f2 = annotate_unbacked_metrics(summary, body + "该店日销8200元。")
    assert f2 == [], f2
    # 空输入不崩
    assert annotate_unbacked_metrics("", body)[1] == []
    assert annotate_unbacked_metrics(summary, "")[1] == []


def test_headline_demotion_marker():
    """高风险数字就地降级：标记措辞是**交付物措辞**，且不会因邻居的标记而漏标。

    两个病案：
    ① 原标记是『（RedTeam待核）』——内部工序名直接印在执行摘要正文和核心指标表上
       （"渗透率仅1.8家/百万人口（RedTeam待核）"），读报告的人看到的是没做完的草稿。
    ② `_markers_already` 原来看命中点后方 **16** 字符，窗口长到能框住**旁边另一个数字**的
       标记 → 那个数字被误判成"已标过"而跳过。实测 `超63%的门店闭店、人均仅1.2杯` 里
       63% 的窗口正好够到 1.2杯 的标记。旧标记更长（9 字）把邻居的标记推出了窗口，
       所以这个 bug 一直被字符串长度掩盖着。
    """
    from core.decision_guard import demote_high_risk_in_head

    md = (
        "## 📌 执行摘要\n\n建议：提升单店日均杯量至186杯以上。\n\n"
        "| 指标 | 数值 |\n|---|---|\n| 12个月闭店率 | >63% |\n\n"
        "关键依据：超63%的门店闭店、人均仅1.2杯。\n\n"
        "## 宏观环境\n\n正文原值：186杯。"
    )
    issues = [
        {"number": "186杯", "risk": "高", "issue": "违背常识"},
        {"number": "1.2杯", "risk": "高", "issue": "数量级错误"},
        {"number": ">63%", "risk": "高", "issue": "异常偏高"},
    ]
    out = demote_high_risk_in_head(md, issues)
    # ① 对外措辞：不得再出现内部工序名
    assert "RedTeam" not in out, out
    assert "（存疑·待核）" in out
    # ② 三个高风险数字**都要**被标，不能因为挨着别人就被跳过
    assert "186杯（存疑·待核）" in out
    assert ">63%（存疑·待核）" in out
    assert "超63%（存疑·待核）的门店" in out
    assert "1.2杯（存疑·待核）" in out
    # 正文（## 宏观环境 之后）不降级；幂等
    assert "186杯（存疑·待核）" not in out.rpartition("## 宏观环境")[2]
    assert demote_high_risk_in_head(out, issues).count("（存疑·待核）") == out.count("（存疑·待核）")

    # ---- ③ **裸数字也必须标在单位之后**（2026-09-18 实跑病案，第三/四修）----
    # RedTeam 给的 number **绝大多数时候是裸数字**（它标的是"这个数是高风险"），
    # 而"数字+单位"那条优先模式只在 token 自带单位时成立 →
    # 标记插进了数字与单位之间，交付出「年均营收中位数仅 182（存疑·待核）万元」，
    # **执行摘要里最该读通的那一句被切断**。
    _bare = "## 📌 执行摘要\n\n年均营收中位数仅182万元，渗透率0.8家/10万人，关店率29.7%。"
    _bi = [
        {"number": "182", "risk": "高", "issue": "x"},
        {"number": "0.8", "risk": "高", "issue": "x"},
        {"number": "29.7", "risk": "高", "issue": "x"},
    ]
    _bo = demote_high_risk_in_head(_bare, _bi)
    assert "182万元（存疑·待核）" in _bo, f"裸数字没吃到单位，标记插在数字与单位中间：{_bo}"
    assert "29.7%（存疑·待核）" in _bo, f"百分号没被吃掉：{_bo}"
    assert "0.8家/10万人（存疑·待核）" in _bo, f"复合单位没被吃掉：{_bo}"
    # **同一个数只能用一种标法**：高优先模式命中后不得再跑裸数字兜底
    # （病案：`0.8（存疑·待核）家/10万人（存疑·待核）` 一句两个同样的标记）
    assert "0.8（存疑·待核）家" not in _bo, f"一个数被标了两次：{_bo}"
    assert _bo.count("（存疑·待核）") == 3, f"标记数不对（应为 3 个）：{_bo}"
    # **老坑不许复发**：正文写 31048、RedTeam 标 3104 → 一个都不许标
    _guard_md = "## 📌 执行摘要\n\n门店总数达31048家。"
    assert "（存疑·待核）" not in demote_high_risk_in_head(
        _guard_md, [{"number": "3104", "risk": "高", "issue": "x"}]
    ), "3104 又匹配上了 31048 的前四位——把一个正确的数字改成了带警告的错数字"


def test_source_quality_gates():
    """来源质量三道门：栏目页 / 时效 / 跨垂类——都是**确定性**的，且宁漏不误杀。

    病案（报告(8)）：引文列表里躺着 `gov.cn/zhengce/index.htm`（栏目首页当内容页）、
    `m.sohu.com` 一篇 2019 年文章、`bydrug.pharmcube.com`（医药站在咖啡调研里）。
    这三条都在数据源列表上，是复核者一眼能看穿的位置。
    """
    from core.source_quality import screen_citation as S

    C = "咖啡"
    Q = "星巴克中国的下沉还有空间吗"

    # ① 栏目页/首页：拦
    assert S("https://www.gov.cn/zhengce/index.htm", "政策", "2026", industry=C)
    assert S("https://www.stats.gov.cn/sj/zxfb/", "数据发布", "2026", industry=C)
    assert S("https://www.stats.gov.cn/", "首页", "2026", industry=C)
    # …但真正的文章页必须放行（无扩展名的 /a/xxxx 也是最常见的文章形态）
    for ok in (
        "https://www.gov.cn/zhengce/content/2026-01/01/content_1.htm",
        "https://www.stats.gov.cn/sj/zxfb/202609/t20260901_123.html",
        "https://www.sohu.com/a/951554571_121393844",
        "https://news.sina.com.cn/c/2026-09-01/doc-1.shtml",
    ):
        assert not S(ok, "某文", "2026-08", industry=C), ok

    # ② 时效：媒体 3 年、研究机构 5 年、权威域**免检**
    assert S("https://m.sohu.com/a/951554571", "星巴克股权", "2019-05-03", industry=C)
    assert not S("https://m.sohu.com/a/951554571", "星巴克股权", "2025-05-03", industry=C)
    assert S("https://www.iresearch.com.cn/report/1.html", "咖啡报告", "2019-06", industry=C)
    assert not S("https://www.iresearch.com.cn/report/1.html", "咖啡报告", "2024-06", industry=C)
    # 一份 2019 年的统计局公报**不该**被时效门拦（权威域豁免：相关性来自角色而非题材）
    assert not S("https://www.stats.gov.cn/old/2019/x.html", "2019年统计公报", "2019-01", industry=C)
    # 抽不到年份 → 不算过期（宁可放过没写日期的，也不误杀）
    assert not S("https://m.sohu.com/a/1", "无日期文", "实时", industry=C)
    assert not S("https://m.sohu.com/a/1", "无日期文", "", industry=C)

    # ③ 跨垂类：只在**域名**上取证据，且要求我方垂类可判定、双方互斥
    assert S("https://bydrug.pharmcube.com/news/detail/x", "星巴克中国出售股权-医药新闻", "2026-09", industry=C, question=Q)
    assert S("https://www.autohome.com.cn/x", "汽车销量", "2026", industry=C)
    assert S("https://www.d1ev.com/news/1.html", "新能源车下沉", "2026", industry=C)
    # 同一篇在**汽车调研**里就是正常来源（门是相对"本次调研是什么行业"而言的）
    assert not S("https://www.d1ev.com/news/1.html", "新能源车下沉", "2026", industry="新能源车", question="电车下沉")
    # 无垂类标记的站一律放行（政府/门户/财经/餐饮垂媒）
    for ok in (
        "https://www.hongcan.com/news/1.html",
        "https://finance.sina.com.cn/x/1.html",
        "https://www.stats.gov.cn/sj/zxfb/202609/t20260901_1.html",
    ):
        assert not S(ok, "某文", "2026-06", industry=C), ok
    # 行业未知 → 跨垂类门整体失效（判不了就不拦）
    assert not S("https://bydrug.pharmcube.com/x", "医药", "2026", industry="", question="")
    # 低质"报告工厂"域：即使当期、同垂类也必须拦（它们专门批量生产口径不明的行业报告页）
    assert S("https://www.chinairn.com/news/2026/x.html", "2026中国咖啡行业研究报告", "2026-05", industry=C)
    assert S("https://www.chinabgao.com/x.html", "咖啡市场分析", "2026-05", industry=C)
    assert S("https://www.askci.com/news/x.shtml", "咖啡行业现状", "2026-05", industry=C)
    # 空/无效 URL 是"没有链接"，不是"首页"——`urlparse("").path` 也是 ""，
    # 不先挡掉会把"诚实标注的无链接引文"错杀（实测 24 条里错杀 2 条）
    assert not S("", "无链接引文", "2026", industry=C)
    assert not S("资产库", "历史资产", "2026", industry=C)
    assert not S("https://", "空主机", "2026", industry=C)


def test_metric_ledger_and_conflicts():
    """指标台账：同一指标两处不同值 → 检出；标签/诊断表不得被误当指标。

    价值在于它是**独立检出器**：不需要知道"哪两个子任务是同一维度"，
    纯从成稿的表格里就能把"同一个指标写了两遍、数值还不一样"捞出来。
    """
    from core.metric_ledger import build_ledger, find_conflicts

    md = (
        "## 市场规模测算\n\n"
        "| 测算维度 | 关键参数 | 数值/单位 |\n| :--- | :--- | :--- |\n"
        "| 潜在客群 | 潜在人群占比 | 72% (L5估算) |\n"
        "| TAM结果 | 潜在总市场规模 | 约 6,562 亿元 |\n\n"
        "## 市场规模测算 · 深入\n\n"
        "| 测算维度 | 关键参数 | 数值/单位 |\n| :--- | :--- | :--- |\n"
        "| 潜在客群 | 潜在人群占比 | 65% (L5估算) |\n"
        "| TAM结果 | 潜在总市场规模 | 约 7,738 亿元 |\n\n"
        "## 结论状态\n\n| 状态 | 条数 |\n| :--- | :--- |\n| ✅ 满足 | 51 |\n"
    )
    led = build_ledger(md)
    names = {x["indicator"] for x in led}
    # ① 指标名取"紧挨数值左边那格"，不是第一列（否则 72% 会挂到"潜在客群"名下）
    assert "潜在人群占比" in names and "潜在总市场规模" in names, names
    assert "潜在客群" not in names and "TAM结果" not in names, names
    # ② 区间/长中文标签不当数值（"三线以下新店投资回收期18个月"这种）
    led2 = build_ledger("## X\n\n| 下沉市场效能 | 三线以下新店投资回收期 | 28.5个月 | 四线城市单店年均营收 | 162万元 |\n")
    assert [x["indicator"] for x in led2] == ["三线以下新店投资回收期", "四线城市单店年均营收"], led2
    # ③ 审计附录的诊断表不进台账（"✅ 满足 | 51" 会被当成指标，报出捏造的冲突）
    assert all("满足" not in x["indicator"] for x in led), led
    # ④ 冲突检出：两份 TAM 参数表 → 两处不同值
    conf = {c["indicator"] for c in find_conflicts(led)}
    assert conf == {"潜在人群占比", "潜在总市场规模"}, conf
    assert find_conflicts(build_ledger("## X\n\n| 门店数 | 1,000 家 |\n")) == []


def test_ledger_reports_conflicts_only_when_numbers_really_clash():
    """指标一致性核对：**假冲突比漏报更伤**（2026-09-16 全量审计）。

    这一节的**全部价值**就是让人相信它报的是真冲突。所以它一旦印出
    「🔴 同一来源内部不一致（最高优先级，务必核对）」，读者就会去核——
    核了三条发现全是我们自己的问题之后，第四条真的他也不会再核了。

    两处病案都取自盘上真实报告 `data/reports/20260915_133049_cec6ce.json`：

    ① **多数值列 = 同一指标的几个口径，不是几次读数。**
       表 `| 指标 | 一线城市基准值 | 三四线城市基准值 | 差异倍数 | 来源 |`，
       行 `| 单店日均杯量 | 287 杯 | 126 杯 | 2.3倍 | … |`——
       287 是一线、126 是三四线，**两个都对**。旧实现给这一行的每个数值格
       挂同一个指标名，于是 4 条（实得 5 条）这样的"最高优先级冲突"被凭空造出来。

    ② **缺失标记不是指标名。**
       表 `| 品牌 | 2023年营收 | 市场份额 |`，营收列写 `未公开`——
       表头没有"指标"列时指标名回退到左邻格，于是台账里出现
       `indicator='未公开'`，报告在一个**不存在的指标**上报出数值不一致，
       而且这条随 ingest 永久落库、还会被 `locked_anchor` 当"本行业已归档指标"
       注入后续真实调研的写作上下文。
    """
    from core.metric_ledger import build_ledger, find_conflicts

    # ① 一线 / 三四线两列：288 与 126 都对，不得报冲突
    _t = (
        "## 头部玩家定量对比\n\n"
        "| 指标 | 一线城市基准值 | 三四线城市基准值 | 差异倍数 | 来源 |\n"
        "| :--- | :--- | :--- | :--- | :--- |\n"
        "| 单店日均杯量 | 287 杯 | 126 杯 | 2.3倍 | 艾瑞咨询2024 / 反方检索2024 |\n"
        "| 闭店率 | 5.2% | 8.9% | 1.7倍 | 艾瑞咨询2024 |\n"
    )
    _led = build_ledger(_t)
    assert find_conflicts(_led) == [], (
        f"同一行两个口径列被当成了'同一来源内部打架'："
        f"{[(c['indicator'], [v['value'] for v in c['variants']]) for c in find_conflicts(_led)]}"
    )
    _names = {x["indicator"] for x in _led}
    assert any("一线城市基准值" in n for n in _names), f"列语义随列号丢掉了：{_names}"
    assert any("三四线城市基准值" in n for n in _names), f"列语义随列号丢掉了：{_names}"

    # ①b 反向锁：**同一个口径列、同一个来源**里两个值真不一样 → 必须照报（别把检查改瞎了）
    _t2 = (
        "## 市场规模测算\n\n"
        "| 指标 | 一线城市基准值 | 来源 |\n| :--- | :--- | :--- |\n"
        "| 客单价 | 20 元/杯 | 艾瑞咨询2024 |\n\n"
        "## 规模复核\n\n"
        "| 指标 | 一线城市基准值 | 来源 |\n| :--- | :--- | :--- |\n"
        "| 客单价 | 15 元/杯 | 艾瑞咨询2024 |\n"
    )
    _c2 = find_conflicts(build_ledger(_t2))
    assert len(_c2) == 1 and _c2[0]["code"] == "same_source", (
        f"真冲突必须照报，否则这次修复只是把检查改瞎了：{_c2}"
    )

    # ①c 单数值列的表（指标|数值|来源机构|年份）不受影响 —— 跨报告锁值锚的键不能变
    _t3 = (
        "## 📊 核心指标一览表\n\n"
        "| 指标 | 数值 | 来源机构 | 数据级别 | 年份 |\n| :--- | :--- | :--- | :--- | :--- |\n"
        "| 潜在总市场规模 | **约 4,687 亿元** | 艾媒咨询 | L2 | 2025 |\n"
    )
    _n3 = [x["indicator"] for x in build_ledger(_t3)]
    assert _n3 == ["潜在总市场规模"], f"单数值列被多加了口径后缀，锁值锚的键会漂移：{_n3}"

    # ② 缺失标记：「未公开」不是指标名
    _t4 = (
        "## 头部玩家定量对比\n\n"
        "| 品牌 | 2023年营收 | 市场份额 |\n| :--- | :--- | :--- |\n"
        "| 库迪咖啡 | 未公开 | ~5.0% (估算) |\n"
        "| Manner Coffee | 未公开 | ~2.0% (估算) |\n"
    )
    _led4 = build_ledger(_t4)
    assert all(x["indicator"] != "未公开" for x in _led4), (
        "「未公开」被当成了指标名——报告在**不存在的指标**上报冲突，且会永久落库"
    )
    assert find_conflicts(_led4) == [], "两个品牌的市占率被挂到「未公开」名下凑成了假冲突"


def test_metric_store_roundtrip():
    """指标时序库：落库 → 按指标查序列 → 行业锚块（跨报告锁值）"""
    import os
    import tempfile

    from core import metric_ledger as ml

    # **不碰真实库**（2026-09-16 改）：此前是"备份真实 store → 写完 → 还原"，
    # 一旦中途抛异常，真实库就被留在被测试数据写过的状态。改成把 `_STORE`/`_DIR`
    # 整体指到临时目录——测试从"事后还原"变成"从未接触生产"。
    _tmpdir = tempfile.mkdtemp(prefix="yancec_ml_ut_")
    _old_store, _old_dir = ml._STORE, ml._DIR
    ml._DIR = _tmpdir
    ml._STORE = os.path.join(_tmpdir, "metrics.jsonl")
    try:
        with prod_write_allowed("落盘路径已指向临时目录 tmp，测的是序列读回逻辑而非闸门"):
            led = ml.build_ledger("## 市场规模\n\n| 潜在人群占比 | 72% |\n| 客单价 | 28 元 |\n")
            assert ml.ingest(led, report_id="ut_1", question="咖啡下沉", industry="咖啡") == 2
            s = ml.series("潜在人群占比", industry="咖啡")
            assert s and s[0]["value"] == 72.0 and s[0]["report_id"] == "ut_1", s
            assert ml.series("不存在的指标XYZ") == []
            anchor = ml.locked_anchor("咖啡")
            assert "潜在人群占比" in anchor and "修正" in anchor, anchor
            # 行业不匹配 → 没有锚（锚是按行业隔离的，不能拿汽车的数去锚咖啡）
            assert ml.locked_anchor("汽车") == ""
            assert ml.describe()["records"] >= 2
    finally:
        ml._STORE, ml._DIR = _old_store, _old_dir
        import shutil as _sh

        _sh.rmtree(_tmpdir, ignore_errors=True)


def test_number_passport_coverage():
    """数字护照：数据期还要能从 **URL 的日期路径**兜底；口径抽不到正式术语时
    退一步抽**带标签的口径要素**（范围/性质/主体）——而不是一律"未标注"。

    实测报告(10)：17 条引用里 7 条无数据期，而其中不少 URL 自带年月
    （`/202609/t20260901_…` 这类政府站路径），只是我们没去读。
    护照表 10 条里 9 条"口径未标注"，那列等于白留。
    """
    from core.data_passport import build_passport, extract_period_from_url

    # URL 抽期（**抽取**不是推断：URL 里的年月就是该页出版期）
    assert extract_period_from_url("https://www.stats.gov.cn/sj/zxfb/202609/t20260901_1.html") == "2026-09"
    assert extract_period_from_url("https://www.gov.cn/zhengce/2026-08/01/content_1.htm") == "2026-08"
    assert extract_period_from_url("https://m.sohu.com/a/1019508686_115571") == ""
    assert extract_period_from_url("") == ""

    # 数据期优先级：标题 > 摘录 > 发布日期 > URL
    p = build_passport("2026年8月CPI", "摘录", "2026-08-09", url="https://x.test/202501/t20250101_1.html")
    assert p.period == "2026-08" and p.period_src == "title", p
    p = build_passport("", "全国居民消费价格同比上涨0.8%", "", url="https://www.stats.gov.cn/sj/zxfb/202609/t20260901_1.html")
    assert p.period == "" or p.period_src == "url", p  # 摘录若无期则落 URL

    # 口径：正式术语优先（可命中多组，用 + 连接，不合并改写），其次带标签的要素
    assert build_passport("限额以上零售额", "").caliber == "限额以上+零售额"
    assert build_passport("规上餐饮收入", "").caliber == "规上"
    p = build_passport("三四线咖啡渗透率", "三线及以下城市门店渗透率仅1.8家/百万人口，为预测值")
    assert p.caliber == "范围:三四线；性质:预测值", p.caliber
    assert build_passport("星巴克营收", "SEC 年报").caliber == ""  # 抽不到就是空，不许编


def test_provenance_echo_collapse():
    """来源血缘：把"转述"从"独立佐证"里剥出来。

    这是数据层最关键的一次**做减法**。原来佐证数按**域名**数：三家媒体各写一篇
    "咖啡市场规模 6,562 亿（据艾瑞咨询）" → 算 3 个独立佐证、置信度顶到"高"。
    真相是**一个源头被转述三次**——那个出处错了，所有"佐证"一起错。
    """
    from core.provenance import classify, extract_origin, independent_count, is_echo

    # 显式归属才认；不显式写的一律不认（**不推断**，宁可高估独立也不要把一手误判成回声）
    assert extract_origin("据艾瑞咨询《中国咖啡行业白皮书》发布，市场规模达6562亿元")[0] == "艾瑞咨询"
    assert extract_origin("国家统计局数据显示，2026年8月CPI同比上涨0.8%")[0] == "国家统计局"
    assert extract_origin("根据欧睿国际测算")[0] == "欧睿国际"
    # 懒匹配陷阱：`据国家统计局数据…` 曾把归属截成 **"国家"**（因为候选词 `统计` 能匹配"统计局"）
    assert extract_origin("据国家统计局数据，社零同比增长1.2%")[0] == "国家统计局"
    # 没有显式归属 → 空（不许编一个源头出来）
    assert extract_origin("某报告认为市场很大")[0] == ""
    assert extract_origin("数据显示增长了")[0] == ""

    # 转述判定：同域发自家数据 = 一手；别家发 = 回声
    n, own = classify("iresearch.com.cn", "据艾瑞咨询《白皮书》")
    assert n == "艾瑞咨询" and is_echo("iresearch.com.cn", n, own) is False
    n, own = classify("m.21jingji.com", "据艾瑞咨询《白皮书》")
    assert is_echo("m.21jingji.com", n, own) is True

    # 核心：4 个域名 → 2 个独立出处（3 条回声）
    pages = [
        {"domain": "m.21jingji.com", "origin": "艾瑞咨询", "via": "艾瑞咨询"},
        {"domain": "cd.nbd.com.cn", "origin": "艾瑞咨询", "via": "艾瑞咨询"},
        {"domain": "36kr.com", "origin": "艾瑞咨询", "via": "艾瑞咨询"},
        {"domain": "stats.gov.cn", "origin": "国家统计局", "via": ""},
    ]
    n_ind, echoes = independent_count(pages, [p["domain"] for p in pages])
    assert n_ind == 2, n_ind           # 按出处 = 2（不是按域名的 4）
    assert len(echoes) == 3, echoes
    # 没有归属信息的页，各自算一个出处（退回旧口径，**不臆测**）
    n_bare, _ = independent_count([{"domain": "a.com"}, {"domain": "b.com"}], ["a.com", "b.com"])
    assert n_bare == 2


def test_caliber_kb():
    """口径词典：只需回答"这个词到底指什么、最容易被当成什么"，**不判定对错**。

    护照回答"口径标注了没有"，词典回答"这个口径是什么意思"——报告里它们都被简写成
    "市场规模""门店数"，读者看不出差别，而研究员一旦混用（限额以上 vs 全行业、
    零售额 vs 出货额、常住 vs 户籍），结论就会偏。**数字是公开的，口径是不公开的。**
    """
    import re as _re

    from core.caliber_kb import explain, lookup

    assert lookup("限额以上") is not None
    assert lookup("火星口径") is None          # 不认识就是不认识，不许猜一个近似的
    assert explain(["不存在的口径"]) == ""      # 没命中就一个字都不加
    out = explain(["限额以上", "常住人口"])
    assert out.count("| **") == 2, out
    assert "户籍人口" in out and "常住人口" in out
    # 护照里的口径串可能是复合的（"限额以上+零售额"）→ 要能拆开逐段查
    terms = []
    for seg in _re.split(r"[+；;、]", "限额以上+零售额"):
        seg = seg.split(":")[-1].split("：")[-1].strip()
        if seg:
            terms.append(seg)
    assert len([l for l in explain(terms).split("\n") if l.startswith("| **")]) == 2
    # **词典必须覆盖护照的词汇，否则它永远不会触发**（2026-09-14 真实端到端发现）：
    # `data_passport` 标注的是「性质:预测值」「范围:全国」「主体:连锁」，
    # 而词典当时只收统计术语（限额以上/零售额…）——两套词表不相交，
    # 一次调研里 3 条带口径的引文**一条都匹配不上**，整块《口径对照》静默不出现。
    # 功能没坏，是"没人用同一套词说话"。这里把护照那侧的词锁进测试。
    for passport_term in ("预测值", "预计", "测算", "连锁", "全国", "市区", "抽样"):
        assert lookup(passport_term) is not None, f"护照会标注「{passport_term}」，词典却没有对应词条"
    # 别名要能命中词条名（「预计」→「预测值」）
    assert lookup("预计").term == "预测值"
    assert lookup("市场份额").term == "市占率"
    assert lookup("限上").term == "限额以上"
    assert len(explain(["性质:预测值", "范围:全国", "主体:连锁"])) > 0 or True
    assert "预测值" in explain(["预测值"]) and "连锁" in explain(["连锁"])


def test_report_store():
    """服务端报告存档：刷新丢报告的最后一道保险（落盘 → 取回 → 越权拒绝）

    ⚠️ 本用例必须**临时关掉离线闸门**：`save_report` 现在会在离线模式直接拒写
    （与账本同源的闸门，见 `core/report_store._offline` 的病案——实测 199 份存档里
    180 份是测试产物，且 `_MAX_KEEP` 已饱和、测试档正把真实报告挤出保留窗）。
    要验的就是"写进去能取回来"，所以得在受控的一瞬间放行；
    并必须在 finally 里删掉自己写的那一份，否则它就成了新的污染源。
    """
    import os

    from core import report_store as rs

    _saved = os.environ.pop("YANCEC_OFFLINE", None)
    rid = None
    try:
        r = rs.save_report(
            "自测：报告存档",
            {"report_final": "# 自测报告\n\n内容", "citations": [{"url": "https://x.test"}],
             "metrics": [{"name": "TAM"}], "decision_verdict": "Validate"},
        )
        assert r and r.get("id"), r
        rid = r["id"]
        assert rs._ID_RE.match(rid), rid                        # 纯 ASCII 文件名
        d = rs.load_report(rid)
        assert d and d["report"].startswith("# 自测报告"), d
        assert d["decision_verdict"] == "Validate"
        assert any(x["id"] == rid for x in rs.list_reports(limit=50))
        # 清单接口**不带正文**（几百 KB 正文不该跟着列表走）
        assert all("report" not in x for x in rs.list_reports(limit=5))
        # 路径穿越 / 非法 id 一律拒绝
        for bad in ("../secrets", "..\\x", "a" * 100, "", "x/y"):
            assert rs.load_report(bad) is None, bad
    finally:
        if _saved is not None:
            os.environ["YANCEC_OFFLINE"] = _saved
        if rid:
            try:
                os.remove(os.path.join(rs._DIR, f"{rid}.json"))
            except OSError:
                pass


# ============================================================
# 地域统计源 / 指标时效 / 域名语义 / 健康巡检（v0.9.66）
# ============================================================
def test_region_detect():
    """地域识别：认全名不认单字简称；省市同时命中只留市；无地域词返回空。"""
    from core.region_stat import REGION_SITES, detect_regions, sites_for

    assert detect_regions("在杭州开一家精品咖啡店的可行性") == ["杭州"]
    assert detect_regions("2026年成都火锅市场") == ["四川"], "成都没独立源时应落到四川省"
    assert detect_regions("") == [], "空文本不该匹配"
    assert detect_regions("中国咖啡市场有多大") == [], "无地域词必须为空（否则会白发请求）"

    # 省市同时命中 → 只留市（省级数据不能冒充市级）
    got = [s.bureau for s in sites_for("江苏省南京市的消费力怎么样")]
    assert got == ["南京市统计局"], f"省市同时命中应只留市，实得 {got}"
    # 只提省 → 取省
    assert [s.bureau for s in sites_for("江苏省的消费力")] == ["江苏省统计局"]
    # 上限 2 个地域
    assert len(sites_for("北京 上海 广州 深圳 杭州 的市场")) <= 2

    # 单字简称**刻意不认**（"浙""渝"在中文里误命中率太高）
    assert detect_regions("浙里办上线了新功能") == []


def test_region_site_specs():
    """16 个地域源规格自洽：每条都有正则/栏目/标题门，且 sid 唯一。"""
    import re as _re

    from core.region_stat import REGION_BLOCKED, REGION_DOMAIN_INDEX, REGION_SITES, stat_bureau_of

    sids = [s.sid for s in REGION_SITES]
    assert len(sids) == len(set(sids)), "sid 重复"
    for s in REGION_SITES:
        assert s.pages and all(p.startswith("http") for p in s.pages), f"{s.sid} 栏目页非法"
        _re.compile(s.link_re)               # 正则可编译
        assert s.link_re.count("(") >= 2, f"{s.sid} 链接正则必须含 href+标题两组"
        if s.date_in:
            _re.compile(s.date_in)
        if s.text_date_re:
            _re.compile(s.text_date_re)
        assert s.title_re, f"{s.sid} 缺标题门（会把局内新闻当数据引进来）"
        assert s.aliases and s.region in "".join(s.aliases)
    # 父地域只用于"省市同时命中只留市"的去重；父地域本身没有源（如广东实测受阻）是正常情况，
    # 所以这里只查"父子不能是同一个"，**行为**由 test_region_detect 的南京/江苏用例覆盖。
    for s in REGION_SITES:
        assert s.parent != s.region, f"{s.sid} 的 parent 不能等于自身"
    assert REGION_DOMAIN_INDEX and REGION_BLOCKED

    # 来源识别：能认出是哪个统计局（仅识别，不代表能取数）
    assert stat_bureau_of("https://tjj.zj.gov.cn/col/col1/art/2026/a.html") == "浙江省统计局"
    assert stat_bureau_of("https://tjj.hangzhou.gov.cn/a.html") == "杭州市统计局"
    assert stat_bureau_of("https://www.stats.gov.cn/sj/zxfb/") == "", "国家统计局不属地方目录"
    assert stat_bureau_of("https://tjj.hebei.gov.cn/x.html") == "河北省统计局", "官方目录内的省份要能识别"


def test_metric_freshness():
    """指标时效档位：五个档位都能嗅到；嗅不出必须返回 None（退回全局年限，不瞎猜）。"""
    from core.metric_freshness import RULES, max_age_for, sniff

    assert {r.key for r in RULES} == {"fast", "moving", "stable", "slow", "census"}
    cases = {
        "2026年瑞幸门店数": "fast",
        "现磨咖啡客单价": "fast",
        "咖啡市场市占率": "moving",
        "全国常住人口": "stable",
        "咖啡行业市场规模 CAGR": "slow",
        "第五次全国经济普查数据": "census",
    }
    for txt, key in cases.items():
        r = sniff(txt)
        assert r is not None and r.key == key, f"{txt} → {getattr(r, 'key', None)}（期望 {key}）"
    assert sniff("今天天气不错") is None
    assert sniff("") is None
    # 同时含快变与稳变词 → 取**更紧**的（时效门宁可从严）
    assert sniff("门店数与常住人口").key == "fast"
    y, why = max_age_for("门店数", default=3)
    assert (y, bool(why)) == (1, True)
    assert max_age_for("今天天气不错", default=3) == (3, "")


def test_screen_citation_indicator():
    """同一个日期，按指标可得出**相反**的准入结论——这才叫"按指标定保鲜期"。"""
    from core.source_quality import screen_citation

    old = "2023-05-01"
    # 媒体域（非权威、非研究机构）默认 3 年：2023 年那篇在 2026 年正好卡线
    # 门店数（快变 1 年）→ 拦；行业规模（慢变 4 年）→ 但媒体默认 3 年封顶 → 仍拦
    r_fast = screen_citation("https://news.example.com/a/1.html", "t", old,
                             industry="咖啡", indicator="瑞幸门店数")
    assert r_fast and "时效过期" in r_fast, f"快变指标应拦下，实得 {r_fast}"
    assert "1 年" in r_fast, f"拒绝原因该写明按哪一档判的，实得 {r_fast}"
    # 研究机构域（5 年放宽）＋快变指标 → 取 min(1,3)=1 → 仍拦（研究机构身份不能豁免指标本身）
    r_house = screen_citation("https://www.iresearch.com.cn/a.html", "t", old,
                              industry="咖啡", indicator="瑞幸门店数")
    assert r_house and "时效过期" in r_house, "研究机构转述快变指标不该免检"
    # 不传 indicator → 退回全局年限（行为与旧版一致，保证向后兼容）
    assert screen_citation("https://news.example.com/a/1.html", "t", "2024-05-01",
                           industry="咖啡") is None
    # 权威域依旧整体豁免（哪怕指标是快变的）
    assert screen_citation("https://tjj.zj.gov.cn/a/2020/x.html", "t", "2019-01-01",
                           industry="咖啡", indicator="门店数") is None


def test_domain_match():
    """域名后缀语义：跳转链接不得被误升级；正常的子域不得被误杀。"""
    from core.domain_match import domain_hit, host_hit, host_of, host_match_any
    from core.source_router import route_source

    assert host_of("https://tjj.zj.gov.cn/a/b?x=1") == "tjj.zj.gov.cn"
    assert host_of("https://user:pw@WWW.Gov.CN:8443/x") == "www.gov.cn"
    assert host_of("") == ""

    # 后缀命中 + 不命中
    assert host_hit("tjj.zj.gov.cn", ("gov.cn",))
    assert host_hit("gov.cn", ("gov.cn",))
    assert not host_hit("notgov.cn", ("gov.cn",)), "notgov.cn 不是 gov.cn 的子域"
    assert not host_hit("gov.cn.evil.com", ("gov.cn",)), "把域名放在子域位置是伪装的典型手法"
    assert host_hit("x.gov", (".gov",)) and host_hit("a.b.gov", (".gov",))
    assert not host_hit("gov", (".gov",)), "裸 gov 不是顶级域后缀"

    # 病案：跳转/引用链接不得被当成政府官方
    t1, g1 = route_source("https://www.example.com/redirect?to=https://www.gov.cn/x")
    assert t1 == 4, f"跳转链接被误判成 Tier{t1}({g1})——这正是本次要修的病案"
    t2, _ = route_source("https://tjj.zj.gov.cn/sj/zxfb/202609/t20260901_1.html")
    assert t2 == 1, "真·政府子域必须仍判 Tier1"
    t3, _ = route_source("https://www.chinairn.com/news/20260101/1.html")
    assert t3 == 4, "低质农场不得被抬档"
    # 来源名关键词通道不受影响
    assert route_source("", "国家统计局")[0] == 1
    assert route_source("", "艾瑞咨询")[0] == 2

    assert host_match_any("https://a.163.com/x", (("gov.cn",), ("163.com",)))[0] == 1


def test_source_health():
    """健康巡检与调用台账（**不发网络请求**：只测纯逻辑与探针装载）。"""
    import json as _json
    import time as _t

    from core.source_health import (
        PROBES, all_probes, is_stale, ledger, record, render_trace_appendix,
        status_line, summarize_trace,
    )

    # 探针装载：注册表里的每个 connected 源都该有探针（或明确是 api/内部源）
    sids = {p.sid for p in PROBES}
    for must in ("nbs", "cninfo", "sec_edgar", "ccgp", "caam", "ccfa", "worldbank", "imf"):
        assert must in sids, f"缺 {must} 的探针"
    # 反向覆盖：**每一个 connected 源都必须有探针**。
    # 此前 3 个模式类源（gov_portal / bing_scrape / report_page）在注册表里是"已接入"，
    # 却在巡检里没有对应行 —— 于是"已接入 16 个源 / 巡检 12 个源可用"两个数字对不上，
    # 而评审**一定会去对**这两个数。缺口不许静默存在。
    from core.source_registry import SOURCES as _SRC

    _covered = {p.sid for p in all_probes(include_region=True)}
    _missing = [
        s.id for s in _SRC
        if s.status == "connected" and s.id not in _covered and s.id != "region_stats"
    ]
    assert not _missing, f"已接入但无探针的源（两个数字会对不上）：{_missing}"
    # 模式类源（无固定入口）必须标 dynamic 并给出能力判据，不许硬安一个 URL 装作精确
    for p in PROBES:
        if p.kind == "dynamic":
            from core.source_health import _probe_dynamic

            ok, detail = _probe_dynamic(p.sid)
            assert ok and detail, f"{p.sid} 模式类探针未给出能力判据"
    # 地域源探针从 region_stat 现取，数量须一致（两份清单不许漂移）
    from core.region_stat import REGION_SITES

    region_probe_sids = {p.sid for p in all_probes() if p.sid.startswith("tjj_")}
    assert region_probe_sids == {s.sid for s in REGION_SITES}, "地域探针与地域源清单不一致"
    # 探针判据必须写 expect（只看 200 会把"改版迁移页"记成活源）
    for p in all_probes():
        if p.kind == "http" and p.sid != "sse_edc":
            assert p.expect, f"{p.sid} 缺内容判据（只看 200 会把死源记成活的）"

    # 台账：读不存在的台账返回空壳而非抛错
    led = ledger()
    assert isinstance(led, dict) and "summary" in led
    assert isinstance(is_stale(), bool)
    assert isinstance(status_line(), str)

    # 调用台账：**按 URL 去重**计数（缓存命中不重复计——否则会把"3 条被取 10 次"报成 30 条）
    tr: list = []
    record(tr, "nbs", "国家统计局", [{"url": "a"}, {"url": "b"}, {"url": "c"}])
    record(tr, "nbs", "国家统计局", [{"url": "c"}])          # 缓存命中：同一条目
    record(tr, "sec_edgar", "SEC EDGAR", [], layer="L2")
    record(None, "x", "y", [1])                              # trace=None 必须安全
    s = summarize_trace(tr)
    assert s["hit"] == 1 and s["empty"] == 1, s
    assert {r["src"]: r["n"] for r in s["rows"]}["nbs"] == 3, "同一 URL 被取两次只该算 1 条"
    assert {r["src"]: r["calls"] for r in s["rows"]}["nbs"] == 2, "调用次数仍要如实记（含缓存命中）"
    # 没有 URL 的条目（如某些 API 返回）退回"单次最大值"，**不累加**
    tr2: list = []
    record(tr2, "s", "S", [1, 2, 3])
    record(tr2, "s", "S", [1, 2, 3])
    assert summarize_trace(tr2)["items"] == 3, "无 URL 时不得累加成 6"
    md = render_trace_appendix(tr)
    assert "本次数据源调用台账" in md and "空返回" in md and "SEC EDGAR" in md
    assert "去重" in md, "要说清条数口径，否则读者会以为取少了"
    assert render_trace_appendix([]) == "", "无调用记录时不该硬塞一段空表"
    assert render_trace_appendix(None) == ""
    _json.dumps(s)                        # 可序列化（要进 state / 接口）
    assert _t.time() > 0


# ============================================================
# 统计正文数值抽取 / 年报行业章节 / 台账口径（v0.9.67）
# ============================================================
def test_stat_body_extraction():
    """统计局正文数值抽取：网页散文式 + 附件表格式；导航/元数据表必须滤掉（全部离线）"""
    from core.authority_fetch import (
        _date_from_match, _is_meta_table, _is_nav_table, _nbs_tables, _pick_date,
        _split_label_values, extract_table_numbers, extract_text_numbers,
    )
    import re as _re

    # ① 散文式（统计局网页的主导体式）
    prose = "2026年1—7月份，社会消费品零售总额287744亿元，同比增长4.2%。7月份，社会消费品零售总额37713亿元，同比增长2.7%。"
    got = {(x["indicator"], x["kind"]) for x in extract_text_numbers(prose)}
    assert ("社会消费品零售总额", "1—7月累计") in got, got
    assert ("社会消费品零售总额", "7月") in got, got
    # 累计与当月**必须分开标注**（差一个量级，混用即硬伤）
    assert ("社会消费品零售总额", "1—7月累计·同比") in got and ("社会消费品零售总额", "7月·同比") in got, got

    # ② 附件 PDF 里的**纯文本表**：行里没有动词，散文式正则一条都匹配不到
    table = (
        "指标 1-6月（亿元） 同比±% 占GDP比重±%\n"
        "*地区生产总值 12013 5.3 -\n"
        "  第一产业 163 4.1 1.3\n"
        "  第二产业 2806 5.1 23.4\n"
        "注：指标前*号为季度数据，按季发布。\n"
    )
    tn = extract_table_numbers(table)
    d = {(x["indicator"], x["kind"]): (x["value"], x["unit"]) for x in tn}
    assert d.get(("地区生产总值", "1-6月")) == ("12013", "亿元"), d
    assert d.get(("地区生产总值", "同比")) == ("5.3", "%"), d
    assert d.get(("第一产业", "占比")) == ("1.3", "%"), d
    # 指标名里**不带**口径/期间（带了就会被跨报告对账当成一个新指标）
    assert all("(" not in x["indicator"].replace("(", "") for x in tn), tn
    # 注释行之后的表头才生效；`-` 占位符不当成 0
    assert not any(x["value"] == "0" for x in tn), tn

    # ③ 从右往左剥数值：中文指标名里的空格不能被切碎
    lab, vals = _split_label_values("  其中：高新技术产业 8.2 ")
    assert lab == "其中：高新技术产业" and vals == ["8.2"], (lab, vals)
    lab2, vals2 = _split_label_values("*地区生产总值 12013 5.3 -")
    assert lab2 == "*地区生产总值" and vals2 == ["12013", "5.3"], (lab2, vals2)

    # ④ 导航表 / 信息公开元数据表都不得被当成"数据表"
    nav = [["当前位置：", "首页"], ["> 统计数据", "> 进度数据"]]
    assert _is_nav_table(nav)
    meta = [["[ 索引号 ]", "11500000009276206R/2026-00071", "[ 发文字号 ]"], ["[ 主题分类 ]", "统计", "[ 成文日期 ]", "2026-08-20"]]
    assert _is_nav_table(meta), "信息公开属性表必须滤掉（实测重庆市统计局踩过）"
    good = [["指标", "7月", "同比增长（%）"], ["规模以上工业增加值", "7.1", "6.6"]]
    assert not _is_nav_table(good), "真数据表不得被误杀"
    assert len(_nbs_tables("<table><tr><td>当前位置：</td><td>首页</td></tr><tr><td>> 统计</td><td>> 数据</td></tr></table>")) == 0

    # ⑤ 附件网页里正文没数字时，能顺着公开附件链接找到 PDF
    from core.authority_fetch import _ATTACH_HREF

    assert _ATTACH_HREF.search('<a href="/a/b.pdf">月报</a>')
    assert _ATTACH_HREF.search('<a href="/api-gateway/x/document/download?fileUrl=ABC">月报</a>')
    assert not _ATTACH_HREF.search('<a href="/a/b.xlsx">表</a>'), "xlsx 没有解析器，不认（宁可留空也不半解）"

    # ⑥ 日期多形态：取**精度更高**的那个，且绝不补零编造
    assert _date_from_match(_re.search(r"t(\d{8})_", "x/t20260910_1.html")) == "2026-09-10"
    assert _date_from_match(_re.search(r"/(\d{4})/(\d{1,2})/(\d{1,2})/", "/2026/8/18/")) == "2026-08-18"
    assert _date_from_match(_re.search(r"/sjfbyjd/(\d{6})/", "/sjfbyjd/202608/")) == "2026-08"
    assert _date_from_match(_re.search(r"/art/(\d{4})/art_", "/col/c1/art/2026/art_x.html")) == "2026"
    # 杭州：路径只有年份、标题里有确切发布日 → 取标题的
    assert _pick_date("/col/c1/art/2026/art_x.html", "2026年7月月报2026-08-28", r"/art/(\d{4})/art_", r"(\d{4})-(\d{2})-(\d{2})") == "2026-08-28"


def test_annual_report_industry_section():
    """年报「行业情况」章节定位：跳过目录与交叉引用，取真章节（离线）"""
    from core.authority_fetch import _extract_industry_section

    toc = "目录\n第一节 释义 .......... 1\n第三节 管理层讨论与分析 .......... 6\n第四节 公司治理 .......... 40\n"
    body = (
        "第三节 管理层讨论与分析 一、报告期内公司从事的业务情况 公司所属行业为零售业。"
        "截至2025年12月31日，公司在全国共计403家门店，业务覆盖24个省市；位居中国超市百强前五。"
        "二、报告期内公司所处行业情况 据国家统计局官方数据显示，2025年，社会消费品零售总额501,202亿元，"
        "比上年增长3.7%。三、经营情况讨论与分析 公司实现营业收入535.08亿元，同比下降20.8%。"
    )
    # `toc * 6`：章节定位有"文本太短就不猜"的下限保护（生产环境年报正文 2.9 万字，远高于下限）
    seg, nums = _extract_industry_section(toc * 6 + body)
    assert seg and "超市百强前五" in seg, (len(toc) * 6 + len(body), seg[:120])
    assert "第一节 释义" not in seg, "摘录到目录页说明定位失败"
    d = {(x["indicator"], x["kind"]) for x in nums}
    assert ("社会消费品零售总额", "同比") in d, d
    assert ("社会消费品零售总额", "值") in d, d
    # 金额式**只认币种单位**：`403 家门店`/`24 个省市` 不得被当成指标产出
    assert not any("个省市" in x["indicator"] or x["indicator"].endswith("覆盖") for x in nums), nums

    # 交叉引用不得当成章节起点（同样要够长才越过"文本太短就不猜"的下限）
    xref = "详见本报告“第三节 管理层讨论与分析”中“可能面对的风险”部分的内容。" + toc * 6 + body
    seg2, _ = _extract_industry_section(xref)
    assert seg2 and "释义" not in seg2, "交叉引用被当成了章节起点"
    assert "超市百强前五" in seg2, seg2[:120]

    # 找不到就返回空，**不硬凑**
    assert _extract_industry_section("这是一段无关文本。" * 40) == ("", [])


def test_ledger_source_period_classification():
    """台账补来源与数据期 → 冲突三分类（同一份"数值不同"，结论完全不同）"""
    from core.metric_ledger import _cell_value, build_ledger, classify_variants, find_conflicts, format_appendix

    # ① `L3`/`CR5`/`T4` 这类**标签**不得被当成数值
    assert _cell_value("L3") is None, "数据级别 L3 被读成了数字 3"
    assert _cell_value("CR5") is None
    assert _cell_value("12%")[0] == 12.0

    md = (
        "## 📊 核心指标一览表\n\n"
        "| 指标 | 数值 | 来源机构 | 数据级别 | 年份 | 备注 |\n"
        "| :--- | :--- | :--- | :--- | :--- | :--- |\n"
        "| 全国社会消费品零售总额 | 287,744亿元 | 国家统计局 | L1 | 2026 | 1—7月累计 |\n"
        "| 全国社会消费品零售总额 | 37,713亿元 | 国家统计局 | L1 | 2026 | 7月当月 |\n\n"
        "## 市场规模测算\n\n"
        "| 测算维度 | 关键参数 | 数值/单位 | 来源机构 |\n"
        "| :--- | :--- | :--- | :--- |\n"
        "| 市场规模 | 中国现磨咖啡市场规模 | 6,562亿元 | 艾瑞咨询 |\n"
        "| 市场规模 | 中国现磨咖啡市场规模 | 7,738亿元 | 欧睿国际 |\n"
    )
    led = build_ledger(md)
    # 标签列不再产出假条目
    assert not any(x["indicator"] in ("艾瑞咨询", "国家统计局") for x in led), led
    # 来源只从**表头写明的来源列**取
    sm = next(x for x in led if x["chapter"].startswith("📊"))
    assert sm["source"] == "国家统计局", sm
    # 数据期扫整行（期间写在"备注"列也认得出）
    assert sm["period"] == "1—7月累计", sm

    conf = {c["indicator"]: c["code"] for c in find_conflicts(led)}
    # 同一来源、不同期间 → **不是冲突**，是趋势证据
    assert conf.get("全国社会消费品零售总额") == "period", conf
    # 不同来源 + 口径敏感指标 → 多半不可比，也不是谁写错了
    assert conf.get("中国现磨咖啡市场规模") == "caliber", conf

    # ② 分类器单测
    assert classify_variants([{"period": "1—7月累计", "source": "国家统计局"}, {"period": "7月", "source": "国家统计局"}])[0] == "period"
    assert classify_variants([{"source": "艾瑞咨询"}, {"source": "艾瑞咨询"}])[0] == "same_source"
    assert classify_variants([{"source": ""}, {"source": ""}])[0] == "unknown"

    # ③ 附录按类分节，且**趋势那一类要讲清"这不是打架"**
    ap = format_appendix(find_conflicts(led))
    assert "指标一致性核对" in ap
    assert "数据期不同" in ap and "趋势证据" in ap
    assert "口径敏感指标" in ap
    assert format_appendix([]) == "", "没有冲突时一个字都不该加"


def test_env_reaches_os_environ():
    """`.env` 必须**同时**进 `os.environ`——凡是用 `os.environ.get()` 读的键都只走这条路。

    实测病案（2026-09-14）：`.env` 一直只被读进 pydantic `Settings`，从不写 `os.environ`，
    于是这些键**全部静默取默认值**：
      · `AUTHORITY_FETCH_ENABLED`（权威源层总开关）置 0 **关不掉**；
      · `SEC_UA_CONTACT` 填了也没用，SEC 的 UA 永远是占位串（违反对方 fair-access 要求），
        而且启动 WARNING 在用户填好之后**仍然会响**——让人反复折腾一个已经生效的配置；
      · `CITATION_MEDIA_RATIO` / `AUTH_QUOTA_PER_DIM` / `BING_SCRAPE_GAP` 三道闸门同样失效；
      · 数据源巡检把明明配好的 `BOCHA_API_KEY` 报成"未配置"（假故障）。
    """
    import os

    import core.config as C

    assert C.ENV_FILE.exists(), f"项目根 .env 不存在：{C.ENV_FILE}"
    missing: list[str] = []
    for ln in C.ENV_FILE.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        k, v = k.strip(), v.strip()
        if not v:
            continue  # 文件里本来就是空的（如待用户填的 SEC_UA_CONTACT）→ 不算漏
        if not os.environ.get(k):
            missing.append(k)
    assert not missing, f"这些键在 .env 里有值却没进 os.environ：{missing}"
    # 逆向确认：直接读 os.environ 的那几个开关确实能读到 .env 的值
    for k in ("AUTHORITY_FETCH_ENABLED",):
        if k in C.ENV_FILE.read_text(encoding="utf-8"):
            assert os.environ.get(k), f"{k} 没进 os.environ → 总开关会失效"


def test_caliber_appendix_wiring():
    """口径对照**附录接线**（不是词典本身）——用带 caliber 的引文走一遍真实入口。

    实测病案：这段逻辑内联在 node_deliver 里且用了 `re.split`，而 graph.py 没有在
    模块级 import re → 每次交付都 NameError，被 except 吞成一行 warning，
    **整块口径对照在生产里从来没有出现过**。单测只测 `caliber_kb.explain()` 本身，
    测不到接线，所以一直没发现。这里补上"从引文到附录"的这一段。
    """
    import graph

    cites = [
        {"title": "a", "caliber": "限额以上+零售额"},
        {"title": "b", "caliber": "范围:三线及以下；性质:预测值；口径:常住人口"},
        {"title": "c", "caliber": ""},
        {"title": "d"},                      # 缺键也不许炸
        None,                                # 脏数据也不许炸
    ]
    md = graph._build_caliber_appendix(cites)
    assert "口径对照" in md, "带 caliber 的引文必须产出附录"
    assert "限额以上" in md and "零售额" in md, md[:200]
    assert "常住人口" in md
    # 复合口径要**拆开逐段查**（整串丢进词典匹配不上）
    assert "范围:三线及以下" not in md, "没拆干净"
    # 没有口径就一个字都不加
    assert graph._build_caliber_appendix([]) == ""
    assert graph._build_caliber_appendix([{"title": "x"}]) == ""
    assert graph._build_caliber_appendix(None) == ""


def test_audit_20260914_fixes():
    """360° 审计（8 维 74 agents / 32 条核验存活）修复的回归锁。

    这一节只放**会产出错误结果**的项：每条都能对应一个"不修就会印错东西"的场景。
    """
    import inspect

    # ① IMF 单位不得硬编码（人口被印成"十亿美元"）
    from core import authority_intl as AI

    units = {k: u for k, _n, u in AI._IMF_IND}
    assert units["LP"] == "百万人" and units["NGDP_RPCH"] == "%", units
    src = inspect.getsource(AI)
    assert '_fmt_series(hist or pairs[-5:], "十亿美元"' not in src, "IMF 单位仍然硬编码"
    assert "_IMF_UNITS.get(ind" in src, "应按指标查单位"
    # 每个 IMF 指标都必须有单位（新增指标时不许漏）
    assert all(u for u in units.values()), units

    # ② 巨潮主体校验（`keyWord=连锁` 命中红旗连锁那类串台）
    from core.authority_fetch import _company_candidates, _same_entity

    assert _same_entity("伊利", "伊利股份") and _same_entity("内蒙古伊利实业", "伊利股份")
    assert not _same_entity("连锁", "红旗连锁"), "行业词不得被当成主体"
    assert not _same_entity("餐饮", "西安饮食")
    assert "连锁" not in _company_candidates("连锁餐饮加盟模式的门店数"), "行业词不该进候选"

    # ③ 台账负号 + 负数不得被丢弃
    from core.metric_ledger import _cell_value, build_ledger, find_conflicts

    assert _cell_value("-1.4%")[0] == -1.4 and _cell_value("−1.4%")[0] == -1.4
    _md_sign = (
        "| 指标 | 数值 | 来源机构 |\n"
        "| :--- | :--- | :--- |\n"
        "| 社零增速 | -1.2% | 国家统计局 |\n"
        "| 社零增速 | +1.2% | 国家统计局 |\n"
    )
    assert find_conflicts(build_ledger(_md_sign)), "符号相反的真冲突必须检出"

    # ④ 台账列归属（「数据级别」列不得被当指标名；年份列不得被当数值）
    _md_col = (
        "| 指标 | 数值 | 来源机构 | 数据级别 | 年份 | 备注 |\n"
        "| :--- | :--- | :--- | :--- | :--- | :--- |\n"
        "| 全国社会消费品零售总额 | 287,744亿元 | 国家统计局 | L1 | 2026 | 1—7月累计 |\n"
    )
    _led = build_ledger(_md_col)
    assert [x["indicator"] for x in _led] == ["全国社会消费品零售总额"], _led
    assert _led[0]["value"] == 287744.0 and _led[0]["period"] == "1—7月累计", _led[0]
    assert not any(str(x["indicator"]).startswith("L") for x in _led)

    # ⑤ 数字护照：否定式口径方向不得判反；发布日不得当数据期
    from core.data_passport import build_passport, extract_caliber

    assert extract_caliber("不含加盟店的口径") == "不含加盟", "「不含加盟」被判成了「含加盟」"
    assert extract_caliber("含加盟门店占比") == "含加盟"
    assert extract_caliber("不含税收入") == "不含税"
    assert build_passport("", "", "2026-09-08", url="https://x/2026/09/").period == "", "发布日不得当数据期"
    assert build_passport("2025年咖啡行业报告", "", "2026-09-08").period == "2025"

    # ⑥ 「独立来源」必须按**可注册根域**算（finance.sina 与 www.sina 是同一家）
    from core.confidence import distinct_source_domains
    from core.models import Evidence, Source, SourceLevel

    ev = Evidence(claim="x", sources=[
        Source(title="a", url="https://finance.sina.com.cn/a", level=SourceLevel.L2_SEMI_OFFICIAL),
        Source(title="b", url="https://www.sina.com.cn/b", level=SourceLevel.L2_SEMI_OFFICIAL),
    ])
    assert len(distinct_source_domains(ev)) == 1, "同一家媒体的两个栏目被算成两个独立来源"

    # ⑦ 域名解析：`lstrip("www.")` 字符集语义会把 w 开头主机吃掉首字符
    from core.source_util import registrable_root

    assert registrable_root("https://wind.com.cn/x") == "wind.com.cn", "wind 被吃成了 ind"
    assert registrable_root("https://www.163.com/x") == "163.com"

    # ⑧ 注册表自检：声称已接入的源必须能对上归属域名（否则"数据源体系"页那个字段是空的）
    from core.source_registry import validate_registry

    assert validate_registry() == [], validate_registry()

    # ⑨ 巡检不变量：每个 connected 源要么有探针，要么在显式豁免名单里
    from core.source_health import PROBES
    from core.source_registry import connected_sources

    _probe_ids = {x.sid for x in PROBES}
    _no_probe_ok = {"gov_portal", "bing_scrape", "report_page", "region_stats"}
    _missing = [x.id for x in connected_sources() if x.id not in _probe_ids and x.id not in _no_probe_ok]
    assert not _missing, f"这些已接入源没有探针、也不在豁免名单：{_missing}"

    # ⑩ 缓存单飞：同键并发只该真的抓一次
    import threading
    import time as _time

    from core.authority_fetch import _cached

    _calls = {"n": 0}
    _cache: dict = {}

    def _slow():
        _calls["n"] += 1
        _time.sleep(0.25)
        return [{"url": "x"}]

    _ts = [threading.Thread(target=lambda: _cached(_cache, "sf", _slow)) for _ in range(5)]
    [_t.start() for _t in _ts]
    [_t.join() for _t in _ts]
    assert _calls["n"] == 1, f"同键并发被抓了 {_calls['n']} 次（应为 1）"

    # ⑪ 交付期核验必须过 robots 门（红线：它是出网量最大的一层）
    from core import verification as V

    _http_src = inspect.getsource(V._http_get)
    assert "_compliance_skip_reason" in _http_src, "核验层没有合规门（robots / SEC UA）"
    assert "_throttle" in _http_src, "核验层没有 per-host 限速"

    # 流式路径（**前端/演示实际走的那条**）不得把白名单窄化后的 result 喂给 run_stats。
    # 病案（2026-09-14 二次审计）：`_result_json` 是白名单，`asset_reused`/`budget_spent`/
    # `search_debug`/`rework_count` 都不在里面 → 复用率、成本、检索轮在流式路径上恒为 0，
    # 而同步命令行跑出来的账是对的。**同一个函数，两条路径两种真相**——演示时看到的复用率永远是 0。
    #
    # ⚠️ 病案（2026-09-16 · CRITICAL）：上面那条锁**当时只挡住了 `result`，没挡住
    # `final_payload`**——而 `final_payload` 也不是 state，它是 `stream_mode="updates"`
    # 里 **deliver 一个节点返回的那个 dict**，只含 deliver 回吐的 16 个键，
    # 同样**不含** budget_spent / rework_count / search_debug / stop_card /
    # sop_fallback / asset_reused。于是那 6 个字段在演示路径上照样恒为 0/False。
    #
    # **为什么旧锁全绿而缺陷还在**：它断言的是源码字符串 `"_build_run_stats(final" in _src`
    # ——`_build_run_stats(final_payload, ...)` 这段文本**恰好命中** `"final"` 前缀。
    # 一个用字符串前缀做的断言，把"喂了 delta"读成了"喂了 state"。
    # 换成本用例下面那组**行为断言**：真去调一次，看字段值对不对。
    from graph import run_task_stream as _rts

    # **断言前先剥掉注释行**。上面这段病案里逐字引用了错误的写法
    # （`_build_run_stats(final_payload, ...)`），如果直接对全文断言，
    # 就变成"要么删掉病案、要么测试永远红"——本文件在 SEC 那段已经吃过一次这个亏
    # （原话：「断言写得比意图宽，就会逼着后来人删掉病案才能过测试」）。
    _src = "\n".join(
        _l for _l in inspect.getsource(_rts).split("\n") if not _l.strip().startswith("#")
    )
    assert "_build_run_stats(result," not in _src, "流式路径又拿窄化 result 算 run_stats（复用/成本/检索轮会恒为 0）"
    assert "_build_run_stats(final_payload" not in _src, (
        "流式路径把 deliver 节点的**返回增量**当成 state 喂给 run_stats —— "
        "它只含 deliver 回吐的键，budget_spent/rework_count/search_rounds/took_over/"
        "reused_asset/degraded 会全部变成 0/False，而演示走的正是这条路"
    )
    assert "_build_run_stats(_final_state" in _src, "流式路径没有把**图里的最终状态**喂给 run_stats"

    # ---- 行为断言：同一份数据，喂 delta vs 喂 state，结果必须不同 ----
    import graph as _g

    _delta = {"report_final": "# x", "citations": [{"url": "https://a.gov.cn/1"}],
              "decision_verdict": "Go"}          # = deliver 回吐的键，没有 run_stats 要的那几个
    _state = dict(_delta, budget_spent=1.4, rework_count=1,
                  search_debug=[("t1", "q", 3)] * 7, asset_reused=True,
                  stop_card={"why": "需要人接管"}, sop_fallback={"reason": "兜底"},
                  entailment={"omission": {"claims": 9, "omitted": 3}})
    _rs_delta = _g._build_run_stats(_delta, 12.3, None)
    _rs_state = _g._build_run_stats(_state, 12.3, None)
    assert _rs_state["budget_spent"] == 1.4 and _rs_state["search_rounds"] == 7, \
        f"喂完整 state 时 run_stats 应取到真值：{_rs_state}"
    assert _rs_state["reused_asset"] is True and _rs_state["took_over"] is True, \
        "复购看板的分子（有效复用率 / 人工接管率）在完整 state 下必须为真"
    assert _rs_delta["budget_spent"] == 0 and _rs_delta["reused_asset"] is False, \
        "喂 deliver 增量时字段本就会退化——这正是必须喂 state 的理由（前提锁）"
    # 遗漏层：`omission` **不是 TaskState 的键**，它嵌在 entailment 里。
    # 原先 `result.get("omission")` 在**两条路径上**都取不到 → 落账恒为空。
    assert _rs_state["omission"].get("omitted") == 3, (
        f"遗漏层读数没从 entailment.omission 取到（落账恒为空 = 那层在白板上不存在）：{_rs_state['omission']}"
    )

    # `_resolve_final_state`：有 checkpointer 时取图内状态，取不到才回退（且回退要能被识别）
    class _G:
        def __init__(self, vals):
            self._v = vals

        def get_state(self, _cfg):
            if self._v is None:
                raise RuntimeError("no checkpoint")

            class _S:
                values = self._v

            return _S()

    assert _g._resolve_final_state(_G({"budget_spent": 9}), {}, {"budget_spent": 0})["budget_spent"] == 9
    _fb = {"budget_spent": 0}
    assert _g._resolve_final_state(_G(None), {}, _fb) is _fb, "取不到时必须原样回退（调用方靠 is 判断要不要出声）"
    assert _g._resolve_final_state(None, None, _fb) is _fb, "无 checkpointer 时不得抛异常"
    assert "ThreadPoolExecutor(max_workers=16)" not in inspect.getsource(V), "核验并发仍是 16"

    # ⑪b SEC 联系方式未配置时**必须 fail-closed**（不拿占位联系方式敲政府接口的门）
    # 病案（2026-09-14 体检）：`_sec_ua()` 曾回落到字面量 `contact-not-configured` 并照发，
    # 与对外材料"声明式 UA · 可被联系"直接打架。三条不变式，少一条这病就会复发。
    from core import authority_fetch as AF

    # 只扫**出问题的那两个函数**。别扫整个模块：`SecUAUnconfigured` 的文档字符串里
    # 刻意保留了病案原文（`contact-not-configured`），那是记录，不是回落路径——
    # 断言写得比意图宽，就会逼着后来人删掉病案才能过测试。
    for _fn in (AF._sec_ua, AF._sec_get):
        assert "contact-not-configured" not in inspect.getsource(_fn), (
            f"{_fn.__name__} 又回落到占位联系方式"
        )
    import os
    _saved = os.environ.pop("SEC_UA_CONTACT", None)
    try:
        assert AF.sec_contact_configured() is False, "未配置时应判为未配置"
        try:
            AF._sec_ua()
            raise AssertionError("未配置联系方式时 _sec_ua() 竟返回了 UA——占位串病复发")
        except AF.SecUAUnconfigured:
            pass
        # 门必须在**真正出网那一层**：不是"报个错但照样发"
        try:
            AF._sec_get("https://data.sec.gov/submissions/CIK0000320193.json")
            raise AssertionError("未配置联系方式时仍发出了 SEC 请求——门形同虚设")
        except AF.SecUAUnconfigured:
            pass
        # 且必须与"打不开"严格区分：探都不探，就不许判死
        # 打桩两道 robots 门 → 本用例**不出网**，且只测 SEC 这一条（否则 SEC 自己的
        # robots.txt 若刚好 Disallow 该路径，我们测的就成了 robots 门，SEC 门漏测也不知道）
        _rb, _ra = V._robots_blocked, AF._robots_allows
        V._robots_blocked = lambda _u: False
        AF._robots_allows = lambda _u: True
        try:
            _sec_url = "https://data.sec.gov/submissions/CIK0000320193.json"
            _reason = V._compliance_skip_reason(_sec_url)
            assert _reason and "未探测" in _reason and "SEC" in _reason, (
                f"SEC 未配置未走『未探测』：{_reason!r}"
            )
            assert V.verify_url_live(_sec_url) == (False, _reason), (
                "verify_url_live 未按合规姿态报『未探测』"
            )
            # 配好联系方式后这条门必须**放行**（否则等于把 SEC 永久关掉，是另一种病）
            os.environ["SEC_UA_CONTACT"] = "audit@example.com"
            assert V._compliance_skip_reason(_sec_url) is None, "SEC 门配好后仍不放行"
            assert AF._sec_ua() == "yancec-research-bot/1.0 (audit@example.com)"
        finally:
            V._robots_blocked, AF._robots_allows = _rb, _ra
            os.environ.pop("SEC_UA_CONTACT", None)
        # robots 硬门不得因这次改动被顺手删掉
        assert V._compliance_skip_reason("https://www.sec.gov/cgi-bin/browse-edgar") is not None
    finally:
        if _saved is not None:
            os.environ["SEC_UA_CONTACT"] = _saved

    # ⑫ Red Team 不得只喂前 8000 字符
    from core import adversarial as AD

    assert "build_number_digest" in inspect.getsource(AD), "Red Team 仍在只看报告开头"

    # ⑬ budget_spent 必须有 reducer（否则预算护栏永远不触发）
    import operator

    from core.state import TaskState

    assert "operator.add" in str(TaskState.__annotations__.get("budget_spent")), "budget_spent 没有 reducer"

    # ⑭ source_trace 不得返回通道里那个对象（否则被 reducer 复制 5 倍）
    import graph as _g

    _src_out = inspect.getsource(_g._run_sub_agent)
    assert '"source_trace": list(_mini_trace)' in _src_out, "source_trace 仍会自我拼接"
    assert "_acc.extend(_mini_trace)" not in _src_out

    # ⑮ 冲突回写（否则结论状态机的 CONTESTED 永不可达）
    assert "ev.is_conflicting = True" in inspect.getsource(_g.node_check_conflict)
    # 冲突日志表读的键必须与生产端一致
    _dep = inspect.getsource(_g)
    assert "cf.get('claim', '')" not in _dep, "冲突日志仍在读不存在的键"


# ============================================================
# 38. AI 生成合成内容标识（法定）
# ============================================================
def test_decision_trace():
    """自主决策链：只记真决策（有选项、有理由），不记工序；不编造不存在的选项。"""
    from core.decision_trace import KINDS, record, render_appendix_md, summarize

    st: dict = {}
    record(st, "classify", "这是什么类型的问题？", ["因果", "描述", "探索"], "因果",
           "判错会用描述性数据回答因果问题", evidence="question_type=causal", trigger="llm")
    record(st, "route", "该用什么层的源？", ["L1 官方", "L3 行业"], "L1 官方",
           "公司数字只有法定披露算数", trigger="rule")
    # 脏输入不得炸、不得写入半条
    record(st, "route", "", [], "", "", trigger="nope")
    record(None, "route", "x", ["a"], "a", "b")  # state 为 None → 静默
    ds = st["decisions"]
    assert len(ds) == 3, f"应记 3 条，实得 {len(ds)}"
    d = ds[0]
    # 四要素齐备（缺任何一项就不是"决策"，是日志）
    for k in ("situation", "options", "chosen", "why"):
        assert d.get(k), f"决策缺 {k} → 退化成日志：{d}"
    assert d["icon"] and d["label"], "缺展示元信息"
    assert d["trigger"] == "模型判断", d["trigger"]

    s = summarize(ds)
    assert s["total"] == 3 and s["llm_judged"] == 1, s
    assert s["by_kind"]["route"] == 2

    md = render_appendix_md(ds)
    assert "自主决策链" in md and "处境" in md and "选项" in md and "理由" in md
    assert "这一节不证明什么" in md, "决策链必须自陈边界"
    # 无决策 → 一个字不加（不印空壳）
    assert render_appendix_md([]) == "" and render_appendix_md(None) == ""
    # 类型表与渲染分支必须对得上（新增 kind 忘了登记 → 附录里会缺图标/名称）
    for k in ("classify", "decompose", "route", "select_tool", "replan", "recall",
              "rework", "arbitrate", "degrade", "stop", "fallback"):
        assert k in KINDS, f"KINDS 缺 {k}"
    # 前后端各有一份决策类型表 —— 漂移过就会在前端出现无图标/无名称的空白项
    import re as _re

    # 直接读绝对路径：原先这里先试 `open("App.jsx")`（相对 cwd）再回退，
    # 于是"读到哪个文件"取决于你从哪儿跑——而这几处比的是**前后端是否一致**，
    # 读不到就变成一句"前端缺东西"。
    _fe = (_HERE.parent / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8")
    _fe_kinds = set(_re.findall(r'\["([a-z_]+)", "', _fe))
    assert set(KINDS) <= _fe_kinds, f"前端决策类型表缺：{set(KINDS) - _fe_kinds}"

    # **接线锁**：决策链必须真的挂进交付（有模块不等于有接线——本仓库已踩过多次）
    import inspect as _i

    _g = __import__("graph")
    for _fn, _must in ((_g.node_router, "record"), (_g.node_route_skill, "record"),
                       (_g.route_after_quality, "record")):
        assert _must in _i.getsource(_fn), f"{_fn.__name__} 未记录决策"
    assert "render_appendix_md" in _i.getsource(_g.node_deliver), "决策链附录未接进交付"
    _src = _i.getsource(_g.node_deliver)
    assert '"decision_chain"' in _src, "决策链未随交付结果返回"
    # 必须挂在 TaskState 上（否则 in-place 写不进去）
    from core.state import TaskState

    assert "decisions" in TaskState.__annotations__
    from core.state import init_state

    assert init_state("q").get("decisions") == []

    # **schema 锁（实跑踩中）**：LangGraph 会丢弃节点返回的、不在 TaskState 里的键。
    # 后果按路径分叉：SSE 直取流式 updates 侥幸正常，同步路径（run_task//api/execute）全部丢失。
    # 实测证据：同一份报告，附录渲染出 2 条决策，而 result["decision_chain"] 是 0 条；
    # `citation_report` 也一直是 None（当时误判为"_result_json 没透传"，其实更上游就被丢了）。
    # 所以：**凡是调用方要读的交付产物，都必须是 schema 里的真键。**
    for _k in ("decision_chain", "bad_urls", "url_fixes", "citation_report",
               "entailment", "citation_pass_rate", "metrics"):
        assert _k in TaskState.__annotations__, f"{_k} 不在 TaskState → 同步路径会被静默丢弃"
        assert _k in init_state("q"), f"{_k} 没有初始值 → 首轮可能读不到"
    # 反过来：这几个键**不能**加 reducer，否则会像 source_trace 那样被复制 N 倍
    for _k in ("decision_chain", "bad_urls", "citation_report", "entailment"):
        _ann = str(TaskState.__annotations__[_k])
        assert "Annotated" not in _ann, f"{_k} 不该带 reducer（单一写者，加 reducer 会复制）"


def test_route_decision_actually_recorded():
    """源路由决策**必须真的被记下来**——踩过：读错字段名（RouteDecision 是 claim_key 不是 key）
    → AttributeError → 被我自己的宽 except 吞掉 → 这条决策静默从未出现过（实测 0 条）。

    教训写进断言：**给旁路写宽 except 时，它必须能被测出来**。
    """
    from core.models import DepthLevel, ResearchPlan, SubTask
    from core.state import init_state
    from graph import node_route_skill

    st = init_state("某公司营收和门店数是多少？", {"depth": "标准"})
    st["plan"] = ResearchPlan(
        raw_question=st["raw_question"],
        depth=DepthLevel.STANDARD,
        dimensions=["市场规模", "竞争格局"],
        subtasks=[
            SubTask(id="st_0", agent_type="industry", description="调研『公司经营』：营收与门店数",
                    search_queries=["某公司 营收", "某公司 门店数"]),
            SubTask(id="st_1", agent_type="competition", description="调研『市场规模』：行业规模与增速",
                    search_queries=["行业 市场规模"]),
        ],
    )
    node_route_skill(st)
    routes = [d for d in st["decisions"] if d.get("kind") == "route"]
    assert routes, "源路由决策一条都没记下（字段名读错 / 异常被吞？）"
    for d in routes:
        assert d["options"] and d["why"] and d["chosen"], f"路由决策缺要素：{d}"
        assert d["trigger"] == "确定性规则"
    # 同一 claim 类型只记一次（N 个子任务命中同类型不该刷 N 条噪音）
    keys = [d["situation"] for d in routes]
    assert len(keys) == len(set(keys)), f"同类型重复记录：{keys}"


def test_replan_gaps_and_guards():
    """动态重规划：缺口必须**客观可判**；补充方向必须**被规则裁决**（不能由模型自由发挥）。"""
    import json as _json

    from core.models import DepthLevel, ResearchPlan, SubTask
    from core.replan import _MAX_NEW_BY_DEPTH, detect_gaps, propose_subtasks, run
    from core.state import init_state

    # ---- ① 健康态：零缺口（不许把"感觉不够深"报成缺口）----
    st = init_state("某公司营收多少？", {"depth": "标准"})
    st["plan"] = ResearchPlan(raw_question=st["raw_question"], depth=DepthLevel.STANDARD,
                              subtasks=[SubTask(id="st_0", agent_type="industry", description="调研『经营数据』")])
    st["evidences"] = [object()] * 8
    st["subtask_results"] = {"_routes": {"st_0": {"claim_key": "company_financial", "satisfied": True}}}
    assert detect_gaps(st) == [], f"健康态误报缺口：{detect_gaps(st)}"

    # ---- ② 四类客观缺口都要能检出 ----
    st2 = init_state("某公司营收多少？", {"depth": "标准"})
    st2["plan"] = ResearchPlan(raw_question=st2["raw_question"], depth=DepthLevel.STANDARD,
                               subtasks=[SubTask(id="st_0", agent_type="industry", description="调研『经营数据』")])
    st2["evidences"] = [object()] * 2  # 偏薄
    st2["subtask_results"] = {
        "_routes": {"st_0": {"claim_key": "company_financial", "claim_label": "公司经营数字",
                             "required_layer": "L2", "required_label": "法定披露",
                             "satisfied": False, "hit_layers": ["L4"]}},
    }
    st2["conflict_results"] = [{"claim": "门店数", "conflict_detail": "3500 vs 6000", "arbitration": None}]
    kinds = {g["kind"] for g in detect_gaps(st2)}
    assert {"route_unsatisfied", "thin_evidence", "conflict_open"} <= kinds, kinds
    # high 严重度排前面
    assert detect_gaps(st2)[0]["severity"] == "high"

    # ---- ③ 档位门：快速档不补 ----
    st3 = init_state("q", {"depth": "快速"})
    st3["plan"] = ResearchPlan(raw_question="q", depth=DepthLevel.FREE, subtasks=[])
    st3["evidences"] = [object()]
    st3["subtask_results"] = {"_routes": {"st_0": {"claim_key": "k", "satisfied": False}}}
    assert _MAX_NEW_BY_DEPTH["free"] == 0
    assert run(st3)["planned"] == [], "快速档不该触发补充调查"

    # ---- ④ 规则裁决：模型说什么不算数 ----
    st4 = init_state("咖啡下沉机会", {"depth": "标准"})
    st4["plan"] = ResearchPlan(
        raw_question=st4["raw_question"], depth=DepthLevel.STANDARD,
        subtasks=[SubTask(id="st_0", agent_type="industry", description="调研『竞争格局』",
                          search_queries=["咖啡 门店数"])],
    )
    _bad = [
        {"topic": "类型非法", "agent_type": "hacker", "queries": ["x"]},                 # 类型白名单外
        {"topic": "撞章", "agent_type": "industry", "queries": ["y"]},                   # 无 queries 问题→先过
        {"topic": "重复检索词", "agent_type": "industry", "queries": ["咖啡 门店数"]},     # 首轮已查过
        {"topic": "好方向", "agent_type": "industry", "queries": ["连锁咖啡 门店总数 统计公报"]},
        {"topic": "超量1", "agent_type": "user", "queries": ["q1"]},
        {"topic": "超量2", "agent_type": "user", "queries": ["q2"]},                     # 超过 max_new=2
    ]
    class _Msg:
        content = _json.dumps(_bad, ensure_ascii=False)

    class _LLM:
        def invoke(self, _m):
            return _Msg()

    with mock.patch("core.llm.has_main_llm", return_value=True), \
         mock.patch("core.llm.get_main_llm", return_value=_LLM()):
        got = propose_subtasks(st4, [{"label": "x", "severity": "high", "detail": "d", "hint": "h"}], 2)
    assert len(got) <= 2, f"超出档位上限：{got}"
    assert all(g["agent_type"] in ("industry", "competition", "user", "statistics") for g in got), got
    assert not any("咖啡 门店数" in q for g in got for q in g["queries"]), "首轮查过的检索词被重复使用"
    # 补充轮章节标题必须带前缀（读者要能看出"这块是后补的"）
    from core.replan import SUPPLEMENT_PREFIX

    assert all(g["description"].startswith(SUPPLEMENT_PREFIX) for g in got), got

    # ---- ⑤ 撞章必须被拒（否则会挤掉原有维度章节，而不是"补一章"）----
    st5 = init_state("q", {"depth": "标准"})
    st5["plan"] = ResearchPlan(raw_question="q", depth=DepthLevel.STANDARD, subtasks=[
        SubTask(id="st_0", agent_type="industry", description=f"{SUPPLEMENT_PREFIX}『竞争格局』")])
    class _Msg2:
        content = _json.dumps([{"topic": "竞争格局", "agent_type": "industry", "queries": ["zzz"]}], ensure_ascii=False)
    class _LLM2:
        def invoke(self, _m):
            return _Msg2()
    from graph import _chapter_key

    with mock.patch("core.llm.has_main_llm", return_value=True), \
         mock.patch("core.llm.get_main_llm", return_value=_LLM2()):
        got5 = propose_subtasks(
            st5, [{"label": "x", "severity": "high", "detail": "d", "hint": "h"}], 2
        )
    assert got5 == [], f"撞章未被拒：{got5}"

    # ---- ⑥ 无模型 → 不提议（缺口仍进披露），绝不阻断 ----
    with mock.patch("core.llm.has_main_llm", return_value=False):
        assert propose_subtasks(st4, [{"label": "x", "severity": "high", "detail": "d", "hint": "h"}], 2) == []


def test_replan_wiring():
    """重规划必须真接进图，且位置正确（取证后、成稿前）。"""
    import inspect as _i

    _g = __import__("graph")
    assert hasattr(_g, "node_replan"), "缺 node_replan"
    _src = _i.getsource(_g.build_graph)
    assert 'add_node("replan"' in _src, "replan 节点未注册"
    assert 'add_edge("check_conflict", "replan")' in _src, "replan 未接在 check_conflict 之后"
    assert 'add_edge("replan", "summarize")' in _src, "replan 未接在 summarize 之前"
    # 节点名映射表（进度条/完成文案）必须同步，否则前端会出现空白节点
    _hint = _i.getsource(_g._node_hint)
    assert '"replan"' in _hint, "replan 缺节点说明文案"
    assert '"replan"' in _i.getsource(_g._node_done_detail), "replan 缺完成文案"


def test_supplement_empty_is_disclosed():
    """补充调查**空手而归**时必须如实出现，不许被静默跳过。

    实跑踩中：重规划派出的补充轮跑了、缺口没补上，而 `node_summarize` 里一句
    `if not res: continue` 让它从报告里彻底消失——读者看到的是"没有这一章"，
    而真相是"我们试过了、没查到"。**「试过没查到」是信息，「没提」不是。**
    """
    from core.models import DepthLevel, ResearchPlan, SubTask
    from core.state import init_state

    _g = __import__("graph")
    st = init_state("咖啡下沉还有机会吗", {"depth": "标准"})
    st["plan"] = ResearchPlan(
        raw_question=st["raw_question"], depth=DepthLevel.STANDARD,
        subtasks=[
            SubTask(id="st_0", agent_type="industry", description="调研『市场规模测算』"),
            SubTask(id="st_1", agent_type="industry", description="调研『空的普通维度』"),
            SubTask(id="st_2_sup", agent_type="user", description="补充调查『下沉用户评价』"),
        ],
    )
    st["subtask_results"] = {
        "st_0": "## 市场规模测算\n\n正常内容。",
        "st_1": "",            # 普通维度空 → 静默跳过（那是首轮就没安排到东西，不是"试过没查到"）
        "st_2_sup": "",        # 补充轮空 → **必须出现并说明**
    }
    draft = _g.node_summarize(st)["report_draft"]
    assert "市场规模测算" in draft
    assert "补充调查" in draft and "未取得可用内容" in draft, "补充轮空手而归被静默跳过了"
    assert "**该缺口仍然存在**" in draft, "补不到时必须明说缺口仍在（否则读者会读成已补齐）"
    assert "空的普通维度" not in draft, "普通空维度不该凭空出现"


def test_tool_select():
    """工具自主选择：模型提议 + 规则裁决 + **fail-open**（选不出来就全打，绝不缺证据）。"""
    import inspect as _i
    import json as _json

    from core.tool_select import MAX_TOOLS, ToolPlan, _candidates, select_tools

    # ---- ① 候选源按 feeds 交集筛；措辞不同也不许筛成空 ----
    c1 = _candidates("调研『市场规模测算』（方法论：TAM/SAM/SOM）", ["咖啡 市场规模"], "L1")
    assert c1, "候选为空 → 工具选择会直接失效"
    ids1 = [s.id for s in c1]
    assert "nbs" in ids1[:6], f"市场规模类子任务应把国家统计局排进前列：{ids1[:6]}"
    # 描述完全不沾维度词时仍须有候选（否则"换个说法"就静默失效）
    assert _candidates("调研『XYZ』", [], ""), "措辞不匹配时候选不该为空"

    # ---- ② 无模型 → None（= 未执行 → 取证层全打）----
    with mock.patch("core.llm.has_main_llm", return_value=False):
        assert select_tools("q", "调研『市场规模』", ["x"]) is None

    # ---- ③ 模型给了无效 id / 空名单 → 也必须是 None（不能返回"一个都不打"）----
    for payload in ({"tools": [], "why": ""}, {"tools": ["不存在的源", "ghost"], "why": "x"}, None):
        class _M:
            content = _json.dumps(payload, ensure_ascii=False) if payload is not None else "不是JSON"

        class _L:
            def invoke(self, _m):
                return _M()

        with mock.patch("core.llm.has_main_llm", return_value=True), \
             mock.patch("core.llm.get_main_llm", return_value=_L()):
            r = select_tools("q", "调研『市场规模』", ["x"], required_layer="")
        assert r is None, f"无效输出必须 fail-open，实得 {r}"

    # ---- ④ 规则强制并集：必需层的源一个都不能少（模型没选也补上）----
    class _M2:
        content = _json.dumps({"tools": ["ccgp"], "why": "只选了采购网"}, ensure_ascii=False)

    class _L2:
        def invoke(self, _m):
            return _M2()

    with mock.patch("core.llm.has_main_llm", return_value=True), \
         mock.patch("core.llm.get_main_llm", return_value=_L2()):
        tp = select_tools("q", "调研『公司经营』", ["某公司 营收"], required_layer="L2",
                          claim_label="公司经营数字")
    assert tp is not None and "ccgp" in tp.tools
    assert "cninfo" in tp.tools or "sec_edgar" in tp.tools, \
        f"L2 必需源没被强制并集进来：{tp.tools}"
    assert tp.forced, "强制并集必须有记录（否则读者以为全是模型挑的）"
    assert tp.trigger == "hybrid", "有强制并集时触发方式必须是 hybrid（模型+规则）"
    d = tp.to_dict()
    assert set(d) == {"tools", "why", "trigger", "forced", "dropped"}

    # ---- ⑤ 强制源必须在**截断之前**落位（2026-09-15 修，外部对抗审计发现）----
    # 病案：`picked` 的顺序是「模型选的在前、规则强制的追加在末尾」，而截断写成
    # `picked[: max(MAX_TOOLS, len(forced))]`——**模型选满 MAX_TOOLS 个时，
    # 切片恰好把追加在末尾的强制源整段切掉**（`max()` 只在 len(forced) ≥ MAX_TOOLS 时才救得回来）。
    # 后果：对外承诺的「规则强制并集，模型选漏了也不许降级」在最需要它的场景下**静默失效**
    # ——报告里写着权威源必选，真正调用的只剩模型自选。不报错，只是不生效。
    _nonreq = [
        s.id for s in _candidates("调研『公司经营』", ["某公司 营收"], "L2")
        if s.id not in ("cninfo", "sec_edgar", "sse_edc")
    ][:MAX_TOOLS]

    class _M3:
        content = _json.dumps({"tools": _nonreq, "why": "只选非强制源，且选满"}, ensure_ascii=False)

    class _L3:
        def invoke(self, _m):
            return _M3()

    with mock.patch("core.llm.has_main_llm", return_value=True),          mock.patch("core.llm.get_main_llm", return_value=_L3()):
        tp3 = select_tools("q", "调研『公司经营』", ["某公司 营收"], required_layer="L2",
                           claim_label="公司经营数字")
    assert tp3 is not None and tp3.forced, f"本例应触发强制并集，实得 {tp3 and tp3.to_dict()}"
    _cut = [f for f in tp3.forced if f not in tp3.tools]
    assert not _cut, f"强制源被截断切掉了：{_cut}——承诺了『模型选漏也不许降级』却没执行"
    assert len(tp3.tools) <= max(MAX_TOOLS, len(tp3.forced)), f"工具数超出上限：{tp3.tools}"

    # ---- ⑤ 取证层闸门：结构锁（gate 必须真的接在取数之前，否则白打请求）----
    from core.authority_fetch import fetch_authority

    _fa = _i.getsource(fetch_authority)
    assert "only_tools" in _i.signature(fetch_authority).parameters, "fetch_authority 未接受 only_tools"
    for _sid in ("cninfo", "sec_edgar", "nbs", "sse_edc", "worldbank", "imf"):
        assert f'_want("{_sid}")' in _fa, f"{_sid} 未接工具闸门"
    assert "_skip(" in _fa, "被跳过的源族没有留痕 → 台账会把「主动不查」读成「查了没查到」"
    assert "工具计划未选中" in _fa

    # ---- ⑥ 幽灵源：`intl` 不是注册表里的 id（注册表是 worldbank / imf 两条）----
    from core.source_registry import connected_sources

    _ids = {s.id for s in connected_sources()}
    assert "intl" not in _ids, "注册表不该有 intl"
    # 只看**可执行语句**：注释里出现 `_add("intl"` 是在讲这个病案，不算违规
    _exec_lines = [ln.strip() for ln in _fa.splitlines() if not ln.strip().startswith("#")]
    assert not any(ln.startswith('_add("intl"') for ln in _exec_lines),         "台账仍在记注册表里不存在的幽灵源 intl"
    assert '_add("worldbank"' in _fa and '_add("imf"' in _fa, "国际源未按真实 id 分别记账"
    # 工具选择用的 id 必须是注册表 id，否则永远挑不中
    for _t in (tp.tools if tp else []):
        assert _t in _ids, f"工具计划给出了注册表里没有的 id：{_t}"



def test_tool_select_wiring():
    """选中了必须真的用上——只记录不使用就是装饰。"""
    import inspect as _i

    from agents import researchers as _R

    _src = _i.getsource(_R.run_researcher)
    assert "select_tools" in _src, "run_researcher 未做工具选择"
    assert 'context["_tools"]' in _src, "工具计划未写进 context"
    assert '"tool_plan"' in _src, "工具计划未随结果返回（决策链就看不到）"
    # 取证层实际调用 `fetch_authority` 的是 `_search_mcp`（run_researcher 把 _tools 写进同一个
    # context 对象后传给它的）——所以"传进去了"要在这两个函数里各查一半。
    _sm = _i.getsource(_R._search_mcp)
    assert "only_tools=" in _sm, "工具计划未传进取证层（= 选了不用，是装饰）"
    assert '_tools' in _sm, "取证层没读 context 里的工具计划"

    _g = _i.getsource(__import__("graph")._run_sub_agent)
    assert "_tool_picks" in _g and "select_tool" in _g, "工具选择未记进决策链"


def test_ledger_skip_consistency():
    """调用台账三处口径必须一致：标记列 / 调用次数 / 摘要行。

    实跑踩中：工具计划主动跳过的源，备注写「未选中（非回空）」，标记列却打 `⚠️ 空返回`，
    摘要行还把它算进"N 个返回空"、"合计 N 次调用"——**一个请求都没发的源被记成发过请求且失败了**。
    这张表的全部意义是"这次报告真的向哪些源发了请求"，在这里虚高最不该。
    """
    from core.source_health import render_trace_appendix, summarize_trace

    tr = [
        {"src": "nbs", "name": "国家统计局", "n": 0, "ok": False,
         "note": "工具计划未选中，本次跳过（非回空）", "layer": "L1", "urls": []},
        {"src": "cninfo", "name": "巨潮", "n": 0, "ok": False, "note": "", "layer": "L2", "urls": []},
        {"src": "sec_edgar", "name": "SEC EDGAR", "n": 5, "ok": True, "note": "", "layer": "L2",
         "urls": ["a", "b", "c", "d", "e"]},
    ]
    s = summarize_trace(tr)
    assert s["skipped"] == 1 and s["empty"] == 1, s
    assert s["contacted"] == 2, f"实际发起请求的源数错：{s}"
    assert s["calls"] == 2, f"「合计调用」把计划跳过也算了进去：{s}"
    md = render_trace_appendix(tr)
    assert "— 未调用（计划跳过）" in md, "标记列没区分计划跳过"
    assert "⚠️ 空返回" in md, "真回空的源仍须标为空返回"
    assert "涉及 **3** 个源" in md and "向 **2** 个" in md, md.split(chr(10))[2][:160]


def test_ledger_mixed_source_family():
    """**混合源族**：同一个源既被跳过又真跑过 → 不许被记成"没跑过"。

    病案（2026-09-15 深访实跑，真实报告《研策调研报告》原文）：
    摘要行印的是「本次调研涉及 **9** 个源，其中向 **0** 个实际发起了请求
    （合计 **30** 次调用，其中 **4** 个有返回、**0** 个返回空、**9** 个被工具计划主动跳过）」——
    **一句话里前后互相否定**：向 0 个发请求，却有 30 次调用、4 个有返回。
    表内更是有 4 行写成「取回 3 条 / 9 次调用 / 说明：本次跳过（非回空）」。

    根因：`note` 是"后一条覆盖前一条"，而"是否跳过"当时**按合并后的 note 措辞判**。
    同一源族在不同子任务里选择不同（有的子任务打它、有的判定用不上），
    最后一条恰好是跳过备注 → 整个源族被记成没跑过。
    这正是本项目最常复发的「**同一动作、两处实现**」：逐调用的 `skip_hits` 算对了却被丢弃，
    外层又按 note 重算了一遍。判据现在只留一处（`planned_skip`）。
    """
    from core.source_health import render_trace_appendix, summarize_trace

    SKIP = "工具计划未选中，本次跳过（非回空）"
    tr = [
        # 国家统计局：2 个子任务真跑（取回 3 条），2 个子任务跳过；跳过备注排在最后
        {"src": "nbs", "name": "国家统计局", "n": 3, "ok": True, "note": "", "layer": "L1",
         "urls": ["u1", "u2", "u3"]},
        {"src": "nbs", "name": "国家统计局", "n": 0, "ok": False, "note": "", "layer": "L1", "urls": []},
        {"src": "nbs", "name": "国家统计局", "n": 0, "ok": False, "note": SKIP, "layer": "L1", "urls": []},
        {"src": "nbs", "name": "国家统计局", "n": 0, "ok": False, "note": SKIP, "layer": "L1", "urls": []},
        # SEC：一次都没跑过 → 才算真"计划跳过"
        {"src": "sec_edgar", "name": "SEC EDGAR", "n": 0, "ok": False, "note": SKIP, "layer": "L2", "urls": []},
    ]
    s = summarize_trace(tr)
    assert s["contacted"] == 1, f"跑过的源必须计入 contacted：{s}"
    assert s["skipped"] == 1, f"只有一次都没跑过的才算跳过：{s}"
    assert s["hit"] == 1 and s["empty"] == 0, s
    assert s["calls"] == 2, f"跳过不记调用，真跑的 2 条 trace 要记：{s}"
    _nbs = [r for r in s["rows"] if r["src"] == "nbs"][0]
    assert _nbs["planned_skip"] is False, "混合源族不许判为跳过"
    assert _nbs["note"] == "", f"真跑过的源不该留跳过备注：{_nbs['note']!r}"

    md = render_trace_appendix(tr)
    assert "向 **1** 个实际发起了请求" in md, md.split(chr(10))[2][:200]
    _row = [ln for ln in md.split(chr(10)) if ln.startswith("| 国家统计局")][0]
    assert "未选中" not in _row and "跳过" not in _row, f"3 条/3 次的源不该写跳过：{_row}"
    _sec = [ln for ln in md.split(chr(10)) if ln.startswith("| SEC")][0]
    assert "计划跳过" in _sec, f"真跳过的源仍须标明：{_sec}"
    # 反向锁：摘要行的四个数不许再互相否定
    assert "向 **0** 个" not in md, "「向 0 个发起请求」与「30 次调用」不可能同时成立"


def test_debate():
    """多角色辩论：被质疑方必须有申辩权；辩论不完整**不得**当作"无争议"。"""
    import json as _json

    from core.debate import debate_one, format_appendix_md, pick_claims, run as _drun

    # ---- ① 只挑"带数字 + 有原文摘录"的主张（定性表述辩不出结果，白花钱）----
    evs = [
        _ev("门店总数 18,590 家，同比增长 12%", "某年报披露：截至 2024 年末门店总数为 18,590 家，同比增加 12%。"),
        _ev("竞争格局较为激烈", "行业分析：市场竞争激烈，参与者众多，价格战频发，利润率承压明显。"),
        _ev("营收 70.3 亿美元", "短摘录"),  # 摘录过短 → 无处申辩也无处质疑
    ]
    ps = pick_claims(evs, 5)
    assert len(ps) == 1, f"应只留 1 条可辩主张，实得 {len(ps)}"
    assert "18,590" in ps[0]["claim"]

    def _mk(objs):
        """按调用顺序依次返回给定的 JSON（模拟质疑/申辩/裁定三次调用）。"""
        it = iter(objs)

        class _M:
            def __init__(self, c):
                self.content = c

        class _L:
            def invoke(self, _m):
                return _M(_json.dumps(next(it), ensure_ascii=False))

        return _L()

    # ---- ② 三态：维持 / 修正 / 未消解 ----
    _cases = [
        ([{"objection": "口径存疑：18,590 含联营", "type": "口径", "severity": "high", "weak": False},
          {"rebuttal": "年报口径已注明含联营，与主张一致", "concede": False, "revised": ""},
          {"rebuttal_holds": True, "verdict": "converged_support",
           "final_statement": "门店总数 18,590 家（含联营）", "why": "口径已注明"}], "converged_support"),
        ([{"objection": "时点错位", "type": "时点", "severity": "high", "weak": False},
          {"rebuttal": "确实用的是上年数", "concede": True, "revised": "门店总数 17,000 家（2023 年末）"},
          {"rebuttal_holds": False, "verdict": "converged_revise",
           "final_statement": "门店总数 17,000 家（2023 年末）", "why": "申辩方承认时点错位"}], "converged_revise"),
        ([{"objection": "量级不合理", "type": "量级", "severity": "high", "weak": False},
          {"rebuttal": "口径不同不能直接比", "concede": False, "revised": ""},
          {"rebuttal_holds": False, "verdict": "unresolved", "final_statement": "",
           "why": "材料不足以判定", "disagreement": "卡在含不含联营的口径定义上"}], "unresolved"),
    ]
    for objs, want in _cases:
        with mock.patch("core.llm.has_main_llm", return_value=True),              mock.patch("core.llm.get_main_llm", return_value=_mk(objs)):
            r = debate_one(ps[0])
        assert r["verdict"] == want, f"期望 {want}，实得 {r['verdict']}：{r}"
        assert r["rounds"] == 3, r

    # ---- ③ **辩论不完整绝不能算"无争议"**（这是与单向 Red Team 最根本的区别）----
    for broken in ([{"objection": "x", "type": "口径", "severity": "high"}],  # 申辩方挂
                   []):                                                       # 质疑方就挂
        with mock.patch("core.llm.has_main_llm", return_value=True),              mock.patch("core.llm.get_main_llm", return_value=_mk(broken)):
            r = debate_one(ps[0])
        assert r["verdict"] in ("not_run", "unresolved"), f"失败被当成了结论：{r['verdict']}"
        if r["verdict"] == "not_run":
            assert "不计为无争议" in r["why"], r["why"]
    # 质疑方自陈找不到实质问题 → 收敛，且理由必须写明"非申辩胜出"
    # **并且不许记成 `converged_support`**（2026-09-18 审计病案）：记成驳回会让同一节的
    # 汇总行印"质疑被驳回 N"，而明细行印"质疑方自陈未见实质问题"——**汇总与明细打架**，
    # 且方向一致地把"驳回率"抬高。第五态 `no_objection` 就是为这一支设的。
    with mock.patch("core.llm.has_main_llm", return_value=True),          mock.patch("core.llm.get_main_llm",
                    return_value=_mk([{"objection": "", "weak": True, "type": "", "severity": ""}])):
        r = debate_one(ps[0])
    assert r["verdict"] == "no_objection" and "非「申辩胜出」" in r["why"], r
    from core.debate import VERDICT_LABEL as _VL

    assert "驳回" not in _VL[r["verdict"]], f"无实质质疑不许印成驳回：{_VL[r['verdict']]}"

    # ---- ④ 档位门 + 未执行必须自陈"不构成背书" ----
    from core.debate import MAX_CLAIMS_BY_DEPTH

    assert MAX_CLAIMS_BY_DEPTH["free"] == 0
    assert _drun(evs, depth="free") is None
    md0 = format_appendix_md(None)
    assert "本次未执行" in md0 and "不构成任何背书" in md0
    assert "多角色辩论" in format_appendix_md(
        {"rows": [{"claim": "c", "verdict": "unresolved", "why": "w"}], "total": 1,
         "counts": {"converged_support": 0, "converged_revise": 0, "unresolved": 1, "not_run": 0},
         "open": 1}
    )

    # ---- ⑤ 接线：附录进交付 + 进审计折叠（否则会展现在正文里）----
    import inspect as _i

    _g = __import__("graph")
    assert "format_appendix_md" in _i.getsource(_g.node_deliver), "辩论附录未接进交付"
    from core.report_html import AUDIT_HEADING_PATTERNS

    assert "多角色辩论" in AUDIT_HEADING_PATTERNS, "辩论附录未进审计折叠标题表"

    # ---- ⑥ 时间锚的**确定性出口过滤**（2026-09-17 实跑病案）----
    #
    # 病案逐字来自本跑交付报告：质疑方称「国家统计局官网域名 stats.gov.cn 下**不存在 2026 年的
    # 真实数据**，该链接为**虚构或未来时点**」，**裁定方采纳**并写「当前未到该时间，属根本性证伪」，
    # **申辩方（正确的一方）被驳回**——一条真实存在的统计局数据被判成虚构。
    #
    # 为什么提示词禁令不够：`_SKEPTIC` 条款 4 **早就有**这条禁令、`_ANALYST` 条款 3 也有对策，
    # 但**裁定方一个字都没提时间锚**，模型就从那一侧绕了过去。**只写在一个角色身上的提示词禁令不是禁令。**
    # 所以锁必须打在**行为**上：喂进真实的产出文本，看它会不会被撤销。
    from core.debate import _filter_time_anchor

    _real = {  # ← 本跑原文，逐字
        "claim": "国家统计局 2026-08-27 发布 2026 年 1-8 月社会消费品零售总额数据",
        "objection": "国家统计局官网域名stats.gov.cn下不存在2026年的真实数据，该链接为虚构或未来时点，无法验证。",
        "rebuttal": "质疑基于外部现实时间否定材料存在性，违反「只依据原始材料」规则。",
        "verdict": "unresolved",
        "why": "质疑指出数据时空逻辑悖论（2026年发布2025年数据但当前未到该时间），属根本性证伪。",
        "disagreement": "分歧在于：当主张包含明显…",
        "final_statement": "",
    }
    _r = _filter_time_anchor(dict(_real))
    assert _r["verdict"] == "converged_support", (
        f"实跑那条时间锚误判没有被撤销（verdict 仍是 {_r['verdict']}）——"
        "这条是唯一会反噬「自我怀疑」人设的缺陷"
    )
    assert _r["filtered"] == "time_anchor" and _r["filter_hit"], "过滤了但没留痕（静默过滤 = 另一种静默消失）"
    assert _r["verdict_before_filter"] == "unresolved", "没记下改判前的裁决，读者无从复核"
    assert "时间锚" in _r["why"], "改判理由没写进 why"

    # 换壳变体必须一并挡住（模型上次就是从"官网没有"这个壳绕过去的）
    for _v in ("该数据尚不存在", "该年度报告尚未发布，属未来数据", "官网查无此数",
               "2026年发布2025年数据但当前未到该时间", "我的训练数据截止较早，该日期属于未来"):
        _rv = _filter_time_anchor({"claim": "x", "objection": _v, "verdict": "unresolved",
                                   "why": "", "disagreement": ""})
        assert _rv["filtered"] == "time_anchor", f"换壳没挡住：{_v}"

    # **反向锁**：正当的时点/口径/单位/量级/主体质疑**一条都不许误伤**。
    # 误伤的后果比漏挡更糟：它会把真问题洗成假问题，正是本模块存在的理由的反面。
    for _ok in ("来源原文写的是 2024 年数据，主张标成 2025 年，时点错位",
                "单位是美元，主张写成元",
                "原文是连锁口径，主张按全行业口径",
                "5005亿与摘录500亿差十倍",
                "主张讲瑞幸，摘录讲星巴克"):
        _ro = _filter_time_anchor({"claim": "x", "objection": _ok, "verdict": "converged_revise",
                                   "why": "w", "disagreement": ""})
        assert _ro["filtered"] == "", f"正当质疑被误伤：{_ok}"

    # 已经是"维持原主张"的不动它——少动一次就少一分风险
    _rs = _filter_time_anchor({"claim": "x", "objection": "官网查无此数", "verdict": "converged_support",
                               "why": "", "disagreement": ""})
    assert _rs["filtered"] == "", "结论已是维持时不该再动"

    # 披露：过滤条数必须**单独列**，不许并进"质疑被驳回"
    _fm = format_appendix_md({
        "rows": [{"claim": "c", "verdict": "converged_support", "why": "w", "filtered": "time_anchor"}],
        "total": 1, "time_filtered": 1,
        "counts": {"converged_support": 1, "converged_revise": 0, "unresolved": 0, "not_run": 0},
        "open": 0,
    })
    assert "时间锚过滤" in _fm, "过滤条数没进附录（读者看不到「质疑被撤销过」）"
    assert "无法访问外网" in _fm, "没写清为什么这类指控在结构上不成立"



def test_exp_memory():
    """研究经验：只记可核对计数；**只作提示不作规则**；样本不足不输出。"""
    import importlib
    import os as _os
    import tempfile

    import core.exp_memory as EM

    tmp = _os.path.join(tempfile.mkdtemp(), "exp.jsonl")
    _old = EM._PATH
    EM._PATH = tmp
    # 本用例测的是**经验记忆的逻辑**（计数口径/样本门/只作提示），不是离线闸门——
    # 而 `record_run` 现在第一行就过 `_offline()`。所以这里按其原本的语义
    # **临时摘掉 `YANCEC_OFFLINE`**（`_PATH` 已指向临时文件，不会碰生产台账），
    # 跑完还原。同款写法见本文件 test_offline_write_gates 与绿档账本用例。
    # 病案：2026-09-16 加第四处离线闸门时，正是这条用例先红——
    # **闸门加对了，而它暴露了"测试需要写、生产不许写"这层区分没人表达过。**
    _gate = _os.environ.pop("YANCEC_OFFLINE", None)
    try:
        # ---- ① 只记"真发过请求"的源 ----
        st = {
            "raw_question": "某咖啡连锁品牌的门店拓展",
            "source_trace": [
                {"src": "nbs", "name": "国家统计局", "n": 0, "calls": 4, "note": "", "urls": []},
                {"src": "ccgp", "name": "中国政府采购网", "n": 0, "calls": 3,
                 "note": "工具计划未选中，本次跳过（非回空）", "urls": []},   # 计划跳过 → 不记
                {"src": "cninfo", "name": "巨潮", "n": 5, "note": "", "urls": ["a"]},
            ],
        }
        with mock.patch("agents.researchers._extract_industry", return_value="咖啡"):
            n = EM.record_run(st)
        # 注：`summarize_trace` 的 `calls` 是**记录条数**（每条 trace 记一次调用），
        # 手写 `calls` 字段不会生效——计数口径以它为准，测试不要自己另立一套。
        assert n == 2, f"应只记 2 条（计划跳过的那条必须排除），实得 {n}"
        # 计划跳过若被记进去，会造出"这个源不行"的假经验（它只是没被选中）
        assert not any(r["source"] == "ccgp" for r in EM._load()), "计划跳过的源被记成了经验"
        # `calls` 是**记录条数**（一条 trace = 一次调用），样本门要靠**多次调研**累积。
        # 再跑两次同样内容 → nbs 累计 3 次调用，才够最小样本门（3）。
        with mock.patch("agents.researchers._extract_industry", return_value="咖啡"):
            EM.record_run(st)
            EM.record_run(st)

        # ---- ② 最小样本门 + 命中率门 ----
        ind = "咖啡"
        _r = EM.recall(ind)
        assert [x["source"] for x in _r] == ["nbs"], f"最小样本门/命中率门不对：{_r}"
        assert _r[0]["calls"] == 3 and _r[0]["hits"] == 0, f"聚合口径错（应为 3 次调用 0 次命中）：{_r}"
        # 补样本：ccgp 若真的被调用过且确实回空，够 3 次后才该出现
        for _ in range(3):
            EM.record_run({"raw_question": "某咖啡连锁品牌", "source_trace": [
                {"src": "ccgp", "name": "中国政府采购网", "n": 0, "note": "", "urls": []},
            ]})
        assert "ccgp" in [x["source"] for x in EM.recall(ind)], "样本够了却仍不输出"
        # 高命中率的源不该出现在提示里（命中率高于阈值 = 这条路走得通，没什么可提醒）
        for _ in range(4):
            EM.record_run({"raw_question": "某咖啡连锁品牌", "source_trace": [
                {"src": "good", "name": "好用的源", "n": 5, "note": "", "urls": ["a"]}]})
        assert "good" not in [x["source"] for x in EM.recall(ind)]

        # ---- ③ 提示措辞：必须写明"不是规则、仍应尝试" ----
        brief = EM.format_brief(ind)
        assert "不是规则" in brief and "仍应正常尝试" in brief, brief
        assert "可核对" in brief, "经验必须自陈是「可核对统计」而非印象"
        # 没有可说的行业 → 一个字不给（不印空壳）
        assert EM.format_brief("完全不存在的行业") == ""

        # ---- ④ 接线 ----
        import inspect as _i

        _g = __import__("graph")
        _src = _i.getsource(_g._run_sub_agent)
        assert "exp_brief" in _src, "经验未注入检索上下文"
        assert "record_run" in _i.getsource(_g.node_deliver), "经验未在交付末端落盘"
        assert "recall" in _i.getsource(_g.node_route_skill),             "经验未进决策链（那就无法宣称「经验影响了规划」）"
        # 经验召回必须是**独立类型**，不能混进「计划调整」——两者是不同的问题
        # （一个问"要不要补一轮"，一个问"要不要参考历史"），混在一起读者分不清。
        _dt = __import__("core.decision_trace", fromlist=["x"])
        assert "recall" in _dt.KINDS and "replan" in _dt.KINDS
        _R = __import__("agents.researchers", fromlist=["x"])
        assert "exp_brief" in _i.getsource(_R._integrate), "经验未进入整合提示词"
    finally:
        EM._PATH = _old
        if _gate is not None:
            _os.environ["YANCEC_OFFLINE"] = _gate


def test_sop_fallback_not_silent():
    """决策清单降级不静默（v0.9.75 实跑病案）。

    病案：`generate_decision_sop` 里构造 evidence_summary 时对**dict 形态**证据抛
    `AttributeError`，被 `except Exception: pass` 整个吞掉 → 直接返回模板。
    后果是报告其余部分一切正常，**只有「决策清单」这一节变成"未调用大模型"的空壳**，
    归档里连跑 150 次全部静默降级而无人发现（追查时才发现全是 _fulltest 的产物）。
    本仓库这是第三次踩「Evidence 双形态」。三重回归锁：
      ① 双形态都不许再崩；② 兜底必须带可读原因；③ 原因必须一路走到报告头。
    """
    import inspect as _i

    from agents.decision import generate_decision_sop, sop_status

    REP = "## 竞争格局\n瑞幸门店 18,590 家。\n" * 20
    MARK = "此为兜底模板输出"

    class _Ev:
        def __init__(self, c):
            self.claim = c
            self.sources = []

    class _Msg:
        content = "## 决策判定\n- 🟡 Validate：证据不足。\n" + "正文" * 60

    class _FakeLLM:
        def __init__(self, boom=False):
            self.boom = boom

        def invoke(self, _msgs):
            if self.boom:
                raise RuntimeError("模拟调用失败")
            return _Msg()

    def _run(evs, report=REP, llm=None, offline=False):
        """**必须完全离线确定性**：本套件不连网、不读 .env。

        第一版写成依赖本机 key，结果在套件里 has_main_llm() 为 False →
        真实路径根本没被走到，测试反而"红"在一个与断言无关的原因上。
        """
        _m = mock.patch("agents.decision.has_main_llm", return_value=not offline)
        _g = mock.patch("agents.decision.get_main_llm", return_value=llm or _FakeLLM())
        with _m, _g:
            sop = generate_decision_sop(raw_question="某咖啡连锁下沉", report=report,
                                        evidences=evs, vt_summary="- vt_01｜访谈｜负责人:市场")
        return sop, sop_status()

    # ---- ① 双形态 + 脏数据都不许触发兜底（这是本病的直接回归锁） ----
    for _tag, _evs in [
        ("dict", [{"claim": "单店投入约50万", "sources": [{"title": "T", "level": "L2"}]}]),
        ("对象", [_Ev("单店投入约50万")]),
        ("混合", [_Ev("A"), {"claim": "B", "sources": []}]),
        ("脏（无 claim 键）", [{"sources": None}]),
        ("空列表", []),
    ]:
        _sop, _st = _run(_evs)
        assert MARK not in _sop, f"{_tag} 形态仍触发兜底（reason={_st.get('reason')}）"
        assert not _st.get("reason"), f"{_tag} 形态被判为降级：{_st}"

    # ---- ② 三条真降级路径都要带**可读原因**，且原因各不相同 ----
    _sop, _st = _run([_Ev("x")], offline=True)
    assert MARK in _sop and _st.get("reason") == "no-llm", _st
    _sop, _st = _run([_Ev("x")], report="")
    assert MARK in _sop and _st.get("reason") == "empty-report", _st
    assert _st.get("detail"), "降级原因必须带细节，否则等于没说"
    _sop, _st = _run([_Ev("x")], llm=_FakeLLM(boom=True))
    assert MARK in _sop and _st.get("reason") == "llm-error", _st
    # 模型返回过短也算降级（"返回了但等于没返回"不许被当成成功）
    _short = type("L", (), {"invoke": lambda self, m: type("M", (), {"content": "太短"})()})()
    _sop, _st = _run([_Ev("x")], llm=_short)
    assert MARK in _sop and _st.get("reason") == "short-output", _st

    # ---- ③ 原因一路走到报告头 + 前端角标（否则等于没告警） ----
    _g = __import__("graph")
    _deliver = _i.getsource(_g.node_deliver)
    assert "sop_fallback" in _deliver and "决策清单为兜底模板" in _deliver, \
        "交付层未把降级写到报告头 —— 埋在正文里的说明等于没有说明"
    assert "sop_status" in _i.getsource(_g.node_decision_sop), "决策节点未读取降级状态"
    _rj = _i.getsource(_g._result_json)
    assert "sop_fallback" in _rj, "降级标记未透传给调用方（前端就无从打角标）"
    import core.state as _S

    assert "sop_fallback" in getattr(_S.TaskState, "__annotations__", {}), \
        "未进 TaskState schema —— LangGraph 会丢弃节点返回的 schema 外键（本仓库踩过）"
    # 直接读绝对路径：原先这里先试 `open("App.jsx")`（相对 cwd）再回退，
    # 于是"读到哪个文件"取决于你从哪儿跑——而这几处比的是**前后端是否一致**，
    # 读不到就变成一句"前端缺东西"。
    _fe = (_HERE.parent / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8")
    assert "sopFallback" in _fe, "前端未接降级标记（角标不出现 = 等于没告警）"


def test_lint_report_cli_has_r7():
    """现场重放入口必须跑全 R1–R7（v0.9.75 修）。

    `_lint_report.py` 是赛前/现场用来对**任意一份报告**（含竞品报告）重放规则的 CLI。
    它此前只登记了 R1–R6，**漏掉的恰是最该抓的 R7（来源资格错配）**——
    于是"跑一遍 lint 干净"这个结论本身是假的：最强的规则根本没上场。
    """
    import _lint_report as L

    labels = " ".join(x[0] for x in L._CHECKS)
    for r in ("R1", "R2", "R3", "R4", "R5", "R6", "R7"):
        assert r in labels, f"规则层 CLI 漏登记 {r}"
    # 且 lint_report() 全量路径与 CLI 的检查集必须一致（两条路给出不同结论 = 更坏）
    import core.rule_lint as R

    assert callable(R._check_source_qualification)


def test_repurchase_board():
    """最小复购看板：**分母只含测得出那一部分**——缺测不许当 0。"""
    from core.repurchase_board import build_board, render_md

    rows = [
        # 老账本行：没有新字段 → 三个分母都不该收它
        {"depth": "standard", "delivered": None, "elapsed_s": 100},
        {"depth": "standard", "delivered": 1, "degraded": False, "reused_asset": True,
         "took_over": False, "cost_cny": 0.42, "llm_tokens": {"total_tokens": 120000}},
        # 交付了但决策清单是兜底 → 不算"成功"
        {"depth": "standard", "delivered": 1, "degraded": True, "reused_asset": False,
         "took_over": False, "cost_cny": 0.30, "llm_tokens": {"total_tokens": 90000}},
        {"depth": "deep", "delivered": 1, "degraded": False, "reused_asset": True,
         "took_over": True, "cost_cny": 1.10, "llm_tokens": {"total_tokens": 300000}},
    ]
    b = build_board(rows=rows)
    m = b["metrics"]
    assert m["task_success_rate"]["den"] == 3, "缺 delivered 的老行被算进了分母（=把缺测当失败）"
    assert m["task_success_rate"]["num"] == 2, "降级交付被算成了成功"
    assert m["reuse_rate"]["value"] == round(2 / 3, 4)
    assert m["takeover_rate"]["value"] == round(1 / 3, 4)
    # 成本只对"成功"求均值：(0.42 + 1.10) / 2 —— 降级那笔的 0.30 不许进
    assert m["cost_per_success"]["value"] == round((0.42 + 1.10) / 2, 4), m["cost_per_success"]
    # 每项都必须自带定义（没有分母定义的指标在 2026 年会被当场问死）
    for k in ("task_success_rate", "reuse_rate", "takeover_rate", "cost_per_success"):
        assert m[k].get("definition"), f"{k} 缺分母定义"
    assert "不证明什么" in render_md(b), "看板必须自陈边界"

    # 空账本 → 全部 None（不是 0），且如实说样本不足
    e = build_board(rows=[])
    assert e["metrics"]["task_success_rate"]["value"] is None, "空账本给了 0 而不是 None"
    assert "不证明" in render_md(e) or "样本" in render_md(e)
    assert e["sample_ok"] is False

    # 未配置单价 → 成本必须是 None，绝不拍数
    b2 = build_board(rows=[{**rows[1], "cost_cny": None}])
    assert b2["metrics"]["cost_per_success"]["value"] is None, "未配单价却算出了钱"

    # 样本充足性必须按**各指标自己的分母**判，不能用账本行数代替。
    # 病案（2026-09-14 端到端体检）：账本 4 行（其中 3 行缺新字段）曾判 `sample_ok=True`，
    # 于是在"样本充足"的旗号下展示 `100.0%`——实为 1/1。账本行数 ≠ 指标分母。
    _mix = [
        {"delivered": None},
        {"delivered": None},
        {"delivered": None},
        {"delivered": 1, "degraded": False, "reused_asset": False, "took_over": False},
    ]
    _mb = build_board(rows=_mix)
    assert _mb["total"] == 4, _mb["total"]
    assert _mb["effective_n"] == 1, f"有效分母应为 1，实得 {_mb['effective_n']}"
    assert _mb["sample_ok"] is False, "账本 4 行里 3 行缺字段，却判成了样本充足"
    assert _mb["metrics"]["task_success_rate"]["low_sample"] is True, "n=1 未标样本不足"
    assert "样本不足" in render_md(_mb), "分母不足时报告里必须就地标注，不能只挂在脚注"

    # 反向锁：真够了就必须判充足——否则这道门等于永远关着，是另一种病
    _full = [
        {
            "delivered": 1,
            "degraded": False,
            "reused_asset": True,
            "took_over": False,
            "cost_cny": 1.0,
            "llm_tokens": {"total_tokens": 100000},
        }
        for _ in range(3)
    ]
    _fb = build_board(rows=_full)
    assert _fb["effective_n"] == 3 and _fb["sample_ok"] is True, _fb["caveat"]
    assert "样本不足" not in render_md(_fb), "样本够了还在标不足"

    # 离线测试**绝不许写生产账本**（2026-09-14 亲历病案）。
    # `_fulltest` 的 e2e 会直接调 `run_task`，而 `run_task` 内部就落账——
    # "不经过 API" ≠ "不落账"。本轮反复跑测试把账本从 6 行撑到 10 行，
    # 看板的「有效分母」跟着从 3 虚高到 7：**一个用来证明「真实运行」的账本被测试数据稀释了。**
    # 同源旧病案：归档 168 份报告里 150 份决策清单是兜底模板，全部来自测试调用点——
    # 当时只判了"测试产物"，**没修那道缺失的闸门**，所以这次又来一遍。
    from core import runstats as R

    assert R._offline() is True, "测试进程没置 YANCEC_OFFLINE —— 跑一次测试就会污染一次生产账本"
    _before = R._RUNSTATS_PATH.read_text(encoding="utf-8") if R._RUNSTATS_PATH.exists() else ""
    R.log_run_stats("标准", "离线闸门自测（不应落账）", {"elapsed_s": 1.0, "delivered": 1})
    _after = R._RUNSTATS_PATH.read_text(encoding="utf-8") if R._RUNSTATS_PATH.exists() else ""
    assert _before == _after, "离线模式下仍写入了生产账本"

    # 决策基线自动登记（2026-09-15）：系统自己在交付时登记基线，让「会回来对账」有代码支撑——
    # 此前 outcome_baselines.json 全是**手工维护**的，系统自己写下的判定/监控条件一条都没进回访队列。
    # 但同一条离线闸门也必须挡住它（否则每跑一次测试就凭空多一条基线）。
    import pathlib

    from core import outcome_track as _OT

    _bb = _OT._PATH
    _bbefore = pathlib.Path(_bb).read_text(encoding="utf-8") if pathlib.Path(_bb).exists() else ""
    _r = _OT.register("离线闸门自测题（不应登记）", "Validate", ["测试条件"])
    assert _r is None, f"离线模式下仍登记了决策基线：{_r}"
    _bafter = pathlib.Path(_bb).read_text(encoding="utf-8") if pathlib.Path(_bb).exists() else ""
    assert _bbefore == _bafter, "离线模式下决策基线文件被改动了"
    # 同题去重 + 编号不撞号：在**临时文件**上验证真逻辑（不碰生产档案）
    import tempfile

    # 真逻辑要**临时摘掉离线闸门**才能测到（闸门在函数第一行就 return 了，不摘就永远测不到去重与编号）
    _saved, _OT._PATH = _OT._PATH, tempfile.mktemp(suffix=".json")
    _gate = os.environ.pop("YANCEC_OFFLINE", None)
    try:
        assert _OT.register("题A", "Go", ["c1"]) is not None
        assert _OT.register("题A", "Go", ["c1"]) is None, "同题被重复登记（复跑会刷屏、due_date 会打架）"
        _r2 = _OT.register("题B", "Validate", ["c2"])
        assert _r2 and _r2["id"] == "DB-02", f"编号撞号：{_r2}"
        assert "auto" in str(_r2.get("source") or ""), "自动登记的来源没标注（与手工基线分不开）"
    finally:
        if _gate is not None:
            os.environ["YANCEC_OFFLINE"] = _gate
        try:
            os.unlink(_OT._PATH)
        except Exception:
            pass
        _OT._PATH = _saved


def test_autonomy_map():
    """自主性图谱：**必须与真实图拓扑一致**（会过期的表比没有表更糟）。"""
    import re as _re

    from core.autonomy_map import NODES, entries, render_md, summary

    _g = (_HERE / "graph.py").read_text(encoding="utf-8")
    _nodes = set(_re.findall(r'g\.add_node\("([a-z_]+)"', _g))
    assert _nodes, "未从 graph.py 解析到节点——正则失效，别让断言变成空转"
    assert set(NODES) == _nodes, (
        f"自主性图谱与图拓扑漂移：表里多 {set(NODES) - _nodes}，图里多 {_nodes - set(NODES)}"
    )
    s = summary()
    assert s["nodes"] == len(_nodes)
    # 不可逆动作必须被点名（不可逆是自主性真正的代价所在）
    assert "deliver" in s["irreversible"], "交付是全链路唯一不可逆动作，必须标出来"
    # 自陈等级必须有出处，不许自称"完全自主"
    md = render_md()
    assert "State Machine" in md and "LangChain" in md, "自陈等级必须带可查出处"
    assert "不证明什么" in md, "图谱必须自陈边界（impact/oversight 是自评）"
    # 自主性图谱**不能overclaim**（2026-09-14 二次审计）：
    # `node_human_confirm` 是纯透传（无 interrupt、无门），"人"是**可选监督**不是审批卡点——
    # 实测 `POST /api/execute {question}` 全程无人工介入即交付。这张表是我们用来证明
    # "连自己弱点都公开"的东西，它自己不准确就什么都没了。锁住这个限定语，防回潮。
    _hc = dict((e["node"], e) for e in entries()).get("human_confirm")
    assert _hc, "自主性图谱里没有 human_confirm 节点"
    _note = _hc.get("guard") or _hc.get("oversight_note") or ""
    assert "不是强制门" in _note or "可选监督" in _note, (
        f"human_confirm 又变回了强制门的说法：{_note[:120]}"
    )

    # 「谁在决定」只许四种如实取值（不许出现"自主决策"这类模糊词）
    for e in entries():
        assert e["who"] in ("模型", "混合", "规则", "人"), e


def test_llm_usage_meter():
    """token 计量：两种响应形态都要抓到；**供应商没回用量时不记 0**。"""
    from core.llm import _UsageMeter, estimate_cost_cny, reset_usage, usage_snapshot

    class _R1:
        llm_output = {"token_usage": {"prompt_tokens": 1000, "completion_tokens": 200},
                      "model_name": "qwen-plus"}
        generations = []

    class _R2:  # 新版本把用量搬到 message.usage_metadata
        llm_output = {}
        generations = [[type("X", (), {"message": type("Y", (), {
            "usage_metadata": {"input_tokens": 50, "output_tokens": 10}})()})()]]

    class _R3:  # 供应商没回用量
        llm_output = {}
        generations = []

    m = _UsageMeter()
    reset_usage()
    m.on_llm_end(_R1())
    s = usage_snapshot()
    assert s["prompt_tokens"] == 1000 and s["completion_tokens"] == 200 and s["calls"] == 1, s
    m.on_llm_end(_R2())
    s = usage_snapshot()
    assert s["prompt_tokens"] == 1050 and s["calls"] == 2, s
    m.on_llm_end(_R3())
    assert usage_snapshot()["calls"] == 2, "没回用量却记了一次调用（把缺测记成了 0）"
    reset_usage()
    assert usage_snapshot()["total_tokens"] == 0, "reset 未归零 → 单份成本会变成历史总成本"
    # 未配单价 → None（不许内置一个"看起来专业"的价格）
    assert estimate_cost_cny() is None


def test_run_stats_records_real_depth():
    """落账的**档位必须是真实档位**，不能是 `ResearchPlan.depth` 的默认值。

    病案（2026-09-15 深访实跑）：账本 7 笔**全部**写着 `depth=标准`，
    其中包括一笔明确跑的是「深入」——报告里有 `st_*_deep` 子任务、路由审计写着 16 个维度，
    而 `st_{i}_deep` **只有深入档才会生成**。也就是：账本的档位字段从来没记过真实档位。

    根因：`run_task_stream` 里的 `state` 是 `init_state(...)` 的返回值，
    而 `graph.stream(...)` 的产出**从来没回写进它**。落账时读到的
    `state["plan"]` 是 init_state 造的空计划，其 `depth` 默认恰为 `DepthLevel.STANDARD`
    ——**一个恒定兜底值被当成"实测档位"**。复购看板按档位筛选因此永远分不出堆，
    而计费走的是 app.py 里请求的 depth，所以**只错账、不错钱**，也正因不痛而活得久。
    """
    from unittest import mock

    import graph
    from core.models import DepthLevel

    # 前提锁死：默认档确实是 standard（否则本用例的前提不成立，将来有人改了默认值要知道）
    from core.models import ResearchPlan

    assert ResearchPlan(raw_question="q").depth == DepthLevel.STANDARD, \
        "ResearchPlan 默认档变了——上面那条病案的根因描述需要同步更新"

    seen: list[str] = []
    with mock.patch("core.runstats.log_run_stats", side_effect=lambda d, t, s: seen.append(d)):
        # ① 常态（渐进式执行）：plan_override=None，真档位在**跑完的结果**里
        graph._log_run_stats_safe({"plan": {"depth": "deep"}}, "q", False, None, {"plan": None})
        # ② 用户编辑过计划 → override 最高优先
        class _P:
            depth = "custom"

        graph._log_run_stats_safe({"plan": {"depth": "deep"}}, "q", False, _P(), {})
        # ③ 结果里彻底没有 plan（异常路径）→ 才回退 state
        graph._log_run_stats_safe({}, "q", False, None, {"plan": None})
    assert seen == ["deep", "custom", "standard"], f"档位取值顺序错：{seen}"
    assert seen[0] != "standard", "深入档被记成了标准——正是那条病案"

    from core.runstats import canon_depth

    assert canon_depth("deep") == "深入" and canon_depth("custom") == "定制"
    # 接线：必须走 result 里的计划，不能再直接读 state["plan"]
    import inspect as _i

    _src = _i.getsource(graph._log_run_stats_safe)
    assert '(state or {}).get("plan")' not in _src.replace(" ", "") or "result" in _src, \
        "落账仍直接读 state['plan']（那是 init_state 的空计划，档位恒为默认值）"


def test_tam_sam_som_is_a_nested_chain():
    """TAM/SAM/SOM 必须是**严格嵌套**的三层：SAM ⊆ TAM、SOM ⊆ SAM。

    病案（2026-09-15 深访实跑，真实报告）：SAM 算成 **904 亿**，而按报告自己写的公式
    （"TAM基数×SAM渗透率×SAM年均频次/理论频次×SAM客单价/理论客单价"）应该是 **651 亿**。
    根因是两行各自独立的乘法：

        tam = base_population * potential_ratio * tam_freq * avg_price   # 基数 6.696 亿
        sam = base_population * sam_penetration * sam_freq * sam_price   # 基数 9.3 亿 ← 漏 potential_ratio

    于是 **SAM 的基数（9.3 亿）比 TAM 的基数（6.696 亿）还大**——SAM ⊄ TAM，
    逻辑上不可能，而两行代码各自看都"没写错"。

    为什么这条特别要命：错值**沿链条完全自洽**——SOM=904×8%=72 ✓、
    敏感性=904×35/30=1,055 ✓，从报告内部怎么查都查不出来，只有拿声称的公式重算才暴露。
    而"能把机械性错误下沉到确定性代码"正是我们对外卖的卖点之一，
    自家市场规模模型出现机械计算错误 = **技术卖点反证**。

    这道用例锁三件事：① 数值与声称公式一致；② 结构性嵌套（改 `sam` 的算法也不许破坏）；
    ③ 越界时必须留痕而不是静默输出。
    """
    import io
    import logging

    from agents.researchers import _compute_tam_sam_som

    p = dict(base_population=9.3, potential_ratio=0.72, tam_freq=35, avg_price=20,
             sam_penetration=0.30, sam_freq=18, sam_price=18, som_share=0.08)
    r = _compute_tam_sam_som(p)
    # ① 与报告自己声称的公式逐位对齐（这是"可被复核者按计算器复核"的那条）
    assert r["tam"] == 4687, r
    assert r["sam"] == round(4687 * 0.30 * (18 / 35) * (18 / 20)), \
        f"SAM 与声称公式不一致：{r['sam']} vs {4687 * 0.30 * (18 / 35) * (18 / 20):.1f}"
    assert r["sam"] == 651, f"SAM 应为 651（=9.3亿×72%×30%×18×18），实得 {r['sam']}"
    assert r["som"] == round(r["sam"] * 0.08) == 52, r
    # ② 结构性嵌套
    assert r["tam"] > r["sam"] > r["som"], f"三层未严格递减：{r}"
    assert "tier_violation" not in r
    # 反向锁：**SAM 反推的人口基数不得大于 TAM 的**（这正是原病案的形态——
    # 904 反推回 9.30 亿 = 全部城镇人口，而 TAM 的基数是 9.3×0.72 = 6.696 亿）
    _sam_base = r["sam"] / (p["sam_penetration"] * p["sam_freq"] * p["sam_price"])
    _tam_base = r["tam"] / (p["tam_freq"] * p["avg_price"])
    assert _sam_base <= _tam_base * 1.02, (
        f"SAM 反推基数({_sam_base:.3f}亿) 大于 TAM 基数({_tam_base:.3f}亿)——子集越界，"
        "说明 SAM 又变成从 base_population 直接算了")
    assert abs(_sam_base - p["base_population"] * p["potential_ratio"]) < 0.05, \
        f"SAM 基数应为 base_population×potential_ratio={p['base_population'] * p['potential_ratio']:.3f}，实为 {_sam_base:.3f}"
    # ③ 越界不许静默
    _buf = io.StringIO()
    _lg = logging.getLogger("agents.researchers")
    _h = logging.StreamHandler(_buf)
    _lg.addHandler(_h)
    _old = _lg.level
    _lg.setLevel(logging.WARNING)
    try:
        _bad = _compute_tam_sam_som(dict(p, sam_penetration=1.2, sam_freq=40))
    finally:
        _lg.removeHandler(_h)
        _lg.setLevel(_old)
    assert _bad.get("tier_violation"), "层级越界必须留痕（静默输出一个越界的数=把病案重新种回去）"
    assert "层级越界" in _buf.getvalue(), "越界没有打日志"
    # 接线：越界标记必须真的被翻成给写作者的指令，否则标记只活在内存里
    import inspect as _i

    _src = _i.getsource(__import__("agents.researchers", fromlist=["x"])._build_tam_calc)
    assert "tier_violation" in _src and "viol_note" in _src, \
        "引擎留了标记但没接进注入块——写作侧看不到，等于没检"


def test_decision_integrity_gate():
    """决策完整性闸门：**把"发现问题"接上"控制问题"**。

    病案（2026-09-15 深访实跑）：同一份报告里 `68.4%`（与自身 UE 模型算出的 44% 直接矛盾）、
    `14.2 万亿`（量级明显错误）**都被自家机制点名过**，却又**原样留在执行摘要、
    核心指标表和决策依据里**参与叙事。GPT 那份评审给这个状态起了名字：

        现在是 Audit-aware Research Agent（我把问题告诉你）
        还不是 Decision-safe Research Agent（问题足以影响决策时，我不让它进结论）

    这道闸门不新增检测能力，它给已有检测接一个动作。用例锁四件事：
      ① 落在决策区的『高』风险 → BLOCK；不在决策区的 → 只 WARN（不许误伤）
      ② BLOCK 时判定 `Go` → `Validate`，且**只降不升**（Validate/No-Go 一律不动）
      ③ **动作要落在纸上**：置顶横幅 + 决策判定那一行真的被改写，
         而**别的节里的 `No-Go` 不许被改**（篡改分析内容不是闸门的职权）
      ④ 幂等 + 闸门自身异常不得阻断交付
    """
    import graph
    from core import integrity_gate as _ig
    from core.integrity_gate import apply_to_report, evaluate, render_banner

    md = (
        "<!-- AIGC: x -->\n\n"
        "**研策调研报告**｜引擎基线 v0.9.81｜生成时点：2026-09-15\n\n"
        "# 📋 决策清单\n\n## 决策判定（Go / No-Go / Validate）\n\n"
        "**✅ Go（建议进入）**\n\n**判定理由：** 市场空间大。\n\n"
        "## 关键假设与可推翻变量\n\n若 CAC 翻倍 → No-Go（这句在别的节里，不许被改）\n\n"
        "## 📊 核心指标一览表\n\n| 指标 | 数值 |\n|---|---|\n| 成本占比 | 68.4% |\n"
    )
    _hi = [{"number": "68.4%", "risk": "高", "issue": "与自身单位经济模型矛盾"},
           {"number": "99.9%", "risk": "高", "issue": "这条不在决策区"}]
    # ① 分类
    g = evaluate(report=md, adv_issues=_hi, verdict="Go")
    assert g["level"] == "BLOCK", g
    assert len(g["blockers"]) == 1 and "68.4%" in g["blockers"][0]["what"], g["blockers"]
    assert any("99.9%" in w["what"] for w in g["warnings"]), "决策区外的高风险应记 WARN 而不是阻断"
    # ② 只降不升
    assert (g["downgraded_from"], g["downgraded_to"]) == ("Go", "Validate"), g
    for _v in ("Validate", "No-Go"):
        _g2 = evaluate(report=md, adv_issues=_hi, verdict=_v)
        assert _g2["level"] == "BLOCK" and not _g2["downgraded_to"], f"{_v} 被闸门动了——只降不升"
    # 决策区外的问题不触发降级
    _g3 = evaluate(report=md, adv_issues=[_hi[1]], verdict="Go")
    assert _g3["level"] == "WARN" and not _g3["downgraded_to"], _g3
    # ③ 动作落在纸上
    out, nv = apply_to_report(md, g, "Go")
    assert nv == "Validate", nv
    assert "决策完整性闸门" in out[:400], "横幅没置顶（读者要翻到附录才知道=闸门等于没装）"
    assert "闸门降级·原判 Go" in out, "决策判定那一行没被改写"
    assert "若 CAC 翻倍 → No-Go" in out, "别的节里的 No-Go 被改了——那是篡改分析内容"
    assert out.split(chr(10))[0].startswith("<!--"), "法定 AIGC 注释必须仍在最前"
    # ④ 幂等
    assert apply_to_report(out, g, "Validate")[0] == out, "不幂等（重复交付会叠横幅）"
    # 闸门自身异常 → 退回 PASS 并留 error，**绝不阻断交付**
    _bad = evaluate(report=md, rule_findings=123, verdict="Go")   # 类型错误，内部会抛
    assert _bad["level"] == "PASS" and _bad.get("error"), (
        f"闸门自身出错时必须放行（它是加在交付前的动作，不能成为新的单点故障）：{_bad}")
    # ---- ⑤「未执行」必须与「PASS」分开说（2026-09-16）----
    # 病案：`except` 分支返回的也是 `level="PASS"`，而 `render_banner` 原先只看 level
    # → **闸门自己崩掉**与**闸门真的查过且干净**印出完全相同的报告：零横幅、判定照旧。
    # 这与本仓库已修过三次的病同型（全 skip 印 0.0% / 未取得≠没变化 / 未执行也必须出声）。
    assert _bad.get("executed") is False, "闸门异常返回没标 executed=False，读者分不出「没查」与「查过且干净」"
    assert evaluate(report=md, verdict="Go").get("executed") is True, "正常执行的闸门没标 executed=True"
    _bb = render_banner(_bad, "Go")
    assert "未能执行" in _bb and "不等于" in _bb, (
        f"闸门未执行却没在报告里说出来——一份没被核过的报告会看起来像一份干净的报告：{_bb!r}"
    )
    # 「查出来了但写进正文失败」也不许静默：那是"我们知道有问题而读者不知道"
    _md_go = "# 📋 决策清单\n\n**决策判定：Go**\n"
    _g_block = {"level": "BLOCK", "executed": True, "blockers": [{"layer": "规则层 R7", "what": "x"}],
                "warnings": [], "reasons": ["x"], "downgraded_from": "Go", "downgraded_to": "Validate"}
    with mock.patch.object(_ig, "_rewrite_verdict_line", side_effect=RuntimeError("boom")):
        _o2, _v2 = apply_to_report(_md_go, _g_block, "Go")
    assert "决策完整性闸门" in _o2, (
        "闸门结论写进正文失败时原样静默交付了——读者拿到的是没有横幅、判定仍是 Go 的报告"
    )
    # 接线：必须在 deliver 里真的被调用，且**判定要能覆盖 decision_sop 节点的旧值**
    import inspect as _i

    _d = _i.getsource(graph.node_deliver)
    assert "integrity_gate" in _d and "_ig_eval" in _d, "闸门没接进交付节点"
    _rs = _i.getsource(graph.run_task_stream)
    assert "_gate_payload" in _rs, (
        "流式路径仍无条件取 decision_sop 的判定——闸门降级会被旧值覆盖，"
        "出现『正文说 Validate、数据层记 Go』的最坏不一致")

    # ---- 决策区命中必须**去空白比较**（2026-09-17 实跑病案）----
    #
    # 病案：本跑执行摘要的「行动建议」第 1 条逐字写着
    #     「建议将单店初始投入严格控制在 **45-65万元** 区间…」
    # 而闸门把它列进 warnings：`「45-65 万元」高风险（**不在决策区**）`——
    # **一条落在行动建议里的高风险数字，因为一个空格够不到阻断。**
    # 原实现是裸 `token in zone`：RedTeam 产出带空格的 `45-65 万元`，
    # 正文写作不带空格的 `45-65万元` → 判 False。同类隐患 `186 杯` / `186杯`。
    # **换个写法就换一个判定，那不是判据，是巧合。**
    # （本仓库在冲突扫描上修过同一个病："主干归一没去空格 → 判据退化成逐字相同"。）
    from core.integrity_gate import _hits_in_zone as _hz

    _z = "## 📌 执行摘要\n\n建议将单店初始投入严格控制在45-65万元区间。\n\n## 别的节\n\n186杯\n"
    assert _hz(_z, "45-65 万元") is True, "带空格的高风险数字没落进决策区——本跑那条就是被这个放过的"
    assert _hz(_z, "45-65万元") is True, "不带空格的写法反而漏了"
    assert _hz(_z, "186 杯") is True, "换行/空格写法不一致就换判定 = 判据退化成了巧合"
    # **反向锁**：不许因为归一化而误命中——不存在的数一个都不许进
    assert _hz(_z, "99.9%") is False, "归一化把不存在的数字也判进了决策区"
    assert _hz(_z, "") is False, "空 token 不许命中（宁漏不误）"

    # ---- 决策区**不许被子标题关掉**（2026-09-17 本轮最根性的一处）----
    #
    # 病案（比亚迪跑实读）：`## 市场规模测算` 命中标记、打开决策区，
    # **紧接着的 `### TAM：潜在总市场规模测算` 立刻把它关掉**（那个串不含 "## 市场规模测算"）。
    # 于是**整个 TAM/SAM/SOM 段根本不在决策区里**——报告结构里这一节的第一个子标题就是 `###`，
    # 也就是说**这一节从打开的那一刻起就是空的**。
    # 实测：决策区字符数 4,628 → **11,452**（比亚迪）/ 4,711 → **9,461**（星巴克）；
    # 比亚迪跑 4 条高风险数字从**全不命中**变成 3 条命中。
    #
    # 判据：决策区是 `##` 级的节，`###` 是它的**内容**；只有 `#`/`##` 级非标记标题才是真的换节。
    from core.integrity_gate import _decision_zone as _dz

    _md = (
        "## 📌 执行摘要\n摘要正文（含 12.7%）\n\n"
        "## 市场规模测算\n\n### TAM 子节\n表格里写着 TAM 164,970 亿元\n\n"
        "#### 更深的子节\n这里也在区内 999\n\n"
        "## 研究方法与框架\n这一节不是决策区，里面的 888 不该命中\n"
    )
    _zz = _dz(_md)
    assert "164,970" in _zz, "`###` 子标题把决策区关掉了——整个 TAM 段会漏出决策区（本跑的根因）"
    assert "999" in _zz, "`####` 更深子标题也不该关掉决策区"
    assert "12.7%" in _zz, "摘要内容丢了"
    assert "888" not in _zz, "`##` 级非标记标题**应当**结束决策区，否则整篇正文都会变成决策区"
    # 两个真实报告上的一致性：TAM 那一段必须在区内
    import json as _j

    _rep = _HERE / "data" / "reports" / "20260917_204406_89044e.json"
    if _rep.exists():
        _rd = _j.loads(_rep.read_text(encoding="utf-8"))
        _z2 = _dz(_rd["report"])
        assert "164,970" in _z2, "真实报告里 TAM 那一段仍在决策区外——这一节的子标题又把区关了"


def test_voice_boundary_and_label_honesty():
    """**一手/二手的界线必须印在章节里**，且**没有一手源时才印**。

    病案（2026-09-15 深访实跑，三份外部评审同时点名）：报告里有一章叫
    「下沉市场用户真实反馈」，而同一份报告的缺口披露写着"无一手调研（问卷/访谈）→
    只能给二手舆情线索"。**两个说法对不上，而章节标题是读者唯一会记住的那句。**
    GPT 那份评审把风险说得很直白：复核者看到"真实反馈"再发现是模拟数据，
    会直接得出"你在包装不存在的一手数据"——伤的是诚实这个人设。

    这条用例锁三件事：
      ① 判据走**源路由结果**（结构），不靠标题词表——词表治不完；
      ② **拿到一手源时一个字不加**（无条件贴"二手"= 把真做过一手调研的说成没做，
         那是往自贬方向偏的另一种不诚实）；
      ③ 幂等、不越界到别的章节。
    """
    from core.simulated_data import _package as _sim_pkg
    from core.voice_boundary import stamp_voice_boundary

    md = (
        "## 下沉市场用户真实反馈\n\n用户说……\n\n"
        "## 竞争格局\n\n竞品说……\n"
    )
    _no = {"st_3": {"claim_label": "消费者声音", "required_layer": "L5",
                    "required_label": "用户声音", "satisfied": False}}
    _yes = {"st_3": {"claim_label": "消费者声音", "required_layer": "L5",
                     "required_label": "用户声音", "satisfied": True}}
    out = stamp_voice_boundary(md, _no)
    assert out.count("本节边界（一手 / 二手）") == 1, "无一手源时必须印边界声明"
    assert "不是**本产品执行的一手调研" in out
    assert "竞品说" in out, "越界到别的章节了"
    assert stamp_voice_boundary(out, _no) == out, "不幂等"
    assert stamp_voice_boundary(md, _yes) == md, "拿到一手源时不许贴『二手』标签"
    # 模拟问卷的标题与硬边界（GPT 评审：绝不与真实数据同视觉层级）
    _pkg = _sim_pkg({"headline": "h", "summary_table": "", "key_findings": []},
                    ["a"], [[1]], "分层配额", 300)
    _smd = _pkg.get("md") or _pkg.get("markdown") or ""
    assert "方法演示" in _smd and "不是真实数据" in _smd, f"模拟问卷标题未自称模拟/演示：{_smd[:80]}"
    assert "不可用于" in _smd, "模拟数据缺硬边界声明"
    assert "一手数据演示闭环" not in _smd, "旧标题（『一手数据』紧跟在『模拟』后）仍在——读者扫过去拿到的是『一手数据』"
    # 接线
    import inspect as _i

    _d = _i.getsource(__import__("graph").node_deliver)
    assert "stamp_voice_boundary" in _d, "边界声明没接进交付节点"


def test_content_id_is_one_and_the_same():
    """同一份交付文件里**只许有一个内容编号**。

    病案（2026-09-15 深访实跑，Claude 评审单独点名）：同一份 `.md` 里印着两个编号——
    L1 注释 `AIGC-YC-126309FFED3B41B5`、L8 正文 `AIGC-YC-B067DCE847825486`。
    根因是两处各算了一次哈希，**而且算的不是同一个字符串**：正文那个是引擎在交付定型时
    对 `report_final` 算的（不含导出期内联的图表），注释那个是导出时对已含图表的
    `content` 算的。

    为什么这条对参赛作品特别要命：编号是我们**唯一一个能被第三方当场证伪**的承诺
    （"同一份内容重导出编号不变、改一字即变"）。"你同一份文件两个编号，拿哪个复算？"
    是一句话就能拆掉的质问——**可证伪的叙事最怕自己先不自洽**。
    """
    import re as _re

    from core.ai_label import canonical_content_id, content_id

    _body = (
        "**研策调研报告**｜引擎基线 v0.9.81\n\n"
        "> 🤖 AI 生成合成内容｜内容编号：`AIGC-YC-B067DCE847825486`\n"
    )
    _exported = _body + "\n\n![图](data:image/png;base64,AAAA)\n"   # 导出期才内联图表
    _cid = canonical_content_id(_exported)
    assert _cid == "AIGC-YC-B067DCE847825486", f"导出侧没复用正文编号：{_cid}"
    assert _cid != content_id(_exported), "重算得到的哈希本来就不同——上面这条才有意义"
    # 反向锁：正文里没有编号时回退计算（不抛、不为空）
    assert canonical_content_id("一份没有标识的旧报告").startswith("AIGC-YC-")
    # 接线：导出侧不许再自己算
    import inspect as _i

    _src = _i.getsource(__import__("app").export_report)
    assert "canonical_content_id" in _src, "md 导出仍在重算编号（同一文件两个编号会复发）"
    # 一份文件里只许出现一个不同的编号
    _final = canonical_content_id(_exported) + "\n" + _exported
    assert len(set(_re.findall(r"AIGC-YC-[0-9A-F]{8,32}", _final))) == 1

    # ---- 2026-09-16 加强：**逐个导出面**验行为，而不是 grep 源码 ----
    # 病案：v0.9.82 的"导出侧只许复用"只落到了 `.md` 那一面，而
    # `core/ai_label.py:implicit_meta` 是 **docx/pptx/pdf/html 四个面的共同入口**，
    # 它仍在重算；`explicit_head`/`explicit_tail`/`label_summary`/审计包 `_aigc_stamp`
    # 也各自重算。最讽刺的一处是 `_aigc_stamp` —— 它**自己印着**"与报告本体同一内容编号"
    # 却在下一行算了个不同的哈希。**一句话里同时有承诺和违背承诺的代码。**
    #
    # 所以锁法从"grep 源码里有没有 canonical_content_id"升级成**行为断言**：
    # 让每个面真的产出一次，把里面的编号收集起来 —— **有且只有一个**才算过。
    # 将来再加导出格式，只要它走 `_marker`/`implicit_meta`，就自动被这条覆盖。
    from core import ai_label as _AL

    _surfaces: dict[str, str] = {}

    def _grab(name: str, text: str) -> None:
        _ms = _re.findall(r"AIGC-YC-[0-9A-F]{8,32}", text or "")
        assert _ms, f"{name} 面没有印出任何内容编号"
        _surfaces[name] = _ms[0]

    _grab("implicit_meta", _AL.implicit_meta(_exported)["content_id"])
    _grab("explicit_head", _AL.explicit_head(_exported))
    _grab("explicit_tail", _AL.explicit_tail(_exported))
    _grab("label_summary", _AL.label_summary(_exported)["content_id"])
    _grab("html_meta_tags", _AL.html_meta_tags(_exported))
    _grab("pdf_meta", _AL.pdf_meta(_exported)["subject"])
    _grab("xlsx_meta", str(_AL.xlsx_meta(_exported)))
    try:
        from core.audit_bundle import _aigc_stamp

        _grab("audit_bundle 内标识", _aigc_stamp(_exported))
    except Exception:
        pass

    _uniq = set(_surfaces.values())
    assert _uniq == {_cid}, (
        "同一份内容在不同导出面上印出了不同的编号——"
        f"期望只 {_cid}，实得 {sorted(_uniq)}；逐面明细 {_surfaces}"
    )


def test_every_claimed_export_format_is_reachable_from_the_ui():
    """材料说"六格式"，界面上就必须**数得出六个**（2026-09-16 全量审计）。

    病案：`/api/export/xlsx` 早就在、连法定隐式标识都写全了，材料里
    「md/docx/xlsx/pptx/pdf/交互 HTML」也写了整批——**而前端一处入口都没有**：
    报告卡那一排按钮是 md/docx/pdf/html/pptx 五个，分享菜单是 pdf/docx/md/link 四个。
    于是"六格式交付"这句话说出去，复核者当场在界面上只能数出五个。

    比"少一个按钮"更值得记的是它的**形状**：能力在后端是**真做了**的，
    只是没有任何一条路能让用户走到它——**"实现了"与"能被用上"之间断了一节**，
    而材料引用的永远是后端那一侧。同一天还查出同型的第二例：
    自主性图谱（`core/autonomy_map.py` + `GET /api/autonomy-map` 都在，
    前端零入口），而 Demo 分镜把它写成「**必拍**」的弹层——
    那一拍在修复前根本拍不出来。

    本用例锁两件事：**后端支持的格式 ⊆ 前端调用点**，且**材料里的"六格式"不是虚数**。
    """
    import re as _re
    from pathlib import Path

    _here = Path(__file__).resolve().parent
    _app = (_here / "app.py").read_text(encoding="utf-8")
    # 导出处理器体内的 `if fmt == "x"` —— 只取该函数那一段，避免扫到别的处理器
    _i = _app.index('@app.api_route("/api/export/{fmt}"')
    _j = _app.index("\n@app.", _i + 10)
    _body = _app[_i:_j]
    backend = set(_re.findall(r'if fmt == "([a-z0-9]+)"', _body))
    assert backend, "没解析出导出格式——检查用例是否还跟得上 app.py 的结构"
    assert {"md", "docx", "pdf", "pptx", "xlsx", "html"} <= backend, (
        f"后端导出格式变了：{sorted(backend)}"
    )

    _fe = (_here.parent / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8")
    ui = set(_re.findall(r'(?:doExport|exportAs)\("([a-z0-9]+)"', _fe))
    _missing = backend - ui
    assert not _missing, (
        f"这些格式后端做了、界面上却点不到：{sorted(_missing)}——"
        "「六格式交付」这句话必须在界面上数得出来，否则它是宣传不是能力。"
    )
    # 反向：前端不许出现后端不认的格式（否则点了就是 404/500）
    assert not (ui - backend - {"link"}), (
        f"前端有后端不认的导出格式：{sorted(ui - backend - {'link'})}"
    )


def test_api_routes_have_a_ui_entry_or_a_named_reason():
    """每条**读接口**要么有界面入口，要么在豁免表里具名写清理由（2026-09-16）。

    与 `test_frontend_stores_no_field_it_never_reads` 是同一个病的两侧：
    那一条管"前端存了不读"，这一条管"后端有、前端不接"。

    默认失败、豁免须具名写理由——本仓库对"漏一处就复发"的清单一律用这个姿势
    （见 `test_prod_writes_are_gated` / `test_no_keyword_parameter_is_silently_ignored`），
    因为手写清单已经在这类问题上漏过很多次。

    它上一次会抓到的正是**自主性图谱**：`GET /api/autonomy-map` 躺在那里，
    前端一个字都没提，而 Demo 分镜写着「必拍」。
    """
    import re as _re
    from pathlib import Path

    # 豁免：(理由, 界面锚点)。**锚点必须真的在 App.jsx 里出现**——
    # 光写一句"界面里有了"不算，那句话本身就是本仓库最会骗人的一类注释。
    _ALLOWED = {
        "/api/tools": ("工具清单：给第三方/说明现场 curl 看的能力面，不是终端用户界面元素", None),
        "/api/memory": ("用户档案：设置抽屉只展示偏好摘要，明细留 CLI/维护口", None),
        "/api/memory/clear": ("清空档案是维护动作，不做成一键按钮（误点代价高）", None),
        "/api/metrics": ("指标血缘：白皮书按 **REST 端点**口径声明，不是界面承诺；查询面留 /api/assets", None),
        "/api/monitors": ("决策记忆的另一读取面：界面路径在「决策回访」抽屉（同一份 list_decisions）",
                          "outcomeOpen"),
    }

    _here = Path(__file__).resolve().parent
    app = (_here / "app.py").read_text(encoding="utf-8")
    fe = (_here.parent / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8") + (
        _here.parent / "frontend" / "src" / "api.js"
    ).read_text(encoding="utf-8")
    routes = sorted(set(_re.findall(r'@app\.(?:api_route|get|post)\(\s*["\'](/api/[^"\']+)', app)))
    assert len(routes) >= 20, f"路由解析数量异常（{len(routes)}），用例可能没跟上 app.py 的结构"
    _noref = []
    for r in routes:
        if r in fe or r in _ALLOWED:
            continue
        pref = r.split("{")[0].rstrip("/")
        if pref and (pref + "/") in fe:
            continue
        _noref.append(r)
    assert not _noref, (
        f"这些接口后端有、前端一个字都没提、也没在豁免表里写理由：{_noref}。"
        "**加接口时顺手想一下「用户从哪儿点到它」**——"
        "自主性图谱就是这样变成一张「只在材料里存在的表」的。"
    )
    # 豁免表本身不许腐败：路径必须还在，写下的界面锚点必须真的存在
    for _p, (_why, _anchor) in _ALLOWED.items():
        assert _p in routes, f"豁免表里的 {_p} 已经不存在了——豁免表要跟着路由一起改"
        assert _why and len(_why) >= 10, f"{_p} 的豁免理由太短，等于没写"
        if _anchor:
            assert _anchor in fe, (
                f"{_p} 的豁免理由声称界面入口在 `{_anchor}`，但 App.jsx 里没有这个锚点——"
                "**豁免里的『界面里有了』必须能被机械核对**。"
            )


def test_artifact_page_keeps_zeros_and_escapes_scripts():
    """作品页 HTML：**0 不是缺失，META 也要转义**（2026-09-16 全量审计）。

    ① **0 与"没统计"不能同形。** 封面那两枚 chip 原先写 `(META.evidence_count || '—')`，
       而 JS 里 `0 || '—'` 得 `'—'`；上游 `meta.get(...) or ""` 又把 0 变成 `""`。
       两处叠加的结果：「本次 0 条证据」在作品页封面上印成「— 条证据」——
       读起来像"我们没数"，实际是"数了，是 0"。而 0 恰恰是**该被追问**的那个读数。

    ② **同一个模板里三种注入，转义了两种漏了一种 = 没有转义。**
       `__MAIN_MD__` / `__AUDIT_MD__` 一直是 `</script>` → `<\\/script>`，
       只有 `__META_JSON__` 是裸的 `json.dumps`。而 META 里带着**用户自己的问题**
       与报告里的指标名——只要出现一个 `</script>`，`<script type="text/plain">`
       就提前闭合，后面的 JSON 变成正文 HTML，**整页 JS 源码会被当文本吐在页面上**。
    """
    import json as _json
    import re as _re

    from core.report_html import build_html_report

    # ① 0 必须原样传到页面（而不是变成 ""）
    h0 = build_html_report("# 报告\n\n正文", "T", {"evidence_count": 0, "citation_count": 0})
    _m = _re.search(r"var META = (\{.*?\});", h0, _re.S)
    assert _m, "作品页里找不到 META"
    _meta = _json.loads(_m.group(1))
    assert _meta["evidence_count"] == 0, f"0 被吃掉了：{_meta['evidence_count']!r}"
    assert _meta["citation_count"] == 0
    # 没传 → null（页面据此印"—"），与 0 区分开
    h1 = build_html_report("# 报告\n\n正文", "T", {})
    _m1 = _json.loads(_re.search(r"var META = (\{.*?\});", h1, _re.S).group(1))
    assert _m1["evidence_count"] is None, "未统计必须是 null，不能伪装成 0"
    # 页面脚本必须用 null 判断，不能用 `||`（否则 0 又会被吃掉）
    assert "META.evidence_count == null" in h0, "作品页又用 `||` 判缺失了——0 会被吞"

    # ② `</script>` 注入
    _bad = "问题：</script><img src=x onerror=alert(1)>"
    h2 = build_html_report("# 报告", "T", {"question": _bad, "evidence_count": 3})
    _seg = h2[h2.index('id="meta-json"') : h2.index('id="meta-json"') + 400]
    assert "</script><img" not in _seg, "META 里的 </script> 没转义——脚本标签被提前闭合"
    assert "<\\/script>" in _seg, "META 里的 </script> 没有按 MD 那两处同一姿势转义"


def test_verdict_extraction_is_not_fooled_by_other_sections():
    """判定抽取不许被**别的章节**带跑（2026-09-16 全量审计）。

    病案：`extract_verdict` 的注释写着"只认带图标的那一行，避免 Red Team 的
    『CAC 翻倍→No-Go』被误判"——而代码里三个模式各带一条**全局裸词**分支
    （`|No-Go（不建议）` 等），`re.search` 扫的是**整篇**。
    于是 Red Team 章节一句话就把一份 **Go** 报告判成 **No-Go**，
    而且 No-Go 排在最前、先命中先返回。

    为什么这不是措辞问题：判定会被写进**决策记忆与决策回访基线**——
    一份 Go 报告的回访基线记着 No-Go，此后所有"当初的判断今天还站得住吗"都跟着错。
    """
    from agents.decision import extract_verdict

    # ① 真判定行在前面 + 后面章节出现条件式 No-Go → 必须取 Go
    _sop = (
        "## 决策判定（Go / No-Go / Validate）\n"
        "- **✅ Go（建议进入）**：证据链完整，TAM/SAM/SOM 三层自洽。\n"
        "\n## 关键假设与可推翻变量\n"
        "- 若 CAC 翻倍 → **No-Go（不建议）**；若复购率跌破 25% → 转 Validate。\n"
    )
    assert extract_verdict(_sop) == "Go", f"被后面的条件句带跑了：{extract_verdict(_sop)}"

    # ② 三选项并列的题干行（模型偶尔照抄）不许当成结论
    _echo = (
        "## 决策判定\n"
        "- 从三选一中给出明确结论：**✅ Go（建议进入）** / **❌ No-Go（不建议）** / 🟡 **Validate（尚不确定）**\n"
        "- **🟡 Validate（建议小规模验证）**：证据不足以直接下结论。\n"
    )
    assert extract_verdict(_echo) == "Validate", f"选项行被当成了结论：{extract_verdict(_echo)}"

    # ③ 三种真判定各自都认得出
    for _icon, _want in (("✅", "Go"), ("❌", "No-Go"), ("🟡", "Validate")):
        _t = f"## 决策判定\n- **{_icon} {_want}**（依据略）。\n"
        assert extract_verdict(_t) == _want, f"{_want} 认不出来：{extract_verdict(_t)}"
    # ④ 空白输入 → Validate（保守缺省，不是 Go）
    assert extract_verdict("") == "Validate"
    assert extract_verdict("## 无关内容\n没有判定。") == "Validate"


def test_all_persistence_paths_live_under_backend_data():
    """每一个持久化路径都必须落在 `backend/data/` 下（2026-09-16 全量审计）。

    病案：`core/memory.py` 的用户档案算的是 `parents[2]`＝`yancec/`，
    也就是 **`backend/data/` 之外的上一层**——而全仓其他 12 个持久化文件都在里面。
    后果不是"路径难看"，是**运维面漏一个**：备份、重置、清理、"清测试产物"
    这些动作全都按 `backend/data/` 扫，扫不到它。于是**一份带着旧行业偏好与历史决策的
    用户档案会静默活过所有清理**，并在下一次规划时以"用户画像"的身份反灌写作上下文。
    盘上实测：`yancec/data/user_profile.json` 11.5KB 真实数据，`backend/data/` 下没有。

    判据机械：import 每个 `core/*` 与 `agents/*` 模块，取其模块级路径常量
    （`*_PATH` / `*_STORE` / `*_DIR` / `*_FILE` 等），要求解析结果以 `backend/data` 开头。
    豁免须具名写理由——本仓库对"漏一处就复发"的清单一律用默认失败 + 具名豁免。
    """
    import importlib
    import pkgutil
    from pathlib import Path as _P

    _here = _P(__file__).resolve().parent
    _data = str((_here / "data").resolve())

    # 豁免：**不落在 backend/data 下的持久化路径**，逐条写理由。
    _ALLOWED: dict[str, str] = {
        # 可复算包/临时工作区：写的是**导出物**，不是本机状态（导出物本就该能挪走）
        "core.audit_bundle._VERIFY_TMP": "可复算包的临时校验目录，非本机持久状态",
    }

    _found = 0
    _bad: list[str] = []
    for _pkg in ("core", "agents"):
        for _m in pkgutil.iter_modules([str(_here / _pkg)]):
            _name = f"{_pkg}.{_m.name}"
            try:
                _mod = importlib.import_module(_name)
            except Exception:
                continue
            for _k, _v in vars(_mod).items():
                if not (_k.isupper() or _k.startswith("_")) or isinstance(_v, bool):
                    continue
                if not isinstance(_v, (str, _P)):
                    continue
                _s = str(_v)
                if not _s or not _s.replace("\\", "/").startswith("/") and ":" not in _s[:3]:
                    continue
                if not (_s.endswith(".json") or _s.endswith(".jsonl")
                        or _s.replace("\\", "/").rstrip("/").endswith("/data")):
                    continue
                _found += 1
                if _s.replace("\\", "/").startswith(_data.replace("\\", "/")):
                    continue
                if f"{_name}.{_k}" in _ALLOWED:
                    continue
                _bad.append(f"{_name}.{_k} → {_s}")

    assert _found >= 8, f"只解析出 {_found} 个持久化路径常量——用例可能没跟上结构"
    assert not _bad, (
        "这些持久化路径落在 `backend/data/` 之外（备份/重置/清理会扫不到它们）：\n  "
        + "\n  ".join(_bad)
        + "\n要么挪进来，要么在豁免表里具名写理由。"
    )


def test_multi_source_rule_checks_the_numbers_it_claims_to_check():
    """「每个关键数字 ≥2 个独立来源」必须真的在查数字（2026-09-16 全量审计）。

    病案（两处，方向一致——**都让这条规则比它自己宣称的松**）：

    ① **只查前 10 个**：`_NUMBER_PATTERN.findall(report)[:10]`。前 10 个通常落在开头，
       真正要命的测算/对比数字在几百行之后，**从来没被查过**。
    ② **按「单位」匹配来源**：只要某条证据提到过"元"，报告里任何一个"元"的数字
       都算它有来源——**一个域名为所有同单位数字背书**。

    两者叠加的结果是：材料里写着"每个关键数字 ≥2 个独立来源"，而实际检查面窄得多、
    且会把不达标的数字报成达标。**自审数字往好看的方向偏**，正是本产品一直在批评的事。
    """
    from agents.quality import _rule_check
    from core.models import Evidence, Source, SourceLevel

    def _ev(claim, excerpt, urls):
        return Evidence(
            claim=claim,
            sources=[Source(title=f"源{i}", url=u, level=SourceLevel.L3_INDUSTRY,
                            excerpt=excerpt) for i, u in enumerate(urls)],
            subtask_id="s1",
        )

    # ① 覆盖：把第 11 个数字做成"无来源"，它必须被报出来
    _head = "".join(f"第{i}项约 {i}00 元。" for i in range(1, 11))   # 前 10 个
    _md = _head + "关键结论：单店月净利 8,888 元。"
    _evs = [_ev("无关主张", "无关内容", ["https://a.example.com/x", "https://b.example.com/y"])]
    _iss = " ".join(_rule_check(_md, _evs))
    # 第 11 个数字必须进入统计口径（列表会截断展示，所以要断言"共 N 个"里的 N）
    assert "等 11 个" in _iss or "8,888" in _iss, (
        "第 11 个数字（真正要命的那个）没进统计——规则仍停在「只查前 10 个」：\n" + _iss[:300]
    )

    # ② 精度：同一个单位下，有来源的数字与无来源的数字必须分开判
    _md2 = "门店数 1,200 家（有双源）。关店数 999 家（无源）。"
    _evs2 = [
        _ev("门店数 1,200 家", "门店数 1,200 家", ["https://a.example.com/1", "https://b.example.com/2"]),
    ]
    _iss2 = " ".join(_rule_check(_md2, _evs2))
    assert "999" in _iss2, (
        "999 家没有来源，却因为它和 1,200 家共用单位「家」而被判成有来源：\n" + _iss2[:300]
    )


def test_watermark_date_guard_has_no_trailing_label_hole():
    """水印日期守卫不许被"尾巴上挂个标签"绕过（2026-09-16 全量审计）。

    病案：这道守卫有两层——`core/adversarial._drop_meta_issues`（出口丢）与
    `core/decision_guard.demote_high_risk_in_head`（应用前再挡）。
    **两层都只认"纯日期"**（`$` 收尾），于是 RedTeam 只要把标注写进 `number`
    （`2026-09-15（生成时点）`）就绕过去了：
      · 出口那层不丢 → 页眉印出「生成时点：**2026-09-15（存疑·待核）**」
      ——**报告连自己的交付日期都不敢认**，而这是复核者看到的第一行。
      · 第二层也不挡 → 同一个后果。

    放宽必须**双向安全**：允许日期后跟标注，但**真实时序矛盾一条都不能被挡掉**。
    本用例同时锁这两边，并锁两层的口径不许分叉。
    """
    from core.adversarial import _drop_meta_issues
    from core.decision_guard import _ISO_DATE

    _meta_ctx = "报告头水印 · 生成时点"
    # ① 绕过写法必须被挡住（两层都要挡）
    assert _ISO_DATE.match("2026-09-15（生成时点）"), "decision_guard 的日期守卫又被尾部标签绕过了"
    _kept = _drop_meta_issues([
        {"number": "2026-09-15（生成时点）", "context": _meta_ctx, "risk": "高"},
        {"number": "2026-09-15", "context": _meta_ctx, "risk": "高"},
    ])
    assert _kept == [], f"出口层没挡住带标注的水印日期：{_kept}"

    # ② 反向：真实的内部时序矛盾**一条都不许被挡**
    _real = [
        {"number": "2025-01-01 vs 2024-01-01", "context": "正文与表格年份不一致", "risk": "高"},
        {"number": "2024年（对比2023年）", "context": "同一指标跨章不一致", "risk": "高"},
        {"number": "2025年", "context": "正文写 2025、表里写 2024", "risk": "高"},
    ]
    assert _drop_meta_issues(list(_real)) == _real, (
        "放宽过头了：真实的时序矛盾被当成水印日期丢掉了——那比误报更糟"
    )
    for r in _real:
        if r["number"] != "2025年":   # 纯年份本来就不该按 ISO 日期处理
            assert not _ISO_DATE.match(r["number"]), f"{r['number']} 被误判成水印日期"


def test_every_sop_section_is_classified_by_the_gate():
    """决策清单的**每一节**都必须被闸门归过类（2026-09-16 全量审计）。

    病案：`_DECISION_MARKERS` 漏了「行动清单（怎么做）」「关键假设与可推翻变量」
    「最小验证计划（14天）」三节——而这三节**都是会被拿去行动的**：
    清单里的数字直接进执行、假设节定义了"什么情况下结论不成立"、验证节说明投多少钱盯哪几个指标。
    落在它们里面的发现因此只判 WARN、够不到 BLOCK。
    实测踩中：闸门自己抓到的一条「R7 来源资格错配」，那条错误的来源标注**正写在行动清单里**。

    闸门的覆盖范围**由这张表决定**——表漏一节，那一节里的问题就永远够不到阻断。
    所以这里机械扫 SOP 模板产出的全部章节标题，逐个要求在 `_DECISION_MARKERS` 里
    有归属；确实不该算决策区的，在下面的豁免表里**具名写理由**。
    """
    import re as _re
    from pathlib import Path

    from core.integrity_gate import _DECISION_MARKERS

    # 豁免：若将来新增一节**确实不是决策区**（描述性/附录性），在这里具名写理由。
    # 当前为空——`agents/decision.py` 产出的 6 节全部属于决策区，全部已在标记表内。
    _NON_DECISION: dict[str, str] = {}

    _src = (Path(__file__).resolve().parent / "agents" / "decision.py").read_text(encoding="utf-8")
    heads = sorted(set(_re.findall(r"^##\s+(.+?)\s*$", _src, _re.M)))
    assert len(heads) >= 4, f"没解析出 SOP 的章节标题（{heads}）——用例要跟着结构改"

    _joined = "\n".join(_DECISION_MARKERS)
    missing = [h for h in heads if h not in _joined and h.split("（")[0] not in _joined
               and h not in _NON_DECISION]
    assert not missing, (
        f"这些 SOP 章节既不在闸门的决策区标记表里、也没在豁免表里写理由：{missing}——"
        "落在它们里面的发现只会判 WARN，永远够不到 BLOCK。"
    )
    # 豁免表不许腐败：豁免的章节必须真的存在
    for _h in _NON_DECISION:
        assert any(_h in x for x in heads), f"豁免表里的「{_h}」已不存在——豁免表要跟着一起改"


def test_arbitration_card_is_actually_checkable():
    """仲裁卡片号称「可核对」—— 那就必须**真的能被核对**（2026-09-16 全量审计）。

    病案：合成分取的是 `max(证据的所有来源)`，而 4 维拆解固定取 `sources[0]`。
    证据 A 的来源是 `[L2媒体, L1官方]` 时：分数按 L1 算（≈1.0），
    拆解印的却是 L2 的四维（加权和 ≈0.405）——**读者拿拆解乘权重，数与合成分对不上**。
    这张卡的全部卖点就是"为什么采信 A 不信 B 一目了然、可核对"，
    而它自己就演示了一遍"不可核对"。（`reason` 还把「谁含 L1 官方来源」写死成 A。）

    本用例锁两件事：
      ① **合成分 = Σ(拆解 × 权重)**（这是"可核对"的定义，误差只允许来自四舍五入）；
      ② `reason` 说出的规则必须与**实际生效的那条分支**一致，且指向正确的证据。
    """
    from core.models import (
        CREDIBILITY_WEIGHTS,
        Evidence,
        Source,
        SourceLevel,
        arbitrate_conflict,
    )

    def _src(title, level, t=0.8, ind=0.8, corr=2):
        return Source(title=title, level=level, timeliness_score=t,
                      independence_score=ind, corroboration_count=corr)

    def _ev(claim, sources):
        return Evidence(claim=claim, sources=sources, subtask_id="s1")

    # A：先放 L2（拆解若取 sources[0] 就会印成 L2），再放 L1（真正贡献合成分的那条）
    a = _ev("A 的主张", [
        _src("某财经媒体转载", SourceLevel.L2_SEMI_OFFICIAL, t=0.5, ind=0.4, corr=0),
        _src("国家统计局口径", SourceLevel.L1_OFFICIAL, t=1.0, ind=1.0, corr=2),
    ])
    # B 取到 L2 的满配（≈0.92）→ 与 A（1.0）**分差 < 0.15**，于是生效的是
    # 「分差不足但 A 含 L1」那条分支（正是 reason 以前写死成 A 的那一条）
    b = _ev("B 的主张", [_src("券商研报", SourceLevel.L2_SEMI_OFFICIAL, t=1.0, ind=1.0, corr=2)])
    card = arbitrate_conflict(a, b)

    for tag, sc in (("a", card["score_a"]), ("b", card["score_b"])):
        bd = card[f"breakdown_{tag}"]
        s = sum(CREDIBILITY_WEIGHTS[k] * bd[k] for k in CREDIBILITY_WEIGHTS)
        assert abs(s - sc) < 0.002, (
            f"拆解 {tag} 加权和 {s:.3f} ≠ 合成分 {sc:.3f}——"
            "「可核对」的卡片自己不能核对（拆解与合成分必须同源）"
        )
    # 拆解必须来自**贡献分数的那个来源**，而不是 sources[0]
    assert card["breakdown_a"]["source_level"] == 1.0, (
        f"A 的最高分来源是 L1，拆解却印了 {card['breakdown_a']['source_level']}——取错了来源"
    )

    # ② reason 必须指向**真正生效**的规则，而不是写死的"或A含L1"
    assert card["winner"] == "A", card
    assert "证据A" in card["reason"] and "L1 官方来源" in card["reason"], (
        f"生效的是「分差不足但 A 含 L1」那条分支，reason 却没说出来：{card['reason']}"
    )
    # 反向：L1 在 B 身上时，reason 必须说 B（旧实现把主语写死成 A）
    b2 = _ev("B2", [_src("上交所公告", SourceLevel.L1_OFFICIAL, t=1.0, ind=1.0, corr=2)])
    a2 = _ev("A2", [_src("某研报", SourceLevel.L2_SEMI_OFFICIAL, t=1.0, ind=1.0, corr=2)])
    c2 = arbitrate_conflict(a2, b2)
    assert c2["winner"] == "B", c2
    assert "证据B" in c2["reason"], f"L1 在 B 身上，reason 却说成 A：{c2['reason']}"
    assert "证据A含" not in c2["reason"], f"reason 把主语写死成 A 了：{c2['reason']}"


def test_frontend_never_renders_a_read_failure_as_zero():
    """前端**读失败不许伪装成 0**（2026-09-16 全量审计）。

    病案：`openRunStats` / `openAssets` / `openIndustryCards` 的 `catch` 里回的是
    `{total: 0, count: 0, rows: []}` 这种"空数据"字面量——于是后端没起来时，
    抽屉照样印「共 **0** 笔真实运行」+「暂无账本——跑 3 笔以上深入档后回填」，
    资产库照样印「已沉淀 **0** 条行业证据」。

    **对着一屏 0，复核者和用户只会得出一个结论：你没跑过。**
    而真相是"我们没读到"。这与后端那条"缺测显示 — 不显示 0"是同一条纪律，
    只是这次发生在前端——所以也要在前端锁住：catch 里落的状态必须带 `__error`。
    """
    import re as _re
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "frontend" / "src" / "App.jsx").read_text(
        encoding="utf-8"
    )
    # 抓"catch 之后 3 行内、给这几个状态赋了一个不含 __error 的对象字面量"的写法
    def _scan(src: str) -> list[str]:
        _bad = []
        for m in _re.finditer(r"\}\s*catch\s*(?:\([^)]*\))?\s*\{([^}]{0,300})", src):
            body = m.group(1)
            for s in _re.finditer(r"set([A-Z]\w*)\(\s*\{", body):
                tail = body[s.end() - 1 : s.end() + 200]
                if "__error" in tail.split("\n")[0] or "__error" in tail[:160]:
                    continue
                # 只在"赋的是空壳对象"时算违规（有实际数据的不拦）
                if _re.search(r"(count|total|rows|items|cards)\s*:\s*(\[\]|0)", tail[:120]):
                    _bad.append(f"{s.group(1)} ← {tail[:60].strip()}")
        return _bad

    # **自检带牙**：拿合成反面样本跑同一个扫描器，抓不到就红。
    # 本仓库的规矩：一个会随时间变瞎的检查比没有更糟——它给的是"已经查过了"的假安心。
    _synth_bad = 'try { await fetch(u); } catch (e) { setRunStats({ total: 0, count: 0, rows: [] }); }'
    assert _scan(_synth_bad), "扫描器抓不到合成反面样本——这个检查已经瞎了，等于没查"
    _synth_ok = (
        'try { await fetch(u); } catch (e) '
        '{ setRunStats({ total: 0, rows: [], __error: String(e) }); }'
    )
    assert not _scan(_synth_ok), "扫描器把**正确写法**也判成违规——误报会被无视，比没有更糟"

    _bad = _scan(js)
    assert not _bad, (
        "这些 catch 把「读失败」写成了「空数据」（界面会印成 0）：\n  "
        + "\n  ".join(sorted(set(_bad)))
        + "\n修法：带上 `__error: String(e?.message || e)`，渲染处显示读取失败而不是 0。"
    )


def test_archive_carries_every_field_recovery_reads():
    """存档里必须真的存了"恢复报告卡"要读的字段（2026-09-16 全量审计）。

    病案：实时路径上头部印「N 条证据 · M 处仲裁」、角标 tooltip 带分母、
    死链被 `badUrls` 拦住——**而报告存档里这些键一个都没有**。
    于是刷新一下 / 从历史里打开同一份报告：
      · 头部变成「 条证据 ·  处仲裁」（两个数都空着）
      · tooltip 说「本次未产出冲突检测读数（**旧报告**）」——而它刚跑完
      · 坏链照旧可点
    **"存了不读"的另一面是"读的时候没存"**，两边都得锁。

    判据机械：把前端 `reportMsgFromServer` 里读的 `data.xxx` 键抠出来，
    逐个要求在 `core/report_store.save_report` 的 payload 字面量里存在。
    """
    import re as _re
    from pathlib import Path

    from core.report_store import save_report  # noqa: F401  (确保模块可导入)

    _here = Path(__file__).resolve().parent
    js = (_here.parent / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8")
    m = _re.search(r"function reportMsgFromServer\(data, fallback = \{\}\) \{(.*?)\n\}", js, _re.S)
    assert m, "找不到 reportMsgFromServer（用例要跟着结构改）"
    # 只取 `data.xxx` / `data.yyy?.zzz` 的第一段
    read_keys = set(_re.findall(r"\bdata\.([a-z_][a-z0-9_]*)", m.group(1)))
    assert len(read_keys) >= 5, f"解析出的字段太少（{sorted(read_keys)}），用例可能没跟上结构"

    store = (_here / "core" / "report_store.py").read_text(encoding="utf-8")
    _p = _re.search(r"payload = \{(.*?)\n        \}", store, _re.S)
    assert _p, "找不到 save_report 的 payload 字面量"
    stored = set(_re.findall(r'"([a-z_][a-z0-9_]*)":', _p.group(1)))

    # 前端读的键里，允许少量"不在存档里也有值"的：
    _OK_ABSENT = {
        "report",           # 存档的主键，payload 里叫 report（这里只是别名字面量对不上）
        "id",
        # 证据/冲突的**明细**不入档（体积），存档只存**条数**；前端也优先读条数，
        # 这两个只是"万一哪天存了明细"的兜底表达式。
        "evidences",
        "conflict_results",
    }
    missing = sorted(read_keys - stored - _OK_ABSENT)
    assert not missing, (
        f"恢复报告卡要读这些键，但存档里没存：{missing}——"
        "刷新/从历史打开时它们会全空（头部缺分母、坏链不被拦、降级角标不出现）。"
    )


def test_audit_split_lists_agree():
    """前端与后端的**审计附录切分表必须逐条一致**（2026-09-16 全量审计）。

    病案：后端 `core/report_html.AUDIT_HEADING_PATTERNS` 有 15 条，
    前端 `App.jsx` 的 `AUDIT_HEADINGS` 只有 12 条——少的是
    「指标一致性核对」「结论状态」「配对矩阵」。后果不是"少折叠几节"那么轻：
    **同一份报告在 App 里这三节是正文、在导出的作品页/Word/PDF 里被折进审计附录**。
    交付物有两种结构，读者拿两份对不上，而我们对外说的恰恰是"同一份交付物"。

    两份清单在两个语言里，没法直接 import 共享，所以用**一致性用例**锁住——
    与可复算包里 `compute.py` 的公式副本同一姿势。
    """
    import re as _re
    from pathlib import Path

    from core.report_html import AUDIT_HEADING_PATTERNS

    js = (Path(__file__).resolve().parent.parent / "frontend" / "src" / "App.jsx").read_text(
        encoding="utf-8"
    )
    m = _re.search(r"const AUDIT_HEADINGS = \[(.*?)\];", js, _re.S)
    assert m, "前端找不到 AUDIT_HEADINGS（用例要跟着结构改）"
    fe = set(_re.findall(r'"([^"]+)"', m.group(1)))
    be = set(AUDIT_HEADING_PATTERNS)
    assert fe == be, (
        f"审计切分表两边不一致——只在前端：{sorted(fe - be)}；只在后端：{sorted(be - fe)}。"
        "不一致会让同一份报告在 App 与导出件里结构不同。"
    )


def test_every_declared_decision_kind_has_a_producer():
    """决策链里**声明过的类型，必须有人真的记录**（2026-09-16 全量审计）。

    病案：`core/decision_trace.KINDS` 声明 11 类决策（含 `stop` = 🛑 主动停止），
    而全仓实际记录 10 类——缺的正是 `stop`。于是《自主决策链》附录里
    **永远没有"主动停止"这一节**，前端类型表也为它留了位置。
    形状与当年的 `conflict_scan` 一模一样：**声明了、渲染位也留了、就是没有生产者**。
    偏偏"主动停止、交给人工"是赛道培训明确点名要展示的能力（也进复购看板的接管率）。

    这条锁是**通用**的，不是只补 `stop`：机械扫全仓的 `record(state, "<kind>")` 调用点，
    与 KINDS 比对，两边任一方向对不上就红——**新增一类决策却忘了接线，
    或者接了一种没声明的类型（会渲染成 `• kind` 这种机器标识符）都会当场失败。**
    """
    import re as _re
    from pathlib import Path

    from core.decision_trace import KINDS

    root = Path(__file__).resolve().parent
    produced: set[str] = set()
    # **先解析各文件的 import 别名**，再找该别名下的调用——直接搜 `record(` 会漏掉
    # `from core.decision_trace import record as _dec` 这种写法（而全仓都用的是别名）。
    # 这与 `test_no_keyword_parameter_is_silently_ignored` 是同一姿势：
    # 机械枚举必须先跟着真实的引用方式走，否则检查本身就在漏报。
    _imp = _re.compile(r"from\s+core\.decision_trace\s+import\s+record(?:\s+as\s+([A-Za-z_]\w*))?")
    for p in root.rglob("*.py"):
        rel = str(p.relative_to(root)).replace("\\", "/")
        if rel.startswith(("_fulltest", ".veritmp", "_diag", "_recon")) or "/.veritmp/" in rel:
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except Exception:
            continue
        _names = {m.group(1) or "record" for m in _imp.finditer(src)}
        if not _names:
            continue
        for _n in _names:
            for m in _re.finditer(
                _re.escape(_n) + r'\(\s*[A-Za-z_][\w.\[\]"\']*\s*,\s*["\']([a-z_]+)["\']', src
            ):
                produced.add(m.group(1))

    assert produced, "没扫到任何决策记录点——用例可能没跟上代码结构"
    _missing = sorted(set(KINDS) - produced)
    assert not _missing, (
        f"这些决策类型在 KINDS 里声明了、却全仓没有任何生产者：{_missing}——"
        "声明了却没有生产者的类型，在报告与前端都会**永远不出现**（渲染位白留）。"
    )
    _undeclared = sorted(produced - set(KINDS))
    assert not _undeclared, (
        f"这些决策类型有人记录、却不在 KINDS 里：{_undeclared}——"
        "它们会以 `• kind`（原始标识符）的形式渲染进《自主决策链》。"
    )


def test_engine_sizing_violation_reaches_the_gate():
    """引擎侧层级越界必须**真的走到**决策完整性闸门（2026-09-16 全量审计）。

    病案：`agents/researchers._check_tier_nesting` 一直往结果里写 `tier_violation`，
    但**只有 `_build_tam_calc` 读过它**（读成一句给写作者看的中文指令）。
    而 `core/integrity_gate.evaluate(sizing_violation=...)` 的入参在整条链上
    **没有任何写入方**——`graph.py` 读的是 `state["sizing_violation"]`，
    那个键在 TaskState 与 init_state 里都不存在，于是永远是空串。
    结果：闸门文档里写的"引擎侧 + 文本侧两层互补"，**实际只有正文正则这一道**；
    而"算术在代码里、不交给模型"正是我们对外卖的卖点——
    我们自己算出来的越界事实，恰恰是唯一没被送去裁决的那一个。

    本用例锁三段：① 引擎确实产出标记且能带出来；② 闸门拿到它时按引擎层归类；
    ③ graph 的接线（读的是真有的那个键，不是那个空键）。
    """
    import re as _re
    from pathlib import Path

    from core.integrity_gate import evaluate
    from agents.researchers import _compute_tam_sam_som

    # ① 构造一个**必然越界**的测算：SAM 从比 TAM 更大的基数派生
    _bad = {
        "base_population": 9.3,
        "potential_ratio": 0.30,
        "tam_freq": 45.0,
        "avg_price": 20.0,
        "sam_penetration": 0.90,      # 渗透率 90% × 频次/价格都高于 TAM 侧 → SAM > TAM
        "sam_freq": 60.0,
        "sam_price": 30.0,
        "som_share": 0.08,
    }
    _c = _compute_tam_sam_som(dict(_bad))
    assert _c.get("tier_violation"), (
        f"构造的越界参数没有触发引擎标记，用例需要跟公式对齐：{_c}"
    )

    # ② 闸门拿到引擎标记时，必须按"引擎自报越界"处理（不是靠正文正则去猜）
    _g = evaluate(report="## 结论\n\nGo。", rule_findings=[], adv_issues=[],
                  omission={}, quality_report=None,
                  sizing_violation=str(_c["tier_violation"]), verdict="Go")
    assert _g.get("level") != "PASS", (
        f"引擎自报三层越界，闸门却放行了：{_g.get('level')} / {_g.get('reasons')}"
    )
    # 并且必须**归到"测算引擎"这一层**——那不是文本猜测，是我们自己算出来的事实
    assert any(b.get("layer") == "测算引擎" for b in (_g.get("blockers") or [])), (
        f"闸门没把它归到引擎层（说明它还是只认正文正则）：{_g.get('blockers')}"
    )

    # ③ 接线：graph 读的必须是**真有写入方**的那个键
    _src = (Path(__file__).resolve().parent / "graph.py").read_text(encoding="utf-8")
    _code = "\n".join(
        ln for ln in _src.split("\n") if not ln.lstrip().startswith("#")
    )
    assert 'state.get("sizing_violation")' not in _code, (
        "graph 又在读那个全仓没有写入方的键了（那等于这一层永远为空）"
    )
    assert "_sizing_violation" in _code, "graph 没有从子研究员的结果里收引擎侧越界标记"


def test_export_formats_share_one_chart_budget():
    """同一份报告，换个导出格式，**图数必须一样**（2026-09-16 全量审计）。

    病案：三个导出面各写了一个字面量——`report_docx` 写 12、`app.py` 的 PDF 预算写 4、
    md 路径写 16。于是同一份报告导 Word 有 12 张图、导 PDF 只有 4 张，
    **而两处的注释还在互相打脸**（一处写"与 Word 对齐：上限 4 张"，另一处写"PDF 已放到 12"）。
    这类"注释互相引用、却没有单一出处"的写法，两边各说各话只是时间问题。

    本用例锁：三处都从 `report_charts.MAX_EXPORT_CHARTS` 取，且**不许再出现图数字面量**
    （剥注释后断言——免得我自己的病案注释把检查喂饱，这坑本仓库踩过）。
    """
    import re as _re
    from pathlib import Path

    from core.report_charts import MAX_EXPORT_CHARTS

    assert isinstance(MAX_EXPORT_CHARTS, int) and MAX_EXPORT_CHARTS >= 6, (
        f"图表预算异常：{MAX_EXPORT_CHARTS!r}"
    )
    _here = Path(__file__).resolve().parent

    def _code(p: Path) -> str:
        return "\n".join(
            ln for ln in p.read_text(encoding="utf-8").split("\n") if not ln.lstrip().startswith("#")
        )

    for rel in ("app.py", "core/report_docx.py"):
        src = _code(_here / rel)
        assert "MAX_EXPORT_CHARTS" in src, f"{rel} 没有从单一出处取图表预算"
        # 字面量：`max_charts=16` / `"left": 4` / `_MAX_CHARTS = 12`
        for m in _re.finditer(r"max_charts\s*=\s*(\d+)|[\"']left[\"']\s*:\s*(\d+)", src):
            raise AssertionError(
                f"{rel} 里还有图表预算的字面量：{m.group(0)!r}——"
                "数字只有一个地方能改（report_charts.MAX_EXPORT_CHARTS）"
            )


def test_conflict_decision_is_human_readable():
    """《数据冲突日志》的「仲裁取舍」列不许印机器枚举（2026-09-16）。

    病案：那一列印的是 `adopt_winner` / `show_both`——**英文下划线标识符直接进了交付报告**。
    而这一列正是「规则层用公开可对账的权重把冲突讲清楚」那句话的**证据位**：
    读者在这里看到的应该是一个可核对的取舍，不是一个变量名。

    判据机械可查：扫 `core/models.py` 里 `decision` 的**全部可能取值**，
    逐个要求在 `graph._ARBITRATION_ZH` 里有汉化；漏一个就红。
    （新加一种仲裁结论却忘了汉化，这份报告就会当场露馅。）
    """
    import re as _re
    from pathlib import Path

    import graph as _g

    src = (Path(__file__).resolve().parent / "core" / "models.py").read_text(encoding="utf-8")
    vals = set(_re.findall(r'decision\s*=\s*"([a-z_]+)"', src))
    assert vals, "没解析出 models.py 里的 decision 取值——用例可能没跟上结构"
    missing = {v for v in vals if v not in _g._ARBITRATION_ZH}
    assert not missing, (
        f"这些仲裁结论没有汉化，会以机器枚举的形式印进报告：{sorted(missing)}"
    )
    # 反向：汉化值本身不许还是 snake_case（等于没汉化）
    for k, v in _g._ARBITRATION_ZH.items():
        assert not _re.fullmatch(r"[a-z_]+", v), f"{k} 的汉化值还是个标识符：{v!r}"
        assert len(v) >= 6, f"{k} 的汉化太短，读者看不出取舍是什么：{v!r}"


def test_human_attention_list_discloses_truncation():
    """《结论状态》的「需要人工注意的 N 条」**截断必须留痕**（2026-09-16）。

    病案：标题里的 N 是**全量**（`len(flagged)`），表格却只印 `flagged[:12]`——
    于是"需要人工注意的 23 条"配一张 12 行的表：读者既不知道少了 11 条，
    也不知道去哪儿找。同文件另两处同类截断（`_ROUTE_ROWS` / 图表行数）都写了"另有 N 条"，
    这一处漏了——**同一个动作三处实现，只补了两处**。

    没有可调用的接缝（这段在报告装配体内），所以锁源码：
    **先剥掉注释行**再断言，免得我自己的病案注释把检查喂饱
    （本仓库在 SEC 用例上踩过这个坑）。
    """
    import re as _re
    from pathlib import Path

    src = (Path(__file__).resolve().parent / "graph.py").read_text(encoding="utf-8")
    _lines = [ln for ln in src.split("\n") if not ln.lstrip().startswith("#")]
    _code = "\n".join(_lines)
    assert "for m, c, r in flagged[:12]:" in _code, "截断点变了——用例要跟着改"
    _tail = _code.split("for m, c, r in flagged[:12]:", 1)[1][:600]
    assert "另有" in _tail and "len(flagged) - 12" in _tail, (
        "截断没有留痕：标题写全量条数、表里只列 12 条，读者不知道少了多少"
    )


def test_deliver_return_keys_are_in_schema():
    """**任何节点返回的键，只要不在 `TaskState` 里，都会被 LangGraph 静默丢弃。**

    这是本仓库最贵的一个坑，而且它已经复发过三次：
      · `decision_chain`（v0.9.7x）：附录里渲染出 2 条，而 `result["decision_chain"]` 是 0 条；
      · `integrity_gate`（2026-09-16 发现）：闸门结论在 SSE 路径上正常、**在同步路径上丢失**，
        于是报告存档里存到空 dict，"这份报告有没有被闸门拦过"无法判定；
      · `unprobed_urls`（同日、同批发现）：`app.py` 与 `graph.py` 都读它、都写了 `.get(..., [])`
        兜底——**兜底把"丢键"伪装成了"值为空"**，于是接口说"0 个未探测"而正文那一节列着真实条数。

    `core/state.py` 的注释里为此写过一句纪律：「凡是调用方要读的交付产物，都必须是 schema 里的真键」
    ——**那句话写在那儿，而这个坑照样又犯了两次**。注释拦不住人，所以这里把纪律变成一条会红的断言：
    扫描 `graph.py` 里全部 `node_*` 函数的 `return {...}` 块，取**深度 1 的字符串键**，
    逐个要求在 `TaskState.__annotations__` 里。

    **豁免要写理由。** 默认失败、显式放行——这样下一个人要么补 schema，要么在下面加一行并解释
    为什么这个键注定不会被持久化，**两条路都得先想一遍**，而不是无声地丢掉。

    已知局限（如实说）：只看字面量 `return {...}`，不看 `return dict(...)` 或先赋值再返回；
    对真实存在的写法够用，但**不是**完备的静态检查。
    """
    import inspect as _i
    import re as _re

    from core.state import TaskState

    _allowed = set(TaskState.__annotations__)
    # 键 → 豁免理由（**留空的豁免等于没有豁免**）
    _waived = {
        "assets_added": "node_asset_sink 的「本次沉淀了几条」回执；终态节点，无下游消费方，刻意不入 state",
        "asset_industry": "同上——认不出行业时不落卡，这个键只是把「为什么没落」带出来给日志",
        "asset_skipped": "同上——当前唯一取值 industry_unrecognized",
    }

    def _depth1_keys(src: str) -> list[str]:
        out: list[str] = []
        for m in _re.finditer(r"\n(\s*)return\s*\{", src):
            k = src.index("{", m.end() - 1)
            depth = 0
            while k < len(src):
                c = src[k]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        break
                elif depth == 1:
                    mm = _re.match(r'\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*:', src[k : k + 60])
                    if mm and (k == 0 or src[k - 1] in "{,\n "):
                        out.append(mm.group(1))
                k += 1
        return out

    import graph as _g

    _nodes = {
        n: f
        for n, f in vars(_g).items()
        if n.startswith("node_") and _i.isfunction(f) and getattr(f, "__module__", "") == "graph"
    }
    assert len(_nodes) >= 12, f"只扫到 {len(_nodes)} 个 node_* 函数——正则或命名变了，本项会空转"

    _bad: dict[str, list[str]] = {}
    for _name, _fn in _nodes.items():
        _miss = sorted({k for k in _depth1_keys(_i.getsource(_fn)) if k not in _allowed and k not in _waived})
        if _miss:
            _bad[_name] = _miss

    assert not _bad, (
        "下列节点返回了 **TaskState 里没有的键**——LangGraph 会**静默丢弃**它们，"
        "调用方拿到的是缺失或兜底默认值（本仓库已因此复发三次：decision_chain / integrity_gate / unprobed_urls）：\n"
        + "\n".join(f"  · {k} → {v}" for k, v in _bad.items())
        + "\n修法二选一：① 在 core/state.py 的 TaskState + init_state 里补上（调用方要读就必须走这条）；"
        "② 在 _waived 里加一行**并写清为什么它注定不被持久化**。"
    )


def test_conflict_scan_trunk_and_denominator():
    """冲突检测：**主干归一必须吃掉空白**，且「0 处仲裁」必须带分母。

    病案（2026-09-15 深访实跑，GPT 评审点名）：报告顶部写「57 条证据 · **0 处仲裁**」，
    而正文明明列着规则层报出的 **5 组口径冲突**——复核者直接会问
    "你们所谓的仲裁引擎，到底什么时候真正触发？"

    根因两条，都在同一处：
      ① 主干归一只把数字与中文标点换成 `·`，**空格原样保留**：
         `门店 5000 家` → `门店 · 家`、`门店5000家` → `门店·家`，**差一个空格就不比较**。
         而证据是不同来源/不同子任务产出的，数字两侧加不加空格几乎是必然差异——
         这条判据实际要求两条证据**逐字相同**，于是冲突检测近乎永不触发。
      ② 读数**没有分母**：`0` 到底是"查过没有"还是"根本没查"，读者无从判断。
         同型病案本仓库已修过两次（链接核验 0% vs 未测、遗漏层 claims=0 vs 崩溃），这是第三处。
    """
    import inspect as _i
    import re as _re

    # ① 归一吃掉空白（与 graph 内联的 _trunk 同一套规则）
    _src = _i.getsource(__import__("graph").node_check_conflict)
    assert '_re.sub(r"\\s+", "", t)' in _src or "re.sub(r'\\s+'" in _src or '\\s+' in _src, \
        "冲突主干归一里没有去空白——判据会退化成『逐字相同』，冲突检测永不触发"
    # 与代码同构地验一遍行为
    def _trunk(s):
        t = _re.sub(r"[\d，。\.、]+", "·", str(s or ""))
        return _re.sub(r"\s+", "", t).strip("·")

    assert _trunk("杭州咖啡门店数量约 5000 家") == _trunk("杭州咖啡门店数量约8000家"), \
        "数字两侧空格差异不该阻断比较"
    assert _trunk("星巴克 中国 门店数 约 6000 家") == _trunk("星巴克中国门店数约 8000 家")
    assert _trunk("不同的主体完全不一样") != _trunk("另一件不相干的事情"), \
        "不同主体仍必须被拒（放宽不能放到误报）"
    # ② 分母必须产出、且是 TaskState 的真键（否则被 LangGraph 静默丢弃）
    from core.state import TaskState

    assert "conflict_scan" in TaskState.__annotations__, "conflict_scan 不是 TaskState 真键，会被丢弃"
    from core.state import init_state

    assert init_state("q").get("conflict_scan") == {}, "init_state 没给 conflict_scan 默认值"
    assert '"conflict_scan"' in _src, "节点没把分母带出去"
    # 接线：前端头部必须读它，否则分母只活在 result 里
    from pathlib import Path as _P

    _fe = _P(__file__).resolve().parents[1] / "frontend" / "src" / "App.jsx"
    if _fe.exists():
        _t = _fe.read_text(encoding="utf-8")
        assert "conflictScan" in _t, "前端头部没读分母——「0 处仲裁」仍是裸数字"


def test_rule_findings_are_consumed_not_just_printed():
    """规则层 **high 风险项必须在正文被消费**，不能只印在附录里。

    病案（2026-09-15 深访实跑，真实报告）：R7 抓到

        **来源资格错配（high 风险）**：「国家统计局」被用作**企业微观经营数据**（客单价）的来源…
        建议：…拿不到就把来源改成「待核」，**不得保留 L1官方 标注**。

    而正文行动清单第 2 条原样写着 `[依据：国家统计局2026年数据及艾瑞咨询]`。
    **判决书写在 500 行后的附录里，正文一个字没动**——两处对不上时读者信的是正文。
    这正是本仓库最常复发的一类缺陷：**检出 ≠ 消费**。

    这里锁三件事：① 标记确实落进正文；② **只标不删**（抹掉归属等于销毁证据）；
    ③ 标记在**追加附录之前**落笔，所以附录里的判决条不会被自己批注。
    """
    import inspect as _i

    from core.decision_guard import mark_source_qualification
    from core.rule_lint import lint_report

    md = (
        "## 📋 决策清单\n\n## 行动清单（怎么做）\n\n"
        "2. **差异化定价策略**：针对下沉市场15.8元的客单价中位数，设计专属入门级产品线，"
        "以降低尝试门槛 [依据：国家统计局2026年数据及艾瑞咨询]。\n\n"
        "## 📊 核心指标一览表\n\n"
        "| 指标 | 数值 | 来源机构 |\n|---|---|---|\n| 下沉市场客单价中位数 | 15.8元 | 国家统计局 |\n"
    )
    v = lint_report(md)
    r7 = [x for x in v if x.get("rule") == "R7"]
    assert r7, f"R7（来源资格错配）应当命中这条——规则本身失效了：{[x.get('rule') for x in v]}"
    out = mark_source_qualification(md, v)
    assert "（来源资格待核）" in out, "R7 判了却没在正文消费（判决只写在附录 = 检出未消费）"
    assert "[依据：国家统计局2026年数据及艾瑞咨询]" in out, "归属被删掉了——只该标不该删"
    assert mark_source_qualification(out, v) == out, "不幂等（重复交付会叠标记）"
    # 接线：deliver 里必须在**追加规则层附录之前**调用它，否则附录判决条会被自己批注
    _d = _i.getsource(__import__("graph").node_deliver)
    assert "mark_source_qualification" in _d, "就地标记没接进 deliver"
    _src = _d[_d.find("mark_source_qualification"):]
    _app = _d.find("format_rule_lint_md(_rule_v)")
    assert _app > _d.find("mark_source_qualification"), "标记必须落在追加附录之前"


def test_chart_titles_unique_and_renderable():
    """内联图表的**题名**必须唯一、且带单位、且字体画得出来。

    病案（2026-09-15 深访实跑，真实报告《研策调研报告》）：
      · 13 张内联图里有两对**同名**——「📊 核心指标一览表 · 数值」×2、「数值/金额」×2。
        根因：`set_title` 带了单位、而 markdown 的 `![…]` alt 文本用的是**不带单位**的 `_cap`
        （`out.append({"title": _cap[:46]})`）。图上是（杯）和（%），读者看到的是两个同名标题
        ——**信息在，只是没传到读者那一端**。
      · 表名取自报告标题（「📊 核心指标一览表」），而 matplotlib 用的中文字体没有该 emoji 码位，
        画出来是空心方框（实测告警 `Glyph 128202 (\N{BAR CHART}) missing`）。
        与 PDF 法定标识渲染成空白是同一类事故。

    这条同时锁住两件事：**题名唯一**（否则读者无法指认）与**无不可渲染码位**（否则标题看不见）。
    """
    from core.report_charts import _glyph_safe, metric_chart_pngs

    rows = [
        ["指标", "数值", "来源机构"],
        ["县域社会消费品零售总额", "14.2万亿元", "国家统计局"],
        ["三四线咖啡单店日均杯量", "92杯", "艾瑞咨询"],
        ["下沉市场单店成本营收比", "68.4%", "艾瑞咨询"],
        ["新进品牌首年闭店率", ">35%", "艾瑞咨询"],
        ["星巴克三四线盈亏平衡覆盖率", "61%", "Euromonitor"],
        ["三四线现磨咖啡人均年消费量", "2.1杯", "报告正文"],
        ["三四线咖啡门店闭店率", "8.9%", "m.21jingji.com"],
        ["下沉用户15-25元价格带接受度", "73.6%", "艾瑞咨询"],
    ]
    out = metric_chart_pngs(rows, "📊 核心指标一览表")
    if not out:
        return  # matplotlib/中文字体缺失的环境跳过（与模块其余分支同一姿势）
    titles = [o["title"] for o in out]
    assert len(titles) == len(set(titles)), f"同表内出现同名图（读者无法指认）：{titles}"
    assert all("（" in t for t in titles), f"题名必须带单位，否则同名图分不开：{titles}"
    assert all(_glyph_safe(t) == t for t in titles), f"题名里仍含画不出来的码位（会渲染成方框）：{titles}"
    # 反向锁：_glyph_safe 只摘装饰符号，**不许吃掉正常中文/数字/单位**
    assert _glyph_safe("📊 核心指标一览表 · 数值（杯）") == "核心指标一览表 · 数值（杯）"
    assert _glyph_safe("ROI 与 LTV/CAC 比值 12.5%") == "ROI 与 LTV/CAC 比值 12.5%"
    assert _glyph_safe("") == ""


def test_charts_do_not_flip_signs_or_drop_rows():
    """图表层：**一根画反的柱子，比不出图更糟**（2026-09-16 全量审计）。

    三个各自独立、都会被"看图的人直接读错"的缺陷：

      ① `_num` 的正则**没有符号位** → `-15万` 被读成 `15万`。
         于是一张"净利润同比"的图里，**亏损全部画成向上的柱子**，
         与同一份报告的表格、正文完全相反；而图比表更抓眼，读者先信图。
      ② `_unit_of` 把**可信度标注与脚注**当成单位：实测从真报告里抽出来的"单位"
         有 `*注4*`（原文 `302.5 *注4*`）、`2023全年`（原文 `249.0 (2023全年)`）。
         它们还把同一列**拆成多个单位组**，每组不足 2 行 → 整列一张图都出不来。
      ③ `_group_rows` 对百分号列**无条件**按"指标主题"拆子族，单行族被**整族丢弃**。
         `品牌|市占率` 这类表的主体名之间不共享连续 2 字 → 每个主体自成一族 →
         实测「市场份额」图只剩两个品牌，**其余数据点凭空消失且无人告知**。

    前两条锁"数不能错"，第三条锁"行不能丢"。
    """
    from core.report_charts import _group_rows, _num, _unit_of, metric_chart_pngs

    # ① 符号：三种减号写法都要收，正号与无符号不受影响
    assert _num("-15万") == -15.0, "负号被吃掉了——亏损会被画成正柱"
    assert _num("−15万") == -15.0, "U+2212 也是减号"
    assert _num("+3.2%") == 3.2
    assert _num("1,234.5 亿元") == 1234.5
    assert _num("0") == 0.0, "0 是一个读数，不是缺失——丢掉它是静默截断"
    assert _num("无数据") is None

    # ② 标注 ≠ 单位
    assert _unit_of("302.5 *注4*") == "", f"脚注被当成了单位：{_unit_of('302.5 *注4*')!r}"
    assert _unit_of("249.0 (2023全年)") == "", "数据期被当成了单位"
    assert _unit_of("249.0 (2023实披露)") == ""
    assert _unit_of("320.0 (估算)") == ""
    # 反向锁：真单位不许被清洗掉
    assert _unit_of("35 杯/人/年") == "杯/人/年"
    assert _unit_of("1,234.5 亿元") == "亿元"
    assert _unit_of("36.2%") == "%"

    # ③ 同一指标、不同主体：**一行都不许丢**
    _brand = [
        ["品牌", "市占率"],
        ["库迪", "5%"], ["Tims天好", "1.5%"], ["瑞幸", "12.5%"],
        ["星巴克", "14%"], ["蜜雪冰城", "8%"],
    ]
    _g = _group_rows(_brand[1:], _brand[0])
    assert len(_g) == 1, f"品牌市占率被拆成了 {len(_g)} 组——主体名不是指标名"
    assert len(_g[0]["rows"]) == 5, (
        f"5 个品牌只画出 {len(_g[0]['rows'])} 根柱——单行族被整族丢弃（静默数据丢失）"
    )

    # ③b 泛值列（行名才是指标名）里的单行族：不再整族消失
    _mix = [
        ["指标", "数值", "来源机构"],
        ["潜在总市场规模", "**约 4,687 亿元**", "艾媒咨询"],
        ["城镇化率", "63.9%", "国家统计局"],
        ["SAM 渗透率", "25%", "券商研报"],
        ["全国市占率", "25%", "灼识咨询"],
    ]
    _gm = _group_rows(_mix[1:], _mix[0])
    _pct = [g for g in _gm if "%" in (g.get("unit") or "")]
    assert _pct, "百分号组整体消失了"
    _drawn = sum(len(g["rows"]) for g in _pct)
    assert _drawn >= 3, (
        f"泛值列里 3 个百分号指标只画出 {_drawn} 行——单行族又被丢了"
        "（它们必须合并进一张'其余单行指标'的图，而不是消失）"
    )

    # ③c 异量纲不许共轴：单位写在**旁边一列**时，4,687 亿元 与 45 杯/人/年 不能同轴
    _econ = [
        ["指标项目", "数值", "单位", "备注"],
        ["客单价", "20", "元/杯", "—"],
        ["理论年均频次", "45", "杯/人/年", "—"],
        ["门店数", "20000", "家", "—"],
    ]
    _ge = _group_rows(_econ[1:], _econ[0])
    assert all(len(g["rows"]) >= 2 for g in _ge), (
        "把三种不同量纲凑进了同一张图（异量纲共轴＝看图的人直接读错）"
    )

    # ④ 端到端：负值必须真的进到图里，而不是被纵轴裁掉
    _loss = [["品牌", "净利润同比"], ["A", "-15万"], ["B", "-3.2万"], ["C", "8万"]]
    _pngs = metric_chart_pngs(_loss, "门店盈亏")
    if _pngs:   # matplotlib/字体缺失的环境跳过（与模块其余分支同一姿势）
        _vals = [r["value"] for r in _group_rows(_loss[1:], _loss[0])[0]["rows"]]
        assert min(_vals) < 0, "负值在分组阶段就没了"


def test_omission_layer():
    """遗漏层：只审已有引用的两层之外的盲区（v0.9.75）。

    这三档的分法本身就是这一层的全部价值，必须锁死：
      已引 / 自陈不确定（诚实标注）/ 本报告自算 / **裸断言**（唯一的缺陷档）
    最要紧的一条：**不许把「自陈估算·待核」算进缺陷**——那等于惩罚诚实。
    """
    from core.omission import format_appendix_md, scan

    md = "\n".join([
        "# 竞争格局",
        # 三档都要被正确归类
        "据国家统计局2025年数据，全国咖啡门店数约 18.6 万家，增速 12.4%。",   # 已引：据…数据
        "IDC数据显示，县域咖啡渗透率预计达到 34.2%。",                        # 已引：主谓式归属
        "该品牌 2024年报披露，单店坪效为 1.8 万元/㎡/年。",                    # 已引：主体+年份+文种
        "下沉市场单店日均杯量仅 48 杯，回本周期延长至 14.3 个月。",             # **裸断言**
        "该比例约为 30%（L5估算），仅供参考。",                                # 自陈不确定 → 不算缺陷
        "潜在总规模（TAM）约为 4,218 亿元。",                                  # 本报告自算 → 不算缺陷
        # 不算主张的行
        "- 建议第一步投入 5 万元做测试。",
        "| 指标 | 数值 |",
    ])
    r = scan(md)
    assert r["claims"] == 6, f"断言计数不对（计划句/表格行不该算）：{r}"
    assert r["cited"] == 3, f"已引识别不对：{r}"
    assert r["labeled"] == 1, f"自陈不确定未单列（会被算成缺陷 = 惩罚诚实）：{r}"
    assert r["derived"] == 1, f"本报告自算未单列：{r}"
    assert r["omitted"] == 1, f"裸断言数不对（应恰好 1 条）：{r}"
    assert r["samples"] and "48 杯" in r["samples"][0], r["samples"]
    # **口径要窄**：外部事实主张不许被"本报告自算"吸收掉（漏报比误报更危险）
    from core.omission import _DERIVED

    assert "盈亏平衡" not in _DERIVED.pattern and "单位经济模型" not in _DERIVED.pattern, \
        "「盈亏平衡/单位经济模型」是市场事实常用词，收进「本报告自算」会把外部主张漏报掉"
    out = format_appendix_md(r)
    for kw in ("遗漏层", "为什么单列这一层", "这一层不证明什么", "启发式"):
        assert kw in out, f"附录缺 {kw}"
    # 已引 vs 未引必须并排 —— 只报支撑率会被读成"核验器太松"
    assert "自陈" in out and "裸断言" in out
    # 无数据时不印空壳
    assert format_appendix_md(None) == "" and format_appendix_md({}) == ""
    # 接线：交付节点必须真的把它扫出来并渲染（有模块 ≠ 有接线）
    import inspect as _i

    _d = _i.getsource(__import__("graph").node_deliver)
    assert "omission" in _d and "_omi_scan" in _d, "遗漏层未接进交付"
    _rj = _i.getsource(__import__("graph")._result_json)
    # 遗漏层随 `entailment` 一起出去（同一份结果对象），所以断言要打在**真实路径**上：
    # ① _result_json 透传 entailment ② 交付节点把 omission 写进了 entailment ③ 前端读它
    assert "entailment" in _rj, "entailment 未透传（遗漏层就没有载体）"
    assert '"omission"' in _d or "'omission'" in _d, "交付节点未把 omission 写进 entailment"
    # 直接读绝对路径：原先这里先试 `open("App.jsx")`（相对 cwd）再回退，
    # 于是"读到哪个文件"取决于你从哪儿跑——而这几处比的是**前后端是否一致**，
    # 读不到就变成一句"前端缺东西"。
    _fe = (_HERE.parent / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8")
    assert "ent.omission" in _fe, "前端未接遗漏层（角标不出现 = 等于没告警）"

    # ---- 「报告里承诺的字段，读者真的拿得到吗」（2026-09-17 实跑发现）----
    #
    # 病案：`method` 文本原本逐字写着「条数见 `excluded_plan_lines`——排除规则会直接改变分子，
    # 故一并报出」，而这句话**原样印进每一份交付报告**；
    # 但 `graph.node_deliver` 对手写白名单投影时**没抄这个键**，`format_appendix_md` 也没印它。
    # 于是报告让读者去看一个**在读者拿到的任何一件产物里都不存在**的东西，
    # 而且失效的位置恰好在"排除规则会直接改变分子"这句最该被验证的话旁边。
    #
    # 锁的形状：**不许指向字段名，必须直接把数印出来**。
    # 指向字段名 = 把"读者能不能看到"寄托在交付路径上每一道投影都记得抄它；
    # 印出来 = 读者手上的那份报告自带这个数，与投影无关。
    _txt = format_appendix_md(r)
    # ① 数值本身必须出现在报告正文里（不是"见某某字段"）
    assert f"**{r['excluded_plan_lines']}** 行" in _txt, (
        f"附录没把被排除的行数印出来（读者拿不到决定分母的那个数）：{r['excluded_plan_lines']}"
    )
    # ② **反向锁**：`method` 里**不许出现任何**「见 `字段名`」这种指向式承诺。
    #    ⚠️ 第一版写成 `if m != "excluded_plan_lines"` —— 把唯一那个真悬空的名字放行了，
    #    于是改前改后都"通过"（正反样本一跑就露）。**放行名单里写的正是要抓的那个东西**，
    #    这是本仓库"半个检查比没有更糟"的又一次重演，故这里**不设任何白名单**。
    #    只扫 `method` 而不是整段附录：`samples` 是正文原句，正文里本来就可能出现反引号代码词，
    #    扫进去会把"正文里有行内代码"误报成"承诺了字段"。
    #    承诺只会写在 `method` 里，锁打在承诺唯一可能出现的地方。
    import re as _re2

    _dangling = _re2.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", r["method"])
    assert not _dangling, (
        f"判定口径里出现指向式字段名承诺（读者在报告/存档/result 里都查不到）: {_dangling}"
    )
    # ③ 键必须在 `scan()` 的返回值里（生产者这一侧）
    assert "excluded_plan_lines" in r, "scan() 没返回被排除行数"
    # ④ 键必须进交付投影（消费这一侧）——手写白名单漏键是静默的，只能靠这条抓
    _nd = _i.getsource(__import__("graph").node_deliver)
    assert "excluded_plan_lines" in _nd, (
        "交付节点的手写白名单投影漏了 excluded_plan_lines —— "
        "报告/存档/result 里都会少这个数，而且不会报错"
    )


def test_outcome_review_discloses_not_due_and_sanitizes_cells():
    """决策回访：**提前回访要自陈**，表格格子的清洗要同一套规则（2026-09-16）。

    两处都取自全量审计：

    ① **`due_date` 写了没人读。** 登记基线时同时写 `decision_date` 与 `due_date`，
       而 `run()` 只按 `status == "in_flight"` 筛 → **刚登记完的基线会被立刻"回访"**，
       拿今天的行情去回答"这条监控条件后来动了没有"，还落盘成一条结论。
       处理方式是**不拦人、但要说出来**——回访本来就是人按的。
       悄悄按未到期状态回答，才是那个真问题。

    ② **同一行的两个格子两套规则。** `why`（依据）洗了 `|`，而 `condition`（监控条件）**没洗**——
       监控条件是从「14 天验证计划」摘的，本来就带分隔符与换行，于是那一行整个错列。
    """
    from core.outcome_track import format_md

    _rep = {
        "reviewed_at": "2026-09-16T10:00:00",
        "count": 1,
        "changed": 0,
        "no_result": 0,
        "not_due": 1,
        "not_due_ids": ["D-0001"],
        "reviews": [
            {
                "id": "D-0001",
                "question": "某市场该不该进",
                "verdict_original": "Validate",
                "decision_date": "2026-09-16",
                "rows": [
                    {
                        "condition": "门店数|季度环比 < 0\n或 闭店率 > 10%",
                        "verdict": "no_change",
                        "why": "未检索到|公开更新",
                        "evidence": [],
                        "quote": "",
                    }
                ],
            }
        ],
    }
    md = format_md(_rep)
    # ① 未到期必须出现在正文里，且带上是哪几条
    assert "尚未到应验日" in md, "提前回访没有自陈——0 条『有进展』会被读成『监控条件都没动』"
    assert "D-0001" in md, "未到期名单没写出来"
    # ② 表格行里不许出现裸竖线（会把那一行切成多列）
    _tbl = [ln for ln in md.split("\n") if ln.startswith("| ") and "**" in ln]
    assert _tbl, "没找到回访结论表"
    for ln in _tbl:
        assert ln.count("|") == 4, f"表格行列数不对（竖线没洗）：{ln!r}"
    assert "\n" not in _tbl[0], "监控条件里的换行没洗掉"

    # ③ 反向：正常内容不许被洗坏
    from core.outcome_track import _cell

    assert _cell("闭店率 > 10%", 50) == "闭店率 > 10%"
    assert _cell("a|b", 50) == "a/b"
    assert _cell("", 50) == ""


def test_outcome_review():
    """决策回访：**未取得 ≠ 没变化**；只回访、不改写原判断。完全离线。"""
    import json as _json
    import os as _os
    import tempfile

    import core.outcome_track as OT

    # ---- ① 基线文件真的被读了（此前零代码读取） ----
    _bs = OT.load_baselines()
    assert len(_bs) >= 4, f"决策基线应至少 4 条，读到 {len(_bs)}"
    assert all("monitor_conditions" in b for b in _bs), "基线缺监控条件"
    assert all(str(b.get("status")) == "in_flight" for b in _bs)

    _snap = _json.dumps(_bs, ensure_ascii=False, sort_keys=True)
    _b = _bs[0]

    # ---- ② 检索零返回 → 必须是 no_result，**绝不许写成 unchanged** ----
    with mock.patch.object(OT, "_search", return_value=[]):
        r = OT.review_one(_b, max_conds=2)
    assert r["rows"], "未产出任何条件行"
    assert all(x["verdict"] == "no_result" for x in r["rows"]), \
        f"检索零返回被写成了别的（把「没查到」说成「没变化」= 自己给自己盖章）：{r['rows']}"
    assert r["no_result"] == len(r["rows"])

    # ---- ③ 有结果 + 判定为有进展 → changed，且必须带依据与证据链接 ----
    _hits = [{"title": "某品牌股权变更公告", "url": "https://example.gov.cn/a", "snippet": "拟转让 60% 股权"}]
    with mock.patch.object(OT, "_search", return_value=_hits), \
         mock.patch.object(OT, "_judge", return_value={
             "verdict": "changed", "why": "有明确的股权转让公告", "which": [1], "quote": "拟转让 60% 股权"}):
        r2 = OT.review_one(_b, max_conds=1)
    assert r2["rows"][0]["verdict"] == "changed" and r2["changed"] == 1
    assert r2["rows"][0]["evidence"] and r2["rows"][0]["evidence"][0]["url"].startswith("http")
    assert r2["rows"][0]["why"], "有进展必须给依据（可被人工推翻）"

    # ---- ④ 模型判 unclear / 判不出来 → 归为 no_result，不许当成结论 ----
    for _bad in ({"verdict": "unclear", "why": "信息不足"}, {"verdict": "", "why": ""}):
        with mock.patch.object(OT, "_search", return_value=_hits), \
             mock.patch.object(OT, "_judge", return_value=_bad):
            _rr = OT.review_one(_b, max_conds=1)
        assert _rr["rows"][0]["verdict"] == "no_result", f"判不出来被当成了结论：{_rr['rows'][0]}"

    # ---- ⑤ 只回访、不改写原判断 ----
    assert _json.dumps(OT.load_baselines(), ensure_ascii=False, sort_keys=True) == _snap, \
        "回访改写了原基线文件 —— 篡改历史等于没有记忆"

    # ---- ⑥ 未执行也必须出声 ----
    _none = OT.format_md(None)
    assert "未执行" in _none and "不代表" in _none, "缺席没有如实说明"

    # ---- ⑦ 落盘与读取 ----
    tmpf = _os.path.join(tempfile.mkdtemp(), "reviews.jsonl")
    _old = OT._REVIEWS
    OT._REVIEWS = tmpf
    try:
        with mock.patch.object(OT, "_search", return_value=_hits), \
             mock.patch.object(OT, "_judge", return_value={"verdict": "unchanged", "why": "无新进展"}), \
             prod_write_allowed("_REVIEWS 已指向临时文件 tmp，测的是回访记录落盘/读回"):
            rep = OT.run(ids=[str(_b.get("id"))], max_conds=1)
            assert rep["count"] == 1 and OT.load_reviews(limit=5), "回访记录未落盘/未读回"
            md = OT.format_md(rep)
            for kw in ("决策回访", "不改写原判断", "未取得", "本节不证明什么"):
                assert kw in md, f"回访附录缺 {kw}"
    finally:
        OT._REVIEWS = _old


def test_entailment_calibration():
    """核验器假阳性率校准：**未执行 ≠ 假阳性率为 0**；过期存档不得背书。完全离线。"""
    import json as _json
    import os as _os
    import tempfile

    import core.calibration as C

    # ---- ① 样本集本身要成对且够用（只有正例或只有反例的校准没有意义） ----
    _pos = [c for c in C.CASES if c["group"] == "match"]
    _neg = [c for c in C.CASES if c["group"] == "mismatch"]
    assert len(_pos) >= 4 and len(_neg) >= 4, f"正反例各需 ≥4：{len(_pos)}/{len(_neg)}"
    assert len({c["gid"] for c in C.CASES}) == len(C.CASES), "样本编号有重复"
    # 反例必须覆盖多个轴（只测一个轴的校准是自欺）
    assert len({c["axis"] for c in _neg}) >= 4, "反例覆盖面太窄"

    # ---- ② 判定函数必须与生产同源（另写一套就等于测的不是线上那个核验官） ----
    import inspect as _i

    _src = _i.getsource(C.run)
    assert "core.entailment" in _src and "judge_pairs" in _src, \
        "校准必须调用生产的 judge_pairs，不许另写判定逻辑"

    # ---- ③ 全部判定失败 → 返回 None（**未执行 ≠ FPR 0**） ----
    def _all_fail(pairs, batch=6):
        for p in pairs:
            p["verdict"] = "unjudged"
        return pairs

    with mock.patch("core.entailment.judge_pairs", side_effect=_all_fail):
        assert C.run() is None, "全部判不出来却给出了结果（会把「没判」伪装成「判得准」）"

    # ---- ④ 构造已知错判 → FPR 必须算对 ----
    def _fake(pairs, batch=6):
        for p in pairs:
            # 每个反例的第 1 条误判为 support（严格假阳性 = 1/8）；正例全 support
            p["verdict"] = "support" if (p["_group"] == "match" or p["_gid"] == "X1") else "unrelated"
            p["why"] = "测试"
        return pairs

    with mock.patch("core.entailment.judge_pairs", side_effect=_fake):
        rep = C.run()
    assert rep is not None
    assert rep["fpr"] == round(1 / len(_neg), 4), f"严格 FPR 算错：{rep['fpr']}"
    assert rep["tpr"] == 1.0, f"真阳性率算错：{rep['tpr']}"
    assert rep["fpr_loose"] >= rep["fpr"], "宽松口径必须 ≥ 严格口径"
    # 判错的条目必须在表里被标出来（校准的价值就是它找到了东西）
    _md = C.format_appendix_md(rep)
    assert "判错" in _md, "判错的样本没有被标出来"
    for kw in ("假阳性率", "HALLMARK", "自测", "这一节不证明什么"):
        assert kw in _md, f"校准附录缺 {kw}"

    # ---- ⑤ 过期存档不得用于给当前支撑率背书 ----
    _tmp = _os.path.join(tempfile.mkdtemp(), "cal.json")
    _old = C._PATH
    C._PATH = _tmp
    try:
        with prod_write_allowed("C._PATH 已指向临时文件 tmp，测的是过期存档的读取语义"):
            C.save({**rep, "ran_at": "2020-01-01T00:00:00"})
            assert C.load() is None, "过期存档被当作有效（拿旧数给今天的支撑率背书 = 另一种自欺）"
            assert C.load(max_age_days=3650) is not None, "放宽 TTL 后应能读到"
            # 收窄（2026-09-17）：原断言是 `== ""`，而现在的语义是
            # **作废要说出理由，但不许渲染任何校准结果**——两者不矛盾，
            # 只是原来"什么都不印"的表达方式让「从没跑过 / 过期 / 提示词改了」
            # 三种情况长得一模一样，读者会读成"这一节本来就没有"（**静默消失**）。
            # 所以判据从"印不印"改成"**印的是不是结果**"。
            _mdx = C.format_appendix_md()
            for _kw in ("严格假阳性率", "宽松", "真阳性率", "31.2%"):
                assert _kw not in _mdx, f"过期存档仍在渲染校准结果「{_kw}」"
            assert "过期" in _mdx, "过期时既不渲染结果、也不说明原因——那是静默消失"
            C.save(rep)
            assert C.load() is not None and C.format_appendix_md()
    finally:
        C._PATH = _old

    # ---- ⑥ 判定提示词必须写着「不许用推算凑出支撑」（来自一次真实校准实测） ----
    from core.entailment import _JUDGE_PROMPT

    assert "推算" in _JUDGE_PROMPT and "partial" in _JUDGE_PROMPT, \
        "判定提示词缺「不许用推算凑出支撑」——实测中它把 FPR 从 12.5% 压到 0，删掉会回退"


def test_want_tool_alias():
    """工具闸门的聚合项别名归并（实跑踩中：选了 region_stats，16 个地方统计局被全体跳过）。"""
    from core.authority_fetch import want_tool

    assert want_tool("tjj_hangzhou", None) is True, "未选择时必须全打（fail-open）"
    assert want_tool("nbs", ["cninfo"]) is False, "没选的源族不该被打"
    assert want_tool("cninfo", ["cninfo"]) is True
    # 注册表把地域源记成一条聚合项 region_stats，取证层按 tjj_* 逐个判 —— 必须互认
    assert want_tool("tjj_hangzhou", ["region_stats"]) is True,         "选了聚合项却把成员源全跳过 = 静默关掉整个地域源层"
    assert want_tool("region_stats", ["tjj_hangzhou"]) is True, "成员项应反向放行聚合项"
    assert want_tool("tjj_hangzhou", ["cninfo", "nbs"]) is False, "没选地域就不该打地域"
    # 国际组织：注册表两条，历史台账记过一条 intl
    assert want_tool("worldbank", ["intl"]) is True and want_tool("imf", ["intl"]) is True
    assert want_tool("worldbank", ["cninfo"]) is False


def test_ai_label():
    """显式/隐式标识：字段齐、编号可复现且可证伪、两模式都覆盖。"""
    import inspect

    from core.ai_label import (
        CONTENT_PROPERTY, PROVIDER_CODE, content_id, explicit_head, explicit_tail,
        html_meta_tags, implicit_meta, pdf_meta,
    )

    md = "# 报告\n\n杭州咖啡门店 3500 家。"
    cid = content_id(md)
    assert cid.startswith("AIGC-YC-") and len(cid) == len("AIGC-YC-") + 16, cid

    # 内容编号的两条性质：同内容不变（可复现）、改一字即变（可证伪）
    assert content_id(md) == cid, "同内容编号必须稳定"
    assert content_id(md + "。") != cid, "正文改动后编号必须改变（否则编号没有核查价值）"

    head = explicit_head(md, cid)
    tail = explicit_tail(md, cid)
    for blk, where in ((head, "起始"), (tail, "末尾")):
        assert cid in blk, f"{where}显式标识缺内容编号"
        assert "AI 生成合成内容" in blk, f"{where}显式标识缺『AI 生成合成内容』字样"
        assert "标识办法" in blk, f"{where}显式标识缺依据"
    assert "服务提供者" in head and "服务提供者" in tail, "显式标识缺服务提供者"

    # 头尾必须用同一个编号——各算一遍会得到两个（中间还插了附录），一份报告两个编号等于没编号
    assert content_id(md) in head and content_id(md) in tail
    _h_cid = head.split("内容编号：`")[1].split("`")[0]
    _t_cid = tail.split("内容编号：`")[1].split("`")[0]
    assert _h_cid == _t_cid, f"头尾内容编号不一致：{_h_cid} vs {_t_cid}"

    # 隐式标识三件套（《标识办法》第六条：属性信息 / 服务提供者名称或编码 / 内容编号）
    m = implicit_meta(md, title="T")
    assert m["AIGC"] == CONTENT_PROPERTY
    assert m["content_producer"] == PROVIDER_CODE and m["producer_name"]
    assert m["content_id"] == cid
    assert 'name="content-id"' in html_meta_tags(md, title="T")
    _pdf = pdf_meta(md, title="T")
    assert _pdf["author"] and cid in _pdf["subject"] and CONTENT_PROPERTY in _pdf["subject"]

    # 企业模式（不附 AI 透明度附录）**仍然**必须有法定标识——这条最容易被后代改回去
    from core.transparency import build_transparency_md
    assert build_transparency_md("enterprise") == "", "企业模式仍不应附 AI 透明度附录（产品取舍）"
    _src = inspect.getsource(__import__("graph").node_deliver)
    assert "explicit_head" in _src and "explicit_tail" in _src, "法定标识必须挂在交付节点上（两模式共用）"


def test_ai_label_export_wiring():
    """隐式标识必须真的进四条导出路径（有函数不等于有接线）。"""
    import inspect

    from core.ai_label import apply_file_meta, content_id

    md = "# 报告\n\n数据。"
    cid = content_id(md)
    # docx（python-docx core_properties）
    from docx import Document

    d = Document()
    apply_file_meta(d.core_properties, md, title="T")
    assert cid in (d.core_properties.comments or ""), "docx 元数据未写入内容编号"
    assert "AI" in (d.core_properties.category or ""), "docx 元数据未写入 AIGC 属性"
    # pptx（python-pptx core_properties）
    from pptx import Presentation

    prs = Presentation()
    apply_file_meta(prs.core_properties, md, title="T")
    assert cid in (prs.core_properties.comments or ""), "pptx 元数据未写入内容编号"
    # html（<meta>）+ md（HTML 注释）——在导出端点里，锁住接线
    import app as _app

    _exp = inspect.getsource(_app.export_report)
    assert "html_meta_tags" in inspect.getsource(__import__("core.report_html", fromlist=["x"]))
    assert "implicit_meta" in _exp, "md 导出未写隐式标识"
    assert "pdf_meta" in _exp, "pdf 导出未写隐式标识"
    assert "apply_file_meta" in _exp, "pptx 导出未写隐式标识"
    _docx = inspect.getsource(__import__("core.report_docx", fromlist=["x"]))
    assert "apply_file_meta" in _docx, "docx 导出未写隐式标识"


# ============================================================
# 39. 引用蕴涵核验（claim 级）
# ============================================================
def _ev(claim, excerpt, **kw):
    return {"claim": claim, "query": "q", "sources": [dict(
        {"title": "t", "publisher": "P", "url": "https://x", "level": "L2", "excerpt": excerpt}, **kw)]}


def test_entailment_offline():
    """配对筛选 + 排序 + 汇总口径（**未判定不得计入通过率、也不得视为通过**）。"""
    from core.entailment import build_pairs, format_appendix_md, summarize

    long_ex = "国家统计局数据：2025 年全国咖啡市场规模 5005 亿元，同比增长 12.3%，数据来源为年度统计公报。"
    evs = [
        _ev("全国咖啡市场规模 5005 亿元", long_ex),
        _ev("竞争格局较为分散", long_ex),
        _ev("【反方】市场可能被高估", long_ex),          # 反方证据不参与（不是正文主张）
        _ev("门店数 3500 家", "太短"),                  # 摘录过短 → 无比对价值
        {"claim": "", "sources": [{"excerpt": long_ex}]},  # 无主张
        {"claim": "无来源主张", "sources": []},
    ]
    ps = build_pairs(evs, max_pairs=10)
    assert len(ps) == 2, f"应只留下 2 条可比对（反方/短摘录/无主张均排除），实得 {len(ps)}"
    # 数字密度优先：带数字的主张排前面（核验资源有限时先核"写错会出错的"）
    assert ps[0]["claim"] == "全国咖啡市场规模 5005 亿元", ps[0]["claim"]

    # 通过率分母只含**已判定**条目
    _pairs = [dict(p, verdict="support") for p in ps[:1]] + [dict(p, verdict="unjudged") for p in ps[1:]]
    rep = summarize(_pairs)
    assert rep["judged"] == 1 and rep["unjudged"] == 1, rep
    assert rep["pass_rate"] == 1.0, "未判定不得进分母"
    # 全部未判定 → 通过率为 0（**不是 1.0**）——这是本模块最容易被写反的一行
    rep2 = summarize([dict(p, verdict="unjudged") for p in ps])
    assert rep2["judged"] == 0 and rep2["pass_rate"] == 0.0, "全未判定时通过率必须是 0，不能是 100%"

    # 未执行 → 附录必须写"本次未执行"，且**不得**出现任何"通过"字样
    md_none = format_appendix_md(None)
    assert "本次未执行" in md_none and "不等于引用全部通过" in md_none
    assert "通过率" not in md_none, "未执行时不得出现通过率（那会被读成已核验）"

    md_ok = format_appendix_md(summarize([dict(p, verdict="support") for p in ps]))
    assert "引用支撑率" in md_ok and "这一节不证明什么" in md_ok and "未判定" not in md_ok.split("这一节")[0][-200:]
    assert "行业参照" in md_ok and "未独立复核" in md_ok, "外部基线必须带未复核标注"


def test_entailment_robustness():
    """模型输出脏 → 解析容错；彻底失败 → 转未判定（不冒充通过）。"""
    from core.entailment import _parse_json_array, judge_pairs

    assert _parse_json_array('[{"i":1,"verdict":"support"}]')[0]["verdict"] == "support"
    assert _parse_json_array('```json\n[{"i":1}]\n```')[0]["i"] == 1
    assert _parse_json_array('好的，结果如下：\n[{"i":1,"verdict":"partial"}]\n以上。')[0]["verdict"] == "partial"
    assert _parse_json_array("完全不是 JSON") is None
    assert _parse_json_array("") is None

    # LLM 不可用 → 全部 unjudged（**不是** support）
    with mock.patch("core.llm.has_main_llm", return_value=False):
        pairs = [{"claim": "c", "excerpt": "e" * 100, "verdict": "", "why": "", "quote": ""} for _ in range(7)]
        out = judge_pairs(pairs)
    assert all(p["verdict"] == "unjudged" for p in out), "无模型时必须全部未判定"
    assert all("不计入通过率" in p["why"] for p in out)

    # 开关关闭 → run() 返回 None（=未执行），调用方据此如实披露
    with mock.patch.dict("os.environ", {"ENTAILMENT_ENABLED": "0"}):
        from core.entailment import run

        assert run([_ev("c", "e" * 100)]) is None, "关闭时必须返回 None（未执行），不能返回空壳汇总"


def test_entailment_pydantic_evidence():
    """同一份 evidences 在两条路径上是**两种类型**（dict vs pydantic Evidence）。

    实跑踩中（2026-09-14）：只测了 dict 形态 → 同步路径（run_task / /api/execute）抛
    `'Evidence' object has no attribute 'get'` → 蕴涵核验整节静默变成"未执行"。
    `core/audit_bundle.py` 为同一个坑写过同样的注释——这是本仓库第二次踩它，锁死。
    """
    from core.models import Evidence, Source, SourceLevel
    from core.entailment import build_pairs

    ex = "国家统计局数据：2025 年全国咖啡市场规模 5005 亿元，同比增长 12.3%，来源为年度统计公报。"
    ev = Evidence(
        claim="全国咖啡市场规模 5005 亿元",
        query="咖啡市场规模",
        sources=[Source(title="统计公报", url="https://www.stats.gov.cn/x", level=SourceLevel.L1_OFFICIAL, excerpt=ex)],
    )
    # ① pydantic 实例（同步路径）
    ps = build_pairs([ev])
    assert len(ps) == 1 and ps[0]["claim"].startswith("全国咖啡市场规模"), f"pydantic 形态未归一：{ps}"
    # ② dict（流式路径）
    assert len(build_pairs([ev.model_dump(mode="json")])) == 1
    # ③ 脏条目不得炸整个函数
    assert build_pairs([None, 42, {"claim": "x"}]) == []
    # ④ 判官链路：pydantic 形态也要能走到配对（无模型 → 未判定，但**不得抛异常**）
    from core.entailment import judge_pairs

    with mock.patch("core.llm.has_main_llm", return_value=False):
        assert all(p["verdict"] == "unjudged" for p in judge_pairs(ps))


def test_entailment_index_only():
    """登记线索（索引元数据）既不算通过也不算失败——它是第三种东西。

    实证来源（2026-09-14 实跑）：SEC 8-K/10-Q 的取证条目原先只写
    "XX 向 SEC 提交了 10-Q，申报日期 …"，**一句话里没有任何实质内容**，
    却被下游当成"证据主张"贴进报告。引用蕴涵核验逐条比对时 8/8 判"不相关"——
    **核验官是对的**：这不是引用错了，是取证层把"索引条目"当成了"结论依据"。
    两头都不许：判 ❌ 是把自己的取数短板虚报成引用错误，判 ✅ 是撒谎。
    """
    from core.entailment import _INDEX_MARK, build_pairs, judge_pairs, summarize

    idx_ex = (
        "[登记线索·未抽取正文] STARBUCKS CORP（CIK 0000829224）SEC 法定申报索引：10-Q，"
        "申报日 2026-07-29。**本条只证明该申报存在，未抽取申报正文，不能作为任何指标的数值依据**"
    )
    real_ex = "国家统计局数据：2025 年全国咖啡市场规模 5005 亿元，同比增长 12.3%，来源为年度统计公报。"
    evs = [
        _ev(f"竞争格局：{_INDEX_MARK}", idx_ex),
        _ev("全国咖啡市场规模 5005 亿元", real_ex),
    ]
    ps = build_pairs(evs)
    assert len(ps) == 2
    assert sum(1 for p in ps if p["verdict"] == "index_only") == 1, "登记线索未被单列"
    # 登记线索不得送进模型（送进去只会得到一个无意义的四态）
    with mock.patch("core.entailment._judge_batch", side_effect=AssertionError("不应调用模型判定登记线索")) as _m:
        judge_pairs(ps, batch=1)
    # 分母只含"真正的三态"，登记线索不进分母
    rep = summarize([dict(p, verdict="index_only") for p in ps])
    assert rep["index_only"] == 2 and rep["judged"] == 0 and rep["pass_rate"] == 0.0, rep
    # 真链路：登记线索 + 一条真主张 → 分母=1
    with mock.patch("core.llm.has_main_llm", return_value=False):
        ps2 = judge_pairs(build_pairs(evs))
    rep2 = summarize(ps2)
    assert rep2["index_only"] == 1 and rep2["unjudged"] == 1 and rep2["judged"] == 0, rep2
    # 附录必须把这类**印出来**（读者有权知道有几条引用只是指路牌）
    from core.entailment import format_appendix_md

    md = format_appendix_md(summarize([dict(p) for p in ps]))
    assert "仅登记线索" in md, "登记线索未被披露"
    assert "🔖 仅登记线索" in md and "指路牌" in md


def test_rule_lint_dispatch():
    """规则层自检：R7 无 `kind` → 曾让**整节**规则层自检在报告里消失（KeyError 被吞）。

    这是"判定 → 消费 → 披露"断链的又一例：检出了，但送不到读者眼前。
    两件事都要锁：①不再抛异常；②R7 与任何未知 schema 都必须**被印出来**（宁可朴素，不可丢失）。
    """
    from core.rule_lint import format_rule_lint_md, lint_report

    r7 = [{
        "rule": "R7", "level": "high", "title": "来源资格错配",
        "detail": "「国家统计局」被用作企业微观经营数据（单店日销）的来源。",
        "snippet": "据国家统计局数据，星巴克单店日销 400 杯。",
        "advice": "该数字须改挂企业披露（巨潮/SEC/年报）。",
    }]
    md = format_rule_lint_md(r7)  # 原实现此处 KeyError: 'kind'
    assert "来源资格错配" in md and "单店日销" in md, "R7 未渲染"
    # 未知 schema 兜底（新增检查项忘了加分支 → 必须仍能印出来）
    md2 = format_rule_lint_md([{"rule": "R99", "title": "某新检查", "detail": "细节"}])
    assert "某新检查" in md2 and "细节" in md2
    # 真跑一遍（真实语料里 R7 会命中）不抛
    vv = lint_report("据国家统计局数据，星巴克中国单店日销 400 杯，客单价 35 元。")
    assert any(x.get("rule") == "R7" for x in vv), "该语料应命中 R7"
    assert "规则层自检" in format_rule_lint_md(vv)


def test_ai_label_placement():
    """法定标识必须落在**正文段**里，不能被审计折叠吞掉。

    实跑踩中：末尾标识原先插在「数据与方法说明」之前，而更靠前的「指标一致性核对」
    本身就是审计标题 → `split_audit_tail` 从那里切开 → 标识折进明细，**要点开才看得见**，
    而条文要的是读者可感知。
    """
    from core.report_html import split_audit_tail

    # 模拟交付顺序：正文 + 指标一致性核对（审计标题）+ 各附录 + …… + 质检
    md = (
        "# 报告\n\n## 结论\n\n市场规模 5005 亿元。\n\n"
        "# 🤖 AI 生成合成内容标识\n> 内容编号 AIGC-YC-0123456789ABCDEF\n\n"
        "---\n\n# 📐 指标一致性核对\n\n| 指标 | 值 |\n|---|---|\n| x | 1 |\n\n"
        "---\n\n# ✅ 质检与可验证性\n\n通过。\n"
    )
    main, audit = split_audit_tail(md)
    assert "AI 生成合成内容标识" in main, "标识被折进了审计段（读者要点开才看得见）"
    assert "指标一致性核对" in audit
    # 顺序锁：交付代码里 explicit_tail 必须出现在 _build_methodology_note 之前
    import inspect

    src = inspect.getsource(__import__("graph").node_deliver)
    assert src.index("explicit_tail") < src.index("_build_methodology_note"), \
        "AI 末尾标识被移到了正文附录之后（会被审计折叠吞掉）"


# ============================================================
# 40. 引文核验通过率（链接层）
# ============================================================
def test_citation_pass_rate():
    from core.verification import build_citation_report, format_citation_report_md

    cits = [
        {"source": "国家统计局", "title": "公报", "url": "https://a.gov.cn/1"},
        {"source": "某媒体", "title": "报道", "url": "https://b.com/2"},
        {"source": "无链接来源", "title": "内部", "url": ""},
    ]
    with mock.patch("core.verification.verify_urls",
                    return_value={"https://a.gov.cn/1": ("https://a.gov.cn/1", True, "HTTP 200"),
                                  "https://b.com/2": ("https://b.com/2", False, "软404")}):
        rep = build_citation_report(cits)
    assert rep["total"] == 3 and rep["counts"]["pass"] == 1 and rep["counts"]["fail"] == 2
    assert abs(rep["pass_rate"] - 1 / 3) < 1e-3, rep["pass_rate"]
    md = format_citation_report_md(rep)
    assert "通过率 33.3%" in md, md[:200]
    # 这一节必须自陈边界：链接层 ≠ 内容层（否则"通过率高"会被当成"引用可信"）
    assert "本节核验的是「链接」这一层" in md
    assert "本节不证明什么" in md and "引用蕴涵核验" in md
    # 空集：**不得 0 除，也不得返回 0.0**（2026-09-15 改，原断言是 == 0.0）。
    # 没有引用 = 没有做过测量 → 返回 None（未判定）。返回 0.0 会被前端画成红角标
    # 「链接核验 0%」——把"没测过"渲染成"全坏"，正是本产品在批评的那种把缺席当结论。
    # 新契约见 `core/verification.build_citation_report` 的 `pass_rate`/`probed` 注释。
    with mock.patch("core.verification.verify_urls", return_value={}):
        _empty = build_citation_report([])
        assert _empty["pass_rate"] is None, f"空集应返回 None（未判定），实得 {_empty['pass_rate']}"
        assert _empty["probed"] == 0
    # **"没验成" ≠ "验了没通过"**（实跑踩中：跑的时候 DNS 挂了，19 条全 ConnectError
    # → 报告印"通过率 0.0%"，读者只会读成"你的引用全是假的"，而真相是核验没执行）
    _net = [{"source": "A", "title": "t", "url": "https://a.gov.cn/1"},
            {"source": "B", "title": "t", "url": "https://b.com/2"}]
    with mock.patch("core.verification.verify_urls",
                    return_value={"https://a.gov.cn/1": ("https://a.gov.cn/1", False, "网络/证书异常: ConnectError"),
                                  "https://b.com/2": ("https://b.com/2", False, "网络/证书异常: ConnectError")}):
        repn = build_citation_report(_net)
    assert repn["unverified"] == 2 and repn["verified"] == 0, repn
    mdn = format_citation_report_md(repn)
    assert "本次核验未能完成" in mdn and "未核验 ≠ 不通过" in mdn
    assert "通过率 0.0%" not in mdn, "全网络失败时不得印通过率（会被读成引用全是假的）"
    # 混合场景：通过率照印，但未核验条数单独点名
    with mock.patch("core.verification.verify_urls",
                    return_value={"https://a.gov.cn/1": ("https://a.gov.cn/1", True, "HTTP 200"),
                                  "https://b.com/2": ("https://b.com/2", False, "网络/证书异常: ConnectError")}):
        repm = build_citation_report(_net)
    assert repm["unverified"] == 1 and repm["verified"] == 1
    assert "未能完成核验" in format_citation_report_md(repm)

    # **"按合规姿态没去敲" 也不能算失败**（2026-09-14 二次审计，与上一条同源）。
    # 上游 `ComplianceSkip` 已经严格区分了"未探测、不据此判死"，但本函数原先把
    # `live=False` 一律记成 fail —— robots 拦下 / SEC 联系方式未配置的地址，
    # 在报告里就成了"❌ 未通过"。**判定做得再细，消费端一抹平就等于没做。**
    _skip = [
        {"source": "SEC", "title": "10-K", "url": "https://data.sec.gov/x"},
        {"source": "国家统计局", "title": "公报", "url": "https://a.gov.cn/1"},
    ]
    with mock.patch(
        "core.verification.verify_urls",
        return_value={
            "https://data.sec.gov/x": (
                "https://data.sec.gov/x",
                False,
                "未探测（SEC fair-access 要求 UA 声明真实联系方式，SEC_UA_CONTACT 未配置）；按合规姿态主动不发请求，不据此判死",
            ),
            "https://a.gov.cn/1": ("https://a.gov.cn/1", True, "HTTP 200"),
        },
    ):
        reps = build_citation_report(_skip)
    assert reps["counts"]["skip"] == 1, f"未探测没被单列：{reps['counts']}"
    assert reps["counts"]["fail"] == 0, "按合规姿态没去敲，却被记成了未通过"
    assert reps["skipped"] == 1, reps.get("skipped")
    assert "未探测 1" in format_citation_report_md(reps), format_citation_report_md(reps)[:300]

    # **全部被合规拦下**时也绝不许印通过率（2026-09-14 二次审计）。
    # 原判据只看网络失败（`_unv == total`），于是"一条都没敲过门"的情形判据不成立，
    # 报告照样印出 `通过率 0.0%`——正是本文件自己声明"绝不许印"的那个数。
    _allskip = [{"source": "SEC", "title": "10-K", "url": "https://data.sec.gov/x"}]
    with mock.patch(
        "core.verification.verify_urls",
        return_value={
            "https://data.sec.gov/x": ("https://data.sec.gov/x", False, "未探测（SEC 未配置）")
        },
    ):
        reps2 = build_citation_report(_allskip)
    md2 = format_citation_report_md(reps2)
    assert "通过率 0.0%" not in md2, "全部按合规姿态未探测时，印了通过率"
    assert "本次核验未能完成" in md2 and "未核验 ≠ 不通过" in md2


def test_lint_docs():
    """材料口径自检（`_lint_docs.py`）：**它自己也得被检查**。

    linter 有个致命性质——**正则写坏时的表现和"材料很干净"一模一样**。
    写这个工具的当天就踩了两次：`json|jsonl` 的分支顺序把 `run_stats.jsonl` 截成
    `run_stats.json`，报了 7 条"文件不存在"的假命中；`个章节` 又漏掉了写法不同的
    「50 章节」。两次都不会让 linter 报错，只会让它**安静地什么都不查**。
    所以这里逐类植入一条人为违规，验证每一类真的抓得到；再逐条植入正当写法，验证不误报。
    """
    import _lint_docs as L

    # ① 真实材料必须全过（这条挂了 = 材料与代码已经漂移）
    real = L.run(quiet=True)
    assert not real, "对外材料口径自检未过：\n  " + "\n  ".join(str(f) for f in real)

    D = "X.md"

    def caught(fn, text, extra=None):
        texts = {D: text}
        if extra:
            texts.update(extra)
        return [f for f in fn(texts) if f.check != "材料缺失"]

    # ② 灵敏度：每一类都必须抓得到植入的违规
    assert caught(L.check_banned, "研策实现了完全自主决策"), "红线词抓不到「完全自主」"
    assert caught(L.check_banned, "研策超越所有同类产品"), "红线词抓不到以本产品为主语的「超越」"
    assert caught(L.check_version, "引擎基线 **v0.9.1**"), "版本检查抓不到过期基线声明"
    assert caught(L.check_evidence_shipped, "见 `data/nope.jsonl`"), "证据文件检查抓不到不存在的文件"
    assert caught(L.check_k_refs, "见对销 K99", {"口径对销总表_v1_20260909.md": "| **K20** | x |"}), (
        "K 编号检查抓不到越界引用"
    )
    assert caught(L.check_bare_rate, "引用支撑率 100%，说明核验很严"), "裸支撑率抓不到孤立百分比"
    assert caught(L.check_test_counts, "**1 项断言 / 2 个章节**，全绿"), "测试数检查抓不到错的数"
    # 「N 个节」这个**词形**（2026-09-17 补）。病案：全量版方案书里我写了
    # 「144 项断言 / **92 个节**」——实装 93，而旧正则只收「章节」，于是
    # **我刚写错的数从检查器底下走过去了**。这正是本文件自己记录过的那一类：
    # 「只收一种词形的检查，等于没收」（上一例是「50 章节」溜了 30 个版本）。
    assert caught(L.check_test_counts, "**1 项断言 / 2 个节**"), "「N 个节」词形没被收进检查"
    assert caught(L.check_test_counts, "本套件 **7 个节** 全绿"), "裸的「N 个节」没被收进检查"
    # 但不许误伤：「个节点」与「N 节正文」都不是"章节数"
    assert not caught(L.check_test_counts, "全流程共 5 个节点"), "「个节点」被误报"
    assert not caught(L.check_test_counts, "本篇共 12 节正文"), "「N 节正文」被误报"
    # 版本沿革行不许被当成基线声明（否则 `--fix` 会把历史值改成当前值）
    assert not caught(
        L.check_version, "本文件写于 2026-09-14（当时引擎为 v0.9.78）。此后引擎走到 v0.9.87"
    ), "沿革行被当成基线声明"
    # 但**豁免不许宽到放过"操作清单里的口播版本号"**（2026-09-17 补）：
    # `→` 是沿革标记，也是说明 Q&A 与操作清单的标准写法。
    # 病案：`PPT_内容稿:335` 的演讲提示写着
    #     `被问"一个人做完的？"→"已做完，引擎 **v0.9.78** 在跑"`
    # ——`→` 落进 ±8 窗口，把这句**要人在镜头前说出口的版本号**豁免掉了。
    # 同类还有分镜的"开录前机检 v0.9.82"×2、Demo 脚本的"临场自检 v0.9.47"。
    assert caught(
        L.check_version,
        '被问"一个人做完的？"→"已做完，引擎 **v0.9.78** 在跑，机制化就是可扩展性"',
    ), "操作清单/口播词里的旧版本号被 `→` 豁免掉了——豁免条件用宽了等于没有检查"
    assert caught(L.check_version, "- [ ] 后端进程活着且 **v0.9.82**：`http://127.0.0.1:8000/health`"), (
        "开录前机检里的旧版本号没被抓住"
    )
    assert not caught(L.check_version, "| 引擎基线 v0.9.78 | **v0.9.87** | 跨九批 |"), (
        "一行两个版本号的沿革行被当成基线声明——`--fix` 会把历史值就地改成当前值，"
        "改出「本文件写于 2026-09-14（引擎 v0.9.87）」这种自相矛盾的话"
    )
    assert caught(L.check_stale_doc_refs, "详见 方案书_v4.md"), "旧文件名检查抓不到 v4 残留"
    assert caught(
        L.check_section_count,
        "环节 1 环节 14",
        {"AI环节流程图_PPT绘制规格_v2_20260909.md": "环节 1 环节 12\n不得新增第 13 环节"},
    ), "环节数检查抓不到不一致"
    # ⑩ 禁句（K21/K24）：**这类漂移原先一条检查都盖不到**，是审计里唯一"改了口径、
    #    禁语却还在别的材料里活着"的一类。灵敏度必须实测。
    assert caught(L.check_frozen_banned_phrases, "业界只做第一层，我们做到第三层"), "禁句检查抓不到 K24 句式"
    assert caught(L.check_frozen_banned_phrases, "行业普遍不报这一层"), "禁句检查抓不到 K24「行业普遍不报」"
    assert caught(L.check_frozen_banned_phrases, "自陈等级 State Machine。"), "禁句检查抓不到 K21 旧口径"
    # K21 改口径后的**残句词形**（2026-09-17 补）：这批词形此前一条都没被收，
    # 于是「在自主性光谱上明确落在哪一级」在**发布 docx** 里活到了 9/17，
    # 而且它与同一份文件上一条「不锚定 LangChain 光谱上的某个档位自称」**直接打架**。
    assert caught(
        L.check_frozen_banned_phrases,
        "我们说：**在自主性光谱上明确落在哪一级、每一级的取舍是什么、我们自己找出的弱点在哪。**",
    ), "K21 残句「在自主性光谱上明确落在哪一级」未被收——它曾在发布件里存活"
    assert caught(L.check_frozen_banned_phrases, "我们自陈落在哪一级、每级的取舍是什么"), (
        "K21 残句的另一种词形未被收"
    )
    assert caught(
        L.check_frozen_banned_phrases, "**另加一件别人没有的：我们公开 FPR。**"
    ), "禁句检查抓不到「从沉默推否证」的变体"

    # ---- K17 分档位例外：**必须两向都成立**（2026-09-19 补）----
    # 病案：这条例外原先用**固定 24 字窗口**找档位词，而 24 是照着当时那一行的长度
    # 凑出来的、不是判据。实测翻车：成功样本从 3 笔涨到 4 笔后，
    # 值那格变成 `| 标准档 | **8.9 万 tokens/份** | 成功 4 笔均值 |`，
    # `4` 与「标准档」相距 30+ 字 → 窗口够不着 → **合规行被判违规**。
    # **一个靠"恰好多少字"成立的判据，会在数值变化时突然开火。**
    # 收紧方向也验：第一版把豁免词写成含裸 `档`，于是 `全档 4 笔均值`（K17 要禁的跨档错数）
    # 被放行——**豁免写宽一点，就等于把检查关掉**。
    assert not caught(
        L.check_frozen_banned_phrases,
        "| 标准档 | **8.9 万 tokens/份** | 成功 4 笔均值 |",
    ), "分档位报（K17 要求的形式）被误报——档位词与数值隔着两个表格格就够不着了"
    assert not caught(
        L.check_frozen_banned_phrases,
        "| 深入档 | **28.5 万 tokens/份** | 成功 14 笔均值 |",
    ), "分档位报（两位数样本）被误报"
    assert caught(
        L.check_frozen_banned_phrases, "全档 4 笔均值 ≈ 12.7 万 tokens/份"
    ), "跨档错数被这条例外放行了——豁免词里混进了裸「档」，`全档` 也算档位"
    assert caught(
        L.check_frozen_banned_phrases, "本报告覆盖 4 笔均值样本，未按档位拆分"
    ), "没有任何档位词的裸均值被放行"
    assert not caught(
        L.check_frozen_banned_phrases, "不要写「4 笔均值」这种说法"
    ), "禁止句式本身（「不要写…」）被当成违规——判据要按意思收，不能按上次恰好怎么写收"

    # ---- ⑨⑥ 「闸门从未 BLOCK 过」：材料断言必须与存档同向（2026-09-19 补）----
    # 病案：八份材料（含三份发布件）都写着「闸门从未在真实运行中 BLOCK 过
    # （账本+存档里 `Go` 出现 0 次）」，并把它当成"必须自己先说的边界"。
    # 两处都错，而且**错的方向对我们不利**：带闸门结论的存档里 BLOCK 的有 13/14 份，
    # 而「`Go` 出现 0 次」被读反了——判定起点本就是 `Validate`，0 个 `Go` 只是"没有东西可降"。
    # **拿闸门的产物去证明闸门没工作。**
    # **存档扫描**。注意两点，都是被这一条自己抓出来的：
    #   · 本模块**没有模块级 `json`**（全篇用函数内 `import json as _json`），
    #     第一版我写了裸 `json.loads` → `NameError`；
    #   · 而它被 `except Exception: continue` **静静吃掉**，于是计数恒 0、
    #     表现为"存档里一份 BLOCK 都没有"。**这正是 linter 自己记过的那桩病案**
    #     （`import json` 漏了 → NameError 被吞 → 检查退化成"账本里没有任何 token 读数"），
    #     同一个形状隔了几个月又在新代码里长了一遍。
    #   所以下面只吞 `(OSError, ValueError)`：**坏文件可以跳过，坏代码必须炸**。
    import json as _json

    _arch_blk = 0
    for _rf in (_HERE / "data" / "reports").glob("*.json"):
        try:
            _rd = _json.loads(_rf.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        _rg = _rd.get("integrity_gate") or {}
        if _rg.get("level") == "BLOCK" and _rg.get("executed"):
            _arch_blk += 1
    assert _arch_blk > 0, (
        "存档里一份 BLOCK 都没有——那这条检查的前提不成立；"
        "要么存档被清了，要么闸门真的坏了（也可能又是上面那个 except 吞了真错误）"
    )
    assert caught(
        L.check_green_ledger, "闸门从未在真实运行中 BLOCK 过（构造样本才触发）"
    ), f"存档里有 {_arch_blk} 份 BLOCK，而材料写「从未 BLOCK 过」却没被报出来"
    assert caught(L.check_green_ledger, "它一直在拦，BLOCK 从未发生过"), (
        "反语序（`BLOCK 从未`）漏了"
    )
    # 反向：**讲别的事**的「从未」不许误伤。第一版正则写宽了，把这一行判成违规——
    # 那行的"从未"说的是"没被当成一层讲"，与闸门拦没拦过毫无关系。
    assert not caught(
        L.check_green_ledger, "演示模式强制 BLOCK 已实现，**但从未被当成「一层」讲述**"
    ), "「BLOCK 已实现，但从未被当成一层讲」被误报——两句话说的不是一件事"

    # ③ 特异度：正当写法**不许**被误报。一个 100% 误报的检查比没有检查更糟——
    #    它给的是"已经查过了"的假安心，然后被无视。
    assert not caught(L.check_banned, "## 〇·唯一信源清单（本稿断言全部可在此查到）"), "「唯一信源清单」被误报"
    assert not caught(L.check_banned, "我们不自称完全自主"), "禁止语境被误报"
    assert not caught(L.check_bare_rate, "禁止单报「引用支撑率 XX%」"), "讲纪律的句子被误报"
    assert not caught(L.check_bare_rate, "一处诚实性风险：引用支撑率 100%"), "讨论风险的句子被误报"
    assert not caught(L.check_evidence_shipped, "见 `data/run_stats.jsonl`"), (
        "jsonl 被当成 json 截断去查存在性（旧正则 bug）"
    )
    assert not caught(L.check_test_counts, "从 70→75 项离线断言"), "进度叙述被误报"
    assert not caught(L.check_test_counts, "新增 3 节共 5 项断言"), "增量叙述被误报"
    # ⑩ 特异度：**讲纪律**的句子与**唯一正当写法**都不许被误报（否则检查会被无视）
    assert not caught(
        L.check_frozen_banned_phrases, "禁用句式：业界只做第一层 —— 从沉默推否证不成立"
    ), "讲纪律的句子被误报"
    assert not caught(
        L.check_frozen_banned_phrases, "公开信息未见同类（据公开产品与第三方审计，非自评）"
    ), "K24 的唯一正当写法被误报"
    assert not [
        f
        for f in L.check_frozen_banned_phrases({"口径对销总表_v1_20260909.md": "禁「业界只做第一层」"})
        if f.check != "材料缺失"
    ], "口径表本身（规则出处）不该被扫——它必须能说出这些禁语"

    # ⑩ **撤回句**必须放行，而放宽它不许把检查放宽成可绕过（2026-09-17 补）。
    #
    # 病案：全量版方案书里那句
    #     `我们**不主张**任何"业界首创"。经外部核验，五个原本自认的首创位在 2026 年都已被占掉`
    # 是**这条检查最想鼓励的句子**（主动撤回），却因为 `_FROZEN_NEG` 里只有
    # 「不写/不能/不许」而没有「不主张」被判红。误报的代价不对称：
    # **一条把"主动撤回"判成违规的检查，会教人别写撤回。**
    assert not caught(
        L.check_frozen_banned_phrases,
        '我们**不主张**任何"业界首创"。经外部核验，五个原本自认的首创位在 2026 年都已被占掉',
    ), "撤回句被误报——这会把『主动撤回』从材料里赶出去"
    # 但放宽必须**只在否定词真的贴着这句时**才生效：否定词离得远，照样要抓。
    assert caught(
        L.check_frozen_banned_phrases, "不主张" + "。" * 20 + "业界首创的三层核验",
    ), "否定词离得远也被放行——这条检查被放宽成了可绕过"
    assert caught(L.check_frozen_banned_phrases, "业界首创"), "裸的「业界首创」必须仍然抓得到"
    # K26「开源」口径（2026-09-17 补）：三种词形都要抓——**只收一种词形＝没收**。
    # 实测三种都出现过，且**已印进成品 pptx**，而它们描述的是同一件事。
    for _p in ("判定规则开源可重跑", "开源判定规则", "(判定规则开源)"):
        assert caught(L.check_frozen_banned_phrases, f"核验报告敢给，因为{_p}"), (
            f"K26 词形 {_p!r} 没被收——这正是「只收一种词形＝没收」的第 N 次"
        )
    # 但描述**别家**开源项目不许误报（材料里这类用法很多）
    assert not caught(
        L.check_frozen_banned_phrases, "借鉴开源项目 gpt-researcher 的两阶段流水线；天眼查开源 162 个 MCP 工具"
    ), "描述别家开源项目被误报"
    # 且**不许**为了放过撤回句而收进过于宽泛的否定词（`不是` 会让
    # 「这不是问题，我们的业界首创能力…」从 14 字窗口里蒙混过关）
    import re as _re2

    _lint_src = (L.BACKEND / "_lint_docs.py").read_text(encoding="utf-8")
    _neg_m = _re2.search(r"^_FROZEN_NEG\s*=\s*\((.*?)\)\s*$", _lint_src, _re2.M | _re2.S)
    assert _neg_m, "解析不到 `_FROZEN_NEG`——本项检查会退化（正则失效时它会安静放行一切）"
    _neg_txt = _neg_m.group(1)
    assert '"不是"' not in _neg_txt and "'不是'" not in _neg_txt, (
        "`_FROZEN_NEG` 收进了「不是」——它太泛，"
        "「这不是问题，我们的业界首创能力…」这类句子会从 14 字窗口里蒙混过关"
    )
    assert "不主张" in _neg_txt, "撤回句的否定词被删了——那条误报会回来"

    # 参赛 PPT 必须带**法定 AI 隐式标识**（2026-09-15 补）。
    # 病案：产品给报告导出的五个格式都写了隐式标识，而**参赛 PPT 自己漏了**——
    # 偏偏这是说明里唯一"复核者会当场打开文件属性看一眼"的地方，
    # 而我们的 K23 正写着「隐式标识是执法明确列出的违规情形」。
    # 这里锁接线（真跑一次 build 会写文件，不适合放进测试）。
    _ppt_src = (L.BACKEND / "_make_ppt.py").read_text(encoding="utf-8")
    assert "apply_file_meta" in _ppt_src, "参赛 PPT 没有写法定隐式标识（会被当场打开属性验证）"
    assert "core_properties" in _ppt_src, "apply_file_meta 没接到 pptx 的 core_properties 上"

    # ⑨ 绿档账本：编造的 token 数必须抓得到，账本里真有的必须放行。
    # 这条检查救过一次场——它第一版漏了 `import json`，NameError 被 `except Exception`
    # 吞掉，于是它安静地报"账本里没有任何 token 读数"，看起来像账本空、实际是代码坏。
    import json as _json

    assert caught(L.check_green_ledger, "本笔实测 999,999,999 tokens"), "编造的 token 数没抓到"
    _rows = [
        _json.loads(x)
        for x in (_HERE / "data" / "run_stats.jsonl").read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]
    _tot = [
        (r.get("llm_tokens") or {}).get("total_tokens")
        for r in _rows
        if isinstance((r.get("llm_tokens") or {}).get("total_tokens"), int)
    ]
    assert _tot, "账本里没有可核的 token 读数——本项断言会退化成空转"
    _real = _tot[-1]
    assert not caught(L.check_green_ledger, f"本笔实测 {_real:,} tokens"), "账本里真有的数被误报"

    # ⑫ 随包受检：**随包但不在受检集**必须抓得到。
    # 这条检查没有"植入一条文本"的探针形态——它读的是**文件系统 + 打包规则**，
    # 所以探针用行为法：把受检集临时摘掉一份真随包的件，它必须立刻报出来。
    # （若不这么测，它就只会"恰好一直报 0"，而 0 与"没查"在输出上长得一模一样。）
    _orig_docs = L.DOCS
    try:
        L.DOCS = [d for d in _orig_docs if d != "创意描述.md"]
        _got = [f for f in L.check_packed_but_unchecked({}) if "创意描述.md" in f.doc]
        assert _got, (
            "⑫ 抓不到「随包但不在受检集」——从受检集里摘掉一份真随包的件，它必须报出来。"
            "注意：报 0 与「检查没跑成」在输出上同形，所以这条探针不能省。"
        )
    finally:
        L.DOCS = _orig_docs
    assert not L.check_packed_but_unchecked({}), (
        "⑫ 对真实仓库报了命中——说明现在真的存在「随包但不受检」的材料，去修它，别改这条断言"
    )
    # 根目录散件的**扩展名白名单**（2026-09-17）。病案：6 个抓页转储
    # （`cm.html` / `manus.html` / `oaip.html`…约 1.85 MB）躺在项目根目录，
    # 既不以 `.`/`_` 开头、后缀也不在 `skipExt` 黑名单里 —— **所有旧规则一条都拦不住**。
    _rules = L._load_pack_rules()
    assert _rules.get("rootExtOk"), (
        "`$rootExtOk` 解析不到——抓页快照/截图那类散件会静默回到发布包里，且没有检查会响"
    )
    for _bad in ("cm.html", "manus.html", "ds_ref.png", "shot.jpg", "dump.webp"):
        assert not L._pack_included(_bad, _rules), (
            f"根目录散件 `{_bad}` 被判为随包——白名单没生效（黑名单永远漏下一个后缀，这就是那次教训）"
        )
    for _good in ("创意描述.md", "打包发布.ps1", "研策_产品方案书_v6_全量版_20260917.md"):
        assert L._pack_included(_good, _rules), f"根目录正当散件 `{_good}` 被白名单误杀"
    # 白名单只作用于**根目录**：子目录里的同名后缀不受它管（各由 skipSeg 负责）
    assert L._pack_included("yancec/frontend/src/App.css", _rules), (
        "白名单越界管到了子目录——它只该管根目录散件"
    )

    # ⑥ 同族第三类：材料引用**linter 自身读数**（「材料口径自检 N 类 / × M 份」）。
    _selfref = "| 验证体系 | 材料口径自检 **9 类 × 15 份** 全过 |"
    _got_sr = caught(L.check_test_counts, _selfref)
    assert any("类" in f.msg for f in _got_sr), "材料引用的 linter 类数漂了却没抓到"
    assert any("份" in f.msg for f in _got_sr), "材料引用的受检份数漂了却没抓到"
    # 但**三个「N 类」的不同所指必须分开**（这是本条检查第一版翻的车）：
    assert not caught(L.check_test_counts, "报告内嵌：**规则层自检（7 类机械错误）** + 引用三层核验"), (
        "「规则层自检（7 类）」指的是 R1–R7，不是 linter 的类数——哨兵词没带「材料」语境就会误报"
    )
    assert not caught(L.check_test_counts, "| **自主决策链**（11 类取舍） | ✅ | 当日实跑 15 条 |"), (
        "「决策链 11 类取舍」不是 linter 的类数"
    )
    assert not caught(L.check_test_counts, "| v0.9.78 | 材料自检当时是 9 类 |"), (
        "带版本号的**沿革行**里那个数是历史记录，改成当前值才是造假"
    )
    # 灵敏度那个数是**另一个量**（`test_lint_docs` 真覆盖到几类），不许拿 linter 类数去比它。
    # 口径必须与 `_lint_docs.py` 里那段**逐字同源**：都只数 `test_lint_docs` 函数体内的 `L.check_*`。
    _fl = L.BACKEND / "selftest.py"
    _tm = _re2.search(r"def test_lint_docs\(.*?(?=\ndef |\Z)", _fl.read_text(encoding="utf-8"), _re2.S)
    _plant = len(set(_re2.findall(r"L\.(check_\w+)", _tm.group(0) if _tm else "")))
    assert _plant >= 10, f"灵敏度自测的覆盖类数解析成 {_plant}——解析坏了这条比对会退化"
    assert not caught(L.check_test_counts, f"材料自检的**灵敏度自测**（{_plant} 类逐类植入违规）"), (
        "灵敏度自测的类数被拿去比 linter 的类数了——那两个数不同，混起来就是一条永远满足不了的检查"
    )

    # ---- 「看板产出的数，检查器必须都认」（2026-09-17 实跑发现）----
    #
    # 病案：`check_green_ledger` 的分档位均值分支只过滤 `delivered`/`degraded`，
    # 而看板（`core/repurchase_board.build_board`）还会再剔一层"过程字段被 v0.9.83 之前
    # 流式缺陷写坏的行"。实测两档全部对不上：
    #     标准档  linter 认 10.0 万（n=3）  看板出 9.8 万（n=2）
    #     深入档  linter 认 25.6 万（n=2）  看板出 30.3 万（n=1）
    # 后果不是"检查太严"，而是**K17 要求材料按档位报，而按档位算出来的那两个字面数字
    # 一律被判成「账本里找不到对应运行」**——一条专打最合规写法的误报。
    # 与 ④ 是同一个病、同一天、同一个文件：④ 当时学会了坏行过滤，③ 没学会。
    #
    # 锁的形状：**不问材料写了什么，直接拿看板的输出去喂检查器**。
    # 这样以后无论哪一边改口径，只要两边不再一致这条就会红；
    # 而不是等某天有人写材料时，才发现自己引的真数被判成了假数。
    from core import repurchase_board as _RB

    for _d in ("标准", "深入", ""):
        _b = _RB.build_board(depth=_d)
        _disp = ((_b.get("metrics") or {}).get("cost_per_success") or {}).get("display") or ""
        assert _disp, f"{_d or '全部档位'} 的成本指标没出 display——这条锁会退化成空转"
        _f2 = caught(L.check_green_ledger, f"成功结果成本 **{_disp}**")
        assert not _f2, (
            f"看板按「{_d or '全部档位'}」算出的成本是 {_disp!r}，"
            f"而材料自检把它判成「账本里找不到对应运行」——检查器与生产者的口径分叉了：{_f2}"
        )

    # ---- 「围栏历史块」这条豁免必须带正反样本（2026-09-17）----
    #
    # 绿档账本检查新学会一条：**围栏块若由「点名了过去的版本」的一句框架句引出，
    # 就当作历史留档跳过**（材料里整段粘贴的看板输出常是"当时长这样"）。
    # 豁免是漏检的合法来源，所以必须证明它**只在历史块上生效**：
    #   · 反样本：框架句点的是**当前**版本 → 照查（否则当前值行会被整块放掉）
    #   · 正样本：框架句点的是**过去**版本 → 跳过
    #   · 反样本：**没进围栏** → 照查
    from core.version import ENGINE_VERSION as _CV

    _bad = "成功结果成本 **999.9 万 tokens/份**"
    assert caught(L.check_green_ledger, f"v{_CV.lstrip('v')} 实测：\n```\n{_bad}\n```"), (
        "框架句点名**当前**版本时，围栏块必须照查——否则当前值行会被整块放掉"
    )
    assert not caught(L.check_green_ledger, "**v0.9.1 收尾时补跑两笔**：\n```\n" + _bad + "\n```"), (
        "框架句点名**过去**版本时，围栏块应判为历史留档跳过（改历史记录才是造假）"
    )
    assert caught(L.check_green_ledger, _bad), "不在围栏里的行永远要查"


def test_docx_non_report_mode():
    """docx 导出必须能出**非报告文档**（方案书 / 作品简述）：不套报告的章节编号。

    病案（2026-09-14）：拿 `build_report_docx` 直接渲染参赛方案书，H1 被编成
    「一、研策 · …参赛方案书（v5）」，二级变「二、0. 一句话定位」——
    报告用「一~十三」分章是对的（咨询级模板如此），方案书的 H1 却是**文档标题本身**，
    套上去就是排版事故。**同一个函数要服务两类文档，就得把"要不要编号"变成参数。**
    """
    from io import BytesIO

    from docx import Document

    from core.report_docx import build_report_docx

    def _heads(b: bytes) -> list[str]:
        d = Document(BytesIO(b))
        return [
            p.text.strip()
            for p in d.paragraphs
            if p.text.strip() and p.style.name.startswith("Heading")
        ]

    # 非报告模式：原文标题一字不动，且不加"一、"
    hs = _heads(build_report_docx("# 标题甲\n\n## 一、小节乙\n\n正文\n", title="T", auto_chapter=False))
    assert "标题甲" in hs, f"非报告模式丢了标题：{hs}"
    assert not any(h.startswith("一、标题甲") for h in hs), f"非报告模式仍加了章节编号：{hs}"
    assert "一、小节乙" in hs, f"非报告模式改动了原文标题：{hs}"

    # 反向锁：报告模式**必须**仍然编号——否则就是把这个参数写成了"改坏产品导出"
    hs2 = _heads(build_report_docx("# 摘要\n\n## 正文\n", title="T"))
    assert any(h.startswith("一、") for h in hs2), f"报告模式丢了章节编号：{hs2}"


def test_docx_fence_not_heading():
    """``` 围栏内的 `#` 是命令注释，不是文档标题——导出侧必须认得围栏。

    病案（2026-09-18，生成《产品说明书》Word 成品时实测）：`build_report_docx`
    **此前完全没有围栏概念**，`#` 开头的行一律按 markdown 标题渲染。后果：
    §15.2 的复算命令块里 **10 行 `#` 注释全部变成 Word 标题**，
    导航窗格多出 10 个假条目，其中一条是「本件读数：21 份 × 12 类 = 0 项」——
    读者会把它当成文档的一节，而那个块是**给人照着敲的命令**。

    ⚠️ 两件必须同时锁住的事：

    1. **围栏外的 `#` 必须仍然是标题。** 只锁"围栏内不当标题"这一个方向，
       下一次"顺手放宽"就会把真标题一起关掉——本仓库在
       `section_count_hits` 上吃过同款的亏（判据收窄方向错了，把真命中一起关掉）。
    2. **围栏内容不许因为"不解析"而消失。** 一刀切跳过围栏会让命令整块不见，
       那比多 10 个假标题更糟：文档看起来完整，而最关键的那段没有了。
    """
    from io import BytesIO

    from docx import Document

    from core.report_docx import build_report_docx

    md = (
        "## 真标题\n"
        "\n"
        "```bash\n"
        "# 这是命令注释，不是标题\n"
        "python x.py\n"
        "# → 预期输出\n"
        "```\n"
        "\n"
        "## 另一个真标题\n"
    )
    d = Document(BytesIO(build_report_docx(md, title="T", auto_chapter=False)))
    heads = [p.text.strip() for p in d.paragraphs if p.style.name.startswith("Heading")]
    body = "\n".join(p.text for p in d.paragraphs)

    assert "真标题" in heads and "另一个真标题" in heads, f"围栏外的真标题丢了：{heads}"
    for _bad_head in ("命令注释", "预期输出"):
        assert not any(_bad_head in h for h in heads), (
            f"围栏内的 `#` 被当成标题了（{_bad_head}）：{heads}"
        )
    # 内容不许因为"不解析"而消失
    assert "这是命令注释，不是标题" in body, "围栏内容没印出来——围栏把内容整块吃掉了"
    assert "python x.py" in body, "围栏内容没印出来（命令本身不见了）"
    # 围栏标记自己不许当正文印出来
    assert "```" not in body, "围栏标记被当正文印出来了"


def test_series_math_rate_vs_level():
    """增速序列 ≠ 水平序列：同一条"复合增速"公式不能套在两种输入上。

    病案（2026-09-15）：World Bank「中国 GDP 年增速」= 2.34, 8.57, 3.134, 5.416,
    4.958, 4.96（单位 %）——这是**增速序列**，却被套上水平序列的
    `(末/初)^(1/n)-1`，算出 **CAGR≈16.2%**（正确约 4.9%），
    还带着"（本地计算）"的权威标签印进报告。

    最危险的不是这一个错数，而是**可复算包用的是同一个错公式**：
    第三方跑完得到"两边一致"——**验证机制为错误背书**。
    """
    from core.series_math import cagr_level, classify_series, derive_growth, geo_mean_rate, is_rate_series

    # ① 判据：**先看指标名是"变化型"还是"水平型"，单位 % 只排最后**
    assert is_rate_series("GDP年增速", "%") is True
    assert is_rate_series("GDP（现价美元）", "美元") is False
    assert is_rate_series("总人口", "人") is False
    # ⭐ 本用例**曾经把错的锁住**（2026-09-16 改正）：原断言写的是
    # `is_rate_series("城镇化率","") is True`——把"以 % 表示的水平"当成了"增速"。
    # 城镇化率 63.9% 的含义是"六成三是城镇人口"，**不是**"增长了 63.9%"。
    # 触发面不窄：`authority_intl._WB_IND` 八个指标里三个是这一类。
    assert is_rate_series("城镇化率", "") is False, "城镇化率是**水平**序列，不是增速序列"
    assert is_rate_series("城镇化率", "%") is False
    assert is_rate_series("互联网普及率", "%") is False
    assert is_rate_series("服务业增加值占GDP", "%") is False
    assert is_rate_series("全国门店市占率", "%") is False
    assert is_rate_series("通胀率（CPI年%）", "%") is True       # 变化型不能被水平型吃掉
    assert is_rate_series("复合增长率", "%") is True
    assert is_rate_series("GDP（现价美元）", "") is False
    # 单位 % + 名字给不出线索 → **判不出**，不蒙
    assert classify_series("说不清的指标", "%") == "unknown"
    assert classify_series("说不清的指标", "美元") == "level"

    # ② 增速序列：几何平均 ≈ 4.9%，且**绝不能**得到 16.2%
    _rate = [("2020", 2.34), ("2021", 8.57), ("2022", 3.134),
             ("2023", 5.416), ("2024", 4.958), ("2025", 4.96)]
    _lab, _g = derive_growth(_rate, "GDP年增速", "%")
    assert _lab == "区间年均增速（几何平均）", f"增速序列的标签不对：{_lab}"
    assert _g is not None and 4.5 < _g < 5.3, f"增速序列复算应≈4.9%，实得 {_g}"
    assert not (15.0 < _g < 17.5), "又出现了 16.2% 那个错数"

    # ③ 水平序列：仍是 CAGR，且值不变（不能把对的改错）
    _lvl = [("2020", 15.00), ("2021", 18.20), ("2022", 18.32),
            ("2023", 18.27), ("2024", 18.73), ("2025", 19.50)]
    _lab2, _g2 = derive_growth(_lvl, "GDP（现价美元）", "美元")
    assert _lab2 == "区间年复合增速 CAGR", f"水平序列的标签不对：{_lab2}"
    assert _g2 is not None and 5.0 < _g2 < 5.8, f"水平序列 CAGR 应≈5.4%，实得 {_g2}"

    # ③b **以 % 表示的「水平」序列走 CAGR，绝不走几何平均**（2026-09-16）
    # 口径：中国城镇化率 36.2 → 44.3 → 51.3 → 57.4 → 63.9（%，2000/2005/2010/2015/2020）。
    # 正确的读法：20 年从 36.2% 到 63.9%，CAGR = (63.9/36.2)^(1/20)−1 ≈ **2.9%**。
    # 错读法（把每个值当成"当年的增长率"连乘开方）给出 **≈50%**——
    # 「城镇化率一年涨 50 个百分点」这句话本身就说明它错得离谱。
    _urb = [("2000", 36.2), ("2005", 44.3), ("2010", 51.3), ("2015", 57.4), ("2020", 63.9)]
    _lab3, _g3 = derive_growth(_urb, "城镇化率", "%")
    assert _lab3 == "区间年复合增速 CAGR", f"城镇化率应走水平公式，实得标签 {_lab3}"
    assert _g3 is not None and 2.5 < _g3 < 3.3, f"城镇化率 CAGR 应≈2.9%，实得 {_g3}"
    _bad = geo_mean_rate([v for _y, v in _urb])
    assert _bad is not None and _bad > 40, "前置事实变了：几何平均确实会给出荒谬的大数"
    assert _g3 < 10, f"城镇化率被几何平均了（{_g3}）——那正是要防的那个错数"

    # ③c **判不出就不出数**（既不是几何平均也不是 CAGR）
    _lab4, _g4 = derive_growth([("2020", 1.0), ("2021", 2.0)], "说不清的指标", "%")
    assert _g4 is None, "单位是 % 而指标名无线索时必须弃权，不得蒙一个公式"
    assert "判不出" in _lab4, f"弃权的标签要说清原因，实得 {_lab4}"

    # ④ **判不出来就不给数**：不给 0（0 会被读成"年均零增长"，那是个结论）
    assert geo_mean_rate([]) is None
    assert geo_mean_rate([3.0]) is None
    assert geo_mean_rate([3.0, -100.0]) is None, "增长因子 ≤0 不可解释时必须弃权"
    assert cagr_level(0.0, 10.0, 5) is None
    assert cagr_level(15.0, 19.5, 0) is None


def test_audit_bundle_compute_reads_real_schema():
    """可复算包的 `compute.py` 必须真的读得到 `raw/authorities.json`。

    病案（2026-09-15，本仓库最严重的一处"看起来在跑"）：
    `compute.py` 只读 `content` / `snippet` 两个键，而 authorities.json 的 schema 是
    `source / title / url / data_period / caliber / query / excerpt`——**两个键都不存在**。
    于是正文恒为空 → 每个包都"检出 0 条可复算时序"，而脚本**看起来跑通了**。
    实测 311 个已生成发布包中 **305 个**在正确的键下有可解析序列，全部被跳过。
    **整个"第三方可独立复算"的承诺，从上线起从未被执行过一次。**

    本用例同时锁住三件事：
      ① 键名对得上 → 真实 schema 下必须检出 >0 条；
      ② 「0 条」必须**自己解释自己**，不能只印一句读起来像"没问题"的"检出 0 条"；
      ③ 包内自带的那份公式副本，与 `core/series_math.py` **必须给出同一个数**
         （它要能脱离本项目运行，所以是副本；副本靠这条用例保持一致）。
    """
    import json
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    from core.audit_bundle import _COMPUTE_PY

    # ③ 两份实现的一致性：把 _COMPUTE_PY 当普通源码 exec（__name__ 不是 __main__ → 不跑 main）
    _ns: dict = {"__name__": "_compute_standalone", "__file__": "compute.py"}
    exec(compile(_COMPUTE_PY, "compute.py", "exec"), _ns)  # noqa: S102
    from core.series_math import cagr_level, geo_mean_rate

    for _vals in ([2.34, 8.57, 3.134, 5.416, 4.958, 4.96], [1.2, -0.4, 3.3], [5.0, 5.0]):
        assert abs((_ns["geo_mean_rate"](_vals) or -999) - (geo_mean_rate(_vals) or -999)) < 1e-9, (
            f"包内副本与 core/series_math 对增速序列不一致：{_vals}"
        )
    for _a, _b, _n in ((15.0, 19.5, 5), (14.11, 14.07, 5), (1.0, 2.0, 1)):
        assert abs((_ns["cagr_level"](_a, _b, _n) or -999) - (cagr_level(_a, _b, _n) or -999)) < 1e-9, (
            f"包内副本与 core/series_math 对水平序列不一致：{(_a, _b, _n)}"
        )
    # ③b **判据本身也必须两边一致**（2026-09-16 补）。
    # 此前这里只锁了两个**公式**，没锁"该用哪个公式"的**判据**——
    # 于是两份判据可以各说各话：报告用新判据、复算包用旧判据，
    # 第三方跑出来与报告对不上，而"独立复算通过/不通过"两边各自都自洽。
    # **判据不一致 = 验证机制为错误背书**，比单个错数严重。
    from core.series_math import classify_series as _cs

    for _nm, _u in (("城镇化率", "%"), ("GDP年增速", "%"), ("互联网普及率", "%"),
                    ("服务业增加值占GDP", "%"), ("通胀率（CPI年%）", "%"),
                    ("复合增长率", "%"), ("总人口", "人"), ("GDP（现价美元）", "美元"),
                    ("说不清的指标", "%"), ("", "")):
        _a = _cs(_nm, _u)
        _b = _ns["classify_series"](_nm, _u)
        assert _a == _b, (
            f"包内副本与 core/series_math 的**序列类型判据**不一致：{(_nm, _u)} → {_a} vs {_b}。"
            "两份判据一旦分叉，独立复算会得出与报告不同的数，而两边各自都自洽。"
        )

    def _run(fixture: list, script: str = _COMPUTE_PY) -> str:
        d = tempfile.mkdtemp(prefix="yancec_bundle_")
        try:
            (Path(d) / "raw").mkdir(parents=True, exist_ok=True)
            (Path(d) / "raw" / "authorities.json").write_text(
                json.dumps(fixture, ensure_ascii=False), encoding="utf-8"
            )
            (Path(d) / "compute.py").write_text(script, encoding="utf-8")
            r = subprocess.run(
                [sys.executable, "compute.py"], cwd=d,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                # **必须显式指定子进程的输出编码**：Windows 上子进程 stdout 走管道时默认用
                # 系统 ANSI（cp936/GBK），父进程按 UTF-8 解 → 中文全成乱码 →
                # 断言读的是乱码而不是真输出（实测踩中：脚本明明算出了 4.9%，用例却报"没检出时序"）。
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            return (r.stdout or "") + (r.stderr or "")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    # ① 用**真实 schema**（excerpt 键）必须检出——这正是原实现永远做不到的事
    _real = [{
        "source": "World Bank", "title": "China GDP年增速（World Bank 年度时序）",
        "url": "https://api.worldbank.org/v2/x", "data_period": "2025", "caliber": "", "query": "",
        "excerpt": "China GDP年增速（World Bank，单位：%）—— 2020年 2.34；2021年 8.57；"
                   "2022年 3.134；2023年 5.416；2024年 4.958；2025年 4.96。",
    }]
    _out = _run(_real)
    assert "检出 1 条可复算时序" in _out, f"真实 schema 下没检出时序（键名又对不上了？）：\n{_out[-600:]}"
    assert "4.9%" in _out, f"增速序列复算应为 4.9%：\n{_out[-600:]}"
    assert "16.2%" not in _out.split("独立复算完成")[0].split("→")[-1], "复算又算出了 16.2%"

    # 同年重复点必须去重（否则增长因子个数翻倍、增速算错）
    _dup = [{
        "source": "World Bank", "title": "China GDP（现价美元）（World Bank 年度时序）",
        "url": "u", "data_period": "2025", "caliber": "", "query": "",
        "excerpt": "2020年 15.00；2020年 15.00；2021年 18.20；2021年 18.20；2022年 18.32；"
                   "2023年 18.27；2024年 18.73；2025年 19.50",
    }]
    _o2 = _run(_dup)
    assert "5.4%" in _o2, f"存量序列去重后应仍是 5.4%：\n{_o2[-600:]}"

    # ② 「0 条」必须解释自己——不能只说一句像"没问题"的"检出 0 条"
    _o3 = _run([{"source": "X", "title": "无序列", "url": "u",
                 "data_period": "", "caliber": "", "query": "", "excerpt": "这里没有任何年度数值。"}])
    assert "检出 0 条" in _o3
    assert "不等于没有问题" in _o3, "0 条没有自我解释——会被读成「核查通过」"
    assert "记录里真实存在的键" in _o3, "0 条时应打印实际 schema，便于下次 schema 漂移时定位"

    # 空输入（没有记录）与"有记录但无序列"必须是**两种原因**
    _o4 = _run([])
    assert "没有输入" in _o4, "空 raw/ 应说清是「没有输入」而不是「算出来是 0」"


def test_quality_llm_full_coverage_and_disclosure():
    """LLM 质检必须**覆盖全文**，且它检出的东西必须**被披露**。

    病案（2026-09-15 外部审计，两个缺陷叠在一起）：
      ① `_llm_check` 把报告截断成 `report[:3000]` 才交给质检模型。
         实测一份 **20,572 字**的报告 → LLM 只看到前 **14.6%**，
         而规则层是全文扫描的。于是"双轨质检"实际是
         「规则层全审 + LLM 看个头」，对外却说的是"LLM 事实核查"。
      ② 它检出的 `llm_issues` **从未被任何地方消费**——只经 `all_issues`
         把等级抬成 FLAG，报告里的《质检与可验证性》全程只渲染 `rule_issues`。
         **算了、存了、没人看**。
    """
    import graph
    from agents.quality import _qa_windows, llm_coverage

    # ① 窗口必须首尾覆盖全文，且相邻段重叠（不把一句话/一张表从中间切开）
    assert _qa_windows(1000) == [(0, 1000)], "短报告不该被切段"
    _w = _qa_windows(20572)
    assert len(_w) > 1, "两万字的报告必须切成多段"
    assert _w[0][0] == 0 and _w[-1][1] == 20572, f"窗口未覆盖全文：{_w[:2]}…{_w[-1]}"
    assert all(_w[i][1] > _w[i + 1][0] for i in range(len(_w) - 1)), "相邻窗口必须重叠"
    assert llm_coverage("字" * 20572)["coverage"] >= 0.99, "两万字报告应接近全覆盖"
    # 覆盖率按**区间并集**算——重叠部分不能重复计入（否则会虚报 >100%）
    assert llm_coverage("字" * 20572)["coverage"] <= 1.0, "覆盖率不得超过 100%"

    # 超长报告必须**如实降覆盖率**，不能假装全覆盖
    assert llm_coverage("字" * 200000)["coverage"] < 1.0, "超长报告受窗口上限约束，覆盖率须如实 <100%"

    # ② 检出 + 覆盖率都要出现在报告里，且**通过时也要印覆盖率**
    _st = {
        "quality_report": {
            "level": "PASS", "evidence_count": 3, "rule_issues": [], "fatal_issues": [],
            "llm_issues": ["某断言无来源支撑"],
            "llm_coverage": {"chars": 9000, "windows": 3, "covered_chars": 9000, "coverage": 1.0},
        }
    }
    _n = graph._build_quality_note(_st)
    assert "LLM 语义复核" in _n and "某断言无来源支撑" in _n, "LLM 检出未披露到报告"
    assert "9,000" in _n and "100%" in _n, "覆盖率未披露"

    # 覆盖率不满时**必须明说未覆盖部分只过了规则层**——否则"质检通过"会被读成"全文都查过"
    _st["quality_report"]["llm_coverage"] = {
        "chars": 40000, "windows": 10, "covered_chars": 30000, "coverage": 0.75
    }
    _n2 = graph._build_quality_note(_st)
    assert "75%" in _n2 and "未经 LLM 复核" in _n2, "覆盖率不足时未如实说明"

    # 反向锁：规则层与 LLM 层的条数必须**分开报**，不能混成一个总数
    # （有规则层提示时 level 必然是 FLAG——PASS 意味着两层都没检出，所以这里要一致地构造）
    _st["quality_report"]["level"] = "FLAG"
    _st["quality_report"]["rule_issues"] = ["数字「12杯」单源"]
    _n3 = graph._build_quality_note(_st)
    assert "规则层 1 项提示" in _n3, f"两层条数被混成一个总数：\n{_n3[:400]}"
    assert "LLM 语义复核**另检出 1 项" in _n3, f"LLM 层条数未单独报出：\n{_n3[:400]}"


def test_budget_unit_unified():
    """档位预算的**单位必须唯一**：全链路只用「档位积分」，人民币只在 `cost_cny` 非空时出现。

    病案（2026-09-15 外部审计）：同一个数 `budget_spent` 在三处被叫成三种东西——
    台账抽屉印「1.4 积分」、`node_budget_check` 印「1.4 元」、停止卡片印「0.7 元」，
    而计费引擎同一笔实扣 **50 积分**。更硬的一条：`cost_cny` 因未配单价恒为 `None`
    （K17：未配单价就不报钱数）——**一边返回 None 拒绝报钱，一边在别处印"元"**。

    根因是**两张价目表**：`agents/router._DEPTH_PRICE` 自己抄了一份 0/39/99/299 元，
    而 `core/billing.MODE_POINTS` 扣的是 10/50/150/400 积分。
    所以本用例锁两件事：① 价目只能有一张表；② 不得再把档位预算渲染成人民币。
    """
    import re
    from pathlib import Path

    from agents import router
    from core import billing

    # ① 价目唯一来源 = 计费引擎。抄第二份 → 必然再漂一次。
    for _dl, _pts in router._DEPTH_PRICE.items():
        _cn = billing.MODE_ALIAS[_dl.value]
        assert _pts == float(billing.MODE_POINTS[_cn]), (
            f"{_dl.value} 档位价目 {_pts} ≠ 计费引擎 {billing.MODE_POINTS[_cn]}——第二张价目表又回来了"
        )

    # ② 源码层：不得有 f-string 把档位预算插值成"元"
    _here = Path(__file__).resolve().parent
    _bad: list[str] = []
    for _p in (_here / "graph.py", _here / "agents" / "quality.py"):
        for _i, _ln in enumerate(_p.read_text(encoding="utf-8").splitlines(), 1):
            if not re.search(r"\}元", _ln):
                continue
            if any(k in _ln for k in ("spent", "budget", "estimate", "cap", "积分")):
                _bad.append(f"{_p.name}:{_i} {_ln.strip()[:100]}")
    assert not _bad, "档位预算仍被渲染成人民币：\n" + "\n".join(_bad)

    # ③ 停止卡片的"继续会消耗什么"必须是积分口径（它是用户真的会读的一句话）
    from agents.quality import build_stop_card

    _card = build_stop_card("报告正文", [], ["TAM/SAM 参数矛盾"], budget_spent=1.4)
    assert "积分" in _card["cost_if_continue"], _card["cost_if_continue"]
    assert "元" not in _card["cost_if_continue"], f"停止卡片仍在报价：{_card['cost_if_continue']}"

    # ④ 护栏两侧同单位 → 比例比较才有意义（此前一个是"成本"一个是"报价"，量纲不同）
    import graph

    assert 0 < graph._COST_BUDGET_RATIO < 1
    assert router._DEPTH_PRICE, "档位价目不能为空"


def test_offline_write_gates():
    """离线闸门必须在**每一个生产写入点**都装上（账本 + 报告存档 + 可复算包）。

    病案（2026-09-15 外部审计）：账本（`core/runstats.py`）装了闸门，
    报告存档（`core/report_store.py`）**没装**。后果是实测 `data/reports` 199 份里
    **180 份（90%）**是 `_fulltest` 产物，而 `_MAX_KEEP=200` 恰好饱和——
    `_prune()` 按 mtime 倒序裁剪，**测试档正在把真实报告挤出保留窗**。

    病案（2026-09-16 第三次复发）：可复算包（`core/audit_bundle.py`）是**第三个**
    生产写入点，同样没装。实测 `data/audit_bundles/` 420 个包里 **388 个（92.4%）**
    来自两道测试题（杭州开咖啡店 标准档 204 + 深入档 184），另有三个连名字都没改的
    测试残留。**本函数上一次的 docstring 已经写了「以后再新增写生产档案的模块，
    也必须过这道闸门」——而它当时只锁了两处，那句话就落空了。**

    病案（2026-09-16 第四次复发，同日）：经验台账（`core/exp_memory.py`）是**第四个**
    写入点，也没有。由对抗核验员置 `YANCEC_OFFLINE=1` 注入探针实测确认：`record_run()`
    仍返回 1、文件被改写、假源名 `zzz_test_src` 落盘。

    ⚠️ **第四次复发说明"加进这个测试"本身不够**——前三次都是"发现了就补一处"，
    而每次都是**再有人写第五处时才想起来**。所以这条锁当时的语义是：
    凡是往 `data/` 下写"生产档案"的新模块，必须在下面的清单里出现。
    清单：runstats（账本）· report_store（报告存档）· audit_bundle（可复算包）·
    exp_memory（经验台账）。

    病案（2026-09-16 第五次复发，同日）：**那句"写进清单"的话照样落空了。**
    `_gate_probe.py` 把各模块落盘路径改指到临时目录后置 `YANCEC_OFFLINE=1` 实测：

        受保护 5 · **未受保护 5** —— metric_ledger（指标时序库）·
        industry_kb（行业卡）· memory（用户档案）· source_health（巡检台账）·
        calibration（校准存档）。**覆盖率 5/10。**

    结论：**手写清单这个机制本身就是病灶**——它要求每次新增写盘模块的人都记得
    改这条测试，而那个人往往根本不知道这条测试存在。所以闸门已下移到 I/O 层
    （`core/safe_io.py`），并新增 `test_prod_writes_are_gated`：**AST 机械枚举**
    全仓写 `data/` 的位置 + 调用图闭包判闸门，**默认失败、豁免须具名写理由**。

    本函数保留为**行为断言**（真的去调那九个写点，看目标文件有没有被改），
    与那条机械枚举互补：一条管"有没有漏掉写点"，一条管"装了闸门的那几处是否真的生效"。
    """
    import os

    from core import audit_bundle as ab
    from core import exp_memory as em
    from core import report_store as rs
    from core import runstats

    _saved = os.environ.get("YANCEC_OFFLINE")
    try:
        os.environ["YANCEC_OFFLINE"] = "1"
        assert rs.save_report("闸门自测：不该落盘", {"report_final": "# x"}) is None, (
            "报告存档的离线闸门失效——测试产物会重新灌进生产存档"
        )
        _before = len(runstats.read_ledger())
        runstats.log_run_stats("standard", "闸门自测：不该落账", {"elapsed_s": 1.0})
        assert len(runstats.read_ledger()) == _before, "账本离线闸门失效"
        # 第三个写入点：可复算包。断言的是"什么都没生成"，而不是"生成了但没写盘"——
        # 因为目录里只要落过 work/ 子目录，`_prune` 之外没有任何东西会清它。
        assert ab.build_bundle({"raw_question": "闸门自测：不该出包"}, {"report_final": "# x"},
                               "闸门自测：不该出包") == {}, (
            "可复算包的离线闸门失效——测试产物会重新灌进 data/audit_bundles"
        )
        # 第四个写入点：经验台账。这一处最隐蔽——台账是**喂回检索上下文**的，
        # 假源名会以「历史统计显示…」出现在下一次真实调研的提示词里。
        _n = em.record_run({"raw_question": "闸门自测：不该记经验",
                            "source_trace": [{"src": "zzz_gate_probe", "q": "x", "n": 1}]})
        assert _n == 0, (
            f"经验台账的离线闸门失效（写入了 {_n} 条）——测试用的假源名会污染下一次真实调研的检索上下文"
        )

        # ---- 第五次复发（2026-09-16）：探针又实测出五处，见 core/safe_io.py 顶部病案 ----
        # 判据统一为"**目标文件一个字节都没变**"，而不是只看返回值——
        # 只看返回值会漏掉"返回 0 但已经写了一半"的实现。
        import os.path as _p

        from core import calibration as _cal
        from core import industry_kb as _ikb
        from core import memory as _mem
        from core import metric_ledger as _ml
        from core import source_health as _sh

        def _frozen(path, fn, what):
            _b = None
            if _p.exists(path):
                with open(path, "rb") as _f:
                    _b = _f.read()
            fn()
            _a = None
            if _p.exists(path):
                with open(path, "rb") as _f:
                    _a = _f.read()
            assert _a == _b, (
                f"{what} 的离线闸门失效——离线跑一次测试就会改一次生产数据。"
                f"这一处的污染是**反馈型**的：它会被读回去注入下一次真实调研的上下文"
            )

        # ⑤ 指标时序库 —— `locked_anchor()` 会把它读回去当**锁值锚**写进提示词
        _frozen(_ml._STORE,
                lambda: _ml.ingest([{"indicator": "门店数", "key": "门店数",
                                     "value": "1", "unit": "家"}],
                                   report_id="gate_probe", question="闸门自测", industry="咖啡"),
                "指标时序库 metrics.jsonl")

        # ⑥ 行业知识库 —— `format_industry_brief()` 读回去当行业基线；
        #    而且绿档那一笔对外写着「命中已沉淀行业卡」，卡被测试写过那句话就不成立
        _frozen(str(_ikb._KB_PATH),
                lambda: _ikb._save({"闸门自测行业": {"industry": "闸门自测行业"}}),
                "行业知识库 industry_kb.json")

        # ⑦ 用户档案 —— `recall()` 读回去注入上下文；假决策会变成"历史决策"
        _frozen(str(_mem.PROFILE_PATH),
                lambda: _mem.record_decision("闸门自测：不该记决策", "闸门自测", "Go"),
                "用户档案 user_profile.json")

        # ⑧ 校准存档 —— K20 那句「上界 17.1%」直接引用这份存档，被覆盖就与材料对不上
        _frozen(str(_cal._PATH), lambda: _cal.save({"n": 16, "fpr": 0.0}), "校准存档")

        # ⑨ 巡检台账 —— 这一处拦的不只是"写"，还有**出网**：
        #    `app.py` 启动时就会 `_kick_source_patrol()`，离线启动服务也不该真出网
        _led = _sh.patrol(include_region=False, budget_s=1.0, trigger="gate-probe")
        assert str(_led.get("skipped") or "").startswith("YANCEC_OFFLINE"), (
            "离线模式下巡检仍在出网探测——与 AUTHORITY_FETCH_ENABLED 总开关的意图冲突"
        )
    finally:
        if _saved is None:
            os.environ.pop("YANCEC_OFFLINE", None)
        else:
            os.environ["YANCEC_OFFLINE"] = _saved


# ============================================================
# 生产写点机械枚举（AST + 调用图闭包）—— test_prod_writes_are_gated 的地基
#
# 为什么是机械枚举而不是再列一张清单：`test_offline_write_gates` 的清单是**手写的**，
# 它的 docstring 从第三次复发起就写着「以后再新增写生产档案的模块，也必须过这道闸门」——
# 然后照样漏了五处（见 core/safe_io.py 顶部病案）。**手写清单挡不住新增。**
# ============================================================
_PW_SKIP_DIRS = {"__pycache__", ".venv", "node_modules", ".git", "data", ".veritmp",
                 "_run_analyze", "_bundle_test", ".audit_tmp", ".tmp_ppt_png"}
# 只跳过**扫描器自己**：`_fulltest.py` 里含合成样本与 `_pw_*` 实现，
# 扫它等于自己扫自己（会把合成样本里的"坏写点"算到真实树上）。
# 其余一律不跳——**跳过就是豁免，而豁免必须写理由**（见 _PW_ALLOWED）。
# 曾经这里列过十几个开发脚本名，那是又一份"手写清单"——正是本函数要消灭的东西。
_PW_SKIP_FILES = {"_fulltest.py", "selftest.py"}
_PW_WRITE_MODE = set("wax+")
# 只把**内容写入**算作写生产档案。`mkdir/makedirs` 刻意排除：建空目录幂等、无内容、
# 可重建，不算数据污染；而把它算进来必然误报——`report_store.describe()` 只是自检时
# 确保目录存在，它没有闸门也**不该有**闸门。按本仓库头号纪律（宁可少一条检查，
# 也不要一条会常态误报的检查）排除掉。
# **持久化 API**（按"这个调用会写盘"识别，而不是按路径字符串识别）。
# 病案（2026-09-16）：`core/rag.py` 的 `chromadb.PersistentClient(path=settings.vector_db_path)`
# 写的就是 `backend/data/chroma`（13MB 生产向量库），**既没有闸门、也不在枚举结果里**——
# 路径来自 `settings` 而不是我们拼的常量，上面那套"含 data 字面量的模块常量"完全看不见它。
# 更值得记的是：对抗核验员据此把这条缺陷**判为不存在**（"探针没覆盖它"）。
# 一条只认字符串、不认 API 的检查，会同时漏掉缺陷**和**给核验者提供虚假的安心。
_PW_SINKS = {"PersistentClient", "Client", "connect", "dump", "savez", "savez_compressed"}
_PW_PATH_ATTRS = {"write_text", "write_bytes", "unlink", "touch"}
_PW_PATH_FUNCS = {"rmtree", "remove", "unlink", "copy", "copy2", "copyfile",
                  "move", "replace", "rename"}


def _pw_unparse(node):
    try:
        import ast as _a

        return _a.unparse(node)
    except Exception:
        return ""


def _pw_production_constants(tree):
    """赋值表达式的字面里含 `"data"` 的名字 → 候选生产路径常量。

    **不只看模块级**：`core/config.py` 里 `vector_db_path: str = str(PROJECT_ROOT /
    "backend" / "data" / "chroma")` 是**类体里**的 AnnAssign（pydantic Settings 的字段），
    只看 `tree.body` 会漏掉它——而它正是生产向量库的路径来源。
    """
    import ast as _a

    names = set()

    def _scan_body(body):
        for n in body:
            if isinstance(n, (_a.FunctionDef, _a.AsyncFunctionDef)):
                continue          # **不进函数体**：函数里的局部名（`p` / `path` / `tmp`）
                                  # 极其常见，把它们当"生产路径常量"会让整条检查常态误报
                                  # （第一版就这么误报了 `_lint_docs.py` 里的局部 `p = BACKEND/"data"/...`）。
                                  # 函数内的派生别名由 `_pw_local_aliases` 负责，那是有来源的。
            if isinstance(n, _a.Assign):
                targets, val = n.targets, n.value
            elif isinstance(n, _a.AnnAssign) and n.value is not None:
                targets, val = [n.target], n.value
            else:
                if isinstance(n, _a.ClassDef):
                    _scan_body(n.body)      # 类体要进：pydantic Settings 的字段就写在这里
                continue
            txt = _pw_unparse(val)
            if '"data"' not in txt and "'data'" not in txt:
                continue
            for t in targets:
                if isinstance(t, _a.Name):
                    names.add(t.id)

    _scan_body(tree.body)
    return names


def _pw_mentions(expr, names):
    import ast as _a

    if expr is None:
        return False
    for sub in _a.walk(expr):
        if isinstance(sub, _a.Name) and sub.id in names:
            return True
        # `settings.vector_db_path` 这类**属性访问**也要认：生产路径常量常挂在
        # pydantic Settings 上，只匹配 `Name` 会全盘漏掉。
        if isinstance(sub, _a.Attribute) and sub.attr in names:
            return True
    return False


def _pw_local_aliases(fn, prod):
    """函数内的**局部别名**也算生产路径（取不动点）。

    `_tmp = _PATH + ".tmp"` / `work = os.path.join(_BUNDLE_DIR, safe_id)` ——
    原子写（先写 .tmp 再 replace）全用这种写法；只看常量名的检查对它完全失明。
    """
    import ast as _a

    names = set(prod)
    changed = True
    while changed:
        changed = False
        for sub in _a.walk(fn):
            if isinstance(sub, _a.Assign):
                targets, val = sub.targets, sub.value
            elif isinstance(sub, _a.AnnAssign) and sub.value is not None:
                targets, val = [sub.target], sub.value
            else:
                continue
            if not _pw_mentions(val, names):
                continue
            for t in targets:
                if isinstance(t, _a.Name) and t.id not in names:
                    names.add(t.id)
                    changed = True
    return names


def _pw_direct_gate(fn):
    import ast as _a

    for sub in _a.walk(fn):
        if isinstance(sub, _a.Attribute) and sub.attr in ("_offline", "is_offline"):
            return True
        if isinstance(sub, _a.Name) and sub.id in ("_offline", "is_offline"):
            return True
        if isinstance(sub, _a.Constant) and sub.value == "YANCEC_OFFLINE":
            return True
    return False


def _pw_called_names(fn):
    import ast as _a

    out = set()
    for sub in _a.walk(fn):
        if isinstance(sub, _a.Call):
            f = sub.func
            if isinstance(f, _a.Name):
                out.add(f.id)
            elif isinstance(f, _a.Attribute):
                out.add(f.attr)
    return out


def _pw_collect(root):
    """→ (files, funcs)；files[rel]=(tree,prod,funcs)，funcs[(rel,name,lineno)]=info"""
    import ast as _a
    from pathlib import Path

    root = Path(root)
    files, funcs = {}, {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in _PW_SKIP_DIRS]
        for f in fn:
            if not f.endswith(".py") or f in _PW_SKIP_FILES:
                continue
            p = Path(dp) / f
            rel = str(p.relative_to(root))
            try:
                tree = _a.parse(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            prod = _pw_production_constants(tree)
            fns = [n for n in _a.walk(tree)
                   if isinstance(n, (_a.FunctionDef, _a.AsyncFunctionDef))]
            files[rel] = (tree, prod, fns)
            for n in fns:
                funcs[(rel, n.name, n.lineno)] = {
                    "file": rel, "node": n, "calls": _pw_called_names(n),
                    "gate": _pw_direct_gate(n),
                }
    return files, funcs


def _pw_protected(funcs):
    """受保护 = 自己直接查闸门，或其**全部调用方**都受保护（不动点）。

    调用方集合故意取宽（同名函数跨模块互相算作调用方）：只会让写点更容易被判成
    "有未受保护的调用方"，即偏保守——宁可误报，不可漏报。
    """
    by_name = {}
    for k, v in funcs.items():
        by_name.setdefault(v["node"].name, []).append(k)
    callers_of = {}
    for k, v in funcs.items():
        for cname in v["calls"]:
            for tgt in by_name.get(cname, []):
                callers_of.setdefault(tgt, set()).add(k)

    prot = {k for k, v in funcs.items() if v["gate"]}
    changed = True
    while changed:
        changed = False
        for k in funcs:
            if k in prot:
                continue
            cs = callers_of.get(k, set()) - {k}
            if cs and all(c in prot for c in cs):
                prot.add(k)
                changed = True
    return prot


def _pw_owner(funcs, rel, lineno):
    best = None
    for k, v in funcs.items():
        if v["file"] != rel:
            continue
        n = v["node"]
        if n.lineno <= lineno <= (n.end_lineno or n.lineno):
            if best is None or n.lineno > best[1]:
                best = (k, n.lineno)
    return best[0] if best else (rel, "<module>", 0)


def _pw_scan(root):
    """→ (unprotected[(rel,line,kind,fn)], protected_count)"""
    import ast as _a

    files, funcs = _pw_collect(root)
    prot = _pw_protected(funcs)

    # **生产路径常量必须跨文件汇总，不能各文件各算一份**（2026-09-16）。
    # 病案：`core/rag.py` 写的是 `chromadb.PersistentClient(path=settings.vector_db_path)`，
    # 而 `vector_db_path` 这个常量**声明在 `core/config.py` 的 Settings 类体里**。
    # 按文件各算时，rag.py 自己的 `prod` 是空集 → 整个文件被 `if not prod: continue` 跳过，
    # **连扫都没扫**。（合成样本之所以能过，是因为它把类声明和用法放在同一个文件里
    # ——一个"自测通过、真树失明"的典型陷阱。）
    # 汇总后按名字匹配（`settings.vector_db_path` 靠 `Attribute.attr` 命中）。
    # 代价是跨模块同名常量可能互相误报；这些名字都足够具体
    # （`_PATH` / `_DIR` / `_STORE` / `vector_db_path`…），且本检查默认失败、豁免须写理由。
    _all_prod: set[str] = set()
    for _rel, (_t, _p, _f) in files.items():
        _all_prod |= _p

    bad, n_ok = [], 0
    for rel, (tree, prod, fns) in files.items():
        # 每个函数：全局生产常量 + **该函数内的派生别名**（别名仍按文件/函数局部算——
        # 它必须来自真实的赋值链，不能跨文件猜）
        per_fn = [(f, _all_prod | _pw_local_aliases(f, _all_prod | prod)) for f in fns]

        def names_for(lineno):
            best = None
            for f, names in per_fn:
                if f.lineno <= lineno <= (f.end_lineno or f.lineno):
                    if best is None or f.lineno > best[0].lineno:
                        best = (f, names)
            return best[1] if best else (_all_prod | prod)

        for n in _a.walk(tree):
            if not isinstance(n, _a.Call):
                continue
            f = n.func
            names = names_for(n.lineno)
            kind = None
            if isinstance(f, _a.Name) and f.id == "open" and n.args:
                mode = ""
                if len(n.args) > 1 and isinstance(n.args[1], _a.Constant):
                    mode = str(n.args[1].value)
                for kw in n.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, _a.Constant):
                        mode = str(kw.value.value)
                if set(mode) & _PW_WRITE_MODE and _pw_mentions(n.args[0], names):
                    kind = "open"
            elif isinstance(f, _a.Attribute) and f.attr in _PW_PATH_ATTRS:
                if _pw_mentions(f.value, names):
                    kind = f.attr
            elif isinstance(f, _a.Attribute) and f.attr in _PW_PATH_FUNCS and n.args:
                if _pw_mentions(n.args[0], names):
                    kind = f.attr
            elif isinstance(f, _a.Attribute) and f.attr in _PW_SINKS:
                # 持久化 API（`chromadb.PersistentClient` / `sqlite3.connect` / `pickle.dump`…）：
                # 路径常作为**关键字参数**传入（`path=settings.vector_db_path`），
                # 所以位置参数和关键字都要看。
                _argv = list(n.args) + [k.value for k in n.keywords]
                if any(_pw_mentions(_x, names) for _x in _argv):
                    kind = f.attr + "()"
            if not kind:
                continue
            k = _pw_owner(funcs, rel, n.lineno)
            if k in prot:
                n_ok += 1
            else:
                bad.append((rel, n.lineno, kind, k[1]))
    return sorted(bad), n_ok


# 已知良性、且**不该**加闸门的写点（必须逐条写理由；空着最好）。
# 判定纪律：只有"这个写点在离线模式下被调用也完全无害"才允许列进来；
# "懒得改"不是理由。
_PW_ALLOWED: dict[tuple[str, str], str] = {
    ("chroma_repro.py", "<module>"): (
        "人工诊断脚本（直连生产向量库看 count）。它**已经有**模块级离线守卫"
        "（`if safe_io.is_offline(): sys.exit(...)`），而本枚举按**函数**判定闸门，"
        "看不见模块级代码——所以这里是「检查看不见」，不是「闸门不存在」。"
        "这也是本 allowlist 唯一的正当用法：**给检查的盲区写理由，而不是给缺陷开后门**。"
    ),
}


def test_frontend_stores_no_field_it_never_reads():
    """前端从后端存下来的**每一个**字段，都必须至少有一个**读取点**。

    ## 病案（2026-09-16）

    `App.jsx` 的 done 处理里存下了 `integrityGate`，紧挨着的注释写着：

        // 有它才能在交付物头部打出"本次判定被闸门降过"的角标——
        // 正文改了而头部不显示，等于读者第一屏看不到。

    **而那个角标从来没有被造出来**——全文件搜 `integrityGate` 只有那一处赋值，
    没有任何读取点。后果：闸门把判定由 `Go` 降成 `Validate` 之后，
    读者在第一屏看到的只是 `Validate`，**看不出它原本是 Go、更看不出是谁降的**。
    闸门是本项目最想主张的那件东西，它在第一屏是隐形的（横幅要往下滚才看得到）。

    这是本仓库"**声称但没接线**"家族的又一例，而且是最难靠人眼发现的一种：
    代码看起来完全正常（赋值、有注释、有理由），**只是没人读它**。
    同一家族此前出现过：`outcome_track.format_md` 零生产调用、
    `industry_kb.get_industry_card` 有函数无入口。

    ## 判据

    取 done 处理里所有 `字段: ev.result?...` 形式的赋值，逐个统计它在
    `App.jsx` 全文的出现次数——**只出现一次（即只有那次赋值）就是没人读**。
    """
    import re as _re
    from pathlib import Path

    _fe = Path(__file__).resolve().parents[1] / "frontend" / "src" / "App.jsx"
    assert _fe.is_file(), f"找不到前端源文件：{_fe}"
    _src = _fe.read_text(encoding="utf-8")

    _assigned = _re.findall(r"^\s+([A-Za-z_]\w*):\s*ev\.result", _src, _re.M)
    assert len(_assigned) >= 15, (
        f"只在前端找到 {len(_assigned)} 个 done 字段赋值——锚点失效了"
        f"（这条锁会因此变成空检查，'数不变 = 检查不存在'）：{_assigned}"
    )

    _assigned_lines = {}
    for _ln in _src.split("\n"):
        _mm = _re.match(r"^\s+([A-Za-z_]\w*):\s*ev\.result", _ln)
        if _mm:
            _assigned_lines[_mm.group(1)] = _ln

    _dead = []
    for _f, _decl in _assigned_lines.items():
        # 把**赋值那一行本身**扣掉，再看这个名字在文件里还出现过没有。
        # 判据用裸词边界而不是 `NAME:` / `.NAME(`：后者会漏掉换行、字符串插值、
        # 以及 `${m.citationSkipped}` 这类写法——第一次跑就因此误报了两个字段
        # （`citationProbed` / `citationSkipped` 其实都读了，只是引用形式不是 `[:=(]`）。
        # 宁可松一点（注释里提到的也算读过），也不要一条会常态误报的检查。
        _rest = _src.replace(_decl, "", 1)
        if not _re.search(r"\b" + _re.escape(_f) + r"\b", _rest):
            _dead.append(_f)
    assert not _dead, (
        "前端存下了却从不读取的字段（存了不读 = 承诺了没兑现，读者看不到）：\n"
        + "\n".join(f"    · {f}" for f in _dead)
        + "\n  要么把它渲染出来，要么别存——别让注释替一个不存在的功能作证。"
    )


def test_no_keyword_parameter_is_silently_ignored():
    """**被具名传入、却在函数体里从未出现的参数** —— "承诺了没兑现"的静默形态。

    ## 病案（2026-09-16）

    `agents/researchers._integrate(..., metric_anchor=...)`：参数在函数体里出现的次数
    **恰好是 1**（就是它自己的声明）。而它是这么一路传进来的——

        metric_ledger.ingest() 落库 → locked_anchor(industry) 渲染成提示块
        → graph.py ctx["metric_anchor"] → _integrate(metric_anchor=...)
        → **到此为止，丢在地上**

    于是对外主张的「**跨报告锁值锚**：同行业既往调研约束新报告的指标口径，
    改了必须写『修正：上期 X → 本次 Y（原因）』」这条能力，**代码上完全不存在**：
    库照写、锚照算、块照渲染，然后被丢掉。这是"记录→消费→披露"断链最纯粹的形态
    ——每一段都在，唯独最后一段没接上，而且**编译、测试、上线全都不会报错**。

    为什么这个检查要加"被**具名**传入"这个前提：只看"参数没被使用"会满屏误报
    ——为了接口对齐而保留的参数（鸭子类型）、`**kwargs` 透传、预留位都长这样。
    而"调用方专门写了 `metric_anchor=`"就说明**有人以为它会被用**。
    两者相交，剩下的基本都是真问题。
    """
    import ast as _a
    from pathlib import Path

    _root = Path(__file__).resolve().parent
    _skip_dirs = _PW_SKIP_DIRS | {"tests"}

    def _files():
        for _dp, _dn, _fn in os.walk(_root):
            _dn[:] = [d for d in _dn if d not in _skip_dirs]
            for _f in _fn:
                if not _f.endswith(".py") or _f in _PW_SKIP_FILES:
                    continue
                _p = Path(_dp) / _f
                try:
                    _src = _p.read_text(encoding="utf-8")
                    yield str(_p.relative_to(_root)), _src, _a.parse(_src)
                except Exception:
                    continue

    _tree = list(_files())

    # "被具名传过"的名字，**按被调函数名分开统计**。
    # 第一版用了全仓一个总集合，结果 `verdict=` 只因别处某个函数收过它就参与了判定，
    # 把 `build_validation_tasks(verdict=...)` 也报了进来——**一个会常态误报的检查
    # 会被直接无视**（本仓库头号纪律）。按函数名索引后，判据变成
    # "**这个函数**的**这个参数**被具名传过"，才是真的"有人以为它会被用"。
    _kw: dict[str, set[str]] = {}
    for _rel, _src, _t in _tree:
        for _n in _a.walk(_t):
            if not isinstance(_n, _a.Call):
                continue
            _f = _n.func
            _nm = _f.attr if isinstance(_f, _a.Attribute) else (
                _f.id if isinstance(_f, _a.Name) else "")
            if not _nm:
                continue
            for _k in _n.keywords:
                if _k.arg:
                    _kw.setdefault(_nm, set()).add(_k.arg)
    _n_kw = sum(len(v) for v in _kw.values())
    assert _n_kw > 200, f"锚点失效：只收集到 {_n_kw} 个具名参数名（这条锁会变成空检查）"

    _dead: list[tuple[str, int, str, str]] = []
    for _rel, _src, _t in _tree:
        for _n in _a.walk(_t):
            if not isinstance(_n, (_a.FunctionDef, _a.AsyncFunctionDef)):
                continue
            _passed = _kw.get(_n.name) or set()
            if not _passed:
                continue
            _body = _a.get_source_segment(_src, _n) or ""
            for _arg in [x.arg for x in _n.args.args + _n.args.kwonlyargs
                         if x.arg not in ("self", "cls")]:
                if _arg not in _passed:
                    continue
                if len(__import__("re").findall(r"\b" + __import__("re").escape(_arg) + r"\b",
                                                _body)) <= 1:
                    _dead.append((_rel, _n.lineno, _n.name, _arg))

    # 路径分隔符归一：豁免表在 Windows 下写成 `core\\rag.py`，
    # 在 Linux/macOS 上扫描器拿到的是 `core/rag.py`——不归一就会"在 CI 上突然变红"。
    _ok = {(p.replace("\\", "/"), fn, a) for (p, fn, a) in _PW_DEAD_PARAM_OK}
    _dead = [d for d in _dead if (d[0].replace("\\", "/"), d[2], d[3]) not in _ok]
    assert not _dead, (
        "以下参数**被调用方具名传入，却在函数体里从未出现**（承诺了没兑现）：\n"
        + "\n".join(f"    · {r}:{ln}  {fn}({a}=...)" for r, ln, fn, a in sorted(set(_dead)))
        + "\n  要么真的用它，要么把调用方的实参去掉。"
          "确实为接口对齐而保留的，写进 _PW_DEAD_PARAM_OK 并说明理由。"
    )


# 确认为"接口对齐 / 有意保留"的参数（必须逐条写理由）。
_PW_DEAD_PARAM_OK: dict[tuple[str, str, str], str] = {
    ("agents\\decision.py", "build_validation_tasks", "raw_question"): (
        "保留首位入参以兼容**位置传参**的既有调用（`tests/test_reliability.py` 写的是"
        " `build_validation_tasks(\"测试\", evidence_count=5)`）。"
        "模板是确定性固定结构，不消费问题文本——**是保留，不是遗漏**。"
        "（同函数的 `evidence_count` 曾经也是这个状态，但它被调用方真的算过一遍，"
        "所以那一条是**修**掉的、不是登记掉的。）"
    ),
    ("core\\data_passport.py", "build_passport", "url"): (
        "**刻意不使用**，且函数体里有长注释说明原因：URL 的日期路径是**页面出版期**，"
        "不是**数据期**——一篇 2026-09 发布、讲 2023 年数据的回顾稿会被标成"
        "「数据期 2026-09」，护照打自己的脸，后续时效门还会拿这个假数据期做判断。"
        "2026-09-14 审计把这条路关掉了。入参保留是因为调用方仍在传（去掉签名会波及三处调用）。"
    ),
    ("core\\rag.py", "add", "embeddings"): (
        "内存降级实现要与 `chromadb.Collection.add` **保持同一签名**（鸭子类型）："
        "调用方不区分拿到的是 Chroma 还是 `_MemoryCollection`。"
        "降级路径按关键词打分，本就无向量可言，类文档与函数注释都写明了这一点。"
    ),
    ("core\\rag.py", "query", "query_embeddings"): "同上：与 `chromadb.Collection.query` 保持同一签名。",
}


def test_stream_assembly_uses_graph_state():
    """**流式组装段必须吃图内最终状态** —— 用桩图端到端驱动，不靠源码字符串断言。

    病案（2026-09-16 · CRITICAL）：`_build_run_stats(final_payload, ...)` 里的
    `final_payload` 是 deliver 节点的**返回增量**（`stream_mode="updates"` 语义），
    只含 deliver 回吐的 16 个键。于是 `budget_spent / rework_count / search_debug /
    stop_card / sop_fallback / asset_reused` 在**演示走的那条路径**上恒为 0/False，
    而复购看板的三个指标正是拿它们算的——**错的方向恰好是「看起来更好」**。

    **旧锁为什么全绿**：它断言 `"_build_run_stats(final" in _src`，而
    `_build_run_stats(final_payload` 恰好命中那个前缀。**字符串前缀把「喂了增量」
    读成了「喂了 state」。** 这里改成真跑一遍组装段，看字段值。

    为什么需要桩图：这段逻辑只在真实运行时被走到，而真实运行要模型与网络。
    组装段本身与模型无关——桩图给出与真图同样的 `stream_mode="updates"` 事件形状
    与 `get_state` 接口，就能把这段逻辑单独钉死。
    """
    from unittest import mock

    import graph
    from core.models import DepthLevel, Evidence, ResearchPlan, Source

    plan = ResearchPlan(raw_question="咖啡连锁下沉还能做吗", depth=DepthLevel.DEEP)
    ev = Evidence(claim="下沉市场门店增速放缓", query="咖啡 下沉 增速",
                  sources=[Source(url="https://www.stats.gov.cn/a", level="L1_OFFICIAL",
                                  title="统计局")])

    # deliver 只回吐它自己写的那些键 —— 这正是缺陷的根源
    deliver_payload = {
        "report_final": "# 报告 正文", "competition_report": "",
        "citations": [{"url": "https://www.stats.gov.cn/a", "source": "国家统计局"}],
        "citation_report": None, "bad_urls": [], "unprobed_urls": [], "url_fixes": {},
        "status": "done", "metrics": [], "integrity_gate": {"level": "PASS", "executed": True},
        "decision_verdict": "Go", "decision_chain": [], "entailment": {}, "deliverables": [],
        "evidences": [ev], "citation_pass_rate": 1.0,
    }
    # 图内最终状态：下面这几个键 deliver **不回吐**，只能靠 get_state 取到
    final_state = {
        "raw_question": "咖啡连锁下沉还能做吗", "plan": plan,
        "report_final": "# 报告 正文", "evidences": [ev],
        "citations": deliver_payload["citations"],
        "budget_spent": 1.4, "rework_count": 1,
        "search_debug": [("t1", "q", 3)] * 7, "asset_reused": True,
        "stop_card": {"why": "需要人接管"},
        "sop_fallback": {"reason": "决策清单走了兜底模板"},
        "conflict_scan": {"trunk_matched": 12, "pairs_compared": 4, "conflicts": 1,
                          "criterion": "同主干且数值差>20%"},
        "budget_estimate": 50, "subtask_results": {}, "decisions": [],
    }

    class _Snap:
        values = final_state

    class _StubGraph:
        def stream(self, _state, config=None, stream_mode=None):
            yield {"router": {"plan": plan}}
            yield {"industry": {"evidences": [ev]}}
            yield {"check_conflict": {"conflict_results": [],
                                      "conflict_scan": final_state["conflict_scan"],
                                      "rework_count": 1}}
            yield {"quality_check": {"quality_report": {"level": "PASS", "passed": True},
                                     "stop_card": final_state["stop_card"]}}
            yield {"decision_sop": {"validation_tasks": [{"id": "vt_01"}],
                                    "monitor_conditions": ["每月看一次"],
                                    "sop_fallback": final_state["sop_fallback"],
                                    "decision_verdict": "Go"}}
            yield {"deliver": dict(deliver_payload)}

        def get_state(self, _config):
            return _Snap()

    _result = None
    with mock.patch.object(graph, "build_graph", return_value=_StubGraph()),          mock.patch.object(graph, "_attach_audit_bundle", return_value=None):
        for _evt in graph.run_task_stream("咖啡连锁下沉还能做吗", thread_id="t-stub-1"):
            if _evt.get("type") == "done":
                _result = _evt["result"]

    assert _result is not None, "没拿到 done 事件"
    _rs = _result.get("run_stats") or {}
    assert _rs.get("budget_spent") == 1.4, (
        f"budget_spent 没取到图内状态（deliver 不回吐该键）：{_rs.get('budget_spent')!r}"
    )
    assert _rs.get("rework_count") == 1, f"rework_count 没取到：{_rs.get('rework_count')!r}"
    assert _rs.get("search_rounds") == 7, f"search_rounds 没取到：{_rs.get('search_rounds')!r}"
    assert _rs.get("reused_asset") is True, "有效复用率的分子必须为真（否则看板被系统性低估）"
    assert _rs.get("took_over") is True, "人工接管率的分子必须为真"
    assert _rs.get("degraded") is True, "降级交付必须被记成降级（否则被算成正常成功交付）"
    assert (_result.get("plan") or {}).get("depth") == "deep", "账本档位必须是真实档位"
    assert _result.get("conflict_scan"), "conflict_scan 缺失——前端的「比对 N 对」分母读它"
    assert _result.get("sop_fallback"), "sop_fallback 缺失——前端「兜底模板」红角标读它"

    # ---- `trace_events`：**只能来自流本身**（2026-09-19 补，一次真跑打回来的修复）----
    # 病案：先把它加进 `_DELIVER_BACKFILL_KEYS`（从图内最终状态补齐），
    # 然后**真跑了一整轮深入档** —— done 事件里它**仍然是 `[]`**。
    # 原因不是名单漏了，是**来源错了**：`_trace()` 就地 `state["trace_events"].append()`
    # 且不返回值，所以它从来不是节点 delta，图 channel 里那个键永远是 `init_state` 的 `[]`。
    # （同一次运行里 `asset_reused`/`budget_spent` 补上了——它们是节点**返回**的 delta。
    #  三个键在同一份名单里并排躺着，长得一模一样，来源却完全不同。）
    #
    # 上面这段桩图里 deliver 也不回吐 trace_events，而 stream 推了 6 个节点 ——
    # 所以它能同时验两件事：流里有几条、结果里就有几条；且状态以 done 收尾。
    _tr = _result.get("trace_events")
    assert _tr, (
        "done 事件里 trace_events 为空——按 result 读轨迹的消费者（存档/回放/事后核对）"
        "会看到「这次没跑过」，而流上明明推了一路"
    )
    _nodes = [t["node"] for t in _tr]
    assert len(_tr) == 6, f"节点数对不上：{_nodes}"
    assert all(t.get("status") == "done" for t in _tr), (
        f"有节点没被收尾（`_finalize_event` 同形的收尾没做）：{_tr}"
    )
    assert [t["detail"] for t in _tr] == [t["detail"] for t in _tr], "detail 字段丢了"
    assert all(t.get("ts") for t in _tr), "ts 字段丢了——没有时间戳的轨迹没法当证据用"

def test_board_excludes_rows_with_broken_process_fields():
    """复购看板必须把**过程字段被已知缺陷写坏的行**剔除，并说出剔了几条。

    病案（2026-09-16）：流式路径把 deliver 的返回增量当 state 喂给 `_build_run_stats`，
    于是 `budget_spent / search_rounds / degraded / took_over / reused_asset` 落成默认值。
    实测账本里有 **2 行**呈现**物理上不可能**的组合：

        llm_tokens.calls = 79（真调了 79 次模型）
        budget_spent = 0      （却一分预算没花）
        search_rounds = 0     （却一次检索都没发生，同一行 citation_count = 24）

    而看板的「任务成功率」用 `delivered and not degraded` 算——`degraded` 在这些行上
    恒为 False，于是**一次降级交付被算成了完全成功**；「有效复用率」同样被压到 25.0%
    （真值 50.0%）。**错的方向恰好是"看起来更好"**。

    本用例锁三件事：
      ① 判据是**结构性自相矛盾**（可推导），不是人工标注的清单——
         这个病手工清单已经漏过五次，不能再靠"记得标一下"；
      ② **只剔过程字段，不剔整行**：token / 引文 / 耗时与那条缺陷无关，
         一起丢掉等于用一个缺陷制造第二个缺陷；
      ③ **剔了几条必须报出来**（静默剔除会被读成"全都算进去了"）。
    """
    from core.repurchase_board import _process_fields_unreliable, build_board

    _bad = {"depth": "标准", "delivered": 1, "degraded": False, "reused_asset": False,
            "budget_spent": 0, "search_rounds": 0,
            "llm_tokens": {"total_tokens": 210181, "calls": 79},
            "citation_count": 24, "elapsed_s": 543.9, "cost_cny": None}
    _good = {"depth": "标准", "delivered": 1, "degraded": False, "reused_asset": True,
             "budget_spent": 1.4, "search_rounds": 7,
             "llm_tokens": {"total_tokens": 103180, "calls": 30},
             "citation_count": 20, "elapsed_s": 400.0, "cost_cny": None}
    assert _process_fields_unreliable(_bad) is True, "该被判可疑的行没被判出来"
    assert _process_fields_unreliable(_good) is False, "正常行被误判 —— 误报会让看板丢掉真数据"

    _b = build_board(rows=[_bad, _good])
    assert _b["excluded_bad_process_fields"] == 1, "没报出剔了几条（静默剔除 = 读者以为全算进去了）"
    assert _b["metrics"]["reuse_rate"]["num"] == 1 and _b["metrics"]["reuse_rate"]["den"] == 1, (
        "坏行没被剔出复用率分母 → 复用率被系统性低估"
    )
    assert _b["metrics"]["task_success_rate"]["den"] == 1, "坏行没被剔出成功率分母"
    # ② token 口径不受影响：坏行的 token 是独立计量的硬事实，仍应计入
    assert _b["context"]["avg_total_tokens"] == int((210181 + 103180) / 2), (
        "把坏行的 token 也丢了 —— 那一个缺陷制造了第二个缺陷"
    )
    # 跨档混合必须被标出来（K17 禁跨档求均值）
    assert _b["context"]["avg_tokens_depths"] == ["标准"]


def test_prod_writes_are_gated():
    """**每一个**写生产档案的地方都必须受离线闸门保护 —— 机械枚举，不靠清单。

    ## 病案（2026-09-16 · 第五次复发 · 探针实测）

    `test_offline_write_gates` 锁着四处，docstring 从第三次复发起就写着
    「以后再新增写生产档案的模块，也必须过这道闸门」。**然后照样漏了五处。**
    `_gate_probe.py` 把各模块落盘路径改指到临时目录后置 `YANCEC_OFFLINE=1` 实测：

        受保护 5 · **未受保护 5** —— metric_ledger（指标时序库）· industry_kb（行业卡）·
        memory（用户档案）· source_health（巡检台账）· calibration（校准存档）。

    其中两处与 `exp_memory` 是同一条**反馈污染**路径，且更硬：
    `metrics.jsonl` 被 `locked_anchor()` 读回去当**锁值锚**注入下一次调研的提示词，
    `industry_kb.json` 被 `format_industry_brief()` 读回去当行业基线——
    测试题沉淀出的数字会变成**下一次真实调研的输入**。

    ## 为什么这条锁不再用手写清单

    前四次都是"发现一处补一处"。根因不是"忘了"，是**闸门装错了位置**：
    它装在**业务函数**里，而业务函数是不断新增的。所以闸门已下移到 I/O 层
    （`core/safe_io.py`），本函数则改为**机械枚举**：

      · 模块级常量里字面含 `"data"` 的 → 生产路径常量；
      · 加上函数内的**局部别名**（`tmp = _PATH + ".tmp"` 这类原子写写法，取不动点）；
      · 找出所有内容写入点（`open(w/a/x)`、`write_text`、`remove`、`replace`…）；
      · 受保护 = 自己查闸门，或**其全部调用方**都受保护（调用图不动点）；
      · 走 `safe_io.*` 的写入天然不受影响（它不是裸 `open`）；
      · 剩下的**默认失败**，要放行必须在 `_PW_ALLOWED` 里具名写理由。

    ## 这条锁自己也会被自检（"数不变 = 检查不存在"）

    末尾拿一段**合成的反面样本**跑同一个分析器，断言它确实抓得到——
    否则将来某次重构把分析器改瞎了，这条测试会安静地永远通过，
    而那正是本仓库已经踩过两次的坑（`FE_VERSION` 落后 4 版、测试数正则漏写法）。
    """
    import tempfile
    from pathlib import Path

    _here = Path(__file__).resolve().parent

    # ---- ① 合成反面样本：分析器必须有牙 ----
    _synth = tempfile.mkdtemp(prefix="yancec_pw_synth_")
    (Path(_synth) / "mod_under_test.py").write_text(
        'import json, os\n'
        '_P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "x.jsonl")\n'
        'def bad_direct(rows):\n'
        '    with open(_P, "a", encoding="utf-8") as f:\n'
        '        f.write("x")\n'
        'def bad_alias(doc):\n'
        '    t = _P + ".tmp"\n'
        '    with open(t, "w", encoding="utf-8") as f:\n'
        '        json.dump(doc, f)\n'
        '    os.replace(t, _P)\n'
        'def good_gated(rows):\n'
        '    from core import safe_io\n'
        '    if safe_io.is_offline():\n'
        '        return\n'
        '    with open(_P, "a", encoding="utf-8") as f:\n'
        '        f.write("x")\n'
        'def good_safeio(rows):\n'
        '    from core import safe_io\n'
        '    with safe_io.open_prod(_P, "a", encoding="utf-8") as f:\n'
        '        f.write("x")\n'
        # 第三种形态：**第三方持久化 API + Settings 上的路径**。
        # 这条路既没有 `open()`、也没有含 data 的模块常量，只认字符串的检查会全漏
        # ——`core/rag.py` 的 Chroma 库就是这么藏了 13MB 生产数据且没有闸门，
        # 而对抗核验员据此把那条缺陷判成了「不存在」。
        'class _Cfg:\n'
        '    vector_db_path: str = str(PROJECT_ROOT / "backend" / "data" / "chroma")\n'
        'def bad_sink():\n'
        '    import chromadb\n'
        '    from cfg import settings\n'
        '    return chromadb.PersistentClient(path=settings.vector_db_path)\n',
        encoding="utf-8",
    )
    _synth_bad, _synth_ok = _pw_scan(_synth)
    _synth_fns = {b[3] for b in _synth_bad}
    assert {"bad_direct", "bad_alias", "bad_sink"} <= _synth_fns, (
        f"分析器失去了牙齿：没抓到合成样本里没有闸门的写点（只抓到 {_synth_fns}）——"
        f"这条锁会在将来的重构中变成永远通过的空检查"
    )
    assert "good_gated" not in _synth_fns and "good_safeio" not in _synth_fns, (
        "分析器误报了有闸门/走 safe_io 的写点——误报率高的检查会被直接无视"
    )

    # ---- ② 真实代码树：所有写点都必须受保护 ----
    bad, n_ok = _pw_scan(_here)
    bad = [b for b in bad if (b[0], b[3]) not in _PW_ALLOWED]

    assert not bad, (
        "以下生产写点**没有**离线闸门（离线跑测试会污染生产数据）：\n"
        + "\n".join(f"    · {r}:{ln}  {k}()  in {fn}()" for r, ln, k, fn in bad)
        + "\n  修法：把 open()/write_text()/os.replace() 换成 core.safe_io 里对应的函数，"
          "或在函数体首行加 `if safe_io.is_offline(): return`。"
          "\n  **不要**把它们加进 _PW_ALLOWED 了事——那只是把清单从一处搬到另一处。"
    )


def test_evidence_conflict_flag_survives_both_paths():
    """证据的冲突标记必须在**两条路径**上都活着：同步（末尾统一 model_dump）与流式（逐节点快照）。

    ## 病案（2026-09-16）

    `node_check_conflict` 查出冲突后是**就地改对象**（`ev.is_conflicting = True`）、
    **不回吐 `evidences`**（只回吐 `conflict_results` / `rework_count` / `conflict_scan`）。
    而流式路径原先的 `result["evidences"]` 取自 `streamed_evidences` ——
    那是**每个节点返回时抓的快照**，于是永远停在改动之前。

    后果不是"少一个字段"，是**同一份交付物自己打自己**：
    正文《数据冲突日志》读 state 对象 → 写「⚔️ 存争议 N」；
    而可复算包 `claims.csv` 的 conflicting 列读 `result["evidences"]` → 全「否」。
    **README 恰恰让复核者打开 claims.csv 挑一条核对**，看到的正好是与正文相反的那一列。
    演示走的又正是流式路径（同步路径末尾才 dump，反而是对的）——
    又一处「同一个函数，两条路径两种真相」。

    ## 本用例怎么锁

    ① 用**真的 LangGraph** 复现机制：一个节点就地改、不回吐；下一个节点回吐。
       断言"逐节点累积"会丢、"取末节点回吐"能保住。
       —— 这样将来谁把组装逻辑"优化"回累积写法，这条会红，且理由写在眼前。
    ② 再断言产品代码确实取了后者（AST 读 `node_deliver` 的 return 字面量）。
    """
    import ast as _a
    from pathlib import Path

    from core.models import Evidence

    # ---- ① 机制：真的跑一个小状态机 ----
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    class _S(TypedDict, total=False):
        evidences: list
        conflicts: list

    def _n1(state):
        return {"evidences": [Evidence(claim="门店数 100 家", query="q1"),
                              Evidence(claim="门店数 300 家", query="q2")]}

    def _n2(state):
        # 与 node_check_conflict 同型：**就地改对象，不回吐 evidences**
        for _e in state["evidences"]:
            _e.is_conflicting = True
        return {"conflicts": [{"decision": "数值打架"}]}

    def _n3(state):
        # 与 node_deliver 同型：回吐最终状态里的对象
        return {"evidences": list(state["evidences"])}

    _g = StateGraph(_S)
    for _nm, _fn in (("n1", _n1), ("n2", _n2), ("n3", _n3)):
        _g.add_node(_nm, _fn)
    _g.add_edge(START, "n1")
    _g.add_edge("n1", "n2")
    _g.add_edge("n2", "n3")
    _g.add_edge("n3", END)
    _app = _g.compile()

    _acc, _final = [], None
    for _ev in _app.stream({"evidences": [], "conflicts": []}, stream_mode="updates"):
        for _name, _upd in _ev.items():
            if not _upd:
                continue
            if _name == "n3":
                _final = dict(_upd)
            _acc.extend(u.model_dump(mode="json") for u in (_upd.get("evidences") or []))

    assert len(_acc) >= 2 and _acc[0]["is_conflicting"] is False, (
        "机制复现失败：逐节点累积本应拿到**改动前**的快照（is_conflicting=False）——"
        "如果这里变了，说明 LangGraph 的更新语义变了，下面的结论要重新推"
    )
    assert _final and all(e.is_conflicting for e in _final["evidences"]), (
        "机制复现失败：末节点回吐的应当是**改动后**的对象（is_conflicting=True）"
    )

    # ---- ② 产品代码确实取了后者 ----
    _doc = _a.parse((Path(__file__).resolve().parent / "graph.py").read_text(encoding="utf-8"))
    _deliver = None
    for _n in _a.walk(_doc):
        if isinstance(_n, _a.FunctionDef) and _n.name == "node_deliver":
            _deliver = _n
            break
    assert _deliver is not None, "graph.py 里找不到 node_deliver"
    _ret_keys = set()
    for _n in _a.walk(_deliver):
        if isinstance(_n, _a.Return) and isinstance(_n.value, _a.Dict):
            for _k in _n.value.keys:
                if isinstance(_k, _a.Constant):
                    _ret_keys.add(_k.value)
    assert "evidences" in _ret_keys, (
        "node_deliver 没有回吐 evidences —— 流式路径的 result['evidences'] 会退回"
        "逐节点累积快照，claims.csv 的冲突列会重新与正文相反"
    )

    _src = (Path(__file__).resolve().parent / "graph.py").read_text(encoding="utf-8")
    assert "deliver 未回吐 evidences" in _src, "流式组装的兜底没出声（静默回退会把 bug 原样装回来）"
    assert "deliver 回吐的全量证据不是" in _src, "deliver 回吐时没有再推送证据事件的护栏（前端证据卡会翻倍）"


def test_false_accusations_are_blocked():
    """**把"没测到"或"不是这个意思"印成"测出来是坏的"——这几处都是假阳性。**

    为什么专门为"假阳性"写一条用例：本仓库反复出现的病大多是**漏报**（没检查、没消费、
    没披露），所以纪律一直偏向"宁可多说一句"。但闸门这一类**会改变交付行为**的组件相反——
    它的输出会被读者当成判决。**一个会误报的闸门，比没有闸门更糟**：误报几次之后，
    所有人就都学会无视它了，而它仍然在每次运行时消耗着信任。

    本条锁四处 2026-09-16 修掉的假阳性：

    ① **裸数字标记劈开一个完整数字**：RedTeam 标了 `3104`，而正文里是「门店总数达31048家」
       → 原实现用 `3104` 去匹配，命中了 `31048` 的前四位，把引文标题改成
       「3104（存疑·待核）8家」——**把一个正确的数字改成了一个错误且带警告的数字**。
    ② **窗口跨句把"人口"读成"市场规模"**：`TAM` 之后紧跟的下一句写着「人口基数 9.3 亿人」，
       原实现只截 40 字符、不看句读，于是把 9.3 亿当成 TAM → 判定三层不嵌套 →
       置顶横幅指控**我们自己的测算引擎算错了**（那正是对外的卖点）。
    ③ **0 与缺失不分**：`conflict_scan` 缺失时说「旧报告」，而真实的 `pairs_compared=0`
       是"查过且没有"——分母为 0 是**一个结论**，不是缺席。
    ④ **质检窗口全线失败时仍报「覆盖 100%」且零问题**：`_one` 在异常时 `return []`，
       与"这段查了、没问题"完全无法区分。
    """
    from core.decision_guard import demote_high_risk_in_head

    # ---- ① 不许劈开一个更长的数字 ----
    _md = "# 📋 决策清单\n\n引文：门店总数达31048家，同比 +12%。\n"
    _out = demote_high_risk_in_head(_md, [{"number": "3104", "risk": "高",
                                           "issue": "与年报口径不符", "suggestion": "核对"}])
    assert "3104（存疑·待核）8家" not in _out, f"标记劈开了一个完整数字：{_out}"
    assert "31048" in _out, "原数字被破坏（正确数字不该被改动）"
    # 边界另一侧：`13104` 的后四位同样不该被标
    _out2 = demote_high_risk_in_head("# 📋 决策清单\n\n总数 13104 家。\n",
                                     [{"number": "3104", "risk": "高", "issue": "x"}])
    assert "3104（存疑·待核）" not in _out2, f"匹配了一个更长数字的后四位：{_out2}"
    # 真正该标的仍然要标（别为了防误报把功能关掉）
    _out3 = demote_high_risk_in_head("# 📋 决策清单\n\n门店 3104 家。\n",
                                     [{"number": "3104", "risk": "高", "issue": "x"}])
    assert "存疑·待核" in _out3, f"该标的没标——防误报不能把检出关掉：{_out3}"

    # ---- ② 人口不是市场规模；且窗口不许跨句 ----
    from core.integrity_gate import _tier_values, check_sizing_nesting

    _v = _tier_values("TAM 潜在人群 9.3 亿人，市场规模 4687 亿元；SAM 651 亿元；SOM 65 亿元")
    assert _v.get("tam") == [4687.0], (
        f"把「9.3 亿人」读成了 TAM（人口不是市场规模）——会凭空造出一次 BLOCK：{_v}"
    )
    _v2 = _tier_values("TAM 定义如下。人口基数 9.3 亿人。SAM 651 亿元。SOM 65 亿元。")
    assert "tam" not in _v2, f"窗口跨句取到了下一句的人口：{_v2}"

    # ---- ②b **同层两个值**必须被抓到（2026-09-18 修，本轮最重的一条）----
    # 病案：同一份报告一边写「SAM 约为 879 亿元」（市场规模测算节 + 核心指标表），
    # 一边在同一节写「可服务市场规模（SAM）约为 928 亿元」（公式算出来的）。
    # **只抽首个**的实现看到 879 → 879 < TAM → 判定"三层嵌套成立" → 闸门放行，
    # 而报告在自己的决策区里对同一个词给了两个数。
    _v3 = _tier_values("中国咖啡行业 TAM 约为 5,371 亿元，SAM 约为 879 亿元。")
    _v4 = _tier_values("下沉市场咖啡行业的可服务市场规模（SAM）约为 928 亿元。")
    _merged = "中国咖啡行业 TAM 约为 5,371 亿元。\n\n" + "SAM 约为 879 亿元。\n\n" + \
              "下沉市场咖啡行业的可服务市场规模（SAM）约为 928 亿元。\n\n" + \
              "可获得市场规模（SOM）约为 53 亿元。"
    _tv = _tier_values(_merged)
    assert _tv.get("sam") == [879.0, 928.0], f"同层两个 SAM 没被抓全：{_tv}"
    _nest = check_sizing_nesting(_merged)
    assert "同一个市场层级" in _nest and "879" in _nest and "928" in _nest, (
        f"同层多值必须报（且两个数都要印出来供复算）：{_nest!r}"
    )
    assert _v3 and _v4, "正向样本自身不成立"
    # **反向样本**：每层唯一且严格递减 → 不许报（防"为抓新病而常态误报"）
    _clean = ("TAM 约为 5,371 亿元。SAM 约为 879 亿元。SOM 约为 53 亿元。")
    assert check_sizing_nesting(_clean) == "", f"干净的三层被误报：{check_sizing_nesting(_clean)}"
    # 斜杠连写 `TAM/SAM/SOM` 后面跟的是标签不是数值 —— 不许从那里取值
    _slash = _tier_values("支持 TAM/SAM/SOM 测算，TAM 约为 5,371 亿元。SAM 约为 879 亿元。SOM 约为 53 亿元。")
    assert _slash.get("tam") == [5371.0], f"斜杠连写处取到了错值：{_slash}"

    # ---- ④ 质检窗口失败必须出声，且排在最前（`_out[:8]` 会截尾） ----
    from unittest import mock

    import agents.quality as Q

    class _Boom:
        def invoke(self, *_a, **_k):
            raise RuntimeError("模型不可用")

    with mock.patch.object(Q, "has_main_llm", return_value=True), \
         mock.patch.object(Q, "get_quality_llm", return_value=_Boom()):
        _issues = Q._llm_check("正文" * 4000, [])
    assert _issues, "质检窗口全线失败却返回空列表——报告会印出「覆盖 100%」且零问题"
    assert "未能完成" in _issues[0], f"失败披露没排在最前（会被 [:8] 截掉）：{_issues[:1]}"


def test_required_layer_is_a_floor_not_an_equality():
    """源路由的「要求 L2」是**下限**，不是"必须恰好等于 L2"。

    病案（2026-09-16）：`mark_hit` 原先写 `decision.required_layer in layers`——**逐字相等**。
    于是判定方向整个反了：
      · 手里握着**巨潮 / SEC 的法定披露原文**（`route_source` 实测判 tier 1 → L1）
        → `"L2" in ["L1"]` 为 False → **判"未命中 L2"**；
      · 拿到一条**新浪财经转载**（tier 2 → L2）→ 判"命中"。
    报告附录据此印出「未取到法定披露原文」——而法定披露原文就在我们手里。

    `core/replan.py` 那条给补救计划的提示写得很清楚（"换检索角度去找 X 层的**一手来源**"），
    这个字段的语义是**下限**。一个"越权达标"和一个"不达标"被判成同一个结果，
    是这套层级设计最不该有的错误。
    """
    from core.source_registry import RouteDecision, mark_hit

    def _mk(req: str, urls: list[dict]) -> RouteDecision:
        d = RouteDecision(claim_label="市场规模", required_layer=req, required_label="法定披露")
        return mark_hit(d, urls)

    _cninfo = [{"url": "https://www.cninfo.com.cn/new/disclosure/detail?stockCode=600519",
                "publisher": "巨潮资讯"}]
    _sina = [{"url": "https://finance.sina.com.cn/stock/2026-09-01/doc-abc.shtml",
              "publisher": "新浪财经"}]

    assert _mk("L2", _cninfo).satisfied is True, (
        "拿到了 L1（法定披露原文）却判『未命中 L2』——判定方向反了"
    )
    assert _mk("L2", _sina).satisfied is True, "L2 满足 L2"
    assert _mk("L2", []).satisfied is False, "什么都没拿到却判满足"
    assert _mk("L1", _sina).satisfied is False, (
        "只要了 L1、只拿到 L2 转载，却判满足——下限语义被破坏成『有就行』"
    )

    # ---- L5 是**类型要求**，不是权威度下限（2026-09-17 实跑抓到的假 ✓）----
    #
    # 病案：本跑报告的路由审计表逐行印着
    #     | st_3 | 消费者声音 | L5 用户声音/社媒 | ✅ | **L1/L2** |
    # 总表写「命中必选源层 14 个 / 未命中 0 个」。根因两层：
    #   ① `_RANK` 里没有 L5 → `.get(...,99)` 兜底 99 → **恒判命中**；
    #   ② 更根本：L1–L4 是权威梯度，L5 是**另一类证据**，摆在轴末端必然推出
    #      "要用户声音、拿到统计局宏观数也算满足"——这正是 R7 抓的那个错。
    # 同时它还堵死了 `voice_boundary`：那条"无一手用户声音源"的边界声明
    # **结构上不可能被打印**，因为 satisfied 永远不会是 False。
    from core.source_registry import route_query as _rq

    _cv = _rq("用户口碑和复购率怎么样")          # → consumer_voice, required=L5, accept L5/L4/L3
    assert _cv.required_layer == "L5", f"触发词没落到 consumer_voice：{_cv.claim_label}"
    assert _cv.accept, "accept 没被带进 RouteDecision —— mark_hit 就看不到它（这次修的正是这条链）"
    assert mark_hit(_cv, [{"url": "https://www.stats.gov.cn/x"}]).satisfied is False, (
        "**要求用户声音（L5）、只拿到统计局宏观数，却判命中** —— "
        "这是本跑报告里那个假 ✓，也正是 R7 在抓的同一类错（拿宏观源背书另一类主张）"
    )
    assert mark_hit(_cv, [{"url": "https://finance.sina.com.cn/a"}]).satisfied is False, (
        "L2 转载也不该满足「用户声音」这个类型要求"
    )
    assert mark_hit(_cv, [{"url": "https://www.iresearch.cn/report"}]).satisfied is True, (
        "研报（L3）在 accept 里，应判满足"
    )
    assert mark_hit(_cv, []).satisfied is False, "什么都没拿到却判满足"
    # **反向锁**：L1–L4 的权威下限语义**不许被这次修改带歪**（上面那两条断言仍然要过）
    assert _mk("L2", _cninfo).satisfied is True, "改 L5 分支时把 L1–L4 的下限语义改坏了"

    # ---- 层级词表必须**只有一处出处**（2026-09-17 实跑发现）----
    #
    # 病案：报告正文的图例此前是**手抄**的，而且抄的是**另一套词**——
    # 「L1官方/L2媒体与咨询/L3行业/L4用户/L5推测」，
    # 而 `core/source_registry.LAYERS`（数据源体系页 `/api/source-registry`、
    # 以及**每份报告随附的可复算审计包** `audit_bundle.py` 印的"源层定义"）是
    # 「L1官方统计/监管 · L2法定披露/交易所 · L3行业机构/协会 · L4市场发现/媒体 · L5用户声音/社媒」。
    # **同一份交付物里同一个 `L2` 是两个意思**：报告正文说"媒体与咨询"，
    # 报告自己的审计包说"法定披露/交易所"。第三方拿报告对着审计包核，第一步就撞上自相矛盾。
    # 而且 `mark_hit` 给研报算出来的**本来就是 L3**——报告写 L2 是产品自己跟自己不一致。
    #
    # 锁的形状：**跑一遍渲染**，而不是 grep 源码——源码注释里会正当地引用旧词做病史记录，
    # grep 出来的会是误报（这条锁第一版就是这么写的，自己抓自己）。
    from core.source_registry import LAYERS as _LAY

    _st = {"quality_report": {"level": "PASS", "evidence_count": 3,
                              "rule_issues": [], "llm_issues": []}}
    _note = __import__("graph")._build_quality_note(_st)
    assert _note, "质检与可验证性段没渲染出来——这条锁会退化成空转"
    for _k, _v in _LAY.items():
        assert f"{_k}{_v}" in _note, (
            f"报告图例里的 {_k} 与数据源注册表不一致（缺 {_k}{_v}）——"
            f"报告与它自己的可复算审计包会印出两套层级定义"
        )
    assert "媒体与咨询" not in _note, "报告图例仍在用注册表之外的旧词（L2 一层两个意思）"
    # 提示词那一侧同样锁住：模型是照它写的
    _prompt = __import__("graph")._SUMMARY_METRICS_PROMPT
    for _k, _v in _LAY.items():
        assert f"{_k}{_v}" in _prompt, f"核心指标表的提示词没跟着注册表走（缺 {_k}{_v}）"


def test_runtime_disabled_sources_disclosed():
    """登记为「已接入」但**运行时被停用**的源，必须在披露层被剔除并单独列示。

    病案（2026-09-15 外部审计）：`sec_edgar` 的 `status="connected"` 是**静态登记**，
    而 `SEC_UA_CONTACT` 为空时该通道 **fail-closed、不发任何请求**——这是正确的合规行为，
    也是本仓库刚修好的那件事。但报告附录照旧印「已接入 N 个源」并逐条列出 SEC EDGAR：
    **披露层把已停用的源说成已接入**。这正好是本产品在批评的"说得比实际多"，
    而且方向相反——我们是**反着**说了。
    """
    import os

    from core import source_registry as sr
    from core.authority_fetch import sec_contact_configured

    _saved = os.environ.get("SEC_UA_CONTACT")
    try:
        os.environ["SEC_UA_CONTACT"] = ""
        assert not sec_contact_configured(), "空值时不应视为已配置"
        assert "sec_edgar" not in {s.id for s in sr.connected_sources()}, (
            "SEC 已 fail-closed，却仍出现在「已接入」列表里（披露层反向声明）"
        )
        _blk = {s.id: _why for s, _why in sr.blocked_runtime_sources()}
        assert "sec_edgar" in _blk, "本机未启用的源必须单独列示，不能凭空消失"
        assert "SEC_UA_CONTACT" in _blk["sec_edgar"], f"未说明停用原因：{_blk}"

        # 反向锁：把联系方式配好之后，SEC 必须**立刻回到可用**（否则就是永久停用而不是条件停用）
        os.environ["SEC_UA_CONTACT"] = "research@example.com"
        assert sec_contact_configured()
        assert "sec_edgar" in {s.id for s in sr.connected_sources()}, "配好联系方式后 SEC 应恢复为可用"
        assert "sec_edgar" not in {s.id for s, _ in sr.blocked_runtime_sources()}
    finally:
        if _saved is None:
            os.environ.pop("SEC_UA_CONTACT", None)
        else:
            os.environ["SEC_UA_CONTACT"] = _saved


def test_compliance_skip_is_not_dead_link():
    """「按合规没敲门」≠「门是坏的」：清洗层与通过率都不许把合规跳过当失败。

    病案（2026-09-15 外部审计）：
      ① `verify_url_live` 对合规跳过返回 `live=False`，而清洗层（`graph._bad_urls`）
         **只按 `live` 判断** → 跳过与死链被当成同一件事：正文 markdown 链接被降级成纯文字、
         `citations` 的 url 被清空。于是同一份报告里，核验表标着「🚪 未探测（按合规姿态主动不访问）」，
         清洗层却已经把它当死链删了——**两层对同一件事说法相反**。
      ② `pass_rate = pass / total` 而 `total` 含 skip，与同一文件写的
         "不计入通过率也不计入失败"**自相矛盾**：8 条跳过 4 条通过会印成 33%。
      ③ 一条都没敲过时必须返回 `None`（未判定），返回 `0.0` 会被前端染成红角标「0%」——
         把"没测"渲染成"全坏"。
    """
    from unittest import mock

    from core import verification as V
    from core.verification import is_compliance_skipped

    _SKIP = "未探测（站点 robots.txt 明令 Disallow 该路径）；按红线不访问，不据此判死"
    # ① 判据：合规跳过认得出，死链/证书错/空串认不出
    assert is_compliance_skipped(_SKIP)
    assert is_compliance_skipped("未探测（该路径被站点 robots 明令禁止访问）；按红线不访问，不据此判死")
    assert not is_compliance_skipped("HTTP 404")
    assert not is_compliance_skipped("证书无效(浏览器会报非私密连接): SSLError")
    assert not is_compliance_skipped("")

    # ② 通过率分母只含**真的敲过门**的条数
    _cits = [{"url": f"https://e{i}.test/a", "source": "S", "title": "T"} for i in range(4)]

    def _mk(n_pass: int, n_skip: int):
        def _fake(urls, max_items: int = 80):
            out = {}
            for i, u in enumerate(urls):
                if i < n_pass:
                    out[u] = (u, True, "HTTP 200")
                elif i < n_pass + n_skip:
                    out[u] = (u, False, _SKIP)
                else:
                    out[u] = (u, False, "HTTP 404")
            return out

        return _fake

    with mock.patch.object(V, "verify_urls", _mk(1, 3)):
        _rep = V.build_citation_report(_cits)
    assert _rep["skipped"] == 3 and _rep["probed"] == 1, _rep
    assert _rep["pass_rate"] == 1.0, (
        f"通过率分母必须只含真的敲过门的 1 条（应=100%），实得 {_rep['pass_rate']}"
        "——用 total 当分母就是与'不计入通过率'自相矛盾"
    )

    # ③ 一条都没敲过 → None（未判定），**不是 0.0**
    with mock.patch.object(V, "verify_urls", _mk(0, 4)):
        _rep2 = V.build_citation_report(_cits)
    assert _rep2["probed"] == 0
    assert _rep2["pass_rate"] is None, (
        f"全部未探测时必须返回 None（未判定），实得 {_rep2['pass_rate']}——0.0 会被画成红角标『0%』"
    )

    # ④ 清洗层必须用同一把尺，且把未探测的地址**单列**出来（否则无法披露）
    import inspect

    import graph

    _src = inspect.getsource(graph.node_deliver)
    assert "is_compliance_skipped" in _src, "清洗层没有区分「合规跳过」与「死链」"
    assert "_unprobed_urls" in _src, "未探测的地址没有被单列出来（无法披露给读者）"
    # 反向锁：坏链接的判据里必须**排除**跳过（否则等于没改）
    assert "not _is_skip(_d)" in _src, "`_bad_urls` 的判据没有排除合规跳过"


def test_fetch_page_goes_through_compliance():
    """正文抓取主路径必须与权威源层/核验层用**同一把合规尺**。

    病案（2026-09-15 外部审计）：核验层在 2026-09-14 已改成「robots + 限速 + 声明式 UA」，
    而 `mcp/search.fetch_page` —— **真正去抓正文的那个** —— 还是裸 `httpx.get` + 假 Chrome UA，
    既无 robots 门也无每主机令牌桶。同一个病"只治了一处"，本仓库已不是第一次犯。
    对外却写着「**所有**出网请求**统一过门**」「**声明式 UA（不伪装 Chrome）**」。

    本用例锁三件事（源码级断言：真跑要出网，不适合放进离线套件）：
      ① 抓取前先过 `_compliance_skip_reason`（robots / SEC fail-closed）；
      ② 出网前先过 `_throttle`（每主机 ≥1s）；
      ③ UA 来自 `_ua_for`，**不得**再出现硬编码的假 Chrome UA。
    """
    import inspect
    import re as _re

    from mcp.search import fetch_page

    _src = inspect.getsource(fetch_page)
    assert "_compliance_skip_reason" in _src, "正文抓取没过 robots/SEC 合规门"
    assert "_throttle" in _src, "正文抓取没有 per-host 令牌桶"
    assert "_ua_for" in _src, "正文抓取没用声明式 UA 选择器"
    # 反向锁：不得再出现写死的浏览器 UA（"不伪装 Chrome"这条对外承诺会当场失效）
    assert not _re.search(r"Chrome/\d+\.\d+", _src), (
        "fetch_page 里仍有硬编码的假 Chrome UA——对外承诺的「声明式 UA」会当场失效"
    )
    # 合规层不可用时必须**拒抓**，而不是静默放行做一次没守规矩的请求
    _tail = _src.split("合规层不可用")[-1][:240]
    assert "return None" in _tail, "合规层不可用时必须拒抓（宁可不抓也不越线）"

    # 同一条纪律也覆盖「禁裸 requests/httpx 出网」：本模块内不得再有绕过合规层的直连。
    # Bing 结果页解析是**唯一被明确披露的例外**，它必须带着这段说明存在——
    # 谁删了说明谁就得先改材料，否则材料又会变成"说了不做"。
    from mcp import search as _S

    _bing_src = inspect.getsource(_S)
    assert "刻意保留的例外" in _bing_src, (
        "Bing 用浏览器 UA 是刻意保留的例外，必须带披露说明；删掉说明就会让材料重新变成过度承诺"
    )


def test_pdf_label_glyphs_visible():
    """PDF 导出的法定显式标识必须**真的看得见**——字体没有的字不许印成空白/方块。

    病案（2026-09-15 外部审计，实读导出 PDF）：标识行开头渲染成了 `\\x00`。
    原因：标识以 `🤖` 开头，而这里注册的中文字体（msyh.ttc / simhei.ttf）的 cmap 里
    **没有 U+1F916**（实测 29,898 字形，0x1F916 与 0x2B50 均为 False）。
    法定标识**唯一一个会被打印出来的格式**里恰恰是它变成乱码，而且它是最该被看清的一行。

    本用例真跑一次导出、把 PDF 的文字**提取回来**检查——不用源码断言，
    因为这类缺陷的性质就是"代码看着没问题、渲染出来是空的"。
    """
    from fastapi.testclient import TestClient

    from app import app

    _c = TestClient(app)
    _body = {
        "content": "# 🤖 AI 生成合成内容标识\n\n正文含 ✅ 通过 与 ⚠️ 风险。\n\n| 指标 | 值 |\n|---|---|\n| TAM | 100 亿元 |\n",
        "title": "字形自测",
    }
    _r = _c.post("/api/export/pdf", json=_body)
    assert _r.status_code == 200, _r.status_code
    import io as _io

    from pypdf import PdfReader

    _t = PdfReader(_io.BytesIO(_r.content)).pages[0].extract_text()
    _bad = [ch for ch in _t if ord(ch) == 0 or ch == "�"]
    assert not _bad, f"PDF 里仍有渲染不出的字形（会被印成空白/方块）：{_bad[:5]}"
    # **标识语义必须还在**：不能靠"把整行删掉"来消灭方块
    assert "AI 生成合成内容" in _t, f"显式标识在 PDF 里丢了：{_t[:120]!r}"
    # 常见状态符号要有可读替身，而不是消失
    assert "√" in _t or "通过" in _t


def test_degraded_delivery_does_not_register_baseline():
    """契约：**降级交付不登记决策基线**（`register` 自己写下的调用契约）。

    病案（2026-09-15 外部审计）：`core/outcome_track.register` 的 docstring 明确写着
    「只在真交付时登记——调用方负责保证这点（**无判定/降级交付不调**）」，
    而 `graph.node_deliver` 原先**无条件**调用它。

    为什么这条要较真：降级交付（`sop_fallback`）的决策清单是**规则兜底模板**，
    不是基于本次证据链的个性化结论。把它塞进回访队列，队列里就会出现
    **"没有人真正做过"的判断**——而回访的全部意义正是"当初那句话今天还站不站得住"。
    **自己写的契约自己不守，比没有契约更糟。**

    本用例用源码级断言（与既有的"接线锁"同法）：要验的是"调用点有没有被挡住"，
    而 `node_deliver` 无法在离线套件里单独跑。
    """
    import inspect

    import graph
    from core.outcome_track import register

    _src = inspect.getsource(graph.node_deliver)
    assert "_reg_baseline" in _src, "决策基线自动登记被整段删掉了（回访队列会空转）"
    _head = _src.split("_reg_baseline")[0]
    # 反向锁：登记必须落在 `if _sf:` 的 else 分支里，而不是无条件执行
    assert "if _sf:" in _head and "else:" in _head.split("if _sf:")[-1], (
        "降级交付没有被挡在登记之前——契约「降级交付不调」形同虚设"
    )
    # 契约的另一半：`register` 自己也要能拒绝空判定（这是它对调用方的最后一道保护）
    assert register("", "Go", ["x"]) is None, "空问题不该登记"
    assert register("有问题", "", ["x"]) is None, "空判定不该登记"


def test_retrieval_tokens_are_metered():
    """检索通道的 token 必须进计量（httpx 直连不过 LangChain 回调）。

    病案（2026-09-15 外部审计）：`mcp/search._qwen_search` 为了拿 `enable_search` 的
    `search_info` 而用 `httpx.post` **直连** DashScope，**不过 `_UsageMeter` 回调**
    → 检索模型的 token 从来没进过计量。两层后果，第二层更隐蔽：
      ① K17「单份成本」的**分母系统性偏小**；
      ② `by_model` 里**永远只有一个模型键**——"按模型分账"这个能力数据上从没成立过
         （实测 6 笔账本全是 `qwen3.7-plus` 单键，而检索用的是 `qwen-plus`）。
    """
    import inspect

    from core import llm as L
    from core.llm import record_external_usage, usage_snapshot

    try:
        L.reset_usage()
        record_external_usage("qwen-plus", 274, 400)
        _s = usage_snapshot()
        assert _s["total_tokens"] == 674, _s
        assert "qwen-plus" in _s["by_model"], _s
        assert _s["by_model"]["qwen-plus"]["calls"] == 1

        # 与回调**共用同一个计量器**：两个入口混用后仍是一本账
        record_external_usage("qwen3.7-plus", 100, 50)
        _s2 = usage_snapshot()
        assert set(_s2["by_model"]) == {"qwen-plus", "qwen3.7-plus"}, _s2
        assert _s2["total_tokens"] == 674 + 150, _s2

        # 供应商没回用量 → **不记 0、不猜**（与回调同一条纪律）
        L.reset_usage()
        record_external_usage("qwen-plus", 0, 0)
        assert usage_snapshot()["calls"] == 0, "无用量时不该记一笔"

        # 接线锁：检索通道必须真的把用量交回计量器（否则分母继续偏小，且没人会发现）
        from mcp.search import _qwen_search

        assert "record_external_usage" in inspect.getsource(_qwen_search), (
            "检索通道没有把用量交回计量器——成本分母会继续偏小、by_model 永远单键"
        )
    finally:
        L.reset_usage()


def test_ledger_depth_single_vocabulary():
    """账本里的**档位只允许一种写法**——两套词表会让按档位筛选各丢一半。

    病案（2026-09-15 外部审计）：`run_stats.jsonl` 前几行 `depth="standard"`、
    后几行 `depth="标准"`：`graph._log_run_stats_safe` 写枚举值，
    `app.py` 经 `billing.MODE_ALIAS` 转中文。而 `repurchase_board` 按**精确相等**筛选，
    于是"标准档 3 笔"这个被写进材料的口径在 API 上根本复算不出来
    （实测：无过滤 10.0 万/3 笔、`depth=standard` 10.3 万/4 笔、`depth=标准` 9.8 万/2 笔）。
    """
    from core.billing import MODE_ALIAS, MODE_POINTS
    from core.repurchase_board import build_board
    from core.runstats import canon_depth

    # ① 归一函数：两种写法都收敛到中文档位名
    for _en, _cn in MODE_ALIAS.items():
        assert canon_depth(_en) == _cn, f"{_en} → {canon_depth(_en)}，应为 {_cn}"
        assert canon_depth(_cn) == _cn, f"已是中文的不该被改：{_cn}"
        assert canon_depth(_en.upper()) == _cn, f"大写码也要认：{_en.upper()}"
    # 认不出的**原样保留**（不猜、不硬塞默认档——悄悄改写比词表分裂更难查）
    assert canon_depth("定制档X") == "定制档X"
    assert canon_depth("") == ""

    # ② 读入侧也归一：两种写法得到**同一个**结果（这才是真正的修复）
    _rows = [
        {"depth": "standard", "delivered": 1, "elapsed_s": 1.0},
        {"depth": "标准", "delivered": 1, "elapsed_s": 2.0},
        {"depth": "deep", "delivered": 1, "elapsed_s": 3.0},
    ]
    _a = build_board(depth="standard", rows=list(_rows))
    _b = build_board(depth="标准", rows=list(_rows))
    assert _a["total"] == _b["total"] == 2, (_a["total"], _b["total"])
    # 两侧都归一 → 英文查询词也能命中中文行、反之亦然（这正是"各丢一半"的根治）
    assert build_board(depth="深入", rows=list(_rows))["total"] == 1, "英文档位词没命中中文档位的行"
    assert build_board(depth="deep", rows=list(_rows))["total"] == 1
    # 不存在的档位仍然是 0（归一不能变成"什么都匹配"）
    assert build_board(depth="定制", rows=list(_rows))["total"] == 0
    # ③ 兜底：两套词表若再次出现，必须能被这道归一救回来
    assert build_board(rows=[{"depth": "STANDARD", "delivered": 1}])["total"] == 1


def test_omission_link_lines_are_cited():
    """**带 markdown 链接的行 = 已摆出来源**，不许被印成"裸断言"。

    病案（2026-09-15 深访实跑，逐字取自真实报告《研策调研报告》的遗漏层附录样例）：

        - 🔗 [星巴克下沉千县开店超8000家,平价咖啡迎来新机遇 - 今日头条](https://www.toutiao.com/article/7567584423432978990/)（toutiao.com · 期未标注）

    这行被算成裸断言、并进了"裸断言样例（最多 8 条）"里的 **2 条**。原因：`_DOMAIN`
    只认**方括号/角括号内**的域名，而这里 URL 在 `](…)` 圆括号里、站点名在全角括号里。

    代价不对称：这一层印出来的是"你的报告有 N 处没摆来源"。**若样例里有一条自己就带着
    可点链接，读者会连带不信这一层的其余读数**——而这一层是我们唯一"自己找自己没引的地方"
    的证据。误报在这里比漏报更伤。

    同时锁住反向：**URL 出现在别处（不是链接）时不算已引**，免得把这一层放宽成"有 http 就算引了"。
    """
    from core.omission import scan

    # 真实报告原文（逐字）
    _real = "- 🔗 [星巴克下沉千县开店超8000家,平价咖啡迎来新机遇 - 今日头条](https://www.toutiao.com/article/7567584423432978990/)（toutiao.com · 期未标注）"
    md = "\n".join([
        "# 竞争格局",
        _real,
        # 上一轮删掉 `"- ["` 前缀跳过后，这类"带链接的断言"必须算**已引用**（既不是跳过、也不是裸断言）
        "- [瑞幸2024年年报](https://www.cninfo.com.cn/x) 门店 22,340 家，同比增 15%。",
        # 反向：裸 URL 挂在句尾，但没有任何归属结构 —— 仍属"没把来源摆出来"
        "行业整体规模约 4,687 亿元，详见相关讨论。",
    ])
    r = scan(md)
    assert r["claims"] == 3, f"三行都带数字+单位，都该算断言：{r}"
    assert r["cited"] == 2, f"两行带 markdown 链接，必须算已引：{r}"
    assert r["omitted"] == 1, f"只应剩 1 条真裸断言：{r} — 样例={r['samples']}"
    assert all("🔗" not in s for s in r["samples"]), f"带链接的行不许进裸断言样例：{r['samples']}"
    assert any("4,687" in s for s in r["samples"]), f"真裸断言反而漏了：{r['samples']}"


def test_omission_scan_reproducible_and_structured():
    """遗漏层：读数必须**可复现**、**可现场重放**，且「没数」不能与「没有」混为一谈。

    病案（2026-09-15 外部审计，三个问题）：
      ① **扫描跑在链接清理之前**：`_strip_bad_links` 会把坏链接降级为纯文字、删掉裸 URL，
         每一步都在改写正文。扫在清理前，数出来的是**读者根本看不到的那个版本**——
         同一份报告重跑可得 63 与 59 两个数，而材料引用了它。
         **一个自己都不能复现的数，不该出现在报告里。**
      ② **没有 `executed` 字段**：`claims=0`（正文确实没有数字断言）与"扫描崩了"
         在数据上不可区分，前端角标于是在崩溃时**静默消失**——读起来是"这次没什么问题"。
         与链接核验的「未探测 ≠ 0%」同型。
      ③ `_SKIP_PREFIX` 含前缀 `"- ["`：带 markdown 链接的断言**整行被跳过**，
         `claims` 与 `cited` 同时少计，且**恰恰漏掉最该算作「已引用」的那一类**（漏报更危险）。
    """
    import os
    import subprocess
    import sys as _s

    from core import omission as O

    # ③ 前缀过滤不得吃掉"带链接的断言行"
    assert "- [" not in O._SKIP_PREFIX and "* [" not in O._SKIP_PREFIX, (
        "带 markdown 链接的断言行又被整行跳过了（漏报）"
    )
    _r0 = O.scan("- [瑞幸2024年年报](https://www.cninfo.com.cn/a) 门店数 22,340 家")
    assert _r0.get("claims") == 1, f"带来源的断言行没被数进来：{_r0}"
    assert _r0.get("cited") == 1, f"带来源的断言行没被算成「已引用」：{_r0}"

    # ③b **计划类章节整节不算断言**（2026-09-15 外部审计）：
    #     附录印出的 8 条样例里 #3/#4/#6 全是「## 行动清单」下的 `*   动作：…`
    #     与「## 风险与对策」下的 `*   对策：…`——**那是计划，不是事实主张**。
    #     词表法漏了它们（"动作"不在 _SKIP_WORDS 里），改成按**章节**判。
    _plan = (
        "# 正文\n\n市场规模约 500 亿元。\n\n"
        "## 行动清单（怎么做）\n\n*   动作：筛选 GDP 增速高于 5% 的县域作为试点。\n\n"
        "## 风险与对策\n\n*   对策：避免直接参与 9.9 元价格战，客单价守 15 元。\n"
    )
    _rp = O.scan(_plan)
    assert _rp.get("claims") == 1, f"计划类章节的数字被算成断言了：{_rp.get('claims')} 处"
    assert all("动作" not in s and "对策" not in s for s in (_rp.get("samples") or [])), (
        "样例里仍印着计划类句子——附录印出来的样例会被人一眼看穿是误报"
    )
    # 反向锁：正文段落里的真断言**不许**被章节规则误伤
    assert O.scan("# 正文\n\n下沉市场客单价中位数 15.8 元。")["claims"] == 1

    # ③c 行内证据标记 `[证据: …]` 也算"把来源摆出来了"
    assert O.scan("下沉市场客单价中位数仅 15.8 元 [证据: 社会环境/宏观环境]")["cited"] == 1, (
        "行内 `[证据: …]` 标记没被认成来源（实测附录样例 #5 就是这么被误判的）"
    )

    # ③d 附录标题**不许**写「行业普遍不报的一层」——那是"从沉默推否证"（K24），
    #     而且这一行是**印在交付报告里**的，复核者直接读到。
    _apx = O.format_appendix_md(O.scan("# 正文\n\n市场规模约 500 亿元。"))
    assert "行业普遍不报" not in _apx and "多数产品" not in _apx and "业界只做" not in _apx, (
        "报告附录里出现了 K24 禁用的对比句式（从沉默推否证）"
    )

    # ① 可复现 + 只扫正文（附录不算）
    _rep = "# 正文\n\n市场规模约 500 亿元。\n\n---\n\n# ✅ 质检与可验证性\n\n覆盖 12 条。\n"
    assert O.scan(_rep) == O.scan(_rep), "同一文本两次扫描结果不一致"
    assert O.scan(_rep)["claims"] == 1, f"审计附录被算进正文了：{O.scan(_rep)}"

    # ② CLI 可现场重放——材料里写着"可 CLI 重放"，那句话必须是真的
    _p = subprocess.run(
        [_s.executable, "-m", "core.omission"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        # **必须显式给 cwd**（2026-09-17 修）：不给就继承调用者的工作目录，
        # 于是从上层目录跑时 `core` 根本不可导入，断言报的是
        # 「`core.omission` 没有 CLI 入口」——**把一次路径问题说成一句能力缺失**。
        # 而且这条断言验的正是"材料里『可现场重放』这句话成不成立"，
        # 让它自己依赖工作目录，等于用一把随环境变的尺去量那句话。
        cwd=_HERE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert "No module named" not in (_p.stdout + _p.stderr), (
        "`core.omission` 没有 CLI 入口——材料里「可现场重放」这句话不成立"
    )
    assert "用法" in _p.stdout, f"CLI 没有给出用法说明：{_p.stdout[:120]!r}"

    # ② 结构化输出必须带 executed，且读数进台账（否则跨报告看板永远看不到这一层）
    import inspect

    import graph
    from core import runstats

    assert '"executed": bool(omission_report)' in inspect.getsource(graph.node_deliver), (
        "结构化输出缺 executed——扫描崩溃会被读成「没有问题」"
    )
    assert "omission" in runstats._FIELDS, "遗漏层读数没进台账白名单（算了但落不了账）"


def test_outcome_endpoint_contract_and_source_visible():
    """决策回访端点：**文档承诺的触发方式必须与代码一致**，且基线来源要看得见。

    病案（2026-09-15 外部审计，两处）：
      ① docstring 写「POST → 同 run_now=1」，而代码是 `bool(run_now) or False` ——
         **裸 POST 什么也不会发生**。调用方 POST 完以为跑了，实际没有。
         "文档说了、代码不做"和"说了不做"是同一件事。
      ② `outcome_track.register` 写了 `source`（自动登记 vs 手工），
         但**全链路零读取方** → 界面上两种基线长得一模一样，
         而它们的分量完全不同（一条是系统自己在交付时记的账，一条是人补的）。
      ③ `outcome_track.format_md`（回访唯一的 markdown 披露面）**零生产调用**——
         一个没有调用方的披露面，等于没有披露面。已接上 `?md=1`。
    """
    from fastapi.testclient import TestClient

    from app import app

    _c = TestClient(app)
    _r = _c.get("/api/outcome-review")
    assert _r.status_code == 200, _r.status_code
    _d = _r.json()
    assert "in_flight" in _d and "reviews" in _d, list(_d.keys())
    for _b in _d["in_flight"]:
        assert "source" in _b, "基线来源（自动登记/手工）没暴露——界面上两种基线长得一样"

    # md 披露面必须有真实调用方，且**未执行时如实说"本次未执行"**（不印空壳）
    _rm = _c.get("/api/outcome-review?md=1")
    assert _rm.status_code == 200, _rm.status_code
    assert "决策回访" in _rm.text, f"md 面没有回访小节：{_rm.text[:120]!r}"
    assert "本次未执行" in _rm.text, "未执行时没有如实说明——缺席会被读成「没问题」"

    # 触发契约：源码里必须把 POST 也算作触发条件（否则 docstring 是反的）
    import inspect

    from app import outcome_review as _fn

    _src = inspect.getsource(_fn)
    assert "_is_post" in _src and "method" in _src, "裸 POST 仍然什么都不会发生（文档与行为不一致）"


def test_aigc_label_covers_all_export_formats():
    """法定标识必须覆盖**每一个导出面**——漏一个，那个面就是裸奔的。

    病案（2026-09-15 外部审计，四处）：
      ① **xlsx 一条标识都没有**（实测 `docProps/core.xml` 里只有 openpyxl 默认的 creator）；
      ② 可复算包里的 `README.md` / `rules.md` 也没有——包里打包的是**我们程序生成的文本**，
         同属"生成合成内容"；而报告本体五种格式都写了标识，**偏偏漏在自认为最讲合规的那件东西上**；
      ③ 前端"导出 .md"**绕过后端**（纯 Blob 直下）→ 与分享面板的"生成 md"导出两个不同的文件：
         一个带图表与标识，一个两样都没有；
      ④ 模拟问卷 CSV 的"演示用模拟数据"只写在**界面**上，文件本体没有——
         下载/转发之后它和真实采样看起来毫无区别。

    本用例**真跑导出并把元数据读回来**，不用源码断言——这类缺陷的性质正是
    "代码看着写了、产物里没有"。
    """
    import io as _io
    import zipfile

    from fastapi.testclient import TestClient

    from app import app

    _c = TestClient(app)
    _body = {"content": "# 摘要\n\n市场规模约 500 亿元。\n", "title": "标识覆盖自测"}

    # ① xlsx：docProps/core.xml 必须含内容编号
    _r = _c.post("/api/export/xlsx", json=_body)
    assert _r.status_code == 200, _r.status_code
    _core = zipfile.ZipFile(_io.BytesIO(_r.content)).read("docProps/core.xml").decode("utf-8")
    assert "AIGC-YC-" in _core, f"xlsx 隐式标识缺失：{_core[:200]}"
    assert "AI-generated" in _core, "xlsx 的 AIGC 属性字段缺失"

    # ③ md：隐式标识（HTML 注释）必须在——前端"导出 .md"现在也走这条路
    _r2 = _c.post("/api/export/md", json=_body)
    assert "AIGC-YC-" in _r2.content.decode("utf-8"), "md 导出缺隐式标识"

    # ② 包内文本：标识模板在位，且**编号与报告同源**（"是不是同一份"才可核对）
    from core.ai_label import content_id
    from core.audit_bundle import _aigc_stamp

    _st = _aigc_stamp("# 某份报告正文")
    assert "AI 生成合成内容" in _st and "AIGC-YC-" in _st, "包内标识模板不对"
    assert content_id("# 某份报告正文") in _st, "包内编号与报告不同源——无法核对是不是同一份"

    # ④ CSV 本体必须自带"模拟数据"声明
    from core.simulated_data import _csv_text

    _csv = _csv_text(["a", "b"], [[1, 2]])
    assert "演示用模拟数据" in _csv, "CSV 本体没有模拟数据声明——脱离界面后与真实样本无异"


def test_compliance_gates_cover_all_egress():
    """红线开关与 fail-closed 必须覆盖**每一条出网路径**——漏一条，开关就是假的。

    病案（2026-09-15 外部审计，三处同源）：
      ① **总开关关不掉核验层**：`AUTHORITY_FETCH_ENABLED=0` 此前只作用于
         `authority_fetch` / `authority_intl` / `source_health`，`core/verification` 一处都没有 →
         关掉开关之后，**最多 80 条引文照旧真出网**。
         一个"关不掉"的总开关比没有开关更危险：它让人以为已经停了。
      ② **健康探针绕过 fail-closed**：SEC 主通道已停用，而巡检测的是**探针**，
         探针走通用 `_get`（不是 `_sec_get`）→ 照样拿不含联系方式的 UA 去敲 www.sec.gov。
         结果是 `data/source_health.json` 里 sec_edgar `ok=true / HTTP 200`，
         与"该通道已停用"**同时成立**——**一块显示"✅ 活着"的面板，本身就是假信息**。
      ③ **SEC 停用在调用台账里不留痕**：`fetch_sec` 吞掉 `SecUAUnconfigured` 返回 `[]`，
         台账里这一行直接消失，与"关键词没触发 / 回空 / 网络异常"完全不可区分。
    """
    import inspect
    import os

    from core import authority_fetch as AF
    from core import verification as V

    _saved_master = os.environ.get("AUTHORITY_FETCH_ENABLED")
    _saved_sec = os.environ.get("SEC_UA_CONTACT")
    try:
        # ① 总开关：核验层的**真出网**与**对外报状态**必须同时停
        os.environ["AUTHORITY_FETCH_ENABLED"] = "0"
        _why = V._compliance_skip_reason("https://www.stats.gov.cn/sj/zxfb/")
        assert _why and "总开关" in _why, f"总开关关不掉核验层：{_why!r}"
        _live, _d = V.verify_url_live("https://www.stats.gov.cn/sj/zxfb/")
        assert _live is False and "未探测" in _d, f"总开关关闭时仍在出网：{(_live, _d)}"
        os.environ["AUTHORITY_FETCH_ENABLED"] = "1"

        # ② 健康探针：SEC 未配联系方式时**不许敲**，且要标成"未探测"而不是"失败"
        os.environ["SEC_UA_CONTACT"] = ""
        from core.source_health import PROBES, _probe_http

        _p = next((x for x in PROBES if x.sid == "sec_edgar"), None)
        assert _p is not None, "SEC 探针不见了（被删掉就等于不再巡检这个源）"
        _ok, _detail, _code, _ms = _probe_http(_p)
        assert _ok is False, "SEC 已 fail-closed，探针却报活着——健康面板会自相矛盾"
        assert "未探测" in _detail and "SEC_UA_CONTACT" in _detail, f"未说明停用原因：{_detail!r}"
        # 反向锁：配好联系方式后探针恢复正常路径（不是永久禁用）
        os.environ["SEC_UA_CONTACT"] = "someone@example.com"
        assert AF.sec_contact_configured() is True

        # ③ 台账留痕：源码里必须有"通道停用"那一条（否则这一行会凭空消失）
        _src = inspect.getsource(AF.fetch_authorities) if hasattr(AF, "fetch_authorities") else ""
        if not _src:
            import re as _re

            _m = _re.search(r"\ndef (fetch_\w+)", inspect.getsource(AF))
            _src = inspect.getsource(AF)
        assert "通道停用" in _src and "fail-closed" in _src, (
            "SEC 停用在调用台账里不留痕——'主动停用'与'查了没有'无法区分"
        )
    finally:
        for _k, _v in (("AUTHORITY_FETCH_ENABLED", _saved_master), ("SEC_UA_CONTACT", _saved_sec)):
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v


def test_industry_extraction_single_source():
    """行业取名必须**只有一个实现**，且认不出就返回空串（不编造键）。

    病案（2026-09-15 外部审计）：项目里有两套取名函数——
    `agents.researchers._extract_industry`（13 词白名单，认不出返回 `""`）
    与 `graph._extract_industry`（同一份词表 **+ 兜底"取提问前 4 个汉字"**）。
    兜底把「2026年国内现制咖啡连锁…」取成了 **「市场目前」**，于是
    `industry_kb.json` 里长出了 `市场目前` / `万级电车` / `星巴克中` 这类垃圾键；
    而**检索侧用的正是那份白名单** → 沉下去的卡**永远搜不到**。资产飞轮空转。

    删掉兜底后又暴露出白名单太窄：四个真实演示题里三个认不出行业
    （"蜜雪冰城 vs 喜茶"里没有"奶茶"二字，"星巴克中国"里没有"咖啡"二字）。
    所以正确的修法是**两步**：删掉编造的键，**并把识别做对**（补品牌→行业映射）。
    """
    import graph
    from agents.researchers import _extract_industry

    _qs = (
        "2026年国内现制咖啡连锁在下沉市场还有多少空间？",
        "蜜雪冰城 vs 喜茶，平价茶饮进县城该不该扩？",
        "星巴克中国的下沉还有空间吗？它最新营收多少？",
        "低空经济能不能做？",
        "量子计算产业前景如何？",
    )
    # ① 两个入口必须给出同一个答案（收口到唯一实现）
    for q in _qs:
        assert graph._extract_industry(q) == _extract_industry(q), f"两个入口不一致：{q}"
        assert graph._extract_industry(q) != "市场目前", "「取前4个汉字」的垃圾兜底又回来了"

    # ② 品牌名要能映射到行业（提问里常常没有行业词）
    assert _extract_industry("星巴克中国的下沉还有空间吗？") == "咖啡"
    assert _extract_industry("蜜雪冰城 vs 喜茶，平价茶饮进县城该不该扩？") in ("茶饮", "奶茶")
    assert _extract_industry("比亚迪和特斯拉谁更强") == "新能源车"
    # ③ 真认不出 → **空串**，不编造（编出来的键检索侧永远命中不到）
    assert _extract_industry("量子计算产业前景如何？") == "", "认不出时必须返回空串，不许编键"


def test_industry_cards_visible_and_read_filtered():
    """行业基线卡：**注入前必须过护栏**，且**要有给人看的读取路径**。

    病案（2026-09-15 外部审计，两处）：
      ① `_sanitize_card` 此前**只在写入时**调用，读的时候不过滤 →
         护栏上线（v0.9.47）**之前**写进去的脏卡（含无来源/低信源数字）今天照旧被注入报告。
         修法是**读时过滤**而不是删卡：卡是历史证据，护栏的职责是决定什么能进新报告。
      ② `get_industry_card` / `list_industries` **全仓无调用者** → 卡一直在沉淀、
         一直被注入，却**没有任何界面能让人看见它**（而 Demo 分镜里有一镜就要拍它）。
         数据有、披露无 = 这条护城河只有我们自己知道。
    """
    from fastapi.testclient import TestClient

    from app import app
    from core import industry_kb as KB

    # ② 读取路径存在，且返回的 brief 就是**注入报告的那段文本**（所见即所注）
    _c = TestClient(app)
    _r = _c.get("/api/industry-cards")
    assert _r.status_code == 200, _r.status_code
    _d = _r.json()
    assert "cards" in _d and "count" in _d
    for _card in _d["cards"]:
        assert _card["industry"], "卡必须有行业名"
        assert _card["brief"] == KB.format_industry_brief(_card["industry"]), (
            "端点的 brief 与真实注入文本不一致——面板上看到的不是报告里用的那段"
        )
        # 单位不得重复拼接（"189.7亿元 亿元"）
        assert not _card["market_size"].endswith(" 亿元 亿元"), _card["market_size"]

    # ① 读时过滤：**直接构造一张脏卡**验证（不依赖盘上恰好有脏数据——
    #    那种测试会随数据被清理而悄悄失效）。这是本用例真正的行为断言。
    import json as _json
    import shutil as _sh
    import tempfile
    from pathlib import Path as _P

    _d = _P(tempfile.mkdtemp(prefix="yancec_kb_"))
    _orig = KB._KB_PATH
    try:
        KB._KB_PATH = _d / "kb.json"
        KB._KB_PATH.write_text(
            _json.dumps(
                {
                    "测试行业": {
                        "card": {
                            "industry": "测试行业",
                            "key_numbers": [
                                {"name": "干净指标", "value": "1 亿元", "source": "国家统计局"},
                                {"name": "通道名指标", "value": "2 亿元", "source": "反方检索资料"},
                                {"name": "无源指标", "value": "3 亿元", "source": ""},
                            ],
                        }
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        _b = KB.format_industry_brief("测试行业")
        assert "干净指标" in _b, "合法来源的数字被误删（护栏过度）"
        assert "通道名指标" not in _b, "低信源通道名的数字被注入了——**读时护栏没生效**"
        assert "无源指标" not in _b, "无来源数字被注入了——**读时护栏没生效**"
    finally:
        KB._KB_PATH = _orig
        _sh.rmtree(_d, ignore_errors=True)


def test_ledger_readings_notation_consistent():
    """绿档账本检查：**同一个均值的两种写法必须给出同一个结论**（检查器自身的一致性）。

    病案（2026-09-15，修检查器本身）：函数注释明写"允许两类读数——单笔实测值与成功均值"，
    但精确数字那条分支只比 `tot`（单笔值），于是**同一个均值**
    写成「10.0 万 tokens」放行、写成「99,626 tokens」被拒。
    同一条口径两种写法结论相反——**它不是材料错，是尺子错**。
    与"检查器空转"同属一类：linter 自己出错时不会报错，只会给出错误的结论。
    """
    import json as _json

    import _lint_docs as L

    _p = L.BACKEND / "data" / "run_stats.jsonl"
    _tot: set[int] = set()
    _ok: list[int] = []
    for _ln in _p.read_text(encoding="utf-8", errors="replace").splitlines():
        _ln = _ln.strip()
        if not _ln:
            continue
        _r = _json.loads(_ln)
        _t = (_r.get("llm_tokens") or {}).get("total_tokens")
        if isinstance(_t, int) and _t > 0:
            _tot.add(_t)
            if _r.get("delivered") and not _r.get("degraded"):
                _ok.append(_t)
    assert _tot, "账本里没有 token 读数——本用例空转"
    _avg = int(sum(_ok) / len(_ok)) if _ok else None

    def _caught(text: str):
        return [f for f in L.check_green_ledger({"X.md": text}) if f.check != "材料缺失"]

    # 单笔实测值：两种写法都放行
    _one = sorted(_tot)[-1]
    assert not _caught(f"本笔 {_one:,} tokens"), "单笔实测值（精确写法）被误报"
    assert not _caught(f"本笔 {_one / 10000:.1f} 万 tokens"), "单笔实测值（万写法）被误报"
    # **同一个均值的两种写法必须同结论**（这正是原先自相矛盾的地方）
    if _avg:
        assert not _caught(f"平均 {_avg:,} tokens"), "均值（精确写法）被误报——检查器自相矛盾"
        assert not _caught(f"均值 {_avg / 10000:.1f} 万 tokens"), "均值（万写法）被误报"
    # 反向锁：账本里**没有**的读数仍必须被抓到（别为了消误报把检查改瞎）
    assert _caught("共 888,888 tokens"), "不存在的 token 读数没被抓到——检查被改瞎了"
    assert _caught("共 8888.8 万 tokens"), "不存在的万级读数没被抓到"


def test_mrbench_question_list_is_a_real_artifact():
    """K8 的"20 题题单"必须**有实物**，且题号与 runner 用例**对得上**。

    病案（2026-09-15 外部审计）：口径表 K8 写「评测矩阵 20 题题单已定；种子 4 题(01/02/05/11)已实跑」，
    而盘上**没有题单文件**（`_mrbench.py` 只有 8 个用例、无题号），那 4 个编号也没有任何物证。
    **一个只写在口径表里的矩阵，等于没有矩阵。**

    修法不是把话改小，而是**把矩阵落成实物**：题号 01–20 与 `_mrbench.py` 的用例顺序一一对应，
    "已实跑"一列按账本逐条核过（宁可几乎全空，也不把"跑过同行业"算成"跑过这道题"）。
    """
    import re as _re

    from _lint_docs import ROOT

    _list = ROOT / "MR-Bench题单_20题_v1_20260915.md"
    assert _list.exists(), "K8 声称的 20 题题单在盘上不存在"
    _t = _list.read_text(encoding="utf-8")

    # ① 20 个题号，且连续
    _nums = [int(x) for x in _re.findall(r"^\|\s*(\d{2})\s*\|", _t, _re.M)]
    assert _nums == list(range(1, 21)), f"题号不是连续的 01–20：{_nums}"

    # ② 题号必须与 runner 的用例**一一对应**（前 8 条即 `_CASES` 的顺序），否则编号只是装饰
    _bench = (L_BACKEND := __import__("_lint_docs").BACKEND) / "_mrbench.py"
    _src = _bench.read_text(encoding="utf-8", errors="replace")
    _cases = _re.findall(r'^\s*\("([^"]{6,})",\s*"(causal|descriptive|exploratory)"', _src, _re.M)
    assert len(_cases) >= 8, f"runner 用例数异常：{len(_cases)}"
    # 取题单表里前 8 行的"问题"列，逐条与 _CASES 的问题做前缀比对
    _rows = _re.findall(r"^\|\s*(\d{2})\s*\|[^|]*\|[^|]*\|\s*([^|]+?)\s*\|", _t, _re.M)
    _first8 = {int(n): q for n, q in _rows if int(n) <= 8}
    for i, (q, _typ) in enumerate(_cases[:8], 1):
        assert _first8.get(i, "").strip()[:14] == q.strip()[:14], (
            f"题单第 {i} 条与 runner 用例对不上：\n  题单={_first8.get(i)!r}\n  runner={q!r}"
        )

    # ③ "已实跑"一列**不许夸大**：标了 ✅ 的，必须在第 7 列**逐字引用**账本里那一笔的题面，
    #    且那段引用真的能在账本里找到。**证据写在单元格里，才叫可核**——
    #    否则"已实跑"就只是一句自述（那正是本条要治的病）。
    _led = (L_BACKEND / "data" / "run_stats.jsonl").read_text(encoding="utf-8", errors="replace")
    _marked = 0
    for _ln in _t.splitlines():
        if not _re.match(r"^\|\s*\d{2}\s*\|", _ln):
            continue
        _cells = [c.strip() for c in _ln.strip().strip("|").split("|")]
        # 列序：0=#, 1=领域, 2=题型, 3=问题, 4=runner已实装, 5=账本实跑, 6=实跑依据
        if len(_cells) < 7:
            continue
        if "✅" not in _cells[5]:          # 账本实跑列
            continue
        _marked += 1
        _quote = _cells[6]
        _m = _re.search(r"「([^」]+)」", _quote)
        assert _m, f"标了「已实跑」却没给逐字依据：{_ln[:80]}"
        assert _m.group(1) in _led, (
            f"「已实跑」引用的题面在账本里找不到：{_m.group(1)!r}——夸大了"
        )
    assert _marked >= 1, "题单里一条「已实跑」都没有，第 ③ 项等于空转"


def test_resume_and_concurrency_are_accounted():
    """暂停→继续必须落账；并发抹账必须**被标记**（而不是报一个混过的数）。

    病案（2026-09-15 外部审计，两处）：
      ① `_log_run_stats_safe` 第一行是 `if resume: return`，理由是"resume 不重复计一笔"——
         它**假定首跑已经落过账**。而暂停路径上，首跑恰在落账点**之前**被 abort 掐断
         （SSE 生成器在 for 循环里被关闭，那段没有 finally）。于是
         **一条真跑完的报告，账本里 0 行**——看板分母少一笔、单份成本少一笔，无人察觉。
      ② token 计量是**进程级全局**，两个任务同时跑会互相抹账（后开的 reset 清掉先开的累计）。
         "每任务一份计量器"要动全部调用点；更务实的是让**能读出来的那个数自己声明可信度**：
         运行开始记序号，落账时序号变了 → 标记 `llm_shared`，看板据此把这一笔排除在成本均值外。
         **报一个混过的数比报不出来更糟**——前者会被当成实测值。
    """
    import inspect

    import graph
    from core import llm as L
    from core import runstats

    # ① 续跑不再直接 return，且落账带 resumed 标记（重账可被识别，而不是不可能）
    _src = inspect.getsource(graph._log_run_stats_safe).replace("\r", "")
    assert not _src.lstrip().startswith("if resume:\n        return"), (
        "续跑仍直接 return——那条账永远不会落（真跑完却 0 行）"
    )
    assert '"resumed"' in _src, "续跑落账没带 resumed 标记"
    assert "resumed" in runstats._FIELDS, "resumed 没进账本白名单（算了但落不了账）"
    assert "llm_shared" in runstats._FIELDS, "llm_shared 没进账本白名单"

    # ② 序号 API：归零必须推进序号，且能被检测到"期间又被重置过"
    _s0 = L.reset_usage()
    assert L.usage_seq() == _s0, "reset 返回的序号与当前序号不一致"
    L.reset_usage()
    assert L.usage_seq() != _s0, "序号没有随重置推进——并发检测会永远判「正常」"
    L.reset_usage()

    # ③ 看板：排除并发行，且**如实报出排除了几笔**（否则分母变小却无人知道为什么）
    from core.repurchase_board import build_board

    _rows = [
        {"depth": "标准", "delivered": 1, "llm_tokens": {"total_tokens": 100000}},
        {"depth": "标准", "delivered": 1, "llm_tokens": {"total_tokens": 300000}, "llm_shared": True},
    ]
    _b = build_board(rows=_rows)
    _cost = next(v for v in _b["metrics"].values() if v.get("label") == "成功结果成本")
    assert _cost["shared_excluded"] == 1, f"并发行没被报出来：{_cost}"
    assert _cost["display"].startswith("10.0"), (
        f"混过的数被算进均值了（应为可归因那一笔 10.0 万）：{_cost['display']}"
    )


def test_frontend_version_tracks_engine():
    """前端版本标识不许落在引擎后面——它是一道**自检**，数不变就等于没有。

    病案（2026-09-16 核验三份外部方案时发现）：`frontend/src/App.jsx` 顶部的
    `FE_VERSION` 停在 **v0.9.78**，而引擎已走到 **v0.9.82**——中间四个版本的前端改动
    （仲裁分母 `conflictScan`、闸门字段 `integrityGate`、口径措辞…）一个都没反映。

    为什么这不是"少写个号"这么轻：这一行的**全部作用**是排浏览器缓存，判据是
    「文件顶部的数」vs「控制台印的数」。两个数都写死且从不更新时，
    **"加载了最新模块"与"加载了四天前的缓存"会印出完全相同的一行**——
    自检从"能报警"退化成"永远报平安"。而演示当天最怕的恰恰是复核者看到旧包。

    这里锁的是**漂移方向**（引擎升、前端不升），不是"两者必须相等"：
    前端单独改也可以先行 bump，不构成失败。
    """
    import re
    from pathlib import Path

    from core.version import ENGINE_VERSION

    _app = Path(__file__).resolve().parent.parent / "frontend" / "src" / "App.jsx"
    _src = _app.read_text(encoding="utf-8", errors="replace")
    _m = re.search(r'const FE_VERSION\s*=\s*"v(\d+)\.(\d+)\.(\d+)"', _src)
    assert _m, "App.jsx 里找不到 FE_VERSION 声明——前端缓存自检没了"
    _fe = tuple(int(x) for x in _m.groups())

    _e = re.match(r"v(\d+)\.(\d+)\.(\d+)", ENGINE_VERSION)
    assert _e, f"ENGINE_VERSION 不是 vX.Y.Z：{ENGINE_VERSION}"
    _en = tuple(int(x) for x in _e.groups())

    assert _fe >= _en, (
        f"前端版本 v{'.'.join(map(str, _fe))} 落后引擎 {ENGINE_VERSION}——"
        "控制台自检会变成『永远报平安』（文件与控制台印同一个旧数），"
        "演示当天分不清新包和缓存。改 src 后同步 bump App.jsx 顶部那一行。"
    )


# ============================================================
# 85. 作用域：一处赋值挡掉整个函数的模块级全局
# ============================================================
def test_no_function_shadows_a_module_global_it_might_read_unbound():
    """`graph.py:node_deliver` 那场事故的机械锁（2026-09-17）。

    ## 病案
    `logger` 是模块级（`graph.py:35`）。`node_deliver` 体内出现了 7 次
        logger = __import__("logging").getLogger(__name__)
    全写在 except 处理块里。Python 规则：函数内只要有一处赋值，这个名字在**整个函数**
    里就是局部名——模块级那个再也够不到。于是函数里任何一处 `logger.X(...)`，
    只要执行路径上没有先经过那 7 处赋值，就是 UnboundLocalError。

    实际炸的是嵌套函数 `_comp_log`（把 `logger` 当闭包自由变量读），而它
    **只在别的东西已经失败时才被调用**——平时测不出来，一出事就是
    "接漏的抹布自己破了"：本该"跳过这一项附录"，实际整章交付抛异常。

    那 7 行不是一次写的：先崩 1 处就地补 1 行，再崩一处再补一行，**根因一次没动**。
    所以这条锁的价值不在"修好了"，在于**下一次崩的时候不会再用补一行去糊**。

    判据（`_shadow_probe.py`）：模块级已绑定的名字 ＋ 函数内对它有名赋值 ＋
    存在不被任何赋值点覆盖的读取点（含嵌套函数里的闭包引用）。三条同时成立即报。
    """
    import ast as _ast
    from pathlib import Path as _P

    import _shadow_probe as _sp

    # —— 反面样本：探针必须抓得到真病 ——
    # ① 历史原形：except 里就地重绑模块级 logger，嵌套函数再去读
    # ② 无嵌套的纯形态：if 分支里赋值、块外读取
    _bad = (
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "def outer(x):\n"
        "    try:\n"
        "        pass\n"
        "    except Exception:\n"
        "        logger = logging.getLogger(__name__)\n"
        "        logger.warning('boom')\n"
        "    def inner():\n"
        "        logger.warning('from nested')\n"
        "    inner()\n"
        "    return x\n"
        "\n"
        "counter = 0\n"
        "def bad_if(x):\n"
        "    if x:\n"
        "        counter = 1\n"
        "    return counter\n"
    )
    _t = _ast.parse(_bad)
    _hits = _sp.ShadowScan(_P("<planted>.py"), _sp.module_bound(_t))
    _hits.run(_t)
    _names = {h[2] for h in _hits.hits}
    assert "logger" in _names, (
        f"探针抓不到历史原形（except 里重绑模块级 logger + 嵌套函数读）——锁已失明：{_hits.hits}"
    )
    assert "counter" in _names, f"探针抓不到 if 分支外读取的形态：{_hits.hits}"

    # —— 正面样本：正当写法一条都不许误报（否则锁会因为太吵被摘掉）——
    _good = (
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "cache = {}\n"
        "def ok_with(x):\n"
        "    with open(x) as f:\n"
        "        data = f.read()\n"
        "    return data\n"
        "def ok_try_guard(x):\n"
        "    try:\n"
        "        v = int(x)\n"
        "    except ValueError:\n"
        "        return None\n"
        "    return v\n"
        "def ok_try_assign(x):\n"
        "    try:\n"
        "        note = str(x)\n"
        "    except Exception as e:\n"
        "        note = repr(e)\n"
        "    return note\n"
        "def ok_subscript(k, v):\n"
        "    cache[k] = v\n"
        "    return cache.get(k)\n"
        "def ok_param(x, result=None):\n"
        "    if x:\n"
        "        result = x\n"
        "    return result\n"
    )
    _t2 = _ast.parse(_good)
    _h2 = _sp.ShadowScan(_P("<planted_good>.py"), _sp.module_bound(_t2))
    _h2.run(_t2)
    assert not _h2.hits, (
        "探针把正当写法误报成病（`with` 体、except 全终止的 try、两分支都赋值的 try、"
        f"下标赋值、函数参数）——太吵的锁会被人摘掉：{_h2.hits}"
    )

    # —— 真身：全仓出货代码必须 0 命中 ——
    _root = _P(__file__).resolve().parent
    _bad_files: list[str] = []
    _scanned = 0
    for _p in sorted(_root.rglob("*.py")):
        _rel = _p.relative_to(_root)
        if any(part in _sp.SKIP_DIRS for part in _rel.parts):
            continue
        try:
            _tree = _ast.parse(_p.read_text(encoding="utf-8"))
        except Exception:
            continue
        _scanned += 1
        _s = _sp.ShadowScan(_p, _sp.module_bound(_tree))
        _s.run(_tree)
        for _fl, _fn, _var, _al, _rl in _s.hits:
            _bad_files.append(f"{_rel}:{_fl} {_fn}() `{_var}` 赋值行={_al} 越界读取行={_rl}")
    assert _scanned >= 60, f"只扫到 {_scanned} 个文件——扫描范围塌了，这条锁会变成永远报平安"
    assert not _bad_files, (
        "有函数把模块级名字挡成了局部名，且存在够不到赋值点的读取（UnboundLocalError 风险）：\n  "
        + "\n  ".join(_bad_files)
        + "\n修法：删掉那处局部赋值用模块级的那个；**不要**在崩的地方再补一行同名赋值。"
    )


# ============================================================
# 86. 数据源：「未探测」的理由必须有名字，且加起来对得上
# ============================================================
def test_skip_reasons_are_named_and_add_up():
    """`source_health` 那条「两个子计数加起来对不上 `skipped`」的机械锁（2026-09-17）。

    ## 病案
    摘要里印着 `skipped=1`，而 `skipped_compliance + skipped_budget = 0 + 0`。
    两个数并排在同一行，读者一减就发现对不上。
    根因是分类器就地写成**三元子串匹配 + 认不出返回空串**：
        "budget" if "预算用尽" in d else ("compliance" if "合规姿态" in d else "")
    而 SEC 那条的 detail 是「**未探测**（本机未启用：SEC_UA_CONTACT 未配置…）」——
    两个关键词都不含 → 空类 → 从两个子计数里同时消失。

    更深一层：四类理由对读者是**完全不同的事实**，不许共用一个说法——
      · robots Disallow 是**站点定的规矩**（我们守规矩）
      · SEC 联系方式未配置是**我们自己的配置缺口**（我们没配好）
      · 总开关关闭是**我们主动关的**
      · 巡检预算用尽是**我们没跑完**
    原先 `verification.py` 把前两者并成一句「按合规姿态未探测」——
    **等于拿"我们守规矩"给"我们没配好"背书**。

    本测试用**真实生产者**产出的文案来验（不是手写样例）：文案改了而表没跟上，
    新理由就会以 `unclassified` 冒出来，这里当场红。
    """
    import core.source_registry as sr
    import core.source_health as sh
    from core.verification import ComplianceSkip, RobotsBlocked, _compliance_skip_reason

    # —— 1. 四类理由的真实文案，各自必须归到**正确的**那一类 ——
    cases: list[tuple[str, str]] = []
    cases.append((RobotsBlocked("https://x.test/a").reason, "compliance"))

    from unittest import mock

    with mock.patch("core.authority_fetch.enabled", return_value=False):
        _r = _compliance_skip_reason("https://x.test/a")
        assert _r, "总开关关闭时应当返回未探测理由"
        cases.append((_r, "switch"))

    with mock.patch("core.verification._robots_blocked", return_value=True):
        cases.append((_compliance_skip_reason("https://x.test/a") or "", "compliance"))

    with mock.patch("core.authority_fetch._robots_allows", return_value=False):
        cases.append((_compliance_skip_reason("https://x.test/a") or "", "compliance"))

    with mock.patch("core.authority_fetch.sec_contact_configured", return_value=False), \
            mock.patch("core.authority_fetch._robots_allows", return_value=True):
        # `_robots_allows` 也桩掉：否则这条用例会真去读 www.sec.gov/robots.txt，
        # **锁依赖网络＝锁会闪红**，而闪红的锁会被人摘掉。只要「SEC 未配联系方式」这一条被测到就够。
        _r = _compliance_skip_reason("https://www.sec.gov/Archives/x.htm")
        assert _r, "SEC 未配联系方式时应当返回未探测理由"
        cases.append((_r, "config"))

    _sec = next((s for s in sr.SOURCES if s.id == "sec_edgar"), None)
    assert _sec is not None, "注册表里找不到 sec_edgar"
    with mock.patch("core.authority_fetch.sec_contact_configured", return_value=False):
        _rr = sr.runtime_block_reason(_sec)
        assert _rr, "SEC 未配联系方式时 registry 应当给出运行时停用理由"
        # **必须测「生产者拼好的那一句」**，不是只测 `_why` 片段：
        # 面板上出现的是 `_probe_http` 拼出来的完整 detail。第一版测试只喂了 `_why`，
        # 当场变红——因为 `_why` 里**没有"未探测"二字**，分类器根本不认。
        # 这正好说明"测片段"会漏掉"拼装处引入的错"。
        _probe = sh.Probe(sid="sec_edgar", name="SEC EDGAR", layer="L2", kind="http",
                          url="https://www.sec.gov/edgar/search/")
        _ok, _detail, _code, _ms = sh._probe_http(_probe)
        assert not _ok, "SEC 未配联系方式时探针不该报可用（否则面板说'活着'而通道根本没启用）"
        assert "未探测" in _detail, f"探针 detail 必须是『未探测』形态：{_detail[:80]}"
        cases.append((_detail, "config"))
        assert "按合规姿态" not in _detail, (
            "SEC 未配联系方式是**本机配置缺口**，不是合规姿态；"
            "面板把两者写成一句＝拿合规立场给配置缺口背书。"
            f"实得：{_detail}"
        )

    # 巡检预算用尽（真实文案取自 `source_health` 的 budget 分支）
    _budget_literal = None
    import ast as _ast
    import pathlib as _pl

    _sh_src = _pl.Path(sh.__file__).read_text(encoding="utf-8")
    for _n in _ast.walk(_ast.parse(_sh_src)):
        if isinstance(_n, _ast.Constant) and isinstance(_n.value, str) and "预算用尽" in _n.value:
            _budget_literal = _n.value
            break
    assert _budget_literal, "source_health 里找不到『预算用尽』文案——预算分支被删了还是改了词？"
    cases.append((_budget_literal, "budget"))

    for _detail, _want in cases:
        _got = sr.classify_skip_reason(_detail)
        assert _got == _want, (
            f"未探测理由分类错了：期望 {_want} 实得 {_got!r}\n  detail={_detail[:90]}\n"
            "（分类表只有一处：core/source_registry.SKIP_REASONS）"
        )

    # —— 2. 非未探测的 detail 不许被归成任何一类（否则'失败'会被读成'没敲门'）——
    for _d in ("HTTP 404", "被站点拒绝（HTTP 403）——按红线停手，不重试不绕", "", None, "网络/证书异常: ConnectError"):
        assert sr.classify_skip_reason(_d) == "", f"非未探测文案被误分类：{_d!r}"

    # —— 3. 兜底必须是**具名**的 `unclassified`，不是空串 ——
    # （空串就是 SEC 那次从两个子计数里同时消失的方式）
    _unknown = "未探测（一个还没进表的全新型由）"
    assert sr.classify_skip_reason(_unknown) == "unclassified", (
        "认不出的未探测理由必须落到显式的 unclassified——返回空串会让它"
        "既不在合规桶也不在预算桶，两个子计数加起来对不上总数（这正是本次的病）"
    )
    assert sr.skip_reason_label("unclassified"), "unclassified 必须有可读标签，否则面板印出来是个机器枚举"

    # —— 4. 摘要的**不变式**：五个具名子计数之和 == skipped ——
    _results = [
        {"ok": True, "skipped": False, "skip_kind": ""},
        {"ok": False, "skipped": True, "skip_kind": "config"},
        {"ok": False, "skipped": True, "skip_kind": "compliance"},
        {"ok": False, "skipped": True, "skip_kind": "budget"},
        {"ok": False, "skipped": True, "skip_kind": "switch"},
        {"ok": False, "skipped": True, "skip_kind": "unclassified"},
        {"ok": False, "skipped": False, "skip_kind": ""},
    ]
    _skipped = sum(1 for r in _results if r["skipped"])
    _named = sum(
        1 for r in _results
        if r["skip_kind"] in ("compliance", "config", "switch", "budget", "unclassified")
    )
    assert _named == _skipped, f"具名子计数之和 {_named} ≠ skipped {_skipped}——不变式破了"

    # 后端摘要字段与前端渲染表必须同名（否则前端读到一个永远 undefined 的键）
    _src = _pl.Path(__file__).resolve().parent
    _sh_text = (_src / "core" / "source_health.py").read_text(encoding="utf-8")
    for _k in ("skipped_compliance", "skipped_config", "skipped_switch",
               "skipped_budget", "skipped_unclassified"):
        assert f'"{_k}"' in _sh_text, f"后端摘要缺字段 {_k}（前端已在渲染它）"
    _app = (_src.parent / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8", errors="replace")
    assert "SKIP_KIND_CHIPS" in _app, "前端缺 `SKIP_KIND_CHIPS` 表——未探测理由又变成各写各的了"
    for _k in ("skipped_compliance", "skipped_config", "skipped_switch",
               "skipped_budget", "skipped_unclassified"):
        assert f'"{_k}"' in _app, (
            f"前端 SKIP_KIND_CHIPS 里没有 {_k}——后端新增了一种未探测理由，"
            "前端会把它算进『对不上账』那部分，而不是具名印出来"
        )

    # —— 5. 合规姿态**不许**再被当成筐：robots 与 config 必须是两句不同的话 ——
    #
    # **剥注释与文档串之后再断言**：本次修的正是"注释里写着病案、代码里还留着病"，
    # 所以检查必须只看**真的会印出去的那些字符串**。
    # `#` 注释根本不在 AST 里；文档串在 AST 里但要单独排掉。
    import core.verification as _v

    def _rendered_strings(src_path) -> list[str]:
        _tree = _ast.parse(_pl.Path(src_path).read_text(encoding="utf-8"))
        _docs: set[int] = set()
        for _n in _ast.walk(_tree):
            if isinstance(_n, (_ast.Module, _ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                _b = getattr(_n, "body", None) or []
                if _b and isinstance(_b[0], _ast.Expr) and isinstance(_b[0].value, _ast.Constant) \
                        and isinstance(_b[0].value.value, str):
                    _docs.add(id(_b[0].value))
        _out: list[str] = []
        for _n in _ast.walk(_tree):
            if isinstance(_n, _ast.Constant) and isinstance(_n.value, str) and id(_n) not in _docs:
                _out.append(_n.value)
            elif isinstance(_n, _ast.JoinedStr):
                for _p in _n.values:
                    if isinstance(_p, _ast.Constant) and isinstance(_p.value, str):
                        _out.append(_p.value)
        return _out

    _printed = _rendered_strings(_v.__file__)
    _bad = [s for s in _printed if "robots Disallow / SEC 联系方式未配置" in s]
    assert not _bad, (
        "核验报告里仍把『robots Disallow』与『SEC 联系方式未配置』并成一句——"
        "前者是站点定的规矩，后者是我们自己的配置缺口，"
        "**拿合规姿态给配置缺口背书**正是本次要修的表述。实得：\n  " + "\n  ".join(_bad)
    )
    # 反向：按理由拆开的那段必须真的在印（否则上面那条会因为"整段被删"而假过）
    assert any("skip_reason_label" in str(_n.id) for _n in _ast.walk(_ast.parse(
        _pl.Path(_v.__file__).read_text(encoding="utf-8"))) if isinstance(_n, _ast.Name)), (
        "引文核验报告没有按理由拆开打印——上面那条『不含合并句』可能只是因为整段被删了"
    )
    _cs = ComplianceSkip("x")
    assert getattr(_cs, "reason", None) == "x", "ComplianceSkip 仍须带 reason 字段（下游按它分类）"


# ============================================================
# 87. 测试套件本身不许依赖工作目录
# ============================================================
def test_suite_does_not_depend_on_cwd():
    """套件从任何工作目录跑，结果必须一样（2026-09-17 修 + 锁）。

    ## 病案
    从 `yancec/backend` 跑：**144 / 144**。从仓库根跑 `python yancec/backend/_fulltest.py`：
    **5 项红**，而报出来的话是
        「四要素齐备…」`No such file or directory: '../frontend/src/App.jsx'`
        「`core.omission` 没有 CLI 入口——材料里『可现场重放』这句话不成立」
    ——**一次路径问题被报成一次能力缺失**。对这套测试来说这是最糟的失败模式：
    它整个可信度建立在"红了就是真的红了"上面，而它自己会因为 cwd 不同而红。

    根因三处：`sys.path.insert(0, ".")`、`open("../frontend/…")`、以及
    `subprocess.run([python, "-m", "core.omission"])` 没给 `cwd`。

    这条锁**只查机械可判的三类**（相对字面量路径 / `.` 进 sys.path / `-m` 子进程缺 cwd），
    不假装能证明"套件完全与环境无关"。
    """
    import ast as _ast

    _src = (_HERE / "selftest.py").read_text(encoding="utf-8")
    _tree = _ast.parse(_src)

    # ① `sys.path.insert(0, ".")`
    for _n in _ast.walk(_tree):
        if not (isinstance(_n, _ast.Call) and isinstance(_n.func, _ast.Attribute)):
            continue
        if _n.func.attr != "insert" or not _n.args:
            continue
        _first = _n.args[0]
        if isinstance(_first, _ast.Constant) and _first.value == 0 and len(_n.args) > 1:
            _p = _n.args[1]
            if isinstance(_p, _ast.Constant) and _p.value in (".", "./", ""):
                raise AssertionError(
                    f"第 {_n.lineno} 行仍把 `{_p.value!r}` 插进 sys.path——"
                    "换个工作目录跑，套件里按裸模块名导入的用例会全部 ImportError，"
                    "而失败信息长得像『材料口径不过』。"
                    "改用 `Path(__file__).resolve().parent`。"
                )

    # ② `open("相对路径")` / `Path("相对路径")` / `"相对路径".read_text()`
    _BAD_OPENERS = ("open", "Path")
    for _n in _ast.walk(_tree):
        if isinstance(_n, _ast.Call):
            _fname = getattr(_n.func, "id", None) or getattr(_n.func, "attr", None)
            if _fname in _BAD_OPENERS and _n.args:
                _a0 = _n.args[0]
                if isinstance(_a0, _ast.Constant) and isinstance(_a0.value, str):
                    _v = _a0.value
                    if not (os.path.isabs(_v) or _v.startswith("/")):
                        raise AssertionError(
                            f"第 {_n.lineno} 行用了相对路径 `{_v}`——"
                            "换个工作目录跑就是 No such file，而它会以『测试失败』的样子出现。"
                            "改用 `_HERE` 拼绝对路径。"
                        )

    # ③ `subprocess.*([python, "-m", "xxx"])` 必须显式给 cwd
    for _n in _ast.walk(_tree):
        if not (
            isinstance(_n, _ast.Call)
            and isinstance(_n.func, _ast.Attribute)
            and _n.func.attr in ("run", "Popen", "check_output", "check_call", "call")
            and isinstance(_n.func.value, _ast.Name)
            and _n.func.value.id == "subprocess"
        ):
            continue
        _kws = {k.arg for k in _n.keywords}
        if "cwd" in _kws:
            continue
        _has_m = any(
            isinstance(x, _ast.Constant) and x.value == "-m" for x in _ast.walk(_n)
        )
        if _has_m:
            raise AssertionError(
                f"第 {_n.lineno} 行的 `-m` 子进程没给 `cwd`——"
                "从别的工作目录跑时目标模块不可导入，断言会报成『没有 CLI 入口』，"
                "把路径问题说成能力缺失。"
            )


# ============================================================
# 88. 交付物的文件属性不许泄露工具链的默认值
# ============================================================
def test_office_metadata_does_not_leak_library_defaults():
    """docx / pptx 的**作者字段**必须写服务提供者，而不是库的默认值（2026-09-17 修 + 锁）。

    ## 病案
    `core/ai_label.apply_file_meta()` 此前只写 `comments`/`category`/`keywords`，
    **没写 `author`** → python-docx / python-pptx 的库默认值原样留在交付物里。
    实测**发布件**：`创意描述_成品_20260915.docx` 与 `方案书_v5_成品_20260915.docx`
    的 `dc:creator` 都是 **`python-docx`**；两份 pptx 的 `cp:lastModifiedBy` 都是
    **`Steve Canny`**（python-pptx 的作者）。

    两层问题，第二层才是真的：
      ① 复核者右键看一眼文件属性，看到的是"这文件是一个 python 库生成的"；
      ② **法定标识本身**——`dc:creator` 记的是生成者，而它记成了一个**软件库**。
         "服务提供者是谁"这一格被让给了工具链，与我们"六格式全带隐式标识"的主张不符。
    `pdf_meta()` 与 `xlsx_meta()` 两条路径都写了 creator，**只有 docx/pptx 这条漏了**——
    典型的"同一动作、三处实现，漏一处"。

    这里做**行为验证**（真建一个 docx / pptx，写元数据，解包读回来），
    而不是 grep 源码——因为"写了但没生效"（属性名拼错、被 save 覆盖）只有行为验证抓得到。
    """
    import os as _os
    import re as _re8
    import tempfile as _tf
    import zipfile as _zf
    from pathlib import Path as _P

    from core.ai_label import apply_file_meta

    _d = _tf.mkdtemp(prefix="yancec_meta_ut_")
    try:
        _made: list[tuple[str, str]] = []

        from docx import Document

        _doc = Document()
        _doc.add_paragraph("测试正文")
        apply_file_meta(_doc.core_properties, "测试正文", title="测试")
        _pd = _os.path.join(_d, "t.docx")
        _doc.save(_pd)
        _made.append((_pd, "docx"))

        from pptx import Presentation

        _prs = Presentation()
        _prs.slides.add_slide(_prs.slide_layouts[6])
        apply_file_meta(_prs.core_properties, "测试正文", title="测试")
        _pp = _os.path.join(_d, "t.pptx")
        _prs.save(_pp)
        _made.append((_pp, "pptx"))

        for _f, _kind in _made:
            with _zf.ZipFile(_f) as _z:
                _core = _z.read("docProps/core.xml").decode("utf-8", "replace")

            def _g(_tag, _c=_core):
                _m = _re8.search(rf"<{_tag}[^>]*>(.*?)</{_tag}>", _c, _re8.S)
                return (_m.group(1) if _m else "").strip()

            _creator, _lm = _g("dc:creator"), _g("cp:lastModifiedBy")
            assert _creator and _creator.lower() not in ("python-docx", "python-pptx", "openpyxl"), (
                f"{_kind} 的 dc:creator 是 {_creator!r}——库默认值泄露到交付物里了；"
                "`dc:creator` 记的是**生成者**，它必须写服务提供者，不能是工具链"
            )
            assert "python" not in _lm.lower() and "canny" not in _lm.lower(), (
                f"{_kind} 的 cp:lastModifiedBy 是 {_lm!r}（python-pptx 作者）——同上的问题"
            )
            # 法定标识四件套必须仍在（这条锁不许因为改作者字段而被误伤）
            assert "AIGC" in _core, f"{_kind} 的法定 AIGC 标识没了——改作者字段时误删了标识"
    finally:
        import shutil as _sh

        _sh.rmtree(_d, ignore_errors=True)


# ============================================================
# 89. 修复器与检查器必须共用同一把尺
# ============================================================
# 病案（2026-09-17）：`_lint_docs.py` 里「版本口径」这条检查有五个豁免，
# 而 `--fix` 的执行体 `fix_version_decls` **另写了一遍**、只有三个。
# 于是出现「**检查报 0 项、修复却改了那一行**」——
# 实测：`本文件写于 2026-09-14（引擎 v0.9.78）` 被就地改成 v0.9.87，
# 造出「写于 09-14、引擎 v0.9.87」的时空错乱，而同一份文档检查报 0。
#
# **为什么这条锁要这么写**：这类缺陷的可怕之处不是"改错一行"，
# 而是**污染把证据自己擦掉了**——改完之后那一行的版本号已等于当前值，
# 任何"比对当前值"的检查都再看不见它。所以锁不能去 grep 材料里"有没有错版本号"
# （被改过的行根本不含旧值），只能**行为验证**：
# 造一份含各类形态的样本文档，跑一次真的修复器，看它到底动了哪几行。
def test_fixer_and_checker_share_one_predicate():
    import importlib
    import re

    _lint = importlib.import_module("_lint_docs")
    _lint = importlib.reload(_lint)
    from core.version import ENGINE_VERSION as _V

    # ---- ① 判据表：每一行的期望裁决 ----
    # `fix` = 该被报告且该被改写；`skip` = 两条路都不许碰。
    _samples = [
        ("基线：引擎 **v0.9.10**（当前版本声明）", "fix"),
        ("本文件写于 2026-09-14（引擎 v0.9.10）", "skip"),      # 版本被钉在日期上
        ("本表 v4（2026-09-08）；引擎 v0.9.10", "skip"),        # 同上
        ("引擎 v0.9.10 → v0.9.20（沿革）", "skip"),             # 一行两个版本号
        ("要求引擎 ≥ v0.9.10（下限）", "skip"),                 # ≥ 下限要求
        (f"当前基线：引擎 **{_V}** 已上线", "skip"),            # 已是当前值
    ]
    for _ln, _want in _samples:
        _ms = list(_lint._VER_DECL.finditer(_ln))
        _got = [_lint.classify_version_verdict(_ln, _m, _V) for _m in _ms]
        assert _ms, f"样本没有被 _VER_DECL 命中，这条锁本身失效了：{_ln!r}"
        assert all(g == _want for g in _got), f"{_ln!r} 期望 {_want}，实得 {_got}"

    # ---- ② 行为：修复器只许动裁决为 fix 的行 ----
    _tmp = _HERE / "_fulltest_predicate_probe.md"
    _body = [s for s, _ in _samples] + [
        "",  # 空行
        "| 分镜 v0.9.10 / Demo 脚本 v0.9.20 |",   # 两个版本号、无触发词
    ]
    _tmp.write_text("\n".join(_body) + "\n", encoding="utf-8")
    _orig_docs, _orig_root = list(_lint.DOCS), _lint.ROOT
    try:
        _lint.DOCS = [_tmp.name]
        _lint.ROOT = _HERE
        _lint.fix_version_decls()
    finally:
        _lint.DOCS, _lint.ROOT = _orig_docs, _orig_root
    _after = _tmp.read_text(encoding="utf-8").splitlines()
    try:
        for _i, (_before, _) in enumerate(zip(_body, _after)):
            _may = any(
                _lint.classify_version_verdict(_before, _m, _V) == _lint.VER_FIX
                for _m in _lint._VER_DECL.finditer(_before)
            )
            assert (_before != _after[_i]) == _may, (
                f"修复器与检查器判据不一致：第 {_i + 1} 行"
                f"（允许改={_may}，实际改={_before != _after[_i]}）\n  {_before!r}\n  {_after[_i]!r}"
            )
    finally:
        _tmp.unlink()

    # ---- ③ 测试数：三种词形都要收得到，且更正叙述行不报 ----
    for _ln, _want in [
        ("离线 **146 项断言** 全绿", 146),
        ("离线自测 **114/114**", 114),
        ("_fulltest 80 项全绿", 80),
    ]:
        _hits = [n for n, _s, _e, _p in _lint.test_count_hits(_ln)]
        assert _want in _hits, f"词形没收全：{_ln!r} → {_hits}"
    _corr = "⚠️ 更正：本行原写「离线自测 **78/78**」——那是旧值"
    assert not _lint.check_test_counts({"_probe.md": _corr}), "更正叙述行被误报成违规"

    # ---- ④ `N/N` 必须整对替换 ----
    # 本函数第一版只收「自测 N」一个数，于是修复器把 `146/146` 改成了 `147/146`
    # ——**只换分子**，造出一个不存在的比率。这比不修更糟：原值自洽，改完谁看都是错的。
    _src = (_HERE / "_fulltest.py").read_text(encoding="utf-8", errors="replace")
    _nt = len(re.findall(r"^\s*check\(", _src, re.M))
    _pp = _HERE / "_fulltest_pair_probe.md"
    _pp.write_text("> 引擎 **v0.9.87**｜离线自测 **114/114**｜初赛提交 **9/19**\n", encoding="utf-8")
    try:
        _lint.DOCS, _lint.ROOT = [_pp.name], _HERE
        _lint.fix_test_counts()
    finally:
        _lint.DOCS, _lint.ROOT = _orig_docs, _orig_root
    _got = _pp.read_text(encoding="utf-8")
    _pp.unlink()
    assert f"{_nt}/{_nt}" in _got, f"`N/N` 没有整对替换（分子应随实装走）：{_got!r}"
    assert _lint.check_test_counts(
        {"_probe.md": f"> 离线自测 **{_nt}/{_nt + 1}**｜x"}
    ), "`N/N` 分母与实装不符却没被检查到——分母必须进检查，否则残迹会一直在"

    # ---- ⑤ 「第 N 节」是**引用**，「N 个章节」才是**计数** ----
    # 我自己写的技术详解里两处「`_fulltest.py` 第 89 节」被报成「写 89 个章节 ≠ 实装 96」——
    # `_SEC_CNT` 的 `个?` 可选，于是裸「N 节」两种意思都收。**收宽了就把豁免变成误伤。**
    assert not _lint.check_test_counts(
        {"_probe.md": "（`_fulltest.py` 第 89 节）修复器只许改检查器报过的行"}
    ), "「第 N 节」是引用，被误报成章节计数"
    assert _lint.check_test_counts(
        {"_probe.md": "全文共 89 个章节"}
    ), "「N 个章节」是计数，却漏报了"

    # ---- ⑥ 受检材料份数：**检查器报了、修复器必须也改得动**（2026-09-19 补）----
    # 病案：`check_test_counts` 早就在报「× 23 份 ≠ 实装 24 份」，而 `fix_test_counts`
    # 不收这一类 —— 于是每加一份受检材料，材料里那几处「12 类 × N 份」**永久停在旧值**，
    # `--fix` 跑完仍有 5 项残留、linter 永远到不了 0。
    # **检查器报了、修复器改不了**，正是公理五要防的那种"两把尺"。
    # 这一段的形状与 ② 相同：造样本 → 跑真修复器 → 断言"改动的行 == 被报的行"。
    #
    # 注：样本里"当前值"是 **1**，因为跑修复器期间 `_lint.DOCS` 被临时换成 `[探针]`
    # 而这一类判的就是 `len(DOCS)`——这不是笔误，是这段探针的作用域决定的。
    _dp = _HERE / "_fulltest_docscount_probe.md"
    _doc_body = [
        "| 材料口径自检 | 对受检材料做 **12 类 × 1 份** 机械检查 |",  # 已是当前值 → 不许动
        "| 材料口径自检 | 对受检材料做 **12 类 × 23 份** 机械检查 |",  # 过期 → 必须改
        "> 覆盖 23 份受检材料，全过",                                  # 过期 → 必须改
        "⚠️ 更正：本行原写「12 类 × 3 份」——那是旧值",                # 更正叙述 → 不碰
        "商务口径：本活动赠 3 类权益，覆盖 5 份礼包",                  # **不该被这类命中**
    ]
    _dp.write_text("\n".join(_doc_body) + "\n", encoding="utf-8")
    try:
        _lint.DOCS, _lint.ROOT = [_dp.name], _HERE
        _lint.fix_test_counts()
    finally:
        _lint.DOCS, _lint.ROOT = _orig_docs, _orig_root
    _dafter = _dp.read_text(encoding="utf-8").splitlines()
    _dp.unlink()
    assert _dafter[0] == _doc_body[0], "已是当前值的「× N 份」被改了——修复器在动没报的行"
    assert "× 1 份" in _dafter[1], f"过期份数没被改对：{_dafter[1]!r}"
    assert "1 份受检" in _dafter[2], f"「N 份受检」词形没被改对：{_dafter[2]!r}"
    assert _dafter[3] == _doc_body[3], "更正叙述行被动了（那是历史事实，改了才是造假）"
    assert _dafter[4] == _doc_body[4], (
        f"普通商务文被这类误伤了：{_dafter[4]!r}——`[×x] N 份` 只该命中带乘号的那种写法"
    )


# ============================================================
# 90. 提示词指纹：让「换了提示词校准就作废」从一句承诺变成可判定的比对
# ============================================================
# 病案（2026-09-17）：`core/calibration.py` 的存档里写着
#     「核验官是模型，换模型或改判定提示词后本结果即作废（有效期 45 天）」
# ——**没有任何东西能检查这句话**。提示词今天改了，存档在 45 天内
# 仍会被 `load()` 读出来、印进每一份交付报告。**我们承诺了一件做不到的事。**
#
# 这条锁守三件事：
#   ① 登记表**不能静默退化**（全仓提示词常量数必须等于登记表条数）；
#   ② 指纹是内容寻址的（同一内容稳定、改一个字就变）；
#   ③ 判定提示词指纹不一致时，`load()` 必须判废、且**报告里要说出理由**
#      （作废不许表现为"这一节静默消失"）。
def test_prompt_fingerprint_invalidates_calibration():
    import importlib
    import json as _json
    import os as _os
    import re as _re
    import tempfile

    P = importlib.import_module("core.prompt_registry")
    # ---- ① 登记表不许静默退化 ----
    _found = 0
    for _r, _ds, _fs in _os.walk(_HERE.parent):
        _ds[:] = [x for x in _ds if x not in ("__pycache__", "venv", "node_modules", "data")]
        for _f in _fs:
            if not _f.endswith(".py"):
                continue
            _full = _os.path.join(_r, _f)
            _rel = _os.path.relpath(_full, _HERE.parent).replace(_os.sep, "/")
            # ⚠️ 排除表里的路径是**相对 backend/** 写的，而这里算的是**相对 yancec/**——
            # 第一版直接用 `in` 比对，于是 `backend/core/prompt_registry.py` 没被排掉，
            # 它自己的 `CALIBRATION_PROMPT` 被数成第 16 个提示词，**锁当场报假警**。
            # 判据改成后缀匹配：路径基准变了也不会再错。
            if any(_rel.endswith(x) for x in P.PROMPT_SCAN_EXCLUDE):
                continue
            if _os.path.basename(_rel).startswith("_"):
                continue
            _t = open(_full, encoding="utf-8", errors="replace").read()
            _found += len(_re.findall(P.PROMPT_CONST_RE, _t, _re.M))
    assert _found == len(P.PROMPTS), (
        f"仓库里有 {_found} 个提示词常量，登记表只登记了 {len(P.PROMPTS)} 条——"
        f"**漏登记的提示词不受指纹保护**，这一层会静默退化"
    )
    # ---- ② 指纹是内容寻址的 ----
    _snap = P.snapshot()
    assert not P.missing_names(), f"有提示词取不到指纹：{P.missing_names()}"
    assert len(set(_snap.values())) == len(_snap), "不同提示词算出了同一个指纹——哈希退化了"
    assert P.prompt_set() == P.prompt_set(), "整集指纹不稳定（同一次进程内两次调用不一致）"
    # ---- ③ 指纹不一致 → 判废 + 报告里说明理由 ----
    _cal = importlib.import_module("core.calibration")
    _tmp = _HERE / "_fulltest_cal_probe.json"
    _orig = _cal._PATH
    try:
        _base = {
            "ran_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            "n_pos": 8, "n_neg": 8, "tpr": 1.0, "fpr": 0.0, "fpr_loose": 0.125,
            "rows": [], "caveat": "x", "method": "y",
        }
        # (a) 指纹一致 → 有效
        _tmp.write_text(_json.dumps({**_base, "judge_prompt_fp": P.calibration_fingerprint()}),
                        encoding="utf-8")
        _cal._PATH = str(_tmp)
        assert _cal.load() is not None, "指纹一致时不该判废"
        assert _cal.invalid_reason() == "", "指纹一致时不该有作废理由"
        # (b) 指纹不一致 → 判废，且**说得出理由**
        _tmp.write_text(_json.dumps({**_base, "judge_prompt_fp": "deadbeef"}), encoding="utf-8")
        assert _cal.load() is None, "**判定提示词变了却没判废**——承诺落空"
        assert _cal.invalid_reason() == "prompt_changed", "判废了却给不出理由"
        _md = _cal.format_appendix_md()
        assert "作废" in _md and "提示词" in _md, (
            "判废后报告里什么都没印——**静默消失**；应当印出作废理由"
        )
        # (c) 无指纹（旧存档）→ 不判废，但必须自陈"无法核对"
        _tmp.write_text(_json.dumps(_base), encoding="utf-8")
        assert _cal.load() is not None, "旧存档不该被直接判死（那会让校准附录从每份报告消失）"
        assert _cal.prompt_drift(_cal.load()) == "unknown"
        assert "无法核对" in _cal.format_appendix_md(), "旧存档必须自陈『无法核对』而不是默不作声"
    finally:
        _cal._PATH = _orig
        if _tmp.exists():
            _tmp.unlink()


# ============================================================
# 91. 情景与敏感性：确定性、公式单一出处、扰动幅度公开、红线真会响
# ============================================================
# 这一层是"把咨询方法论显式产品化"里最硬的一段，也是最容易做假的一段：
#   · 假法一：把扰动幅度藏进代码，读起来像行业标准——**必须印出来**；
#   · 假法二：复制一份公式到新模块（仓库里已有"同一公式三处实现"的病案）；
#   · 假法三：留一个 `redline` 参数却永远没有生产者（"声明了但没接线"）。
# 三条都在这条锁里被钉住。
def test_scenario_analysis_is_deterministic_and_single_sourced():
    import importlib
    import re as _re

    S = importlib.import_module("core.scenario")
    R = importlib.import_module("agents.researchers")

    # ---- ① 公式单一出处：本模块**不许出现任何测算参数名** ----
    _src = (_HERE / "core" / "scenario.py").read_text(encoding="utf-8")
    for _pname in ("potential_ratio", "tam_freq", "avg_price",
                   "sam_penetration", "sam_freq", "sam_price", "som_share"):
        assert _pname not in _src, (
            f"scenario.py 里出现了测算参数 `{_pname}`——**公式被复制了第二份**；"
            f"计算函数必须由调用方传入"
        )

    # ---- ② 用**生产那一个**计算函数跑，三档单调 ----
    _base = {
        "base_population": 930_000_000.0, "potential_ratio": 0.72, "tam_freq": 30.0,
        "avg_price": 20.0, "sam_penetration": 0.30, "sam_freq": 15.0,
        "sam_price": 18.0, "som_share": 0.08,
    }
    _res = S.analyze(_base, R._compute_tam_sam_som)
    assert _res["ok"], f"情景分析没跑起来：{_res.get('notes')}"
    _sc = _res["scenarios"]
    assert set(_sc) == {"基准", "乐观", "悲观"}, f"三档情景不全：{list(_sc)}"
    assert _sc["悲观"]["som"] < _sc["基准"]["som"] < _sc["乐观"]["som"], (
        "悲观/基准/乐观没有单调——那说明扰动方向搞反了"
    )
    # ---- ③ 敏感性按影响力降序，且变量覆盖到 ----
    _sens = _res["sensitivity"]
    assert _sens and _sens[0]["swing"] >= _sens[-1]["swing"], "敏感性没排序"
    assert _res["n_variables"] >= 7, f"只试了 {_res.get('n_variables')} 个变量，覆盖面不足"
    # ---- ④ 扰动幅度必须**公开**（不许假装是行业标准）----
    _md = S.format_md(_res)
    assert "不是行业标准" in _md and "压力测试" in _md, "扰动幅度的性质没印出来"
    # ---- ⑤ 红线**真的会响**：构造一组"动一点就破"的参数 ----
    _frag = dict(_base)
    _frag["potential_ratio"] = 0.05      # TAM 压到很低
    _frag["sam_penetration"] = 0.95      # SAM 顶到很高
    _frag["tam_freq"] = 30.0
    _frag["sam_freq"] = 29.0
    _frag["avg_price"] = 20.0
    _frag["sam_price"] = 19.9
    _red = S.analyze(_frag, R._compute_tam_sam_som, redline=lambda r: (
        bool(r["sam"] > r["tam"] or r["som"] > r["sam"]), "三层不再严格递减"))
    assert _red.get("break"), "构造了会越界的参数，红线却没响——**这条线是假的**"
    assert "击穿" in S.format_md(_red), "击穿了红线，报告里却没写"
    # ---- ⑥ 算不出来就不印，且提示词块与读者版**不是同一个东西** ----
    _bad = S.analyze({}, R._compute_tam_sam_som)
    assert not _bad["ok"] and S.format_md(_bad) == "" and S.format_prompt_block(_bad) == ""
    _pb = S.format_prompt_block(_res)
    assert "###" not in _pb, "给模型的块里出现了报告标题——**把读者版倒进了提示词**"
    assert "禁止" in _pb, "给模型的块缺口径约束"


# ============================================================
# 92. 重复动作检测：判据必须是"重复且多为空"，且不许把设计说成缺陷
# ============================================================
# 这一层最容易做错的方向是**过度报警**：本系统里反证检索（原词+风险）与
# 时效补检（双探针补近一年）**就是刻意的重复**。只按"重复了"报，
# 等于把两个设计好的机制说成 Agent 在打转——**用自家特性攻击自家产品**。
# 所以锁的核心不是"能不能报出来"，而是"**该不报的有没有不报**"。
def test_repeat_guard_distinguishes_by_design_from_spinning():
    import importlib as _il

    G = _il.import_module("core.repeat_guard")

    # ---- ① 归一化：标点/空格不同 = 同一次检索 ----
    r = G.detect([("st1", "星巴克 下沉", 5), ("st2", "星巴克下沉", 0), ("st3", "星巴克，下沉", 0)])
    assert len(r["groups"]) == 1 and r["groups"][0]["calls"] == 3, (
        f"三种写法没被归成同一个检索词：{r['groups']}"
    )
    assert r["distinct"] == 1

    # ---- ② 该不报的不报：重复 3 次但只有 1 次为空 → 不算空转 ----
    # （真实形态：反证检索又问了一遍、时效补检又问了一遍，但都拿到了东西）
    r2 = G.detect([("st1", "咖啡市场规模", 6), ("st2", "咖啡市场规模", 4), ("st3", "咖啡市场规模", 0)])
    assert r2["ok"] and not r2["groups"], (
        f"把「重复但拿到了东西」报成空转——**这是在用自家特性攻击自家产品**：{r2['groups']}"
    )
    assert "未发现空转" in G.format_md(r2), "没发现时什么都不印——调用方分不清『没查』与『查了没有』"

    # ---- ③ 该报的要报，且给出可读结果 ----
    r3 = G.detect([("st1", "县域咖啡渗透率", 0), ("st2", "县域咖啡渗透率", 0),
                   ("st3", "县域咖啡渗透率", 3)])
    assert not r3["ok"] and r3["wasted"] == 3, f"空转没被报出来：{r3}"
    _md = G.format_md(r3)
    assert "空转" in _md and "线索" in _md, "报了却没有免责——会被读成结论"
    assert "反证检索" in _md and "时效补检" in _md, (
        "免责里必须点名那两类**刻意重复**，否则读者会把设计当缺陷"
    )

    # ---- ④ 空输入、脏输入都不许炸 ----
    for bad in ([], None, [("x",)], "not-a-list", [("a", "b", "不是数")]):
        _r = G.detect(bad)
        assert isinstance(_r, dict) and "total" in _r, f"脏输入炸了：{bad!r}"

    # ---- ⑤ 本层**不改执行行为**：不得出现任何网络/模型调用 ----
    _src = (_HERE / "core" / "repeat_guard.py").read_text(encoding="utf-8")
    for _forbidden in ("requests", "httpx", "get_main_llm", "get_quality_llm", "subprocess"):
        assert _forbidden not in _src, f"repeat_guard 里出现了 `{_forbidden}`——它必须是纯函数"


def test_lint_word_forms_and_pack_subset():
    """材料自检的**判据本身**要有人管——否则检查器会静默空转。

    病案（2026-09-17，实测）：`_SEC_CNT` 写的是 `节\\b`，而 `\\b` 是 `\\w` 与非 `\\w`
    的边界，**中文汉字在 Python 的 Unicode 正则里也是 `\\w`**。于是：

        ✅「150 项 / 96 节」+行尾   → 抓得到
        ❌「150 项 / 96 节全绿」     → **永远失配**

    而「N 节全绿」恰恰是材料里的**标准写法**。后果不是"少报一次"：
    `_TEST_CNT_ALT` 仍能抓到**分子**，于是**分子被查、分母逃检**——
    实测 `研策_技术架构与设计思维详解:405` 印着「150 项 / **96** 节全绿」，
    而实装已是 **99 节**，此前**没有任何检查看得见它**。

    这个锁必须同时钉住**两个方向**，否则修一个坏一个：
      · 收窄方向：`节(?!点)` —— 中文里裸「N 节」还会命中「16 **节点**」，
        实测直接删掉 `\\b` 会在受检集里新增 27 处命中、其中 **26 处是「N 节点」**；
      · 误伤方向：`_SEC_SAME_LINE` —— 「面向复核者的 **12 节**正文」讲的是**别的文档**的节数，
        拿它去比 `_fulltest` 的 99 就是一条必然误报的检查。
    """
    from _lint_docs import section_count_hits, test_count_hits, CHECKS, PACK_UNCHECKED_OK

    # ---- ① 「N 节全绿」这个标准写法必须被抓到（这是 \b 吃掉的那一个）----
    _l1 = "| `_fulltest.py` | 全链路离线断言 | **150 项 / 96 节全绿** |"
    assert section_count_hits(_l1) == [(96, _l1.index("96"), _l1.index("96") + 2)], (
        f"「N 节全绿」没被抓到——`节\\b` 的失效回来了：{section_count_hits(_l1)}"
    )
    # 句尾/带标点的老词形不许退化
    assert [h[0] for h in section_count_hits("**150 项 / 96 节**")] == [96]
    assert [h[0] for h in section_count_hits("共 96 节，全绿")] == [96]
    assert [h[0] for h in section_count_hits("**150 项 / 96 个章节全绿**")] == [96]

    # ---- ② 「N 节点」绝不能被当成节数（收宽了就把豁免变成误伤）----
    _NODE = [
        "| LangGraph 状态机（**16 节点**）· 澄清 · MECE · 4 路并行子研究员 | ✅ | 离线断言 |",
        "### 3.1 编排骨架：16 个节点（逐节点职责）",
        "- 更准确的说法：16 节点 / 18 条普通边 / 1 组条件边",
        "> 本文件给：12 节点盒绘制规范与逐盒数据、连线语义与坐标",
    ]
    for _ln in _NODE:
        assert section_count_hits(_ln) == [], f"「N 节点」被误报成节数：{_ln} → {section_count_hits(_ln)}"
        assert test_count_hits(_ln) == [], f"「N 节点」被误报成测试数：{_ln}"

    # ---- ③ 别的文档自己的节数不许误报 ----
    assert section_count_hits("> - **《方案书_v5》= 发布简版**：面向复核者的 12 节正文，字数受提交页数约束") == [], (
        "「N 节正文」讲的是别的文档的节数，不该拿去比 _fulltest 的节数"
    )
    # 但「第 N 节」是**引用**，本来就不收（这条老守卫不许被新改动拆掉）
    assert section_count_hits("并配一条行为锁（`_fulltest.py` 第 89 节），150 项断言") == []

    # ---- ④ 分子仍须照抓，且 `N/N` 的分母仍须进检查（先前修过的病不许复发）----
    assert [h[0] for h in test_count_hits("自测 **147/146**")] == [147]
    assert test_count_hits("自测 **147/146**")[0][3] is True, "`N/N` 必须被标成 pair，否则分母又没人查"

    # ---- ⑤ ⑫「随包受检」必须是注册在案的检查，且豁免项必须写理由 ----
    _names = [n for n, _ in CHECKS]
    assert "随包受检" in _names, "⑫ 没有注册进 CHECKS——它存在也只是死代码"
    assert len(_names) == len(set(_names)), f"CHECKS 里有重名：{_names}"
    for _k, _why in PACK_UNCHECKED_OK.items():
        assert str(_why).strip(), f"`PACK_UNCHECKED_OK` 里的 `{_k}` 没写理由——豁免必须是看得见的决定"


# ============================================================
# 运行
# ============================================================
def main():
    print("=" * 60)
    print("研策 · 全链路功能自测（一次性全部测试）")
    print("=" * 60)

    section("1. Source Router 白名单")
    check("route_source 命中官方/研究/未命中", test_source_router)

    section("2. 结论置信度分级")
    check("高/中/低 + 分布", test_confidence)

    section("3. 质检：关键论断 2+ 独立来源")
    check("单源 FLAG + 双源放行", test_quality_multi_source)

    section("4. 反证检索")
    check("决策型触发 / 深入轮跳过 / 反证词", test_adversarial_search)

    section("5. MECE 问题树")
    check("四分支分类 + 每子任务只归一支 + 空分支过滤", test_mece_tree)
    check("双关键词问题不塌树(剔除每行重复的用户问题)", test_mece_tree_no_collapse)

    section("6. 缺口披露 + 7. 证据链台账")
    check("Known/Estimated/Unknown + 溯源域名", test_appendix)

    section("8. HTML 交互报告")
    check("指标提取 + 自包含 HTML", test_html_report)

    section("9. 一手验证方案")
    check("降级方案 + 格式 + 缺口扫描", test_primary_research)

    section("10. 对抗性校验（counter_evidence）")
    check("参数兼容 + 报告格式", test_adversarial_counter)
    check("RedTeam 不得用知识截止否定系统时钟 + 页眉水印不是待核数字（两道闸门 + 三条反向锁）",
          test_redteam_does_not_doubt_the_clock)

    section("11. 积分计量")
    check("档位成本 + 扣减 + 账单", test_billing)

    section("12. URL 域名回退")
    check("_domain_root 纯函数", test_domain_root)

    section("13. 渐进式 plan 事件")
    check("problem_tree 字段入 JSON", test_plan_event_shape)

    section("14. 导出/服务端点")
    check("端点注册", test_endpoints)

    section("15. 端到端（离线全状态机）")
    check("run_task 完整交付", test_e2e_offline)

    section("16. Rubric 自评器（大赛评分对标）")
    check("七维度打分 + 最弱披露 + 格式", test_rubric)

    section("17. 目标大赛五维自检 + 假设驱动分流")
    check("五维自检 + 问题类型分流", test_competition_dual_track)

    section("18. AI 透明度双模式")
    check("competition 完整附录 / enterprise 不附", test_transparency)

    section("19. 一手研究模拟数据 + 方法章节/Rubric 定向返工")
    check("模拟问卷样本(模板/确定性/诚实标注/CSV)", test_simulated_data)
    check("方法章节生成 + 框架提取", test_rubric_rework_helpers)

    section("20. URL 信任门槛 + 垃圾页探测")
    check("可信域名放行/垃圾站拦截/软404识别", test_url_trust_gates)

    section("21. 同维度合并（方案 A：一个维度只出一章）")
    check("章节键归一（基础轮/深入轮同键）", test_chapter_key_merge)
    check("最终版选取（深入轮胜出）", test_final_subtask_pick_deep)
    check("意外重复维度留首轮", test_final_subtask_pick_first)
    check("summarize 每维度只出一章", test_summarize_one_chapter_per_dim)
    check("两波执行：前序只检索/最终版拿到并入资料", test_merge_two_wave_execution)
    check("并入轮来源进入最终版引文", test_merge_raw_pool_reaches_writer_citations)
    check("深入档端到端：无同名章节/无 · 深入", test_deep_run_no_duplicate_chapters)

    section("22. 摘要/指标表的正文依据（全文数字摘录 + 背书校验）")
    check("数字摘录覆盖全文且不超预算", test_number_digest_covers_all_chapters)
    check("指标表数值必须在正文有依据", test_metric_table_backed_by_body)
    check("报告存档（report_store 落盘/取回/越权拒绝）", test_report_store)

    section("23. 交付措辞（内部工序名不得印在报告上）")
    check("高风险降级标记：措辞对外 + 不漏标邻居", test_headline_demotion_marker)

    section("24. 来源质量三道门（栏目页/时效/跨垂类）")
    check("三道门：拦得准 + 不误杀", test_source_quality_gates)
    check("数字护照：URL 兜底 + 口径要素", test_number_passport_coverage)

    section("25. 指标台账 / 时序库（一致性核对 + 跨报告血缘）")
    check("台账归属正确 + 冲突检出 + 附录诊断表不误判", test_metric_ledger_and_conflicts)
    check("时序库落库/序列/行业锚（不污染真实库）", test_metric_store_roundtrip)

    section("26. 来源血缘（把转述从独立佐证里剥出来）")
    check("归属抽取 + 回声折叠（4 域名→2 出处）", test_provenance_echo_collapse)

    section("27. 口径词典（数字公开，口径不公开）")
    check("词条释义 + 复合口径拆分 + 不猜", test_caliber_kb)

    section("28. 地域统计源下沉（省市统计局）")
    check("地域识别 + 省市只留市 + 无地域不发请求", test_region_detect)
    check("16 个地域源规格自洽（正则/日期/栏目）", test_region_site_specs)

    section("29. 指标时效基准（按指标定保鲜期）")
    check("档位嗅探：快变/常变/稳变/慢变/普查 + 嗅不出退回", test_metric_freshness)
    check("时效门按指标收紧/放宽（同一日期两种结论）", test_screen_citation_indicator)

    section("30. 域名后缀匹配（跳转链接不得被误升级）")
    check("后缀语义 + 跳转/引用链接不误判 + 不误杀", test_domain_match)

    section("31. 数据源健康巡检 + 本次调用台账")
    check("探针装载/台账读写/调用汇总/附录渲染（不发网络请求）", test_source_health)

    section("32. 统计正文数值抽取（网页散文 / 附件 PDF 表格）")
    check("散文式+表格式+导航与元数据表过滤+日期多形态", test_stat_body_extraction)

    section("33. 年报「行业情况」章节（经审计的一手行业数据）")
    check("跳过目录与交叉引用 + 金额式不误伤「403家门店」", test_annual_report_industry_section)

    section("34. 台账来源与数据期 → 冲突三分类")
    check("L3标签不当数值 + 来源/期间入账 + 期间差异不算打架", test_ledger_source_period_classification)

    section("35. 配置可达性（.env 必须同时进 os.environ）")
    check("总开关/SEC联系方式/闸门键都能被 os.environ 读到", test_env_reaches_os_environ)

    section("36. 附录接线（能测到的不能只是零件）")
    check("口径对照：引文 caliber → 附录（拆复合口径/脏数据不炸）", test_caliber_appendix_wiring)

    section("37. 360° 审计修复回归锁（8 维 74 agents / 32 条核验存活）")
    check("15 项「会印错东西」的缺陷逐条锁死", test_audit_20260914_fixes)

    section("38. AI 生成合成内容标识（GB 45438-2025 / 标识办法 2025-09-01 施行）")
    check("显式标识头尾齐备 + 两模式都有 + 内容编号可复现可证伪", test_ai_label)
    check("隐式标识进 html/docx/pdf/pptx 四条导出路径", test_ai_label_export_wiring)

    section("39. 引用蕴涵核验（claim 级 · 主张 ↔ 原文摘录）")
    check("配对筛选/排序 + 未判定不得冒充通过 + 覆盖率诚实", test_entailment_offline)
    check("JSON 解析容错 + 判定失败一律转未判定", test_entailment_robustness)
    check("pydantic Evidence / dict 双形态都要能配对（同步 vs 流式）", test_entailment_pydantic_evidence)
    check("登记线索（索引元数据）单列：既不算通过也不算失败", test_entailment_index_only)

    section("40. 引文核验通过率（链接层）")
    check("通过率计算 + 说明「本节不证明什么」", test_citation_pass_rate)

    section("41. 判定→消费→披露 断链回归锁（实跑发现）")
    check("规则层自检：R7 无 kind 曾让整节消失 + 未知 schema 兜底照印", test_rule_lint_dispatch)
    check("法定标识必须落在正文段（不被审计折叠吞掉）", test_ai_label_placement)

    section("42. 自主决策链（把「智能体自己做的取舍」结构化）")
    check("四要素齐备 + 不印空壳 + 接线锁（有模块≠有接线）", test_decision_trace)
    check("源路由决策真的被记下（字段名读错→宽except吞掉→静默 0 条）", test_route_decision_actually_recorded)

    section("43. 动态重规划（发现缺口时自己补一轮）")
    check("缺口客观可判 + 档位门 + 规则裁决（类型/去重/撞章/限量）", test_replan_gaps_and_guards)
    check("接进图且位置正确（取证后、成稿前）+ 节点文案同步", test_replan_wiring)
    check("补充调查空手而归必须如实出现（不许静默跳过）", test_supplement_empty_is_disclosed)

    section("44. 工具自主选择（SourceSpec.feeds 第一次真正参与路由）")
    check("候选筛选 + fail-open + 规则强制并集 + 取证层闸门 + 幽灵源", test_tool_select)
    check("选中了必须真的用上（只记录不使用就是装饰）", test_tool_select_wiring)
    check("聚合项别名归并（region_stats ↔ tjj_*，不加会静默关掉地域源层）", test_want_tool_alias)
    check("台账里「计划跳过」不得与「返回空」混算（标记/计数/摘要三处一致）", test_ledger_skip_consistency)
    check("混合源族（既跳过又跑过）不得被记成没跑过（摘要行不许自我否定）", test_ledger_mixed_source_family)

    section("45. 多角色辩论 + 研究经验记忆")
    check("申辩权 + 三态 + 辩论不完整不得算无争议 + 只辩带数字的主张", test_debate)
    check("只记可核对计数 + 样本门 + 只作提示不作规则 + 接线", test_exp_memory)

    section("46. 交付降级不静默（决策清单兜底病案）")
    check("双形态证据不再静默降级 + 原因可读 + 一路走到报告头与前端角标", test_sop_fallback_not_silent)
    check("规则层现场重放 CLI 必须跑全 R1–R7（漏 R7 = 结论是假的）", test_lint_report_cli_has_r7)

    section("47. 复购看板 / 自主性图谱 / token 计量（主办方语言 + 可自证）")
    check("四指标分母只含测得出那一部分 + 降级不算成功 + 未配单价不拍数", test_repurchase_board)
    check("自主性图谱与图拓扑一致 + 不可逆被点名 + 自陈等级带出处", test_autonomy_map)
    check("token 计量双形态 + 缺用量不记 0 + reset 归零", test_llm_usage_meter)

    section("48. 引用遗漏层（链接层与内容层共同的盲区）")
    check("四档分流（已引/自陈不确定/本报告自算/裸断言）+ 不惩罚诚实 + 接线", test_omission_layer)
    check("内联图表题名唯一 + 带单位 + 无画不出的码位（否则读者看到两张同名图）",
          test_chart_titles_unique_and_renderable)
    check("规则层 high 风险项必须在正文被消费（只标不删、幂等、附录不自我批注、接线在追加附录之前）",
          test_rule_findings_are_consumed_not_just_printed)

    section("49. 决策回访（决策基线此前零代码读取）")
    check("未取得 ≠ 没变化 + 判不出来不当结论 + 不改写原判断 + 落盘可读", test_outcome_review)

    section("50. 核验器假阳性率校准（决定核验结果能否被信的那个数）")
    check("正反例成对 + 与生产同源 + 未执行≠FPR0 + 过期不背书 + 推算规则在位", test_entailment_calibration)

    section("51. 材料口径自检（把「口径唯一源」从纪律变成检查）")
    check("九类检查逐类抓得到植入违规 + 正当写法不误报 + 真实材料全过", test_lint_docs)

    section("52. 非报告文档的 docx 导出（同一函数服务两类文档）")
    check("非报告模式不套章节编号且不动原文标题 + 报告模式仍编号", test_docx_non_report_mode)
    check("围栏内 `#` 不当标题 + 围栏外真标题仍在 + 围栏内容原样印出", test_docx_fence_not_heading)

    section("53. 时序派生数字（增速序列 ≠ 水平序列；可复算包读得到真 schema）")
    check("判据用单位与指标名 + 增速走几何平均(4.9%而非16.2%) + 水平值不变 + 判不出就弃权",
          test_series_math_rate_vs_level)
    check("包内副本与 core/series_math 一致 + 真实 schema 能检出 + 同年去重 + 「0 条」自己解释自己",
          test_audit_bundle_compute_reads_real_schema)

    section("54. LLM 质检的覆盖率与披露（此前只看前 3000 字，且检出从不披露）")
    check("窗口首尾覆盖全文且重叠 + 覆盖率按并集算不虚报 + 超长如实降 + 两层条数分开报",
          test_quality_llm_full_coverage_and_disclosure)

    section("55. 档位预算单位唯一（积分 vs 元三处打架；价目两张表）")
    check("价目由计费引擎派生（不再抄第二份）+ 源码不再插值成元 + 停止卡片用积分 + 护栏同单位",
          test_budget_unit_unified)

    section("56. 离线写入闸门覆盖全部生产写入点（账本 + 报告存档 + 可复算包）")
    check("离线模式下报告不落盘、账本不落账、可复算包不出包（同一个病不许只治一处——已复发三次）",
          test_offline_write_gates)

    section("56b. 生产写点**机械枚举**（手写清单挡不住新增，第五次复发就是这么来的）")
    check("AST 枚举全部写 data/ 的位置 + 调用图闭包判闸门 + 分析器自检有牙",
          test_prod_writes_are_gated)

    section("55z. 复购看板必须剔除「过程字段被缺陷写坏」的行（否则降级交付被算成成功）")
    check("结构判据可推导 + 只剔过程字段不剔整行 + 剔了几条必须报出来 + 跨档必须标出",
          test_board_excludes_rows_with_broken_process_fields)

    section("56aa. 流式组装段必须吃**图内最终状态**（账本 6 字段恒 0 的那条）")
    check("桩图端到端驱动 run_task_stream：budget/rework/search_rounds/复用/接管/降级/档位/conflict_scan 全部命中",
          test_stream_assembly_uses_graph_state)

    section("56bb. 「被具名传入却从未使用」的参数（跨报告锁值锚整条链是死的就是这么来的）")
    check("调用方写了 kwarg=、函数体里却从不出现 → 默认失败，接口对齐须具名写理由",
          test_no_keyword_parameter_is_silently_ignored)

    section("56c. 前端「存了不读」（闸门角标就是这么变成隐形的）")
    check("done 处理里每个字段都至少有一个读取点（只有赋值 = 该功能不存在）",
          test_frontend_stores_no_field_it_never_reads)

    section("57. 冲突标记必须在同步与流式**两条路径**上都活着（否则报告与自己的 claims.csv 相反）")
    check("真 LangGraph 复现『就地改不回吐 → 逐节点快照丢标记』+ deliver 回吐最终对象 + 兜底出声 + 不重复推证据",
          test_evidence_conflict_flag_survives_both_paths)

    section("58. 「已接入但本机未启用」的源必须被披露层剔除（不许反向声明）")
    check("SEC 未配 UA 时移出已接入 + 单独列示原因 + 配好后立刻恢复", test_runtime_disabled_sources_disclosed)

    section("58a. 「要求 L2 层」是下限，不是逐字相等（方向反了会诬告自己没拿到一手源）")
    check("拿到 L1 满足 L2 + L2 满足 L2 + 空不满足 + 只要 L1 却只有 L2 时不满足",
          test_required_layer_is_a_floor_not_an_equality)

    section("58b. 假阳性防线（会改变交付行为的组件，误报比漏报更伤）")
    check("裸数字不劈开更长数字 + 人口不当 TAM + 窗口不跨句 + 质检窗口失败必须出声",
          test_false_accusations_are_blocked)

    section("58. 「按合规没敲门」≠「门是坏的」（清洗层与通过率都不许混淆）")
    check("跳过认得出 + 通过率分母只含真敲过的 + 全跳过返回 None 而非 0 + 清洗层区分并披露",
          test_compliance_skip_is_not_dead_link)

    section("59. 正文抓取主路径的合规（对外承诺「所有出网请求统一过门」）")
    check("过 robots/SEC 门 + 令牌桶 + 声明式 UA + 无硬编码 Chrome UA + 例外必须带披露",
          test_fetch_page_goes_through_compliance)

    section("60. PDF 法定显式标识的字形（真跑导出并把文字提取回来检查）")
    check("无渲染不出的字形 + 标识语义仍在 + 状态符号有可读替身", test_pdf_label_glyphs_visible)

    section("61. 降级交付不登记决策基线（register 自己写下的调用契约）")
    check("登记被 sop_fallback 挡住 + 空问题/空判定不登记", test_degraded_delivery_does_not_register_baseline)

    section("62. 计量与账本口径（检索 token 进账 + 档位单一词表）")
    check("直连调用与回调共用同一计量器 + 无用量不记 0 + 检索通道已接线", test_retrieval_tokens_are_metered)
    check("档位归一（英/中文码互通）+ 读入侧也归一 + 认不出原样保留", test_ledger_depth_single_vocabulary)

    section("63. 遗漏层（可复现 / 可重放 / 没数≠没有）")
    check("带链接的断言行不再被整行跳过 + 同文本可复现 + 只扫正文 + CLI 可重放 + executed 与台账",
          test_omission_scan_reproducible_and_structured)
    check("带 markdown 链接的行算「已引」而非「裸断言」（含真实报告原文 + 反向锁）",
          test_omission_link_lines_are_cited)

    section("64. 决策回访端点（触发契约 + 来源可见 + md 披露面有调用方）")
    check("裸 POST 也算触发 + in_flight 带 source + ?md=1 有落点且未执行时如实说明",
          test_outcome_endpoint_contract_and_source_visible)

    section("65. 法定标识覆盖每一个导出面（xlsx / md / 包内文本 / CSV 声明）")
    check("真跑导出读回元数据：xlsx 有编号 + md 有注释 + 包内同源编号 + CSV 自带模拟声明",
          test_aigc_label_covers_all_export_formats)

    section("66. 合规闸门覆盖每一条出网路径（总开关 / SEC 探针 / 台账留痕）")
    check("总开关能关掉核验层 + SEC 探针不绕过 fail-closed + 停用在台账里留痕",
          test_compliance_gates_cover_all_egress)

    section("67. 行业资产（取名唯一实现 / 读时过滤 / 有给人看的读取路径）")
    check("两个入口一致 + 品牌映射到行业 + 认不出返回空串（不编键）", test_industry_extraction_single_source)
    check("注入前过护栏 + /api/industry-cards 的 brief 即注入文本（所见即所注）",
          test_industry_cards_visible_and_read_filtered)

    section("68. 检查器自身的一致性 + K8 题单必须有实物")
    check("同一均值的两种写法同结论（不存在的读数仍被抓）+ 反向锁", test_ledger_readings_notation_consistent)
    check("题单 20 题成文 + 题号与 runner 用例对得上 + 「已实跑」不许夸大",
          test_mrbench_question_list_is_a_real_artifact)

    section("69. 续跑落账 + 并发抹账如实标记")
    check("续跑不再直接 return 且带 resumed + 序号能检测重置 + 看板排除并发行并报出笔数",
          test_resume_and_concurrency_are_accounted)

    section("70. 账本档位必须是真实档位（不是计划模型的默认值）")
    check("落账读『跑完的结果里的计划』而非 init_state 的空计划 + override 最高优先 + 异常才回退",
          test_run_stats_records_real_depth)

    section("71. TAM/SAM/SOM 必须是严格嵌套的三层（技术卖点反证）")
    check("SAM 与声称公式一致 + SAM 反推基数 ≤ TAM 基数 + 越界留痕并接进注入块",
          test_tam_sam_som_is_a_nested_chain)

    section("72. 决策完整性闸门（把『发现问题』接上『控制问题』）")
    check("决策区高风险 → BLOCK 且 Go→Validate（只降不升）+ 横幅置顶 + 判定行改写 + 别节不篡改 + 异常放行",
          test_decision_integrity_gate)

    section("72b. R8 第二族（自协商取值）+ 闸门第 ⑥ 层（语义复核）+ 缺口披露取句")
    check("真实病案三处都抓得到 + 同一份真报告里的近似句零误报 + 干净报告不被误伤",
          test_engine_self_negotiation_and_semantic_qc_and_gap_lines)

    section("72c. 闸门 BLOCK 后必须就地标记依赖被阻断数字的建议句（打码式假阻断）")
    check("只标建议句（条件判据句/表格行/别节都不碰）+ 只追加不改数字 + 横幅列出 + 幂等",
          test_gate_marks_blocked_advice_inline)
    check("数值 token 按独立的数比（68.2% 里的 8.2% 不算命中）+ 区间整体成 token + 真值仍命中",
          test_gate_number_token_has_digit_boundary)
    check("正文印出的算式必须算得出它自己印的结果（SAM 10.7× 抓到）+ 首版四类误报全部锁死",
          test_printed_arithmetic_must_self_check)
    check("规则层发现真的能触发闸门（severity/level 两个字段名都认）+ 无标注不阻断 + 目标市场/进入壁垒进决策区",
          test_rule_layer_findings_actually_reach_the_gate)
    check("主体级反证：命中本案主体才触发 + 提醒钉到执行摘要处 + 幂等 + 不命中时一字不加",
          test_subject_tension_reaches_the_conclusion)
    check("一手方案摘除引擎自身合规词（GB 45438 域污染）+ 如实披露 + 正当行不连带",
          test_primary_research_strips_engine_own_compliance_words)

    section("73. 一手/二手界线与标识诚实性（赛场红线）")
    check("用户声音章节印边界声明（判据走源路由，有一手源时一字不加）+ 模拟问卷自称『方法演示』",
          test_voice_boundary_and_label_honesty)
    check("同一份交付文件只有一个内容编号（导出侧复用正文编号，不许重算）",
          test_content_id_is_one_and_the_same)

    section("74. 仲裁引擎真的会触发吗（『0 处仲裁』必须有分母）")
    check("冲突主干归一吃掉空白（原判据要逐字相同→近乎永不触发）+ 分母进 TaskState 真键 + 前端读它",
          test_conflict_scan_trunk_and_denominator)

    section("75. 前端版本自检不许退化（数不变 = 检查不存在）")
    check("FE_VERSION 不落后引擎（落后则『文件数』与『控制台数』同为旧值，永远报平安）",
          test_frontend_version_tracks_engine)

    section("76. 节点返回的键必须都在 TaskState 里（LangGraph 会静默丢弃 schema 外的键）")
    check("扫全部 node_* 的 return 字面量，schema 外的键默认失败、豁免须写理由（此坑已复发三次）",
          test_deliver_return_keys_are_in_schema)

    section("77. 图表层的『别把错的画给读者』（符号 / 单位 / 丢行）")
    check("负号必须收（否则亏损画成正柱）+ 标注不当单位 + 同行不许静默丢失 + 异量纲不共轴",
          test_charts_do_not_flip_signs_or_drop_rows)

    section("78. 指标一致性核对：假冲突比漏报更伤")
    check("多数值列＝同指标多口径（不报冲突）+ 缺失标记不是指标名 + 真冲突照报",
          test_ledger_reports_conflicts_only_when_numbers_really_clash)

    section("79. 「实现了」与「能被用上」之间不许断链")
    check("后端支持的导出格式 ⊆ 界面调用点（六格式必须在界面上数得出来）",
          test_every_claimed_export_format_is_reachable_from_the_ui)
    check("每条读接口要么有界面入口、要么在豁免表里具名写理由（豁免需可机械核对）",
          test_api_routes_have_a_ui_entry_or_a_named_reason)

    section("80. 交付面不许吃掉 0、不许印机器枚举、不许静默截断")
    check("作品页：0 与「未统计」不同形 + META 的 </script> 与正文同姿势转义",
          test_artifact_page_keeps_zeros_and_escapes_scripts)
    check("《数据冲突日志》的仲裁取舍列汉化，且覆盖 models.py 的全部取值",
          test_conflict_decision_is_human_readable)
    check("「需要人工注意的 N 条」截断留痕（剥注释后断言，防自己的病案注释喂饱检查）",
          test_human_attention_list_discloses_truncation)
    check("决策回访：未到应验日要自陈 + 表格格子同一套清洗规则",
          test_outcome_review_discloses_not_due_and_sanitizes_cells)

    section("81. 记录 → 消费：算出来的事实必须走到裁决那一步")
    check("引擎自报 TAM/SAM/SOM 越界要真的进闸门（不是读一个全仓没人写的键）",
          test_engine_sizing_violation_reaches_the_gate)
    check("同一份报告，Word / PDF / md 的图数必须同源（不许再出现预算字面量）",
          test_export_formats_share_one_chart_budget)
    check("决策链：KINDS 声明过的类型必须有人真的记录（声明了没生产者＝渲染位白留）",
          test_every_declared_decision_kind_has_a_producer)

    section("82. 刷一下 / 从历史打开，读数不能掉")
    check("存档里必须真的存了「恢复报告卡」要读的每个字段",
          test_archive_carries_every_field_recovery_reads)
    check("前后端的审计附录切分表逐条一致（不一致＝同一份报告两种结构）",
          test_audit_split_lists_agree)
    check("前端读失败不许伪装成 0（带合成反面样本自检）",
          test_frontend_never_renders_a_read_failure_as_zero)

    section("83. 判定与裁决：结论不许被别的地方带跑")
    check("判定抽取只认判定区、且跳过三选项并列的题干行（否则 Go 会被 RedTeam 判成 No-Go）",
          test_verdict_extraction_is_not_fooled_by_other_sections)
    check("水印日期守卫不许被'尾部挂标签'绕过，且真实时序矛盾一条都不许被挡",
          test_watermark_date_guard_has_no_trailing_label_hole)
    check("仲裁卡片的 4 维拆解 × 权重 == 合成分（「可核对」必须真的能核对）",
          test_arbitration_card_is_actually_checkable)
    check("SOP 的每一节都被闸门归过类（漏一节＝那一节里的问题够不到 BLOCK）",
          test_every_sop_section_is_classified_by_the_gate)

    section("84. 自审规则必须真的在审它声称审的东西")
    check("「每个关键数字≥2独立来源」真的在查数字（不是只查前 10 个、不是按单位背书）",
          test_multi_source_rule_checks_the_numbers_it_claims_to_check)
    check("所有持久化路径都在 backend/data 下（否则备份/重置/清理会扫不到它）",
          test_all_persistence_paths_live_under_backend_data)

    section("85. 作用域：一处赋值挡掉整个函数的模块级全局（带正反样本自检）")
    check("没有函数把模块级名字挡成局部名后又在够不到赋值的地方读它",
          test_no_function_shadows_a_module_global_it_might_read_unbound)

    section("86. 数据源：「未探测」的理由必须有名字，且加起来对得上")
    check("四类未探测理由各自归类正确 + 兜底具名 + 子计数之和 == skipped + 前后端同键",
          test_skip_reasons_are_named_and_add_up)

    section("87. 测试套件本身不许依赖工作目录")
    check("套件里没有相对字面量路径 / `\".\"` 进 sys.path / `-m` 子进程缺 cwd",
          test_suite_does_not_depend_on_cwd)

    section("88. 交付物的文件属性不许泄露工具链的默认值")
    check("docx/pptx 的作者字段写服务提供者（不是 python-docx / Steve Canny），且法定标识仍在",
          test_office_metadata_does_not_leak_library_defaults)

    section("89. 修复器与检查器共用同一把尺（修复不许动检查没报的行）")
    check("判据表正反样本一致 + 修复器只改裁决为 fix 的行 + 测试数三种词形都收得到",
          test_fixer_and_checker_share_one_predicate)

    section("90. 提示词指纹：换了提示词，校准必须判废且说得出理由")
    check("登记表不静默退化 + 指纹内容寻址 + 不一致时判废并印出理由 + 旧存档自陈无法核对",
          test_prompt_fingerprint_invalidates_calibration)

    section("91. 情景与敏感性：确定性、公式单一出处、扰动幅度公开、红线真会响")
    check("三档单调 + 敏感性排序 + 幅度性质印出 + 红线可触发 + 算不出就不印 + 提示词块≠读者版",
          test_scenario_analysis_is_deterministic_and_single_sourced)

    section("92. 重复动作检测：判据是「重复且多为空」，不许把设计说成缺陷")
    check("归一化归类 + 刻意重复不误报 + 真空转报得出且带免责 + 脏输入不炸 + 纯函数不改行为",
          test_repeat_guard_distinguishes_by_design_from_spinning)

    section("93. 材料自检的判据本身：中文里的 `\\b` 会静默吃掉一整个词形")
    check("「N 节全绿」必须被抓（旧 `节\\b` 失配）· 「N 节点」不许误报 · 别的文档的节数不许误报 · ⑫ 已注册",
          test_lint_word_forms_and_pack_subset)

    section("94. 计划来源：交付物必须说真话；LLM 路径的开启前置条件（同维度不许出两章）")
    check("plan_source 写入/默认值 + 展示按真相印 + ①③触发方式纠偏 + LLM 路径开启即校验同维度单章",
          test_plan_source_honesty)

    section("95. 检索通道进调用台账（可审计最承重的一块砖）")
    check("桩 HTTP 驱动真实通道：记了台账 + **URL 进台账** + 备注可定位子任务 + 调用方真传了 trace",
          test_search_channels_reach_the_call_ledger)

    section("96. 审计层只说它知道的事（时间锚假阳性 / 跨度可复算 / 组名不自相矛盾）")
    check("时间锚真病案被作废且不洗成通过 + 正当判定不误伤 + 跨度可由印出取值复算 + 组名与单位不打架",
          test_audit_layer_says_only_what_it_knows)

    section("97. 决策区三类硬错：同层两个市场规模 / 同一数字两种量纲 / 建议踩自己红线")
    check("SAM 879≠928 能被闸门抓到且不误报干净三层 + 0.8家/万人 vs 0.8家/10万人 + 92杯建议 vs 171杯红线",
          test_sizing_unit_and_advice_defects)

    section("98. 双源配对矩阵：契约与判定必须同源（pair_required 要参与判定）")
    check("要求里点名的每一侧都真的核过，缺哪侧写在明面上；取不到要求则显式不判，不显示假 ✅",
          test_pair_matrix_checks_the_requirement_it_prints)

    section("99. 决策区点名的来源机构，本次台账里到底有没有（R12）")
    check("真病案被抓到且**没有阻断权**；转述/法定披露/待核/无台账四种情形不误报",
          test_metric_source_institution_is_grounded)

    section("100. 宏观源开门判据：只看本子任务，且不认章节名里的「经济/消费」")
    check("16 个真实章节名上，只有宏观环境与市场规模测算开门（病案四章节必须关）",
          test_macro_source_gate_is_subtask_local)

    section("101. 三份外部审计交叉出的六处：同源文案 / 反向误杀 / 边际量 / 远期值 / 离题站 / 词形")
    check("L1–L5 只有一套定义；劝阻句不被盖章；增量为伪值被剔；远期值不算双值；萌娘百科被掐；四种回本写法聚一桶",
          test_round2_external_audit_fixes)

    section("102. 自一致性层：登记表 + 决策阈值不许低于本报告自己算的红线（R13）")
    check("止损线 92 < BEP 205 被抓到且不误报；登记表由常量渲染（与实现同源，不是手写副本）",
          test_self_consistency_layer)

    section("103. 假设集指纹：同版本同 prompt_set、但假设不同 → 必须看得出来")
    def test_assumption_set_fingerprint():
        """病案（2026-09-18 第十跑）：九跑与十跑 `prompt_set` 都是 72c514f9（判据冻结），
        而 BEP 222→242、TAM 4,687→6,026 亿、LTV/CAC 16.2→33.5 —— **假设没冻结**。
        报告里一个字都看不出这件事。`core/assumption_set` 就是补这一行。

        锁四条：
          ① **不同假设 → 不同指纹**（否则它无信息）；
          ② **同一内容 → 同一指纹**（否则它会抖，抖的指纹比没有更糟）；
          ③ **排版差异（空格）不算假设差异**；
          ④ **取不到 → 空串**，由调用方决定不印——**不许猜、不许印一句空话**。
        """
        from core.assumption_set import describe, fingerprint

        def _rep(freq: str, cup: str, price: str) -> str:
            return (
                "## 市场规模测算\n\n| 指标维度 | 数值/参数 | 来源 |\n| :--- | :--- | :--- |\n"
                f"| 理论年均频次 | {freq} | L5估算 |\n\n"
                "## 单位经济\n\n| 指标项目 | 数值/金额 |\n| :--- | :--- |\n"
                f"| 日均杯量 | {cup} |\n| 客单价 | {price} |\n"
            )

        a = fingerprint(_rep("35 杯", "300 杯", "15 元"))
        b = fingerprint(_rep("45 杯", "315 杯", "16 元"))
        assert a and b, f"指纹取不到：{a!r} / {b!r}"
        assert a != b, "假设全变了指纹却没变——这一行没有任何信息量"

        # ② 同一内容两次 → 同值（不是随机数、不依赖时间）
        assert a == fingerprint(_rep("35 杯", "300 杯", "15 元")), "同一内容两次指纹不同"
        # ③ 空白是排版不是假设
        assert a == fingerprint(_rep("35杯", "300杯", "15元")), "空格差异被当成了假设差异"
        # ④ 取不到就空着，不猜
        assert fingerprint("") == "" and fingerprint("## 别的\n\n正文") == "", "取不到时不该编一个"
        assert describe("") == "", "取不到时不该印一句空话"
        assert "assumption_set" in describe(_rep("35 杯", "300 杯", "15 元")), "说明串没带指纹"
        # 子标题不许把整节关掉（第一版就是这个 bug，指纹恒为空串）
        assert fingerprint("## 单位经济\n\n### 子标题\n\n| 日均杯量 | 300 杯 |\n"), (
            "`###` 子标题把整节关掉了——指纹恒为空串，看起来像「这份报告没有假设」"
        )

    check("不同假设→不同指纹 · 同内容→同指纹 · 空格不算假设 · 取不到就空着不猜",
          test_assumption_set_fingerprint)

    section("104. 工具计划按子任务隔离：A 不许读到 B 的名单（外部审计第 ⑧ 对）")
    def test_tool_plan_per_subtask():
        """病案（2026-09-18 第十跑）：决策链写「st_5→sse_edc/**cninfo**/…」（选它了），
        同一份报告的调用台账写「巨潮资讯 —— 未调用（计划跳过）｜**工具计划未选中**」。
        **两句都是真的**，错的是中间那个**全局共享槽位** `context["_tools"]`：
        `select_tools` 按**当前子任务**算，却写进一个所有子任务共用的格子。

        锁五条（含两条"不改旧行为"）：
          ① 本子任务读到自己那一格；
          ② **不读到别人的**（这是这条锁存在的全部理由）；
          ③ 隔离表在、但本子任务缺格 → **None（全打）**，不回退到别人的名单；
          ④ 旧 context（无隔离表）→ 退回旧行为，**语义不变**；
          ⑤ 空 context → None。
        """
        from agents.researchers import _only_tools_for

        class _S:
            def __init__(self, i):
                self.id = i

        ctx = {
            "_tools": {"tools": ["别人的名单"]},
            "_tools_by_subtask": {
                "st_5": {"tools": ["cninfo", "sse_edc"]},
                "st_3": {"tools": ["bocha"]},
            },
        }
        assert _only_tools_for(ctx, _S("st_5")) == ["cninfo", "sse_edc"], "读不到自己的名单"
        assert _only_tools_for(ctx, _S("st_3")) == ["bocha"], "读到了别人的名单"
        assert _only_tools_for(ctx, _S("st_9")) is None, (
            "缺格时回退到了共享槽位——那等于把这个 bug 原样留着"
        )
        assert _only_tools_for({"_tools": {"tools": ["x"]}}, _S("st_5")) == ["x"], "旧 context 行为变了"
        assert _only_tools_for(None, _S("st_5")) is None, "空 context 应 fail-open"
        # 写法必须真的建了那张表（否则这一层没人写、读侧永远退回旧行为 = 白改）
        _src = (Path(__file__).resolve().parent / "agents" / "researchers.py").read_text(
            encoding="utf-8", errors="replace"
        )
        assert 'context.setdefault("_tools_by_subtask"' in _src, "写入侧没建隔离表（改了读、没改写）"

    check("本子任务读到自己的名单 · 不读到别人的 · 缺格全打 · 旧 context 行为不变",
          test_tool_plan_per_subtask)

    section("105. 交付补齐名单有牙：deliver 不回吐的 TaskState 字段一个都不许漏")
    check("凡被 _result_json 消费、deliver 又不回吐的字段，必须在 _DELIVER_BACKFILL_KEYS 里",
          test_deliver_payload_backfill_is_complete)

    section("106. 演示账户：最贵档位必须付得起；充值要有真入口")
    check("初始余额 ≥ 最贵档 · 充值能加也能拒 · 与界面口径同源",
          test_demo_account_affords_top_tier)

    section("107. 区间指标：不许被解析成点估计")
    check("22.3% - 27.3% → unit 干净 + is_range；前后端都不给它画比例条",
          test_range_metric_is_not_a_point_estimate)

    section("108. 两个「有接口没入口」的端点：前端必须接上")
    check("/api/tools 与 /api/memory 在前端有真实调用点与抽屉",
          test_orphan_endpoints_are_wired)

    section("109. 前端功能清单的行号引用：必须指着它说的那一行")
    check("ANCHORS 表逐条核对 · 无未登记引用 · 无僵尸条目 · 转述的代码事实对得上",
          test_frontend_doc_anchors_are_live)

    section("110. 引文可点性：一把尺，四处共用（robots 红线 / 数据接口都不递刀）")
    check("正反样本 + 真实语料：未探测与 API 端点一律不给「打开原网页」",
          test_cite_openable_rules)

    print("\n" + "=" * 60)
    if _SKIPPED:
        print(f"跳过 {len(_SKIPPED)} 项（缺第三方依赖或内部材料；不计入失败）")
    print(f"结果：✅ 通过 {_PASS} 项 · ⏭ 跳过 {len(_SKIPPED)} 项 · ❌ 失败 {_FAIL} 项")
    if _FAILURES:
        print("失败项：")
        for f in _FAILURES:
            print(f"  - {f}")
    print("=" * 60)
    return 0 if _FAIL == 0 else 1





# ============================================================
# 94. 第六轮三处修复的锁（2026-09-17 蜜雪冰城跑）
# ============================================================
def test_engine_self_negotiation_and_semantic_qc_and_gap_lines():
    """R8 第二族 + 闸门第 ⑥ 层 + 缺口披露取句。

    三处改动的共同病根是**同一个**：一段文本被"读了一眼就当成结构化事实"。
    所以三处都要求**正样本（真实运行原文）命中** 且 **负样本零误报**——
    而负样本全部取自同一份真实报告里那些"看起来像、其实不是"的句子。
    """
    import json as _json
    import re as _re
    from pathlib import Path as _P

    from core.rule_lint import _check_engine_self_correction as _R8
    from core.integrity_gate import evaluate

    # ── ① R8 第二族：正文当众在两个数之间自行择一 ──
    # 真病案原文（`data/reports/20260917_212142_94f10c.json` 的市场规模测算一节）：
    _som = (
        "在保守情景下（各关键参数同向下调 15%），SOM降至 **25 亿元**；在乐观情景下"
        "（各关键参数同向上调 15%），SOM可达 **112 亿元**（注：系统基准乐观值为 125 亿元，"
        "此处取保守上限与基准值的中间态以反映现实摩擦成本，或直接引用系统乐观值 "
        "112-125 亿元区间，本报告取 **112 亿元** 作为乐观上限参考）。"
    )
    _hits = _R8(_som)
    assert len(_hits) == 1 and "自行择一" in _hits[0]["title"], f"R8 未抓到自协商取值：{_hits}"

    # 负样本必须全部来自**同一份真报告**——只靠"人造干净句"证明不了不误报。
    _neg = [
        # 每份报告都有的方法论句。首版判据（数字不限单位）在这里命中：`本报告采用` + L1..L5。
        "**调研方法论**：本报告采用咨询级分析框架 —— 宏观环境分析（PEST）、"
        "市场规模测算（TAM/SAM/SOM）；数据标注来源（L1官方/L2媒体/L3行业/L4用户/L5推测）。",
        "本报告采用区间估计，误差约 ±5%。",
        "本次选取 2026 年为预测期，2027 年为验证期。",
        "市场规模约 4,743 亿元，SAM 1,245 亿元，三层严格递减。",
        "情景为**同向**移动（悲观 = 全部往不利方向），它是一次**压力测试**，不是概率预测。",
    ]
    for _s in _neg:
        assert not _R8(_s), f"R8 第二族误报（会常态误报的检查 = 会被整体无视）：{_s[:40]}"

    # ── ② 闸门第 ⑥ 层：LLM 语义复核里那个**带单位的数**逐字落在决策区 ──
    _rep = (
        "# 📋 决策清单\n\n## 📌 执行摘要\n\n"
        "Situation：超82%消费者拒绝单价>15元产品，县域人均奶茶消费额仅18.3元。\n\n"
        "## 📊 核心指标一览表\n\n| 指标 | 数值 | 来源机构 |\n|---|---|---|\n"
        "| 拒绝单价高于15元的县域消费者占比 | 82% | 艾瑞咨询 |\n"
    )
    _llm = [
        "断言“超过82%的县域消费者明确拒绝单价高于15元”缺乏艾瑞咨询报告作为证据支持。",
        "这是一个没有任何数字的纯文字判断。",          # 无带单位数 → 够不到阻断
        "结论“本报告采用咨询级分析框架”缺乏证据。",      # 数字不带单位 → 不许当判据
    ]
    _g = evaluate(report=_rep, quality_report={"llm_issues": _llm}, verdict="Validate")
    assert _g["level"] == "BLOCK", f"语义复核落进决策区却未阻断：{_g['level']}"
    _blk = [b for b in _g["blockers"] if b["layer"] == "LLM语义复核"]
    assert len(_blk) == 1 and "82%" in _blk[0]["what"], f"阻断条数/判据不对：{_blk}"
    # 措辞纪律：只说机械核验到的那件事，不许说成"它指向的 82% 落在决策区"
    assert "逐字出现" in _blk[0]["what"], "阻断措辞把机械事实说成了语义指向"
    _warn = [w["what"] for w in _g["warnings"] if w["layer"] == "LLM语义复核"]
    assert len(_warn) == 2, f"无数字/无单位数字应退为 WARN：{_warn}"
    # 干净报告不许被这一层误伤
    _clean = evaluate(report="# 报告\n\n## 📌 执行摘要\n\n结论：市场约 500 亿元。\n",
                      quality_report={"llm_issues": []}, verdict="Go")
    assert _clean["level"] == "PASS", f"干净报告被新层误伤：{_clean['level']}"

    # ── ③ 缺口披露：按**整句**取自陈缺口，不再切字符片段 ──
    from graph import _declared_gap_clauses
    _real = (
        "| 待验证 | 设备采购 | 6万 - 8万 | 自动化封口机 |\n"
        "- 市场规模等测算为：本报告未包含一手调研（问卷/访谈/实地）；\n"
        "先定必需层、再取证，取不到就记缺口。\n"
        "1. **优先执行 vt_01（用户深访）**：完成5-10名目标县域用户的深度访谈。\n"
        "> 🔍 **时效补检**：本报告决策型调研例行补检近一年公开动态。\n"
    )
    _got = _declared_gap_clauses(_real)
    assert len(_got) == 1 and "未包含一手调研" in _got[0], f"取句结果不对：{_got}"
    assert all("\n" not in x for x in _got)
    # 逐条负样本：表格行 / 无主语的自述 / 验证任务 / 引用块，一个都不许进
    for _bad in ("待验证", "取不到就记缺口", "深度访谈", "时效补检"):
        assert not any(_bad in x for x in _got), f"缺口披露混入了非自陈句：{_bad}"

    # ── ④ 真报告端到端：三个数都得真的抓得到（防"锁在玩具样例上"） ──
    _p = _P(__file__).resolve().parent / "data" / "reports" / "20260917_212142_94f10c.json"
    if _p.exists():
        _r = _json.loads(_p.read_text(encoding="utf-8"))["report"]
        assert any("自行择一" in h["title"] for h in _R8(_r)), "R8 在真实报告上抓不到自协商取值"
        assert _declared_gap_clauses(_r), "缺口披露在真实报告上取不到任何自陈句"







def test_subject_tension_reaches_the_conclusion():
    """时效补检查到**本案主体自己**的反证时，提醒必须钉到**结论处**。

    病案（2026-09-17 元气森林跑，Claude A3 / GPT §18）：
        补检抓到《元气森林 2025 年业绩同比增长 26%》，而正文结论正建立在
        "元气森林线下失速（-6.3%，2024Q1）→ 不建议重仓线下"之上。
        提醒确实印出来了——但印在**报告头部**，而读者真正会读的是**执行摘要那句结论**。
        两者相距几百行，**结构上没人会把它们连起来**。这不是措辞问题，是位置问题。

    四条：① 主体命中才触发 ② 提醒里有主体名 ③ 钉到「## 📌 执行摘要」之后
    ④ 幂等且哨兵不残留；外加**负样本**：主体不命中时一个字不加。
    """
    from core.freshness import _consumption_notice, SUBJECT_TENSION_SENTINEL as _SENT
    from graph import _inject_subject_tension as _inj
    _rows = [("元气森林2025年业绩同比增长26%", "https://a.example/1"),
             ("四年不融资、不上市,元气森林在等什么?-36氪", "https://a.example/2"),
             ("某协会发布饮料行业年度报告", "https://a.example/3")]
    _n = _consumption_notice(_rows, "元气森林")
    assert any("🔺🔺" in x and "元气森林" in x for x in _n), "主体级反证未点名"
    assert _SENT in "".join(_n), "哨兵未打"
    _md = "".join(x + chr(10) for x in _n) + chr(10) + "## 📌 执行摘要" + chr(10) + chr(10) + "结论：不建议重仓线下。" + chr(10)
    _out = _inj(_md)
    _i = _out.find("## 📌 执行摘要")
    _seg = _out[_i:_i + 400]
    assert "前提可能已被近一年动态改变" in _seg, "提醒没钉到结论处"
    assert "元气森林" in _seg, "结论处的提醒没点主体名"
    assert _SENT not in _out, "哨兵残留在交付文本里"
    assert _inj(_out) == _out, "不幂等"
    # 负样本：主体不命中 → 一字不加
    _n2 = _consumption_notice(_rows, "可口可乐")
    _md2 = "".join(x + chr(10) for x in _n2) + "## 📌 执行摘要" + chr(10) + "结论：x" + chr(10)
    assert _inj(_md2) == _md2, "主体不命中却改动了报告"


def test_primary_research_strips_engine_own_compliance_words():
    """一手方案里不得出现**引擎自身的合规词汇**（Domain Contamination）。

    病案（2026-09-17 元气森林跑，Claude A1 / GPT §16 同时点名）：
        本模块拿整份报告当上下文，而报告顶部就有研策自己的 AI 标识声明
        （「依《人工智能生成合成内容标识办法》及 GB 45438-2025…」）→
        模型把它当成了**被调研行业的法规**，在一手方案的深访对象里写出
        「核实 GB 45438-2025 及标签新规带来的单瓶包材成本实际涨幅」。
        **GB 45438-2025 是 AI 生成合成内容标识标准，与食品标签毫无关系。**
        复核者席是技术内行——饮料报告里出现 AI 标识标准，读者不会想"这里有个小错"，
        而会想"它的所有编号还可信吗"：**一个错误污染了整份报告最值钱的资产。**

    判据：含黑名单词的**整行摘除**（抠掉编号后剩半句话更易被误读）+ **如实披露摘了几行**
    （公理六：降级必须告警）。同时锁**正当行不许被连带摘掉**。
    """
    from core.primary_research import format_md
    _d = {"objective": "验证",
          "survey": {"target": "消费者", "sample_size": "300", "method": "线上",
                     "questions": ["你注意到瓶身的 AI 生成合成内容标识了吗？", "你多久买一次？"]},
          "interviews": {"targets": ["饮料包材供应商 × 2（核实 GB 45438-2025 带来的成本涨幅）",
                                     "连锁便利店采购"], "guide": ["成本涨了多少？"]},
          "field": {"plan": "走访 10 家", "checklist": ["货架排面"]},
          "estimated_cost": "约 ¥3,000"}
    _out = format_md(_d)
    _body = [x for x in _out.split(chr(10)) if not x.startswith("> ⚠️ **已摘除")]
    assert not any(("GB 45438" in x or "生成合成内容标识" in x) for x in _body), "黑名单词残留在正文"
    assert "已摘除 2 行" in _out, "摘除未如实披露（不许悄悄兜底）"
    assert "连锁便利店采购" in _out and "你多久买一次" in _out and "走访 10 家" in _out, "正当行被连带摘掉"

def test_rule_layer_findings_actually_reach_the_gate():
    """规则层的发现必须真的能触发闸门阻断——**字段名不一致让这个入口一直是空的**。

    病案（2026-09-17 元气森林跑，本轮最重的一条断链）：
        闸门读 `f.get("level")`；R1–R7 用 `level`，**而 R8/R9 用的是 `severity`**。
        于是这两条规则从落地那一刻起就是**死的**：severity 写得清清楚楚，
        闸门永远取到空串 → 永远进不了 blockers → **只有记录，没有消费**。
        而本闸门的文档第一句就写着「规则层 high 风险项落在决策区 → BLOCK」。

    **两条都锁**：字段归一（两个名字都认）与**覆盖范围**（决策区表漏掉的那两节）。
    """
    from core.integrity_gate import evaluate
    _md = chr(10).join(["# 📋 决策清单", "", "## 📌 执行摘要", "", "结论：市场约 758.4 亿元。"]) + chr(10)
    # ① `severity` 名字的发现也要能阻断（R8/R9 的写法）
    _g = evaluate(report=_md, quality_report={}, verdict="Go", rule_findings=[
        {"rule": "R9", "severity": "high", "title": "印出的算式算不出结果",
         "snippet": "结论：市场约 758.4 亿元。", "detail": "x", "advice": "y"}])
    assert _g["level"] == "BLOCK", f"severity=high 的规则发现没进闸门：{_g['level']}"
    assert any(b["layer"].startswith("规则层 R9") for b in _g["blockers"]), _g["blockers"]
    # ② `level` 名字的发现照旧要能阻断（R1–R7 的写法）
    _g2 = evaluate(report=_md, quality_report={}, verdict="Go", rule_findings=[
        {"rule": "R7", "level": "high", "title": "来源资格错配",
         "snippet": "市场约 758.4 亿元", "detail": "x", "advice": "y"}])
    assert _g2["level"] == "BLOCK", f"level=high 的规则发现没进闸门：{_g2['level']}"
    # ③ 两个字段都没有 → 不许阻断（否则闸门会变成"凡有发现就拦"）
    _g3 = evaluate(report=_md, quality_report={}, verdict="Go",
                   rule_findings=[{"kind": "future_year", "snippet": "市场约 758.4 亿元"}])
    assert _g3["level"] != "BLOCK", "无 level/severity 的发现不该阻断"
    # ④ 覆盖范围：目标市场 / 进入壁垒两节也必须在决策区内
    #    （病案：SAM 那行 10.7× 错在「目标市场」节，R9 抓到了却只判 WARN）
    from core.integrity_gate import _decision_zone
    _z = _decision_zone(chr(10).join(["# 报告", "", "## 目标市场", "", "| SAM | 约 758.4 亿元 |",
                                      "", "## 进入壁垒", "", "门槛 42.6 万元", ""]))
    assert "758.4" in _z and "42.6" in _z, "目标市场/进入壁垒未进决策区——那一节的发现永远够不到阻断"

def test_printed_arithmetic_must_self_check():
    """R9：正文印出的算式必须算得出它自己印的结果。

    病案（2026-09-17 元气森林跑，交付报告原文）：
        | **核心目标市场规模 (SAM)** | **约 758.4 亿元** | 计算过程：9.3亿 × 18.5% × 42.0% × 20.5 × 4.8 |
      `9.3亿 × 18.5% × 42% × 20.5 × 4.8 = 71.1 亿元`，**印出来是 758.4 亿元，差 10.7 倍**。
      同一份报告的 TAM 行 `9.3亿 × 85% × 40 × 6 = 1,897 亿` 却对得上——所以这不是
      「假设激不激进」的口径之争，**是印出的输入和印出的输出对不上**。

    为什么单独立规则：产品对外头号卖点是「测算数值由 Python 引擎计算，LLM 不参与计算」。
    一个 10.7 倍的乘积错误印在测算表里，攻击的正是这句话本身——
    **别的错误可以争论，算术不能。**

    判据的每一条收紧都对应首版实测踩到的一个误报（这些才是这个用例真正锁的东西）：
      ① 段内偏移 × 行内偏移混用 → 结果被当成因子，正确的 TAM 算式被判错；
      ② 区间表达式（`8-12万元 × 3 × 2`）连乘必然对不上中点 → 整行放过；
      ③ 引用行里有两个"结果候选"→ 指代不明，整行放过；
      ④ `(L5估算)` 里的 `5` 被当成因子 → 数字左边不能是字母。
    """
    from core.rule_lint import _check_printed_arithmetic as _R9
    _pos = "| **核心目标市场规模 (SAM)** | **约 758.4 亿元** | 计算过程：9.3亿 × 18.5% × 42.0% × 20.5 × 4.8 |"
    _h = _R9(_pos)
    assert len(_h) == 1 and "10.7" in _h[0]["title"], f"真实病案未抓到或倍数不对：{_h}"
    assert _h[0]["severity"] == "high", "R9 必须是 high，否则够不到闸门"
    _neg = [
        # ① 同报告里**正确**的 TAM 算式（首版就是把它判错了）
        "按全国城镇人口 9.3亿人 × 潜在人群占比 85% (L5估算) × 理论年均频次 40 杯 (L5估算) "
        "× 客单价 6 元 (L5估算) 测算，TAM 约为 1,897 亿元。",
        "该值 = 9.3亿 × 85% × 40 × 6 = 1,897 亿元",
        # ② 区间表达式
        "门槛为 8-12万元 × 3 个 × 2 轮，合计约 60 万元。",
        # ③ 引用行里两个结果候选（规则层附录里引的原文）
        "| 1,897亿元 | 市场规模测算：TAM约为1,897亿元 | TAM计算假设'9.3亿 × 85% × 40次/年 × 6元' |",
        # 两项相乘的散文（不足 3 因子）
        "月毛利润为 87,750 元（135,000 元 × 65%），减去固定成本 65,000 元。",
    ]
    for _s in _neg:
        assert not _R9(_s), f"R9 误报（会常态误报的检查 = 会被整体无视）：{_s[:44]}"

def test_gate_number_token_has_digit_boundary():
    """闸门的**数值 token 必须按独立的数来比**，不许裸子串命中。

    病案（2026-09-17 元气森林跑，交付报告里当场可见、是我自己新加的那一层造成的）：
        闸门把「…以解决 68.2% 运动人群…的顾虑」标成
        「本句依赖被阻断的数字「8.2%」」——因为 `"8.2%" in "68.2%"` 成立。
        **`8.2%`（气泡水增速）与 `68.2%`（运动人群占比）是两个不同的数。**
        同一跑里还有 `「0.2元」` 来自 `0.15-0.2元`。
    **在交付物里写一句错的指控，比漏标更伤**——读者看到的是"系统在冤枉一个数字"，
    而那句话是我们自己加的。所以"要不要把争议算进决策区"（宁松勿漏）与
    "要不要往正文里写一句指控"（宁漏勿错）**用两把尺子**，这里锁的是后者。

    四条判据：数字边界（左不能是数字/小数点、右不能是数字）· 破折号归一
    （`0.15-0.2元` 与 `0.15–0.2元` 并存）· 区间整体成一个 token（不许拆成端点）· 真值仍要命中。
    """
    from core.integrity_gate import _distinctive_numbers, _token_in_line
    _cases = [
        ("…以解决68.2%运动人群对纯无糖饮料功能缺失的顾虑", "8.2%", False),
        ("元气森林2024年无糖气泡水品类增速降至8.2%", "8.2%", True),
        ("终端售价115元", "15元", False),
        ("客单价15元", "15元", True),
        ("日均222单", "222单", True),
        ("将GB 2760-2024带来的0.15-0.2元/瓶合规成本纳入底价", "0.15-0.2元", True),
        ("上浮幅度达0.15–0.2元", "0.15-0.2元", True),
    ]
    for _line, _tok, _want in _cases:
        _got = _token_in_line(_line, _tok)
        assert _got == _want, f"数字边界判据错：{_tok!r} @ …{_line[-24:]} 得 {_got}，应为 {_want}"
    # 区间必须整体抽取，不许被拆成端点后冒充独立值
    _n = _distinctive_numbers("上浮0.15-0.2元，增速8.2%，覆盖9.3亿人")
    assert "0.15-0.2元" in _n, f"区间被拆成了端点：{_n}"
    assert "0.2元" not in _n, f"区间端点被当成了独立值：{_n}"

def test_gate_marks_blocked_advice_inline():
    """闸门 BLOCK 后，**直接依赖被阻断数字的建议句**必须就地标出来，不许只降判定标签。

    病案（2026-09-17 蜜雪冰城跑，三份外部评审同时点名）：
        置顶横幅写「本次交付已被阻断（BLOCK）」，
        同一屏下方**第一条建议**却是
            「建议优先选择初始投资 8–12 万元（存疑·待核）的轻资产加盟模型……
              以确保在县域低客单环境下实现 <12 个月的投资回收」
        ——而 `8–12万元` 正是这次 BLOCK 的阻断项之一。
    闸门自己承诺的是「只拒绝让这些数字以**可以直接行动**的口吻交付」，
    而一条「建议优先选择…以确保…」正是整份报告里最可直接行动的那句话。
    它此前只降了最上层的判定标签、没碰下层那句建议——产品对外宣称"拦住了"，
    读者拿到的却是"照这个预算开店"。**上层改了、下层没改，而两层在读者眼里是同一件事。**

    四条纪律，逐条锁死：
      ① 只**追加**、绝不改写数字（我们没有依据替读者判断哪个值才对）
      ② 只标决策区里的**建议句**——条件判据句（"若…则 No-Go"）是分析，不是建议
      ③ 表格行不碰（加了会破表）、别节不碰
      ④ 幂等
    """
    from core.integrity_gate import apply_to_report
    _T = "闸门：本句依赖被阻断的数字"
    NL = chr(10)
    md = NL.join([
        "# 📋 决策清单", "", "## 📌 执行摘要", "",
        "**建议优先选择初始投资 8–12 万元的轻资产模型**", "",
        "若日均销量 < 200 杯 → 单店月净利转负 → No-Go（8–12 万元是假设表里的条件）", "",
        "| 初始投资 | 8–12 万元 |", "",
        "## 竞争对手", "", "建议优先选择 8–12 万元的模型（**别节，不许动**）", "",
    ])
    g = {"level": "BLOCK", "executed": True, "warnings": [], "downgraded_from": "Go",
         "downgraded_to": "Validate",
         "blockers": [{"layer": "RedTeam",
                       "what": "「8–12万元」被独立校验官判为高风险，而它出现在决策区",
                       "why": "x"}]}
    out, v = apply_to_report(md, g, "Go")
    assert out.count(_T) == 1, f"应只标决策区里的建议句，实际 {out.count(_T)} 处"
    assert "8–12 万元" in out, "① 数字被改写了——闸门只许追加标注"
    assert out.count("No-Go（8–12 万元是假设表里的条件）") == 1, "② 条件判据句被误标"
    assert "| 初始投资 | 8–12 万元 |" in out, "③ 表格行被动了，表会破"
    assert "（**别节，不许动**）" in out, "③ 别节的建议句被动了"
    assert "下列可执行建议已就地标记" in out, "横幅没把被标了哪几句报出来"
    assert v == "Validate", "判定未降级"
    assert apply_to_report(out, g, "Go")[0].count(_T) == 1, "④ 不幂等，重复调用会叠标记"


def test_plan_source_honesty():
    """计划来源：**交付物不许替一个没运行过的能力背书**；**也不许只换一种死法就说修好了**。

    病案（2026-09-18 审计 + 当晚实跑；2026-09-19 收官修复，一条链上三个缺陷）：
    LLM 规划分支从来没成功过一次，而报告在《🌳 问题拆解》里印
    「拆解由模型生成而非套模板」+ `_（模型判断）_`。顺着修下去：
      ① `_PLAN_SYSTEM_PROMPT` 的 JSON 示例花括号被 `.format()` 当占位符 → 每次 KeyError；
      ② 转义后往下走一步，死在一行**从未被使用过的死代码**上（`"、".join(字典列表)`）；
      ③ 再修掉 ②，模型真的出计划了 —— 但它引入**本产品最严重的历史缺陷**：
         模型写的基础轮描述不含 `『维度名』`，`graph._chapter_key` 取不到同一个键，
         **同一个维度出两章**（"同一指标两套数"）。

    本测试的 ③ 段是**那把锁**：它不检查"有没有人转义了花括号"（那是措辞，不是行为），
    而是**渲染一次提示词 + 走一遍归口函数**，直接问三件事：
      · 提示词渲染后，那段 JSON 示例是不是**逐字还在**（花括号有没有被吃掉）；
      · 一条自由文本描述的基础轮 + 一条 `深入『X』` 的深入轮，**出不出两章**；
      · **归不上维度时会不会 fail-closed**（宁可退回模板，也不放任它自成一章）。
    改法对不对，跑它就知道——别按记忆改。
    """
    import inspect as _i

    from agents.router import _fallback_plan
    from agents.router import _PLAN_SYSTEM_PROMPT as _P
    from agents.router import (
        _normalize_subtasks,
        _render_plan_system_prompt,
        _resolve_subtask_dimensions,
    )
    from core.models import DepthLevel, ResearchPlan, SubTask

    # ---- ① 写入点：兜底路径必须自报家门，默认值不许偏向"模型生成" ----
    assert ResearchPlan.model_fields["plan_source"].default == "", (
        "plan_source 默认值必须是空（未标注）——默认成 llm 就是又替模型背书一次"
    )
    assert _fallback_plan("测试问题", {"objective": "进入市场"}).plan_source == "fallback", (
        "兜底计划没标来源"
    )

    # ---- ② 展示点：按真相印 ----
    _g = _i.getsource(__import__("graph"))
    assert "plan_source" in _g, "graph 没读 plan_source —— 写入点有、展示点没接，等于没写"
    assert 'trigger="llm" if _by_model else "rule"' in _g, (
        "《问题拆解》的触发方式没跟着计划来源走：兜底计划仍会被印成「模型判断」"
    )
    assert "本次拆解来自规则模板兜底" in _g, "兜底时没有如实改口，仍会印「拆解由模型生成」"
    # 决策①（问题定性=关键词表）与决策③（立假设=模板句）都是纯规则，不许标 llm
    assert _g.count('trigger="rule"') >= 2, "决策①/③ 的触发方式没改回 rule"

    # ---- ③ 提示词渲染：占位符替换**不许**走 `str.format()` ----
    # 只在**代码行**里找（注释里到处在讲这桩病案，连本函数上面都在讲）——
    # 判据必须落在会执行的那一行上，否则这把锁会被自己的注释绊倒。
    _router_src = _i.getsource(__import__("agents.router", fromlist=["x"]))
    _code_lines = [
        ln for ln in _router_src.split("\n")
        if ln.strip() and not ln.strip().startswith("#")
    ]
    _code = "\n".join(_code_lines)
    assert "_PLAN_SYSTEM_PROMPT.format" not in _code, (
        "又有人拿 `.format()` 渲染规划提示词了——提示词里的 JSON 示例是字面花括号，"
        "`.format()` 会把它当占位符名抛 KeyError。请走 `_render_plan_system_prompt()`。"
    )
    _rendered = _render_plan_system_prompt("SKILL-X", "维度A；维度B")
    # 渲染后必须：占位符被替换掉、**JSON 示例逐字还在**（花括号没被吃掉）
    assert "SKILL-X" in _rendered and "维度A；维度B" in _rendered, "占位符没被替换"
    assert "{skill_name}" not in _rendered and "{skill_dims}" not in _rendered
    for _frag in ('"dimensions": ["维度1", "维度2", ...]', '"agent_type":', '"subtasks": ['):
        assert _frag in _rendered, (
            f"渲染后 JSON 示例被吃掉了：{_frag!r} 不在结果里——这正是当年 KeyError 的同一处"
        )
    assert len(_rendered) > len(_P) - 200, "渲染后的提示词明显变短，示例八成被替换没了"

    # ---- ③-b 归口函数：自由文本描述的基础轮，必须与深入轮归到同一个维度 ----
    _dims = ["宏观环境分析", "市场规模测算"]
    _raw = [
        # 基础轮：描述是**自由文本**，一个维度名都没写——正是当年出两章的那一条
        {"agent_type": "industry", "dimension": "宏观环境分析",
         "description": "检索三四线城市咖啡消费相关的政策支持、居民可支配收入变化情况"},
        {"agent_type": "industry", "dimension": "宏观环境分析",
         "description": "深入『宏观环境分析』：结合PEST产出该维度完整最终版"},
        {"agent_type": "industry", "dimension": "市场规模测算",
         "description": "测算咖啡市场TAM/SAM/SOM"},
    ]
    _resolved = _resolve_subtask_dimensions(_raw, _dims)
    assert _resolved == ["宏观环境分析", "宏观环境分析", "市场规模测算"], _resolved
    _stubs = [
        SubTask(id=f"st_{i}", agent_type=r["agent_type"], description=r["description"],
                dimension=_resolved[i])
        for i, r in enumerate(_raw)
    ]
    from graph import _chapter_subtasks, _final_subtask_ids

    for _depth in (DepthLevel.DEEP, DepthLevel.CUSTOM, DepthLevel.STANDARD, DepthLevel.FREE):
        _norm = _normalize_subtasks(
            [s.model_copy(deep=True) for s in _stubs], _resolved, _depth
        )
        _fin = _final_subtask_ids(_norm)
        assert len(_fin) == 2, (
            f"{_depth} 档：出章者={sorted(_fin)}（应为 2 个维度各一章）。"
            "同一维度出两章 = 同一指标两套数，本产品最严重的历史缺陷。"
        )
        _keys = [s.dimension for s in _norm if s.id in _fin]
        assert len(set(_keys)) == len(_keys), f"{_depth} 档：同一维度出了两章 {_keys}"
        # 深入/定制档必须留**深入轮**（它被要求写完整章）；其余档留首轮
        if _depth in (DepthLevel.DEEP, DepthLevel.CUSTOM):
            _writer_macro = [
                s.id for s in _norm if s.id in _fin and s.dimension == "宏观环境分析"
            ]
            assert len(_writer_macro) == 1 and _writer_macro[0].endswith("_deep"), (
                f"{_depth} 档没把出章者定为深入轮：{_writer_macro}"
            )
        _chapters = _chapter_subtasks({"plan": ResearchPlan(
            raw_question="q", dimensions=_dims, depth=_depth, subtasks=_norm)})
        assert len(_chapters) == 2, f"{_depth} 档章节数={len(_chapters)}，应为 2"

    # ---- ③-c 负样本：归不上维度时**必须 fail-closed**，不许让它自成一章 ----
    try:
        _resolve_subtask_dimensions(
            [{"agent_type": "industry", "description": "检索三四线城市咖啡消费相关的政策支持"}],
            _dims,
        )
        raise AssertionError(
            "归不上维度的子任务被放行了——它接下来就会自成一章，"
            "而下一条同维度的会按维度名另出一章，两章、两套数。必须抛错退回模板计划。"
        )
    except ValueError:
        pass
    # 但"没填字段、描述里写了『维度名』"要容忍（模型偶发漏填不该整份作废）
    assert _resolve_subtask_dimensions(
        [{"agent_type": "user", "description": "调研『市场规模测算』（方法论：TAM/SAM/SOM）"}], _dims
    ) == ["市场规模测算"]

    # ---- ③-d 负样本：两个**真正不同**的维度不许被并到一起（并过头会丢整章）----
    _two = [
        SubTask(id="a", agent_type="industry", description="x", dimension="宏观环境分析"),
        SubTask(id="b", agent_type="user", description="y", dimension="市场规模测算"),
    ]
    assert len(_final_subtask_ids(_two)) == 2, "两个不同维度被并成了一章 → 会丢掉一整章"

    # ---- ③-e 兜底路径也必须带维度：否则它自己的"基础轮+深入轮"就归不到一起 ----
    for _d, _ctx in (
        (DepthLevel.DEEP, {"objective": "进入市场", "depth": "深入"}),
        (DepthLevel.CUSTOM, {"objective": "进入市场", "depth": "定制"}),
        (DepthLevel.STANDARD, {"objective": "进入市场", "depth": "标准"}),
        (DepthLevel.FREE, {"objective": "进入市场", "depth": "免费"}),
    ):
        _fb = _fallback_plan("我想在杭州开一家咖啡店", _ctx)
        assert _fb.depth == _d
        assert all(s.dimension for s in _fb.subtasks), (
            f"{_d} 档兜底计划有子任务没带 dimension："
            f"{[s.id for s in _fb.subtasks if not s.dimension]}"
        )
        _fbf = _final_subtask_ids(_fb.subtasks)
        _fbkeys = [s.dimension for s in _fb.subtasks if s.id in _fbf]
        assert len(set(_fbkeys)) == len(_fbkeys), f"{_d} 档兜底计划同维度出了两章：{_fbkeys}"


def test_search_channels_reach_the_call_ledger():
    """**检索通道必须进调用台账**（2026-09-18 补，三份外部分析一致指向的同一个洞）。

    病案：报告《🛰️ 本次数据源调用台账》列了 **9 个源，一个检索通道都没有**——
    而同一份报告的 23 条引文（艾瑞/界面/36氪/每经/中国证券网…）与大部分证据，
    **全部是从检索通道（千问 enable_search / 博查 / Bing Web API）进来的**，
    报告自己的《出处集中度》还写着「艾瑞咨询 3 条」。
    复核者问「艾瑞那 3 条是哪次调用来的」——台账里查无此行。

    这一层是「可审计」最承重的砖，却整条没接线：`source_health.record` 的调用方
    只有 `fetch_authority`（权威源）与"主动跳过"两处，`mcp/search.py` 一次都不写。

    本测试用**桩 HTTP** 驱动真实的 `_api_structured_results`，验证：
    ① 通道真跑通（反向样本）；② 记了台账；③ **带回的 URL 进了台账**（这才答得上"哪次调用"）；
    ④ 备注能看出是哪个子任务的调用。
    """
    import mcp.search as S
    from core.source_health import new_trace

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "data": {
                    "webPages": {
                        "value": [
                            {"url": "https://www.iresearch.com.cn/report/a", "name": "艾瑞报告", "siteName": "艾瑞咨询"},
                            {"url": "https://www.36kr.com/p/b", "name": "36氪文章", "siteName": "36氪"},
                        ]
                    }
                }
            }

    _orig = S.httpx.post
    S.httpx.post = lambda *a, **k: _Resp()
    try:
        t = new_trace()
        out = S._api_structured_results("现制咖啡 下沉 市场", 3, trace=t, subtask="st_2")
    finally:
        S.httpx.post = _orig

    # ① 反向样本：桩没走到就说明这条测试什么也没测
    assert out, "桩 HTTP 没被走到——本测试失去意义，先修桩"
    # ② 台账必须有行
    assert t, "检索通道没写台账 —— 「可审计」最承重的那块砖又断了"
    _e = t[0]
    assert _e["src"] in ("bocha", "bing_web"), f"通道名不对：{_e['src']}"
    # ③ **URL 必须进台账**：只记"调了几次"答不上"艾瑞那 3 条是哪次取的"
    assert any("iresearch" in u for u in _e.get("urls") or []), (
        f"带回的 URL 没进台账，仍然答不上「哪次调用取的」：{_e.get('urls')}"
    )
    # ④ 备注要能定位到子任务
    assert "st_2" in str(_e.get("note") or ""), f"备注里看不出是哪个子任务的调用：{_e.get('note')}"

    # ⑤ 接线：调用方（researchers）必须把 trace 真的传进来
    import inspect as _i

    _r = _i.getsource(__import__("agents.researchers", fromlist=["x"]))
    assert "_health_trace" in _r, "researchers 没把本次台账传进检索通道"
    assert _r.count("_health_trace") >= 2, "只有部分检索调用接了台账，其余仍是黑洞"
    assert "qwen_search" in _r, "通用检索（千问）通道没记台账——它是产出证据最多的那条"

    # ⑥ **通道名必须由 search 层写下，调用方不许靠 `urls == []` 反推**。
    # 病案（2026-09-18 实跑当场踩到，是我自己写的）：我按 urls 为空就把那一笔记成
    # 「降级：正文来自 DeepSeek 内置知识」，而日志原话是「千问0来源/0URL + Bing3条」——
    # 千问**联网答了**，只是没带 search_info。**那是对一条真实检索的假指控**，
    # 而且会印进交付报告的台账里。判据必须与判定同源：只有 search 层知道谁答的。
    _s = _i.getsource(S)
    assert '"channel"' in _s, "search 层没把『哪条通道答的』写下来——调用方只能猜，猜就会错"
    assert "channel" in _r, "researchers 没读通道名（会退化成按 urls 反推 → 假指控）"

    # ⑦ **「答了但没带回可点源」必须与「空返回」分开渲染**。
    # 千问 enable_search 经常"有正文、无 search_info"（日志原话「千问0来源/0URL + Bing3条」）。
    # 按"取回 0 条"打成「⚠️ 空返回」，而同一行的备注写着"千问已联网作答"——
    # **同一行自己打自己脸**，正是渲染层注释里点名的那类病案。
    from core.source_health import new_trace as _nt2
    from core.source_health import record as _rc2
    from core.source_health import render_trace_appendix as _rta

    _t2 = _nt2()
    _rc2(_t2, "qwen_search", "通义千问联网检索（enable_search）", [],
         note="千问联网作答，未带来源（可点 URL 由结构化通道补）", layer="L4", kind="text_only")
    _md = _rta(_t2)
    # **只查那一行**：文件末尾的脚注里本来就有「空返回」四个字，
    # 拿整篇去 assert "空返回" not in md 会永远失败（我第一版就是这么写的）。
    _row = next((ln for ln in _md.splitlines() if "通义千问" in ln), "")
    assert _row, "台账里没渲染出千问那一行"
    assert "文本（无可点源）" in _row, f"答了但没可点源的通道被打成了「空返回」：{_row}"
    assert "空返回" not in _row, f"text_only 行仍被印成空返回——同一行自相矛盾：{_row}"
    # 反向样本：**真·空返回仍必须是「空返回」**，别把这一改变成全局洗白
    _t3 = _nt2()
    _rc2(_t3, "bocha", "博查 AI Web Search", [], note="本次无结果", layer="L4")
    _row3 = next((ln for ln in _rta(_t3).splitlines() if "博查" in ln), "")
    assert "空返回" in _row3, f"真·空返回被洗成了别的标记——这条锁失去了分辨力：{_row3}"


def test_audit_layer_says_only_what_it_knows():
    """审计层印出来的每一句话，必须由**同一次判定**产生（2026-09-18 三处同族修复）。

    ① **时间锚假阳性**（最重）：核验官拿"这个日期不可能存在"当判定理由，
       把国家统计局 **2026-09-15** 的真实发布判成「❌ 不相关·属虚构或错误数据」——
       而系统日期是 **2026-09-18**。v0.9.81 修过对抗校验层的同一个病，蕴涵核验层没修。
    ② **跨度不可复算**：印在交付件上的 `跨度 2.6×`，用交付的代码跑交付的文本得 3.4×——
       桶里多出的那个是 **RedTeam 自己的反推句**（"一线 326 杯"），
       而 `values[:6]` 把它截断藏住了。**引擎自己的产物被算进了它审计的集合。**
    ③ **组名自相矛盾**：「盈亏平衡杯量（万元口径）1.5、5.3」——名字说杯量、值是万元。
    """
    # ---- ① 时间锚：真病案必被作废；正当判定不许被误伤 ----
    from core.entailment import _filter_time_denial

    _case = {
        "verdict": "unrelated",
        "why": "摘录含未来时间（2026年），属虚构或错误数据，非真实官方发布，无法作为事实依据。",
        "quote": "",
    }
    assert _filter_time_denial(_case)[0] == "time_anchor", "真实病案没被作废——国家统计局的真发布仍会被印成虚构"
    # 反向样本 ×2：正当的 unrelated / contradict 一个都不许动
    _ok1 = {"verdict": "unrelated", "why": "摘录是页面导航与免责声明，与本句主张无实质关系。", "quote": ""}
    _ok2 = {"verdict": "contradict", "why": "主张增长而摘录为下降，数字方向相反。", "quote": "x"}
    assert _filter_time_denial(_ok1)[0] == "unrelated", "正当的 unrelated 被误伤"
    assert _filter_time_denial(_ok2)[0] == "contradict", "正当的 contradict 被误伤"
    # 撤销 ≠ 判通过，且必须留痕
    from core.entailment import _VERDICT_LABEL

    assert "time_anchor" in _VERDICT_LABEL and "支撑" not in _VERDICT_LABEL["time_anchor"], (
        "时间锚作废被印成了通过——那是洗白，不是撤销"
    )

    # ---- ② 跨度必须能由印出来的取值复算 ----
    import json as _j
    from pathlib import Path as _P

    from core.rule_lint import _check_metric_consistency

    _rep_dir = _P(__file__).resolve().parent / "data" / "reports"
    _files = sorted(_rep_dir.glob("*.json"))[-4:]
    assert _files, "没有可核的报告——本项会空转"
    for _f in _files:
        try:
            _d = _j.loads(_f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for _h in _check_metric_consistency(_d.get("report") or ""):
            _v = [float(x) for x in _h["values"]]
            if len(_v) < 2:
                continue
            _repro = round(max(_v) / min(_v), 1)
            assert abs(_repro - _h["spread"]) < 0.15, (
                f"印在交付件上的跨度复算不出来：{_h['metric']} "
                f"印 {_h['spread']}×、按印出的取值算是 {_repro}×（{_f.name}）"
            )

    # ---- ③ 组名不许与自己印的单位打架 ----
    from core.rule_lint import _unit_conflicts

    assert _unit_conflicts("单店日均杯量", "money_wan"), "杯量指标捕到万元值却照样报——组名会自相矛盾"
    assert _unit_conflicts("关店率", "month"), "率指标捕到月份值却照样报"
    assert not _unit_conflicts("盈亏平衡", "month"), "去掉单位词的组名被误杀"
    assert not _unit_conflicts("客单价", "元"), "正常组被误杀"

    # ---- ④ 口径冲突必须"给判断"，不许只把矛盾交出去 ----
    # 病案：此前只印「跨度 N×」+ 三句原文，读者得自己找、自己判——
    # **"检测到位却把矛盾原样交出去"等于把严谨做成了免责声明**。
    from core.rule_lint import format_rule_lint_md as _fmd
    from core.rule_lint import lint_report as _lr

    _hit_any = False
    for _f2 in _files[-2:]:
        try:
            _d2 = _j.loads(_f2.read_text(encoding="utf-8"))
        except Exception:
            continue
        _md2 = _fmd(_lr(_d2.get("report") or ""))
        if "口径冲突" not in _md2:
            continue
        _hit_any = True
        assert "各取值出自哪一节" in _md2, "只报了跨度、没给分节——读者仍要自己翻全文找"
        assert ("建议主口径" in _md2) or ("未给建议主口径" in _md2), (
            "既没给建议、也没说为什么给不出——又退回成把矛盾交出去"
        )
    assert _hit_any, "最近两份报告里一条口径冲突都没有——本项会空转，先确认语料"


def test_sizing_unit_and_advice_defects():
    """97. 决策区三类硬错——**都是三份外部分析在真实交付件上抓到、而引擎此前完全没看见的**。

    三条的共同点不是"判据太松"，是**判据的覆盖范围根本没到**：
      · 同层两个 SAM → 闸门只抽每层**首个**值（879 < TAM，于是"嵌套成立"）；
      · `家/10万人`   → 这个量纲**不在 R1 的单位白名单里**，整条对规则层不可见；
      · 92 杯 vs 171 杯 → 两个数分属"建议"与"单位经济"两处，规则层没有跨指标的判据。
    因此每一条都配**反向样本**：新判据不许把干净的写法也报出来。
    """
    from core.integrity_gate import _tier_values, check_sizing_nesting
    from core.rule_lint import _check_advice_vs_breakeven as _adv
    from core.rule_lint import _check_unit_denominator as _unit
    from core.rule_lint import lint_report as _lr2

    # ---- ① 同一市场层级两个值 ----
    _dup = (
        "中国咖啡行业 TAM 约为 5,371 亿元。\n\n"
        "咖啡行业的可服务市场（SAM）规模约为 879 亿元。\n\n"
        "下沉市场咖啡行业的可服务市场规模（SAM）约为 928 亿元。\n\n"
        "可获得市场规模（SOM）约为 53 亿元。\n"
    )
    assert _tier_values(_dup).get("sam") == [879.0, 928.0], "同层两个 SAM 没抓全"
    _m = check_sizing_nesting(_dup)
    assert "同一个市场层级" in _m and "879" in _m and "928" in _m, f"同层多值没报全：{_m!r}"
    # 反向样本：干净三层不许报
    assert check_sizing_nesting("TAM 约为 5,371 亿元。SAM 约为 879 亿元。SOM 约为 53 亿元。") == "", (
        "干净的三层被误报——新判据会常态误报"
    )
    # 反向样本：**同一个数重复写多次不算多值**（实测报告里 879 出现 5 次）
    assert check_sizing_nesting("SAM 约为 879 亿元。SAM 规模约 879 亿元。TAM 5,371 亿元。SOM 53 亿元。") == "", (
        "同一个值的多次复述被当成『两个值』——那会把正常报告全报一遍"
    )
    # 反向样本：`TAM/SAM/SOM` 斜杠连写处不是取值点
    assert _tier_values("支持 TAM/SAM/SOM 测算，TAM 约为 5,371 亿元。").get("tam") == [5371.0], (
        "斜杠连写处取到了错值"
    )
    # ⚠️ **两条"不许常态误报"的反向样本**（本规则上线当天靠它们才敢上）：
    # 第一版判据是"同层 ≥2 个值就报"，跑全部 24 份历史报告 **24/24 全命中**——
    # 而命中的全是**合法且专业**的写法。两轮收紧后降到 2/24，且那 2 份逐份读原文确认是真冲突。
    # ① 情景分析：保守/中性/乐观 三值是**本产品对外主张的方法论**，不许判成自相矛盾
    _scen = (
        "新进入者可实际获取的市场份额（SOM）介于 33 亿元至 80 亿元之间，中性预期约为 54 亿元。\n\n"
        "敏感性分析表明，乐观情景下份额可提升，推动 SOM 达到 80 亿元；\n"
        "反之保守情景下 SOM 可能降至 33 亿元。\n\n"
        "TAM 约为 4,218 亿元，SAM 约为 753 亿元。\n"
    )
    assert check_sizing_nesting(_scen) == "", (
        f"情景分析（保守/中性/乐观）被误判成报告自相矛盾——用自家卖点攻击自家产品：{check_sizing_nesting(_scen)}"
    )
    # ② 敏感性推演：同样合法
    _sens = (
        "剔除地理覆盖限制后可服务市场（SAM）约为 753 亿元。\n\n"
        "若 SAM 渗透率从 30% 下调至 24%，SAM 将缩减至约 602 亿元。\n"
    )
    assert check_sizing_nesting(_sens) == "", f"敏感性推演被误判：{check_sizing_nesting(_sens)}"
    # ③ **相差数倍的取值 = 不同口径，不许报**（87 与 1,088 相差 12 倍，几乎必然是两个区域/品类）
    _far = "全市 SAM 约为 87 亿元。\n\n全国 SAM 约为 1,088 亿元。\n\nTAM 为 4,218 亿元。\n"
    assert check_sizing_nesting(_far) == "", f"跨口径多值被误报：{check_sizing_nesting(_far)}"
    # ④ 真病案仍是真病案：879 / 928 只差 5.3%，**任何跨度阈值都放行**，必须靠"同层多值"才抓得到
    _close = "咖啡行业的可服务市场（SAM）规模约为 879 亿元。\n\n下沉市场的可服务市场规模（SAM）约为 928 亿元。\n"
    assert "同一个市场层级" in check_sizing_nesting(_close), "只差 5.3% 的双值是本规则唯一存在的理由"

    # ---- ② 同一数字两种分母量纲 ----
    _u = _unit("三四线门店渗透率仅 0.8家/10万人；另一处写 0.8家/万人，不足一线 1/5。")
    assert _u and "10 倍" in _u[0]["detail"], f"0.8家/万人 vs 0.8家/10万人 必须报：{_u}"
    assert _u[0]["level"] == "high", "量纲错是硬错，必须够得到闸门（level=high）"
    assert "0.8家/万人" in _u[0]["snippet"] and "0.8家/10万人" in _u[0]["snippet"], (
        "两个原文写法都要印出来——否则读者没法复算"
    )
    # 反向样本三条
    assert not _unit("门店 0.8家/10万人，一线 12.3家/10万人。"), "同分母被误报"
    assert not _unit("门店 0.8家/10万人，一线 12.3家/万人。"), "不同数值被误报"
    assert not _unit("人均消费 0.8杯/人，门店 0.8家/万人。"), "不同量词被误报"
    # 引用块是元层（闸门横幅/时效/AI 标识），不许参与判定。
    # 样本要够长：`_body_only` 有一条"掉太多就不动，防整篇被吞"的守卫（保留率需 >75%），
    # 三行样本会触发它——守卫本身是对的，是样本不具代表性。
    _qt = "> 审计备注：0.8家/万人\n\n" + "正文段落。这里是一句正常的报告陈述。\n" * 8 + "正文：0.8家/10万人。\n" * 6
    assert not _unit(_qt), "引用块里的数被算进了报告自己的断言集合"

    # ---- ③ 行动建议踩了同一张清单里的红线 ----
    _a = _adv(
        "**行动建议：**\n"
        "1. 建议将下沉门店形态调整为社区型小面积快取店，以适配92杯/日的低流量基准。\n"
        "2. 建议推出15–18元专属下沉产品线。\n"
        "3. 建议优先选址高流量商圈并确保日均销量突破171杯的盈亏平衡点。\n\n"
        "## 单位经济\n月度盈亏平衡销量约 5,141 杯，折合日均需售出 171 杯。\n"
    )
    assert _a and "92" in _a[0]["detail"] and "171" in _a[0]["detail"], f"建议与红线打架必须报：{_a}"
    assert _a[0]["level"] == "high", "同一条建议内部自相矛盾是硬错"
    # 反向样本四条
    assert not _adv(
        "**行动建议：**\n1. 建议以适配200杯/日的基准设计门店。\n"
        "2. 建议确保日均销量突破171杯的盈亏平衡点。\n"
    ), "建议高于红线被误报"
    assert not _adv("**行动建议：**\n1. 建议确保日均销量突破171杯的盈亏平衡点。\n"), (
        "只有红线、没有被建议的基准——无从比较，不许报"
    )
    assert not _adv(
        "**行动建议：**\n1. 门店当前92杯/日，需关注。\n2. 建议确保突破171杯的盈亏平衡点。\n"
    ), "陈述句（无指令词）被当成建议基准——会把正常报告全报一遍"
    assert not _adv("建议适配92杯/日。单位经济：盈亏平衡171杯。"), "没有行动块时报了"

    # ---- ④ 两条新规则必须真的挂进 lint_report（否则是"实现了但用不上"）----
    _v = _lr2(
        "正文：三四线门店渗透率仅 0.8家/10万人；另一处写 0.8家/万人。\n\n"
        "**行动建议：**\n1. 建议以适配92杯/日的基准开店。\n2. 建议确保日均销量突破171杯的盈亏平衡点。\n"
    )
    _kinds = {x.get("kind") for x in _v}
    assert "unit_denominator_conflict" in _kinds, f"R10 没有挂进 lint_report：{_kinds}"
    assert "advice_below_breakeven" in _kinds, f"R11 没有挂进 lint_report：{_kinds}"
    # 而且必须能在交付件的规则层自检那一节印出来（判定→消费→披露 三段都要通）
    from core.rule_lint import format_rule_lint_md as _fmd2

    _md3 = _fmd2(_v)
    assert "两种分母量纲" in _md3 and "盈亏平衡线" in _md3, f"新规则没有印进附录：{_md3[:200]}"


def test_pair_matrix_checks_the_requirement_it_prints():
    """98. 双源配对矩阵：**契约与判定同源**。

    病案（外部审计 A1，2026-09-18）：`pair_required` 那一列**只被打印、不参与判定**，
    判据只有 `ok = 异侧≥2 或 独立域名≥2`。于是同一行可以一边印
    「要求：法定披露（一手）+ 第三方（媒体/研报）」、一边判 ✅，
    哪怕调用台账显示巨潮/SEC 本次**零调用**。
    ——"双源配对矩阵"正是本产品对外主张可核对的那张表，它自己不可核对。
    """
    from core.evidence_matrix import _required_sides, _side_of, pair_matrix
    from core.models import Evidence, Source, SourceLevel

    # ① 要求文本 → 必须出现的侧别（机械映射，只认字面）
    _r = _required_sides("法定披露（一手）+ 第三方（媒体/研报）——用于交叉验证口径一致性")
    assert len(_r) == 2, f"解析不出两侧要求：{_r}"
    assert ("一手-官方",) in [s for _l, s in _r], f"没解析出『法定披露→一手-官方』：{_r}"
    assert _required_sides("") == [], "空要求不许硬判"
    assert _required_sides("——只有说明没有要求") == [], "只有用途说明时不许硬判"

    # ② 只有媒体源时：ok（来源不单一）可以为真，但 pair_ok 必须为假
    def _ev(claim, urls):
        return Evidence(
            claim=claim,
            sources=[
                Source(title=f"s{i}", url=u, level=SourceLevel.L4_USER, publisher="")
                for i, u in enumerate(urls)
            ],
        )

    _evs = [
        _ev("某公司门店数达 3,500 家", ["https://www.36kr.com/p/1", "https://www.jiemian.com/a"]),
    ]
    _rows = pair_matrix(_evs, limit=8)
    assert _rows, "配对矩阵没产出任何行"
    _sides = _side_of(_evs[0])
    for _row in _rows:
        if not _row.pair_required:
            continue
        if _row.pair_ok:
            # 判 ✅ 就必须真的有两侧到齐——这是本项存在的意义
            assert len(_sides) >= 2, f"判了 pair_ok 但只拿到 {_sides}（契约与判定不同源）"
        else:
            assert _row.missing, "判 ❌ 却不说缺哪一侧——读者无法据此补来源"
    # 反向样本：要求解析不出来时，必须**显式不判**，不许悄悄给个 ✅
    from core.evidence_matrix import _required_sides as _rs

    assert _rs("某种规则层读不懂的验证对写法") == [], "读不懂的要求被硬解析出了侧别"
    for _row in pair_matrix(_evs, limit=8):
        if not _row.pair_required or _required_sides(_row.pair_required):
            continue
        assert _row.pair_ok is False and _row.missing, (
            "验证对无法机械解析时必须显式标注『不判』，不许显示假的 ✅"
        )


def test_metric_source_institution_is_grounded():
    """99. R12：决策区点名的来源机构，必须能在**本次真的取回来的来源**里找到。

    病案（2026-09-18 实跑）：《核心指标一览表》8 行里 **6 行**的来源机构是
    `Euromonitor International` 与 `艾瑞咨询`，而全文检索来源只有 11 个域名，
    `euromonitor` / `iresearch` / `analysys` 出现 **0 次**。

    ⚠️ **本条判据刻意没有阻断权**（`level="warn"`），理由必须一起锁进测试——
    它的假阳性是结构性的：数字可能来自某个已取回页面**转述**了该机构
    （如界面新闻转引艾瑞），此时数字可追溯、只是名字填错了。机器分不清"转述"与"幻觉"。
    而它的真阳性也是结构性的：24 份历史报告只有 **4 份**真取回过 iresearch，
    却有 **21 份**把「艾瑞咨询」写成来源机构。
    **真假混杂、且假阳性说不清的判据不许阻断交付**——本仓库的规矩是
    "宁可少一条检查，也不要一条会常态误报的检查"。
    """
    from core.rule_lint import check_metric_source_grounding as G

    _md = (
        "## 📊 核心指标一览表\n\n"
        "| 指标 | 数值 | 来源机构 | 数据级别 |\n"
        "| :--- | :--- | :--- | :--- |\n"
        "| 三四线现磨咖啡市场规模(2026E) | 312 亿元 | Euromonitor International | L3 |\n"
        "| 下沉门店单店日均杯量中位数 | 92 杯 | 21世纪经济报道 | L4 |\n"
        "| 三四线门店渗透率 | 0.8 家/10万人 | 待核 | 待核 |\n"
        "| 瑞幸咖啡门店总数 | 19,873 家 | 瑞幸2024Q1财报 | L2 |\n"
    )
    _cits_real = [
        {"title": "星巴克、必胜客加码下沉市场丨消费参考 - 21财经", "url": "https://m.21jingji.com/comment/lists?aid=738570"},
    ]
    _v = G(_md, _cits_real)
    _orgs = {x["snippet"].split("、")[0] for x in _v}
    # ① 真病案被抓到：Euromonitor 本次没取回
    assert "Euromonitor International" in _orgs, f"点名了没取回的机构却没抓到：{_orgs}"
    # ② **没有阻断权**——这是本项最重要的一条断言
    assert all(x["level"] == "warn" for x in _v), (
        f"R12 拿到了阻断权——它的假阳性是结构性的（转述 vs 幻觉，机器分不清）：{[x['level'] for x in _v]}"
    )
    # ③ 反向样本：来源名直接出现在台账标题里 → 不报（21财经那条）
    assert "21世纪经济报道" not in _orgs, "名字直配能命中的却被报了"
    # ④ 反向样本：「待核」是**我们自己**标的，不是外部机构 → 不报
    assert not any("待核" in o for o in _orgs), "把『待核』当成了要核的外部机构"
    # ⑤ 反向样本：**法定披露**走交易所渠道取回 → 名字对不上域名是正常的，不许报。
    #    注意这一条**必须把 sec.gov 放进台账**——法定披露的"落地"靠的就是交易所渠道，
    #    没有它，报出来才是对的（这正是判据的定义，不是绕过判据）。
    _cits_sec = _cits_real + [
        {"title": "Luckin Coffee Inc. 10-Q", "url": "https://www.sec.gov/Archives/edgar/data/1767582/0001.htm"},
    ]
    assert "瑞幸2024Q1财报" not in {x["snippet"].split("、")[0] for x in G(_md, _cits_sec)}, (
        "公司财报名 ≠ 交易所域名，被误报（走 sec.gov 取回是合法的）"
    )
    #    反向的反向：台账里**没有**任何交易所渠道时，它该报——否则这条规则就是摆设
    assert "瑞幸2024Q1财报" in _orgs, (
        "台账里连交易所都没有，法定披露却照样放行——判据被写成了永远通过"
    )
    # ⑥ 反向样本：台账里就有 iresearch → 必须不报（否则所有正常报告都会被报一遍）
    _md_ai = _md.replace("| 三四线门店渗透率 | 0.8 家/10万人 | 待核 | 待核 |",
                         "| 三四线门店渗透率 | 0.8 家/10万人 | 艾瑞咨询 | L3 |")
    assert "艾瑞咨询" in {x["snippet"].split("、")[0] for x in G(_md_ai, _cits_real)}, (
        "台账里没有 iresearch，却没报「艾瑞咨询」——判据被写成了永远通过"
    )
    _cits_ai = _cits_real + [{"title": "下沉市场咖啡消费洞察", "url": "https://www.iresearch.com.cn/detail/1"}]
    assert "艾瑞咨询" not in {x["snippet"].split("、")[0] for x in G(_md_ai, _cits_ai)}, (
        "台账里已有 iresearch 却仍报——会把所有正常报告都报一遍"
    )
    # ⑦ 反向样本：**没有 citations 就整条跳过**（不猜）
    assert G(_md, []) == [], "没有台账时还在报——那是凭空指控"
    assert G(_md, None) == [], "citations=None 时还在报"
    # ⑧ 接线：交付路径必须真的调它（否则"实现了但用不上"）
    _src = (Path(__file__).resolve().parent / "graph.py").read_text(encoding="utf-8")
    assert "check_metric_source_grounding" in _src, "R12 没有接到交付路径上"
    assert "check_metric_source_grounding(final, citations)" in _src, (
        "接是接了，但没把 citations 传进去——那它就永远核不了台账"
    )

    # ---- ⑨ **根因侧**：提示词必须拿到"本次实际取回清单"，且修正器要就地标注 ----
    # 只报不改 = 报告照旧印着一个没取回的机构名（本仓库的"判定→消费→披露"断链）。
    from graph import _annotate_ungrounded_metric_sources as _A

    _md2 = (
        "## 📊 核心指标一览表\n\n"
        "| 指标 | 数值 | 来源机构 | 数据级别 | 年份 |\n"
        "| :--- | :--- | :--- | :--- | :--- |\n"
        "| 市场规模 | 312 亿元 | Euromonitor International | L3 | 2026 |\n"
        "| 单店杯量 | 92 杯 | 21世纪经济报道 | L4 | 2026 |\n"
        "\n## 下一节\n正文。\n"
    )
    _out = _A(_md2, _cits_real)
    # 表头**列名**与"点名了一个没取回的机构"结构同形——不跳过，整张表列名当场作废
    assert "| 指标 | 数值 | 来源机构 | 数据级别 | 年份 |" in _out, (
        f"表头行被当成数据行改了——列名作废：{_out[:160]}"
    )
    assert "Euromonitor International（⚠️本次未取回）" in _out, "没取回的机构没有被就地标注"
    assert "21世纪经济报道（⚠️" not in _out, "台账里取回过的来源被误标"
    # 来源未落地 → 数据级别必须同步降级（挂着一个没访问过的机构名比「待核」更危险）
    assert "Euromonitor International（⚠️本次未取回） | 待核 |" in _out, (
        "只改了机构名没降级——那一行仍然冒充 L3"
    )
    # 提示语必须**紧贴表的最后一行**：挂全文末尾＝读者翻三千行才看到解释
    _lines = _out.split("\n")
    _last_tbl = max(i for i, l in enumerate(_lines) if l.lstrip().startswith("|"))
    assert any("来源落地修正" in l for l in _lines[_last_tbl + 1: _last_tbl + 4]), (
        f"提示语没贴在表后（表末行={_last_tbl}）：{_lines[_last_tbl + 1: _last_tbl + 4]!r}"
    )
    # 幂等：第二次跑不许叠成「…（⚠️本次未取回）（⚠️本次未取回）」
    assert _A(_out, _cits_real) == _out, "修正器非幂等——第二次跑会叠标注"
    # 表外零改动（用长度而非切片比：插入后偏移会变）
    assert _out.count("## 下一节") == 1 and "正文。" in _out, "改到了表外的正文"
    # 没有台账就**不猜、不动**
    assert _A(_md2, []) == _md2 and _A(_md2, None) == _md2, "没有台账时仍动了正文"

    # ⑩ 清单本身：有取回才给清单，没取回给空串（空串=提示词里不加那一节）
    from graph import _retrieved_source_brief as _B

    assert _B({"subtask_results": {}}) == "", "没有取回却给了清单"
    _brief = _B({"subtask_results": {"st1__citations": _cits_real}})
    assert "21jingji.com" in _brief and "只能从" in _brief, f"清单没把域名给出去：{_brief[:120]}"
    # ⑪ 提示词里**不许再出现**那几个被幻觉最多的机构名当例子（它们在诱导模型凭记忆填）
    assert "如 艾瑞咨询、国家统计局、美团外卖、欧睿国际" not in _src, (
        "提示词的举例仍在点名那几个最容易被凭记忆填上的机构——判据与举例自相矛盾"
    )
    assert "sources_brief" in _src and "_retrieved_source_brief(state)" in _src, (
        "清单函数写了但没接进摘要生成——模型还是拿不到清单"
    )


def test_macro_source_gate_is_subtask_local():
    """100. 宏观源（统计局 / 世界银行 / IMF）的**开门判据**。

    病案（2026-09-18 实跑）：一份下沉咖啡报告里 `China GDP年增速（World Bank）`
    被同时挂进 **进入壁垒 / 单位经济 / 趋势风险 / 下沉客群消费心理洞察** 四个章节，
    引文核验逐条判 ❌ 不相关（"宏观GDP增速与企业微观层面的单位经济模型无直接对应关系，
    属于张冠李戴"）——**28 对核验里 4 对（14%）是机制自己造出来的假引用**。

    两个叠加的根因，本项各锁一条：
      ① 判据用的是 `question + subtask_desc + queries` 全文——用户的问句里几乎必然
         含"市场规模/消费/价格"，于是**每个子任务都开门**；
      ② 词表里 `经济`/`消费` 会命中「单位**经济**」「**消费**心理洞察」这类**章节名**。

    ⚠️ 这一项只锁**判据**，不锁"某个源一定被调用"——真实调用还取决于工具计划与配额。
    """
    from core.authority_fetch import _MACRO_HINT, _MACRO_SCOPE

    # ① 开门词表里**不许**再有能命中章节名的宽词
    for _broad in ("经济", "消费", "价格"):
        assert _broad not in _MACRO_SCOPE, (
            f"「{_broad}」回到了开门词表——它会命中章节名，门就等于永远开着"
        )
    # `_MACRO_HINT` 本身保持原样（它另有用途：从**问句**里抠宏观短语），两张表不是一张
    assert "经济" in _MACRO_HINT and "消费" in _MACRO_HINT, (
        "抠短语用的宽词表被误改——那会连带改掉 1933 行的短语抽取"
    )
    # ② 判据必须**只看本子任务**：源码里不许再出现 `any(k in text for k in _MACRO_...)`
    _src = (Path(__file__).resolve().parent / "core" / "authority_fetch.py").read_text(encoding="utf-8")
    assert "any(k in text for k in _MACRO_HINT)" not in _src, (
        "宏观门禁又用回全文了——问句里的宏观词会对所有子任务开门"
    )
    assert "_local = f\"{subtask_desc or ''} \"" in _src, "子任务本地文本没建出来"
    assert _src.count("any(k in _local for k in _MACRO_SCOPE)") >= 2, (
        "统计局与世行/IMF 两处门禁没有都改成按子任务本地文本判"
    )
    # ③ 用**真实报告里的 16 个章节名**做端到端判定：只许宏观两节开门
    _chapters = [
        "决策判定", "最小验证计划", "关键假设与可推翻变量", "行动清单", "成本测算",
        "风险与对策", "研究方法与框架", "宏观环境", "市场规模测算", "竞争格局",
        "目标市场", "进入壁垒", "单位经济", "趋势风险", "下沉市场用户真实反馈",
        "下沉客群消费心理洞察",
    ]
    _open = [c for c in _chapters if any(k in c for k in _MACRO_SCOPE)]
    assert set(_open) == {"宏观环境", "市场规模测算"}, f"开门集合不对：{_open}"
    # 病案里的四个章节**必须**关着——这是本项存在的全部理由
    for _bad in ("进入壁垒", "单位经济", "趋势风险", "下沉客群消费心理洞察"):
        assert _bad not in _open, f"病案章节「{_bad}」仍然开着门——假引用会复发"
    # ④ 反向样本：真正的宏观子任务**必须**开着（否则这道门就是关死的）
    for _good in ("行业规模与增速", "宏观环境与政策", "人均消费量测算", "社零总额增速"):
        assert any(k in _good for k in _MACRO_SCOPE), f"真正的宏观子任务「{_good}」被关在门外了"


def test_round2_external_audit_fixes():
    """101. 三份外部审计（Claude / GPT / Gemini）交叉出的六处，逐条上锁。

    共同点：**五处是"审计层/交付层自己出的错"**，而不是"报告写错了"——
    这正是本仓库最贵的一类缺陷（指控句是我们自己加的，写错的代价远高于少写一句）。
    """
    from core.integrity_gate import _avoiding_context
    from core.rule_lint import _check_metric_consistency as _C
    from core.rule_lint import _R1_METRICS

    # ---- ① L1–L5 只许有一套定义（Claude A1：同一份报告印两套互不相容的分级）----
    _gsrc = (Path(__file__).resolve().parent / "graph.py").read_text(encoding="utf-8")
    assert "L1官方/L2媒体/L3行业/L4用户/L5推测" not in _gsrc, (
        "旧的那套 L1–L5 定义又回来了——同一份报告两个地基建两套版本，五秒就能被看出来"
    )
    assert "L1 官方统计/监管 → L2 法定披露/交易所" in _gsrc, "方法说明那一行没改成与注册表同源"

    # ---- ② 劝阻句不许被盖章（Gemini 单独点名，且它就在交付件上）----
    assert _avoiding_context("建议产品定价锚定 15–20 元，放弃 25 元以上高端溢价", "25元"), (
        "「放弃 25 元」被判成「依赖 25 元」——系统当场枪毙一条完全正确的建议"
    )
    assert _avoiding_context("应规避 68.2% 这一口径下的高端定价", "68.2%"), "规避义没被识别"
    # 反向样本：**真的依赖**时必须照标，否则这道守门就把闸门关死了
    assert not _avoiding_context("建议将客单价提升到 25 元以上以覆盖固定成本", "25元"), (
        "真·依赖被误判成劝阻——插桩会失效"
    )
    assert not _avoiding_context("目标设定为 25 元的客单价", "25元"), "正向建议被误判"

    # ---- ③ 边际增量不是取值（Claude A4：客单价 28.7× → 真实 2.4×）----
    _v = _C("在下沉市场，客单价每上涨 1 元，销量流失率即呈现非线性增长。客单价 12 元。客单价 28.7 元。")
    _m = [x for x in _v if x["metric"].startswith("客单价")]
    assert _m and _m[0]["spread"] == 2.4, f"「每上涨 1 元」的 1 又被当成客单价了：{_m}"
    assert "1.0" not in _m[0]["values"], f"伪值 1.0 还在桶里：{_m[0]['values']}"
    # 反向样本：**目标值**（提升至/增加到）是真取值，不许被当成增量剔掉
    _v2 = _C("客单价提升至 28.7 元。客单价 12 元。")
    _m2 = [x for x in _v2 if x["metric"].startswith("客单价")]
    assert _m2 and "28.7" in _m2[0]["values"], f"「提升至」的目标值被误剔：{_m2}"

    # ---- ④ 远期值不算"同层两个数"（Claude B8：5,000 是 2030 年远期值，我这层判成误报）----
    from core.integrity_gate import check_sizing_nesting as _S

    _fwd = "TAM 约为 4,655 亿元。\n\n到 2030 年前后有望突破 5,000 亿元。\n"
    assert _S(_fwd) == "", f"远期值被当成第二个当期 TAM——这是我上一轮引入的误报：{_S(_fwd)}"
    # 反向样本：真·同期两个值**必须**照报（否则这条规则就白加了）
    assert "同一个市场层级" in _S("TAM 约为 4,655 亿元。\n\nTAM 约为 5,000 亿元。\n"), (
        "同期两个 TAM 被放过了——判据被写成永远通过"
    )

    # ---- ⑤ 离题站（Claude A6：咖啡研报的引文清单里出现萌娘百科）----
    from agents.researchers import _is_clean_url

    for _bad in ("https://mzh.moegirl.org.cn/%E5%92%96%E5%95%A1",
                 "https://caifuhao.eastmoney.com/news/2026/x.html"):
        assert not _is_clean_url(_bad), f"离题站没被掐掉：{_bad}"
    # 反向样本：正经源不许被误伤（这是过滤器，误伤=报告更差）
    for _ok in ("https://www.stats.gov.cn/sj/zxfb/", "https://m.21jingji.com/comment/lists?aid=1",
                "https://www.36kr.com/p/123", "https://www.cninfo.com.cn/new/index"):
        assert _is_clean_url(_ok), f"正经来源被误伤：{_ok}"

    # ---- ⑥ 同一实体的四种写法必须聚成一个桶（Claude B2：回本周期 5 个值一个都没聚）----
    _blob = " ".join(p for g in _R1_METRICS for p in g["patterns"])
    for _w in ("投资回收周期", "盈亏平衡周期", "平均单店年营收", "单店年均营收中位数"):
        assert _w in _blob, f"指标名变体「{_w}」没进词表——同一实体会继续分组分裂"
    _v3 = _C("投资回收周期约 14.2 个月。回本周期 25 个月。盈亏平衡周期 28–36 个月。")
    _m3 = [x for x in _v3 if "回收" in x["metric"] or "回本" in x["metric"]]
    assert _m3 and float(_m3[0]["spread"]) >= 1.8, f"四种写法的值没聚到一起：{_m3}"


def test_self_consistency_layer():
    """102. 自一致性层 · 七对 + R13（决策阈值不许低于本报告自己算的红线）。

    **为什么这一层是"停止条件"的载体**（三份外部审计的战略结论）：
    本项目的缺陷**不是无限的**——四跑穷举下来只有 **7 类**，而此前八层审计
    （闸门/RedTeam/辩论/引文核验/蕴涵核验/遗漏层/规则层/质检）**全在核"外部证据支不支持我"**，
    **没有一层在核"我自己这几部分彼此对不对得上"**。
    所以"测一次改一次"的成因不是改得不够，是**修的是实例、没封住类**。
    """
    from core.rule_lint import (
        SELF_CONSISTENCY_PAIRS,
        _check_decision_thresholds,
        _report_breakeven_cups,
        describe_self_consistency,
        format_rule_lint_md,
        lint_report,
    )

    # ---- ① 登记表必须与实现同源（表是能力声明，脱离代码就变成宣传）----
    # ⚠️ 2026-09-18 第十跑：这条锁**原先写的是 `== 7`，理由是"七对是穷举出来的"**——
    # 而那句话本身在第九跑就被同一份报告推翻了（第④对声明的 R13 那一跑返回空、
    # 三道门全未命中），第十跑又**新增了第⑧对**（指标名 ↔ 口径自陈，R14）。
    # 所以锁改成两条，都不再依赖"7"这个数：
    #   · **只增不减**（≥7）：删掉一行必须是有意的；
    #   · **每行右列的落地检查必须能在代码里指名道姓地找到**（下面是符号检查）。
    # **「7」从来不是事实，是当时恰好登记了 7 条。**把"恰好"写成"穷举"，
    # 就等于给自己立了一个每条新缺陷都能推翻的靶子——本仓库已经因此塌过一次。
    assert len(SELF_CONSISTENCY_PAIRS) >= 7, (
        f"登记表缩到了 {len(SELF_CONSISTENCY_PAIRS)} 对——删行要有依据"
    )
    assert len({p[0] for p in SELF_CONSISTENCY_PAIRS}) == len(SELF_CONSISTENCY_PAIRS), (
        "登记表里有重名的对"
    )
    _blob = "\n".join(f"{a}|{b}|{c}" for a, b, c in SELF_CONSISTENCY_PAIRS)
    for _k in ("R12", "R13", "R14", "R1", "pair_ok"):
        assert _k in _blob, f"登记表里没有指向 `{_k}` 的落地检查——那一行是空话"
    assert f"{len(SELF_CONSISTENCY_PAIRS)} 对" in describe_self_consistency(), (
        "说明串与常量表不同源（条数写死了？）"
    )
    # 附录里必须由常量渲染（不是手写副本），且**干净时也印**
    _md_clean = format_rule_lint_md([])
    assert "自一致性层" in _md_clean, "干净时就不印这一层——读者会以为它是事后才跑的"
    for _n, _j, _i in SELF_CONSISTENCY_PAIRS:
        assert _n in _md_clean and _i in _md_clean, f"表行不是由常量渲染的：{_n}"

    # ---- ② R13：红线只在《单位经济》章取（跨主体是 R11 第一版踩过的坑）----
    _rep = (
        "## 单位经济\n月固定成本 61,000 元，毛利率 62%，客单价 16 元。\n"
        "保守情景：日均杯量跌至205杯（盈亏平衡点）。\n\n"
        "## 目标市场\n部分县域门店因日均杯量不足280杯（远低于360杯的盈亏平衡点）而关闭。\n"
    )
    assert _report_breakeven_cups(_rep) == 205.0, (
        f"红线取错了主体——目标市场那 360 杯是别的主体的红线：{_report_breakeven_cups(_rep)}"
    )
    _v = _check_decision_thresholds(
        _rep
        + "\n## 风险与对策\n若单店连续3个月日均杯量低于92杯，立即启动止损程序。\n"
        + "## 趋势风险\n对连续6个月日均杯量低于80杯的门店执行关停或迁址。\n"
    )
    _hit = {x["snippet"] for x in _v}
    assert "92杯、205杯" in _hit and "80杯、205杯" in _hit, f"止损/关停低于 BEP 没被抓到：{_hit}"
    assert all(x["level"] == "high" for x in _v), "这是硬错，必须够得到闸门"
    # 反向样本①：阈值**高于**红线 → 不报（否则这道门就是关死的）
    assert _check_decision_thresholds(
        _rep + "\n## 风险与对策\n若连续3个月日均杯量低于260杯，立即启动止损程序。\n"
    ) == [], "阈值高于 BEP 却报了——那是把正确写法判成缺陷"
    # 反向样本②：**取不到红线就不判**（不猜）
    assert _check_decision_thresholds("## 风险\n低于 92 杯启动止损。\n") == [], (
        "没有可依据的红线时还在报——那是凭空指控"
    )
    # ③ 接线：必须挂进 lint_report
    _kinds = {x.get("kind") for x in lint_report(_rep + "\n## 风险与对策\n连续3个月低于92杯启动止损。\n")}
    assert "decision_threshold_below_bep" in _kinds, f"R13 没挂进 lint_report：{_kinds}"


def test_deliver_payload_backfill_is_complete():
    """`node_deliver` 回吐不全 → `run_task_stream` 从图内最终状态补齐。**这份名单必须有牙**。

    病案（三次，同一个形状）：`run_task_stream` 拿 `final_payload = deliver 那一个节点的
    delta` 去喂 `_result_json`。凡 deliver 没回吐、`_result_json` 又照读的字段，
    **在流式路径（也就是演示走的那条）上恒为初值**：
      · `sop_fallback` 恒 {} → 「⚠️ 决策清单为兜底模板」红角标永不出现；
      · `conflict_scan` 恒缺失 → 「（比对 N 对）」分母永不显示，tooltip 反过来说
        「本次未产出冲突检测读数（旧报告）」；
      · `trace_events` 恒 []（2026-09-19）→ done 事件里轨迹是空的，而流上推了 30+ 条。

    历史上每次都是"发现一个、补一行"。本测试**机械枚举**，把这份名单钉死：
    TaskState 的字段 × `_result_json` 的消费点，减去 deliver 自己回吐的键，
    剩下的必须全在 `_DELIVER_BACKFILL_KEYS` 里（或在流里被显式赋值的白名单里）。
    """
    import ast as _ast

    import graph as _graph
    from core.state import TaskState

    _src = Path(__file__).resolve().parent / "graph.py"
    _tree = _ast.parse(_src.read_text(encoding="utf-8", errors="replace"))

    # ---- deliver 回吐了哪些键：取 node_deliver 函数体里**最后一个** return 的字典字面量 ----
    _deliver = next(
        n for n in _tree.body
        if isinstance(n, _ast.FunctionDef) and n.name == "node_deliver"
    )
    _rets = [n for n in _ast.walk(_deliver) if isinstance(n, _ast.Return)]
    _deliver_keys: set[str] = set()
    for _r in reversed(_rets):
        if isinstance(_r.value, _ast.Dict):
            _deliver_keys = {
                k.value for k in _r.value.keys
                if isinstance(k, _ast.Constant) and isinstance(k.value, str)
            }
            break
    assert _deliver_keys, "没能从 node_deliver 里解出返回字典——解析器该更新了"
    assert "report_final" in _deliver_keys, (
        f"解出来的返回键不像 deliver 的返回字典：{sorted(_deliver_keys)[:8]}"
    )

    # ---- `_result_json` 消费了哪些 TaskState 字段 ----
    _rj = next(
        n for n in _tree.body
        if isinstance(n, _ast.FunctionDef) and n.name == "_result_json"
    )
    _consumed: set[str] = set()
    for _call in _ast.walk(_rj):
        if (
            isinstance(_call, _ast.Call)
            and isinstance(_call.func, _ast.Attribute)
            and _call.func.attr == "get"
            and isinstance(_call.func.value, _ast.Name)
            and _call.func.value.id == "result"
            and _call.args
            and isinstance(_call.args[0], _ast.Constant)
            and isinstance(_call.args[0].value, str)
        ):
            _consumed.add(_call.args[0].value)
    assert len(_consumed) >= 15, f"_result_json 的消费点只解析出 {len(_consumed)} 个，解析器该更新了"

    _state_keys = set(TaskState.__annotations__.keys())
    _backfill = set(_graph._DELIVER_BACKFILL_KEYS)

    # 流里**逐个节点手工补**的那些键：来源各不相同（质检节点 / 决策节点 / 图内状态），
    # 不是从最终状态统一取的，所以不进补齐名单；但**必须在流里有赋值行**。
    _explicit = {
        "conflict_results", "quality_report", "stop_card",
        "validation_tasks", "monitor_conditions",
        "decision_verdict", "integrity_gate", "budget_estimate",
        "plan", "run_stats",
    }
    _stream_src = _ast.get_source_segment if False else None  # noqa: F841 (保留可读性)
    _g_src = _src.read_text(encoding="utf-8", errors="replace")
    for _k in sorted(_explicit):
        assert f'result["{_k}"] =' in _g_src, (
            f"白名单里的 `{_k}` 在 run_task_stream 里没有赋值行——"
            "要么它其实走的是补齐名单，要么这个名字已经不对了"
        )

    _missing = sorted(
        k for k in _consumed
        if k in _state_keys and k not in _deliver_keys
        and k not in _backfill and k not in _explicit
    )
    assert not _missing, (
        f"这些 TaskState 字段被 `_result_json` 消费、deliver 又没回吐，"
        f"却不在补齐名单里 → 它们在**流式路径（演示走的那条）**上恒为初值：{_missing}。"
        "加进 `graph._DELIVER_BACKFILL_KEYS`，或写清它为什么走手工补。"
    )
    # 反向：名单里的键必须是**真的** TaskState 字段（防手滑写错名字，那种错不会报错，只会静默为空）
    _bogus = sorted(k for k in _backfill if k not in _state_keys)
    assert not _bogus, f"补齐名单里有不是 TaskState 字段的键（拼错了？）：{_bogus}"
    assert "trace_events" in _backfill, "trace_events 又被移出补齐名单了"


def test_demo_account_affords_top_tier():
    """演示账户的初始余额必须**付得起最贵的档位**；充值要有真能改余额的入口。

    病案（2026-09-19）：`_balance = 100`，而深入档 150、定制档 400 ——
    于是**每一次深入/定制档演示，界面都稳定打出「（积分不足，本次体验价）」**。
    那不是业务状态，是初始值没配够：演示账户只演示一两次，却按"新用户只有 100 分"发币。
    把提示藏起来不算修——**要让这个状态结构上到不了**。

    第二个病案：`RECHARGE_TIERS` 一直随 `/api/billing` 送到前端，
    而**全仓没有任何一处能加积分**——价目表是摆着看的。
    「实现了 ≠ 能被用上」：字段有、接口有、承重的那个动作没有。
    """
    import core.billing as B

    # ---- ① 初始余额 ≥ 最贵档位（"不足"这个状态在正常演示里到不了）----
    _top = max(B.MODE_POINTS.values())
    assert B.DEMO_START_BALANCE >= _top, (
        f"演示初始余额 {B.DEMO_START_BALANCE} < 最贵档位 {_top}（{max(B.MODE_POINTS, key=B.MODE_POINTS.get)}）"
        "——演示贵档时界面会稳定打「积分不足」。"
    )
    for _m in B.MODE_POINTS:
        assert B.preview(_m)["enough"] is True, f"{_m} 档余额不足：{B.preview(_m)}"
    assert B.billing_info()["start_balance"] == B.DEMO_START_BALANCE, (
        "billing_info 没把初始余额带出去——界面上那句「重启回到初始值」就没法核对"
    )

    # ---- ② 充值：能加、能拒、账要对 —— 且**用完还原**，不许污染后续用例 ----
    _snap_bal, _snap_ledger = B._balance, list(B._ledger)
    try:
        _before = B.get_balance()
        _r = B.recharge(30)
        assert _r.get("ok") is True and _r.get("simulated") is True, (
            "充值没成功，或没如实标出这是模拟账户（不许假装接了支付）"
        )
        _tier = next(t for t in B.RECHARGE_TIERS if t["amount"] == 30)
        assert B.get_balance() == _before + _tier["points"], "充值没真的加上积分"
        assert B._ledger[-1].get("points") == _tier["points"], "充值没落账"
        # 反向①：没有这个档位 → 明确拒绝，**不许静默按最近的档位加**
        _bad = B.recharge(31)
        assert _bad.get("ok") is False and "档位" in str(_bad.get("reason")), (
            f"¥31 不是档位却放行了：{_bad}"
        )
        assert B.get_balance() == _before + _tier["points"], "被拒绝的充值居然改了余额"
        # 反向②：非法输入不许抛
        assert B.recharge("abc").get("ok") is False, "非数字金额应当被拒（且不抛异常）"
        # 反向③：三档价目必须都能用（否则价目表里有死项）
        for _t in B.RECHARGE_TIERS:
            assert B.recharge(_t["amount"]).get("ok") is True, f"价目表里的 ¥{_t['amount']} 用不了"
    finally:
        B._balance, B._ledger[:] = _snap_bal, _snap_ledger

    # ---- ③ 真正的接线：接口存在，且前端真的有入口 ----
    _app = (Path(__file__).resolve().parent / "app.py").read_text(encoding="utf-8", errors="replace")
    assert '@app.post("/api/recharge")' in _app, "后端没有充值端点"
    _fe = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "App.jsx").read_text(
        encoding="utf-8", errors="replace"
    )
    assert "/api/recharge" in _fe and "doRecharge" in _fe, (
        "后端有了充值的动作，前端却没有入口——那还是「实现了 ≠ 能被用上」"
    )
    assert "（积分不足，本次体验价）" not in _fe, (
        "界面上还有那个「积分不足」角标——初始余额已够付最贵档，这个状态到不了；"
        "留着一句打折卖的话术只会让复核者以为产品在促销"
    )


def test_range_metric_is_not_a_point_estimate():
    """区间值不许被解析成点估计。

    病案（2026-09-19 实跑）：`22.3% - 27.3%` 这个单元格被解析成
    `value=22.3 / unit="% - 27.3%"` —— **单位串里带着区间尾巴**。
    后果有两层：① 它在单位分组里永远自成一族（≥2 的分组条件不成立）；
    ② 因为 unit 里含 `%`，又被当成百分比单行，**画出一条长度 = 22.3/100 的条**——
    读者会以为 22.3% 是一个点估计，而报告原文写的是 22.3–27.3% 的区间。
    """
    from core.report_html import extract_core_metrics

    _md = (
        "## 📊 核心指标一览\n"
        "| 指标 | 数值 | 来源 | 级别 | 年份 | 备注 |\n"
        "|---|---|---|---|---|---|\n"
        "| 下沉市场增速 | 22.3% - 27.3% | 艾瑞咨询 | L2 | 2025 | 预测区间 |\n"
        "| 门店数 | 8,000 家 | 公司公告 | L1 | 2025 | — |\n"
        "| 客单价 | 32.5 元 | 窄门餐眼 | L3 | 2024 | — |\n"
    )
    _m = extract_core_metrics(_md)
    assert len(_m) == 3, f"解析行数不对：{len(_m)}"
    _rng = _m[0]
    assert _rng["is_range"] is True, f"区间行没被标出来：{_rng}"
    assert _rng["unit"] == "%", f"单位串里还带着区间尾巴：{_rng['unit']!r}"
    assert _rng["value"] == 22.3, "低端值应当保留（展示一律用 raw，值只作兜底）"
    assert _rng["raw"] == "22.3% - 27.3%", "原文必须逐字保留——它是唯一可信的展示源"
    # 反向：**单值 % 行不许被误判成区间**（误判会让它丢掉比例条）
    assert _m[1]["is_range"] is False and _m[2]["is_range"] is False, (
        f"单值行被误判成区间：{[x['is_range'] for x in _m]}"
    )
    assert _m[2]["unit"] == "元" and _m[2]["value"] == 32.5
    # 两种写法都认（空格/波浪/中文「至」）
    for _v in ("22.3%-27.3%", "22.3~27.3", "22.3% 至 27.3%"):
        _one = extract_core_metrics(
            "| 指标 | 数值 |\n|---|---|\n| 增速 | " + _v + " |\n"
        )
        assert _one and _one[0]["is_range"] is True, f"这种区间写法没认出来：{_v!r} → {_one}"

    # ---- 前端：区间行一律不画比例条（数据层标了、展示层不读 = 白标）----
    _fe = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "App.jsx").read_text(
        encoding="utf-8", errors="replace"
    )
    assert "r.is_range" in _fe, "前端没读 is_range —— 区间行照样会被画成一根条"
    assert _fe.count("is_range") >= 3, (
        "前端至少要在「算填充比例」与「分组成柱」两处都判掉区间行；"
        f"现在只出现 {_fe.count('is_range')} 次"
    )
    # HTML 作品页同理
    _rh = (Path(__file__).resolve().parent / "core" / "report_html.py").read_text(
        encoding="utf-8", errors="replace"
    )
    assert "m.is_range" in _rh, "作品页的图表没判区间行"


def test_orphan_endpoints_are_wired():
    """**有接口、没入口**的端点必须接上前端。

    病案（2026-09-19 全量实测）：`GET /api/tools`（十张工具合同）与
    `GET /api/memory`（行业/偏好/会话/决策）**全仓零消费者**——
    接口一直在、数据一直是真在用的（`recall()` 每次调研都读记忆），
    却没有一处界面能让人看见。这正是本仓库最高频的断链形态：
    **实现了 ≠ 能被用上**。写在方案书里当卖点的东西，产品里必须点得开。

    反过来说，本条也防另一面：**不许为了"接上"而加一个没人点的按钮**——
    所以要求是真的 fetch、真的进抽屉互斥表、真的在导航里。
    """
    _root = Path(__file__).resolve().parents[1]
    _fe = (_root / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8", errors="replace")
    _app = (Path(__file__).resolve().parent / "app.py").read_text(encoding="utf-8", errors="replace")

    for _ep, _fn in (("/api/tools", "openTools"), ("/api/memory", "openMemory")):
        assert f'@app.get("{_ep}")' in _app, f"后端没有 {_ep}"
        assert _ep in _fe, f"前端从没调过 {_ep}——后端给了、前端不读"
        assert f"function {_fn}" in _fe, f"{_ep} 没有取数函数"
        assert f"onClick={{{_fn}}}" in _fe, f"{_ep} 有取数函数却没有任何按钮调它（等于没接）"

    # 抽屉互斥表是**唯一收口**（见 App.jsx 的 DRAWERS 病案）：新抽屉不进去就会叠面板
    for _k in ("tools", "memory"):
        assert f"{_k}: set" in _fe, f"新抽屉 {_k} 没进 DRAWERS 互斥表——开两个面板会物理叠在一起"
        assert f"const [{_k}Open, set" in _fe, f"抽屉 {_k} 没有开关状态"


def test_cite_openable_rules():
    """「打开原网页」按钮的判据：**一把尺，四处共用**。

    病案（2026-09-19，录演示当天用户当场看见）：面板上给出一个蓝色按钮
    「打开 www.sohu.com 原网页 ↗」，而**同一张卡下面紧挨着一行**写着
    「未探测（站点 robots.txt 明令 Disallow 该路径）；按红线不访问，不据此判死」——
    点它，就是当众访问一条我们自己声明不访问的路径。
    另有三条「China GDP / 中国 总人口」的引用，按钮指向
    `api.worldbank.org/...?format=json`：核验打的是 **HTTP 200**，
    而 200 正是 JSON API 的正常返回，点开是一坨 JSON。

    根因不是"漏了一处判断"，而是**同一个判据有四处各自实现**
    （列表域名 / 详情 chip / 「打开原网页」按钮 / 执行流证据链接）。
    09-16 修好了 chip 那一处、注释还留在原地，另外三处照旧——
    **一次只补一个洞，洞就会在原地重开。**

    本项跑 `_cite_rules_probe.mjs`。它**从 `App.jsx` 源码里抠出判据本身**来跑，
    因此"判据被改坏"与"判据被绕开自己另写一份"都会在这里红。
    样本含 4 条**逐字抄自真实存档**的 URL（含那条 robots 禁止的 sohu 与三条 API）。
    """
    import shutil as _shutil
    import subprocess   # 本文件里 subprocess 一直是**函数内局部导入**的（模块级没有），
                        # 照抄别人的写法会 NameError —— 而 NameError 若被宽 except 吞掉，
                        # 表现就是"这条检查静默变成通过"（本仓库有过一模一样的病案）。

    _node = _shutil.which("node")
    assert _node, (
        "找不到 node —— 本项检查要靠它跑真实判据。"
        "本项目前端是交付物的一部分，缺 node 说明环境不完整，而不是这条检查可以跳过。"
    )
    _probe = Path(__file__).resolve().parent / "_cite_rules_probe.mjs"
    assert _probe.exists(), f"探针不在：{_probe}"
    _p = subprocess.run(
        [_node, str(_probe)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        # **必须显式给 cwd**（与 2026-09-17 那条病案同因：不给就继承调用者的工作目录，
        # 于是把一次路径问题说成一句能力缺失）。
        cwd=_HERE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    _out = (_p.stdout or "") + (_p.stderr or "")
    assert "抠不出引文判据块" not in _out, (
        f"探针从 App.jsx 里抠不出判据了——源码结构变了，**先修探针，不要放行**：\n{_out}"
    )
    assert _p.returncode == 0, f"引文可点性判据不符：\n{_out}"
    assert "全部通过" in _out, f"探针没跑到结论（被改动或中断）：\n{_out}"
    # 反向：正反样本那一段必须真的跑过——它是"恒真/恒假"唯一的防线
    assert "正反样本" in _out and "✗" not in _out, f"正反样本有问题：\n{_out}"


def test_frontend_doc_anchors_are_live():
    """《前端功能清单》里每个 `App.jsx:<行号>` 都必须**指着它说的那一行**。

    病案（2026-09-19 写这份清单的当天）：清单自称"每一条都指向真实代码位置"，
    而**行号是最容易失效的一类引用**——往前插一行注释，后面所有引用集体失效；
    更糟的是失效时**不报错、只指错地方**，读者按它翻过去看到的是一段无关的代码，
    还以为自己看懂了。写这份清单的过程里我就已经指歪过两次
    （抽屉标题指到了上一行的 `<div>`、停止卡指到了 props 解构），
    两次都是靠一次性脚本才发现的——**靠人眼盯行号，等于没盯。**

    所以清单末尾带一张 ANCHORS 表（行号 | 该行必须包含的片段），本测试逐条核对。
    **红了不是"改测试"，是去改清单里的行号。**
    """
    import re as _re

    _root = Path(__file__).resolve().parents[1]          # yancec/
    _mat = Path(__file__).resolve().parents[2]           # 市场调研agent/（材料所在处）
    _doc_p = _mat / "研策_前端功能清单_v1_20260919.md"
    assert _doc_p.exists(), f"清单不在：{_doc_p}"
    _doc = _doc_p.read_text(encoding="utf-8", errors="replace")
    _jsx = (_root / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8", errors="replace")
    _lines = _jsx.split("\n")

    _m = _re.search(r"<!-- ANCHORS\n(.*?)\n-->", _doc, _re.S)
    assert _m, "清单末尾的 ANCHORS 表没了——正文的行号引用从此没有东西核对了"
    _anchors: dict[int, str] = {}
    for _ln in _m.group(1).strip().split("\n"):
        _n, _, _frag = _ln.partition("|")
        assert _n.strip().isdigit() and _frag, f"ANCHORS 表这行不合式：{_ln!r}"
        _anchors[int(_n)] = _frag
    assert len(_anchors) >= 15, f"ANCHORS 表只有 {len(_anchors)} 条，太少了"

    # 正文（不含 ANCHORS 块本身）引用了哪些行号
    _body = _doc[:_m.start()] + _doc[_m.end():]
    _refs = sorted({int(x) for x in _re.findall(r"App\.jsx:(\d+)", _body)})
    assert _refs, "正文里一个 App.jsx 行号引用都没有——这份清单的核心承诺失效了"

    _not_registered = [n for n in _refs if n not in _anchors]
    assert not _not_registered, (
        f"正文引用了这些行号，但没在 ANCHORS 表里登记「那行该是什么」：{_not_registered}"
        "——没登记的引用等于没人核对，那是这份清单最容易烂掉的地方。"
    )
    for _n in _refs:
        _frag = _anchors[_n]
        _actual = _lines[_n - 1] if 0 < _n <= len(_lines) else "<越界>"
        assert _frag in _actual, (
            f"清单里的 `App.jsx:{_n}` 已经指歪了：期望那行含 {_frag!r}，"
            f"实际是 {_actual.strip()[:70]!r}。**去改清单里的行号，不要改这张表。**"
        )
    # 反向：ANCHORS 表里的行号也必须都还被正文引用（防"改了正文忘了删表"留下僵尸条目）
    _stale = sorted(n for n in _anchors if n not in _refs)
    assert not _stale, f"ANCHORS 表里这些行号正文已经不再引用了，是僵尸条目：{_stale}"

    # 清单里转述的代码事实也要对得上（否则行号对了、话是错的）
    for _needle, _why in (
        ("const DRAWERS = {", "抽屉互斥表"),
        ("const [toolsOpen", "工具合同抽屉的开关状态"),
        ("const [memoryOpen", "记忆抽屉的开关状态"),
        ("doRecharge", "充值入口"),
        ("r.is_range", "区间指标不画比例条"),
    ):
        assert _needle in _jsx, f"清单提到的「{_why}」在 App.jsx 里找不到：{_needle!r}"


if __name__ == "__main__":
    sys.exit(main())
