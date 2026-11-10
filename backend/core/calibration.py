"""研策 · 引用蕴涵核验的假阳性率校准

## 为什么必须做这件事

支撑率是这套系统对外最硬的一个指标。但它有一个致命的形式弱点：
**分母里全是我们自己判的**。如果核验官手松，支撑率就会虚高，而读者无从察觉。

学界对这件事有明确结论——**HALLMARK（arXiv:2607.18360）指出：
决定引用核验器能否部署的是「假阳性率（FPR）」而不是召回率。**
理由**不是**"误报更危险"——原文说的是：**旗标一旦多为误报，就会沦为噪声、不再被相信**
（HALLMARK 区分的是旗标 mostly true catches 还是 mostly noise）。
⚠️ **方向不许说反**：同一篇摘要明写 *an undetected fabrication remains the costlier
error for the scientific record*——**漏检才是对记录代价更高的那个错误**。
所以本模块报 FPR，但**不主张误报比漏报更严重**；报的是"这个数决定旗标还能不能被信"。

所以本模块做一件很朴素的事：**造一组答案已知的样本，看核验官判错多少。**

## 样本怎么造的（这决定了结果能不能被信）

按核验官 prompt 里被明确告知的五个轴各造一对，**外加一对逻辑跳跃**：

| 轴 | 正例（应判 support） | 反例（**绝不能判 support**） |
|---|---|---|
| 数字 | 同值不同写法（`18,590` / `18590`） | **直接冲突**（18,590 / 16,581） |
| 单位 | 同值同单位 | **单位不同**（22.6 元 / 22.6 美元） |
| 时点 | 同期同口径 | **同数字不同年份**（2026 上半年 / 2025 上半年） |
| 口径 | 同范围 | **范围不同**（全国 / 一线城市） |
| 主体 | 同主体用简称（瑞幸 / 瑞幸咖啡） | **主体张冠李戴**（瑞幸数 / 星巴克数） |
| 逻辑 | — | **摘录与结论方向相反** |

**纪律：只用"清楚到没有争议"的样本。** 一个含模糊项的校准集，
算出来的"准确率"没有任何意义——因为它连"正确答案是什么"都不确定。
凡是我们自己都觉得两可的句子，一律不进这个集合。

## 两个口径都报

- **严格 FPR** = 反例中被判 `support`（= 直接被算进"通过率"）的比例 ← 这是污染支撑率的那个数
- **宽松 FPR** = 反例中被判 `support` 或 `partial` 的比例

只报严格口径会显得好看但不够诚实；只报宽松口径又混进了"其实判得挺谨慎"的情况。
两个都给，让读者自己选。

## 它不证明什么（必须写在输出里）

**样本是我们自己造的**——所以这是一次**自测**，不是第三方认证。
它能证明的是"核验官在这 16 个已知答案上判错了几个"，
不能证明"它在真实语料上的假阳性率就是这个数"。
真实语料的分布在样本量足够之前无法估计——这一点必须说清，否则又成了自说自话。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any

from core import safe_io

logger = logging.getLogger(__name__)

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "entailment_calibration.json",
)
# 校准结果的有效期。核验官是模型，模型换了/提示词改了这个数就作废——
# 拿一份半年前的校准结果给今天的支撑率背书，就是另一种自欺。
TTL_DAYS = 45

# group: "match" = 摘录确实支撑主张；"mismatch" = **绝不支撑**
CASES: list[dict[str, str]] = [
    # ---------------- 正例：应判 support ----------------
    dict(gid="M1", group="match", axis="数字", claim="2025年全国咖啡门店数约18.6万家",
         excerpt="截至2025年末，全国咖啡门店总数达 186,427 家，同比增长 12.4%。",
         publisher="行业统计", level="L3"),
    dict(gid="M2", group="match", axis="数字·写法", claim="瑞幸咖啡门店总数18590家",
         excerpt="瑞幸咖啡（LKNCY）门店总数为 18,590 家，其中自营门店占比较高。",
         publisher="公司财报", level="L2"),
    dict(gid="M3", group="match", axis="百分比", claim="县域市场咖啡渗透率为9.3%",
         excerpt="调研数据显示，县域市场的咖啡渗透率为 9.3%，低于一线城市。",
         publisher="咨询机构白皮书", level="L3"),
    dict(gid="M4", group="match", axis="时点·预测", claim="预计2026年中国GDP增速约5%",
         excerpt="预计 2026 年中国 GDP 增长 5.0%，保持温和增长态势。",
         publisher="国际组织预测", level="L1"),
    dict(gid="M5", group="match", axis="主体·简称", claim="瑞幸门店数已超过1.8万家",
         excerpt="瑞幸咖啡门店数已超过 1.8 万家，规模位居国内现制咖啡首位。",
         publisher="财经媒体", level="L3"),
    dict(gid="M6", group="match", axis="约数", claim="中国咖啡市场规模增速约45%",
         excerpt="中国咖啡市场保持高速增长，同比增长 45.2%。",
         publisher="行业研究", level="L3"),
    dict(gid="M7", group="match", axis="时点·期间", claim="2025年全年营收为249亿元",
         excerpt="2025 年度实现营业收入 249 亿元，同比增长 39.4%。",
         publisher="公司年报", level="L2"),
    dict(gid="M8", group="match", axis="口径·范围", claim="全国（不含港澳台）咖啡门店总数约18.6万家",
         excerpt="全国（不含港澳台）咖啡门店总数为 186,427 家。",
         publisher="行业统计", level="L3"),
    # ---------------- 反例：绝不能判 support ----------------
    dict(gid="X1", group="mismatch", axis="数字冲突", claim="某品牌门店总数为18,590家",
         excerpt="该品牌门店总数为 16,581 家，较上季度净减少 210 家。",
         publisher="公司财报", level="L2"),
    dict(gid="X2", group="mismatch", axis="单位不符", claim="该市场客单价为22.6元",
         excerpt="该市场的客单价为 22.6 美元，显著高于新兴市场平均水平。",
         publisher="咨询机构", level="L3"),
    dict(gid="X3", group="mismatch", axis="主体张冠李戴", claim="瑞幸咖啡门店总数达18,590家",
         excerpt="星巴克在中国内地运营约 7,000 家门店，其中直营门店占绝大多数。",
         publisher="公司公告", level="L2"),
    dict(gid="X4", group="mismatch", axis="时点错位", claim="2026年上半年该公司营收为100亿元",
         excerpt="2025 年上半年该公司实现营业收入 100 亿元。",
         publisher="财经媒体", level="L3"),
    dict(gid="X5", group="mismatch", axis="口径范围", claim="全国咖啡门店总数约18.6万家",
         excerpt="一线城市咖啡门店约 4.2 万家，占全国门店总数的两成左右。",
         publisher="行业统计", level="L3"),
    dict(gid="X6", group="mismatch", axis="量级不符", claim="中国咖啡市场规模约5005亿元",
         excerpt="中国咖啡市场规模约为 500 亿元，其中现磨咖啡占比持续提升。",
         publisher="行业研究", level="L3"),
    dict(gid="X7", group="mismatch", axis="完全无关", claim="县域市场咖啡渗透率为9.3%",
         excerpt="公司第五届董事会第九次会议审议通过了《关于聘任首席财务官的议案》。",
         publisher="公司公告", level="L2"),
    dict(gid="X8", group="mismatch", axis="逻辑跳跃", claim="县域市场竞争尚不激烈，因此不存在竞争壁垒",
         excerpt="县域市场咖啡门店数同比增长 40%，头部品牌正在加速下沉开店。",
         publisher="行业研究", level="L3"),
]

_AXIS_LABEL = {"match": "正例（应判支撑）", "mismatch": "反例（绝不能判支撑）"}


def _pair(c: dict[str, str]) -> dict[str, Any]:
    return {
        "claim": c["claim"],
        "excerpt": c["excerpt"],
        "publisher": c.get("publisher", "校准样本"),
        "title": c.get("publisher", "校准样本"),
        "level": c.get("level", "L3"),
        "verdict": "pending",
        "why": "",
        "quote": "",
        "_gid": c["gid"],
        "_group": c["group"],
        "_axis": c["axis"],
    }


def run(batch: int = 6) -> dict[str, Any] | None:
    """跑一次校准。返回 None = **未执行**（无模型/全部判定失败）——调用方须如实披露。"""
    from core.entailment import judge_pairs

    pairs = [_pair(c) for c in CASES]
    try:
        judged = judge_pairs(pairs, batch=batch)
    except Exception as e:
        logger.warning("校准执行失败: %s", e)
        return None
    rows = []
    for p in judged:
        _v = str(p.get("verdict") or "")
        if _v in ("pending", "unjudged", ""):
            continue  # 判不出来的不算样本（算进去会把"没判"混进准确率）
        rows.append({
            "gid": p.get("_gid"), "group": p.get("_group"), "axis": p.get("_axis"),
            "claim": p["claim"][:70], "verdict": _v,
            "why": str(p.get("why") or "")[:110],
        })
    if not rows:
        return None
    pos = [r for r in rows if r["group"] == "match"]
    neg = [r for r in rows if r["group"] == "mismatch"]
    if not pos or not neg:
        return None
    tp = sum(1 for r in pos if r["verdict"] == "support")
    fp = sum(1 for r in neg if r["verdict"] == "support")
    fp_loose = sum(1 for r in neg if r["verdict"] in ("support", "partial"))
    rep = {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "n_pos": len(pos), "n_neg": len(neg),
        "tpr": round(tp / len(pos), 4),                 # 真阳性率（该判支撑的判对了吗）
        "fpr": round(fp / len(neg), 4),                 # **严格假阳性率：反例被判 support**
        "fpr_loose": round(fp_loose / len(neg), 4),     # 宽松：反例被判 support 或 partial
        "rows": rows,
        "caveat": (
            "**这是一次自测，不是第三方认证**：16 个样本由本系统自己构造、答案事先确定，"
            "所以它证明的是「核验官在这 16 个已知答案上判错了几个」，"
            "**不能证明真实语料上的假阳性率就是这个数**——真实分布的估计需要足够大的样本量。"
            "另：核验官是模型，**换模型或改判定提示词后本结果即作废**"
            f"（有效期 {TTL_DAYS} 天）。"
        ),
        "method": "用与生产完全相同的判定函数（core.entailment.judge_pairs）跑固定样本集，"
                  "不另写一套判定逻辑——否则测的不是线上那个核验官",
    }
    return rep


def save(rep: dict[str, Any]) -> None:
    """存档校准结果。

    ## 离线闸门（2026-09-16 补 · 第五次复发病案的第十处）

    此前没有闸门，`_gate_probe.py` 实测确认离线模式下 `entailment_calibration.json`
    仍被改写。这一处的风险与其它写点不同——它覆盖的是**对外口径的物证**：
    K20 里那句「8 个反例、零事件 → 单侧 95% 置信上界 31.2%（分母是反例数 8，不是 16）」直接引用这份存档。
    一次离线/诊断性的重算若把存档刷新，材料里引用的那份结果就与盘上对不上了，
    而校准结果**带 45 天有效期**、材料里还写明了"换模型或改提示词即作废"。
    """
    if safe_io.is_offline():
        logger.info("离线模式：本次不覆盖生产校准存档")
        return
    # 记下**判定提示词**的指纹（2026-09-17）。
    # 为什么必须有它：本模块的存档里写着"换模型或改判定提示词后本结果即作废"，
    # 而在此之前**没有任何东西能检查这句话**——45 天 TTL 只是它的一个粗糙代理。
    # 提示词今天改了，存档在 45 天内仍会被 `load()` 读出来、印进每一份交付报告。
    # 也就是说：**我们承诺了一件做不到的事**，而它印在每份报告里。
    try:
        from core.prompt_registry import calibration_fingerprint

        rep = {**rep, "judge_prompt_fp": calibration_fingerprint()}
    except Exception as e:  # pragma: no cover - 指纹层是旁证，取不到不应阻断裂盘
        logger.debug("提示词指纹取不到（不阻断落盘）: %s", e)
        rep = {**rep, "judge_prompt_fp": ""}
    try:
        safe_io.makedirs(os.path.dirname(_PATH))
        with safe_io.open_prod(_PATH, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
    except Exception as e:
        logger.warning("校准结果落盘失败: %s", e)


def prompt_drift(rep: dict[str, Any] | None) -> str:
    """判定提示词有没有变过 → `""`（没变/无法判断）| `"changed"` | `"unknown"`。

    三态而不是布尔，是因为**"不知道"必须与"没变"分开**：
    存档早于指纹机制（2026-09-14 那份就没有 `judge_prompt_fp`），
    那时我们**确实无法判断**提示词是否变过——**不能默认它没变**，
    也不能因此把一份真实跑出来的校准判死。所以第三种状态叫 `unknown`，
    并且它会**印在报告附录里**（见 `format_appendix_md`），而不是被吞掉。
    """
    if not rep:
        return ""
    fp = str(rep.get("judge_prompt_fp") or "")
    if not fp:
        return "unknown"
    try:
        from core.prompt_registry import calibration_fingerprint

        return "" if fp == calibration_fingerprint() else "changed"
    except Exception:
        return "unknown"


def invalid_reason(max_age_days: int = TTL_DAYS) -> str:
    """存档存在但不能用 → 给出**理由**（`""` = 可用或压根没有存档）。

    为什么要一个单独的入口：`load()` 只回 `None`，调用方无法区分
    「从没跑过」「过期了」「提示词改了」——于是**三种情况在报告里长得一模一样：
    什么都不印**。而"不印"读者会读成"这一节本来就没有"，
    **这正是一种静默消失**（本仓库对"静默"的纪律是：可以没有，但不许不说）。
    """
    try:
        with open(_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return ""
    if not isinstance(d, dict) or not d.get("ran_at"):
        return ""
    try:
        if datetime.now() - datetime.fromisoformat(d["ran_at"]) > timedelta(days=max_age_days):
            return "expired"
    except Exception:
        return "expired"
    if prompt_drift(d) == "changed":
        return "prompt_changed"
    return ""


def load(max_age_days: int = TTL_DAYS) -> dict[str, Any] | None:
    """读**未过期**的校准结果。过期/不存在 → None（宁可不说，也不拿旧数背书）。"""
    try:
        with open(_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or not d.get("ran_at"):
            return None
        _t = datetime.fromisoformat(d["ran_at"])
        if datetime.now() - _t > timedelta(days=max_age_days):
            return None
        # 判定提示词变了 → 这份校准测的**已经不是今天的核验官**，与过期同等处理。
        # （承诺见模块 docstring："换模型或改判定提示词后本结果即作废"。）
        if prompt_drift(d) == "changed":
            return None
        return d
    except Exception:
        return None


def format_appendix_md(rep: dict[str, Any] | None = None) -> str:
    """校准结果 → 报告附录片段（接在引用蕴涵核验之后）。无结果/已过期 → 空串。"""
    r = rep if rep is not None else load()
    if not r:
        # **作废不许静默消失**（2026-09-17 补）。
        # 病案：`load()` 只回 None，于是「从没跑过」「过期了」「提示词改了」
        # 三种情况在报告里长得一模一样——**什么都不印**。
        # 而"不印"读者会读成"这一节本来就没有"，**那是一种静默消失**。
        # 本仓库对静默的纪律是：可以没有，但不许不说。
        _why = invalid_reason()
        if _why == "prompt_changed":
            return (
                "\n### 🎯 核验器自校准\n\n"
                "> ⚠️ **校准存档已作废**：**判定提示词在上次校准之后被修改过**。\n"
                "> 换提示词等于换了一个核验官，原校准不再代表今天这一个——"
                "**因此本报告不引用该校准结果**。重跑 `_calibrate_entailment.py` 即恢复。\n"
            )
        if _why == "expired":
            return (
                "\n### 🎯 核验器自校准\n\n"
                f"> ⚠️ **校准存档已过期**（有效期 {TTL_DAYS} 天），本报告不引用该校准结果。重跑即恢复。\n"
            )
        return ""
    _fp = f"{r['fpr'] * 100:.1f}%"
    _fl = f"{r['fpr_loose'] * 100:.1f}%"
    # **零事件下的单侧 95% 精确二项上界**（2026-09-17 补，K20 要求"必须带区间"）。
    #
    # 病案：本节此前**只报点估计**，而 K20 明文写着"并报假阳性率校准，且**必须带区间**"——
    # 也就是说**交付报告附录在违反我们自己的口径**（口径只写进了材料，没写进产物）。
    # 更重的是**分母**：FPR 的定义域是**反例**，所以分母只能是 `n_neg`。
    # 上一版对外写的是「16 个样本、零事件 → 上界 17.1%」——17.1% = 1 − 0.05^(1/16)，
    # 那是**把 8 个正例也塞进了分母**。正确的（n_neg=8）是
    #     1 − 0.05^(1/8) = 0.312344 → **31.2%**
    # 两个数差 14 个百分点，而错的那个**方向恰好对我们有利**（把区间收窄了 45%）。
    # 这正是本仓库反复出问题的那一类：不是算错，是算了另一个数并挂了好看的标签。
    _n_neg = int(r.get("n_neg") or 0)
    _ub = ""
    if _n_neg > 0 and not r.get("fpr"):
        # 判据用 `fpr == 0`（零事件），不用 `fp` 计数——存档里**没有 `fp` 这个键**，
        # 用 `.get("fp") or 0` 会在"有假阳性"时也判成零事件，把区间算错。
        import math as _math

        _p = 1.0 - (0.05 ** (1.0 / _n_neg))
        _ub = (
            f"零事件下**单侧 95% 精确二项（Clopper–Pearson）置信上界 "
            f"{_p * 100:.1f}%**（分母 = {_n_neg} 个**反例**；"
            f"1 − 0.05^(1/{_n_neg}) = {_p:.4f}）"
        )
    lines = [
        "",
        "### 🎯 核验器自校准（假阳性率 —— 决定核验结果能不能被信的那个数）",
        "",
        f"在一组**答案已知**的样本上实测（{r['n_pos']} 正例 + {r['n_neg']} 反例，"
        f"核验时点 {r['ran_at'][:10]}）："
        f"**严格假阳性率 {_fp}**（反例被判「明确支撑」）· "
        f"宽松假阳性率 {_fl}（反例被判「支撑」或「部分支撑」）· "
        f"真阳性率 {r['tpr'] * 100:.1f}%。",
        "",
    ]
    # **"不知道"要说出来**（2026-09-17）：存档早于指纹机制时，我们**无法判断**
    # 判定提示词有没有变过。这一行不是免责，是**把不确定性印出来**——
    # 它会在下一次重跑校准后自动消失（那时指纹就写进去了）。
    if prompt_drift(r) == "unknown":
        lines += [
            "> ⚠️ **本存档早于提示词指纹机制**（2026-09-17 起才有）："
            "因此**无法核对判定提示词自那次校准以来是否被改过**。"
            "重跑一次校准即可写入指纹、此行随之消失。",
            "",
        ]
    # 提示词整集指纹：让读者能判断"这份报告是哪一套指令下跑出来的"。
    try:
        from core.prompt_registry import prompt_set as _pset

        lines += [f"> 提示词整集指纹 `prompt_set = {_pset()}`（15 条指令正文的内容哈希）。", ""]
    except Exception:
        pass
    # 区间必须与点估计**并排**（K20）。只报「0.0%」会被读成"率就是 0"，
    # 而零事件在 n=8 上的真实含义是"上界 31%"——**把区间印出来是在替读者纠错**。
    if _ub:
        lines.append(f"> {_ub}。")
    lines += [
        "> **区间比点估计重要**：零事件不等于零风险。8 个反例上一个都没判错，"
        "只能说明「在这 8 个上没出错」；**上界 31.2% 才是这组样本真正排除掉的东西**。"
        "报点估计而不报区间，等于用样本量换一个好看的数字。",
        "",
        "> **为什么校准比准确率本身重要**：HALLMARK（arXiv:2607.18360）指出，"
        "**决定引用核验器能否部署的是假阳性率而不是召回率**——"
        "理由不是「误报更危险」，而是**旗标一旦多为误报，就会沦为噪声、不再被相信**"
        "（原文区分的是旗标 mostly true catches 还是 mostly noise）。"
        "⚠️ **方向不许说反**：同一篇摘要明写 an undetected fabrication remains the "
        "costlier error for the scientific record——**漏检才是对记录代价更高的那个错误**。"
        "所以本节报的是「它会不会把错的判成对的」，但**不主张误报比漏报更严重**。",
        "",
        "> **本组样本的构成比与真实语料的差距（必读）**：本组 16 条里**一半是反例**"
        "（8:8），而**真实语料里「引用与原文不符」的天然比例远低于此**——"
        "公开基线里引用不支撑结论的比例是两位数百分比（DeepTRACE 各系统 40–80% 的引用准确率"
        "意味着不准确率同样可观，但那与「每条都可能是伪造」是两回事）。"
        "**构造比失衡的直接后果**：上界 31.2% 是在「8 个反例」上算的，"
        "**它描述的是这个核验官在这类样本上的行为，不能直接当成生产环境里的期望错误率**——"
        "真实基率下，同一个核验官的表现必须重新测。这条自陈来自 HALLMARK 的做法"
        "（其基准自陈「62.5% 为人为幻觉」，并明说与真实语料的差距）。",
        "",
        "> **样本构造**：按核验官被明确告知的五个轴（数字 / 单位 / 时点 / 口径 / 主体）"
        "各造一对，外加一对逻辑跳跃。**只用清楚到没有争议的样本**——"
        "一个含模糊项的校准集，算出来的准确率没有意义。",
        "",
        "| 编号 | 轴 | 组 | 判定 | 说明 |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for row in r["rows"]:
        _mark = {"support": "✅ support", "partial": "⚠️ partial",
                 "unrelated": "❌ unrelated", "contradict": "❌ contradict"}.get(
                     row["verdict"], row["verdict"])
        _ok = (row["group"] == "match") == (row["verdict"] == "support")
        lines.append(
            f"| {row['gid']} | {row['axis']} | {'正例' if row['group'] == 'match' else '反例'} | "
            f"{_mark} {'✔' if _ok else '**✘ 判错**'} | {row['why'][:70]} |"
        )
    lines += [
        "",
        f"> ⚠️ {r['caveat']}",
        "",
        "> **这一节不证明什么**：它**不证明**真实语料上的假阳性率等于上面这个数，"
        "也**不证明**核验官在样本外同样可靠。它证明的是：**我们把自己的核验器拿去测过，"
        "并把测得的错判逐条列了出来。**",
    ]
    return "\n".join(lines)


def describe() -> str:
    return (
        "引用蕴涵核验的假阳性率校准：用与生产完全相同的判定函数，跑一组答案已知的正反例，"
        "给出严格/宽松假阳性率与逐条判定，并标注为**自测**、带有效期。"
        "依据 HALLMARK（arXiv:2607.18360）：决定核验器能否部署的是假阳性率而非召回率。"
    )
