"""研策 · HTML 研报交付物（作品页：封面 + 决策英雄卡 + 指标可视化 + 折叠审计附录）

把 markdown 报告转成"一份能递出去的研报作品"（借鉴 Manus 交付物 = 独立成品，而非聊天长文）：
- 封面：品牌 + 用户问题/标题 + 决策判定徽章 + 证据/来源条数
- hero：决策卡（Go/No-Go/Validate）+ 验证任务 + 监控条件
- 核心指标：先渲染纯 CSS stat tile（离线/断 CDN 首屏也有作品感），再按"同单位组"画 ECharts
  线性小图（同量纲才可比）；单位全异构时才退到对数轴单图；ECharts 缺失 → 表格兜底
- 正文只渲染 main 段，审计附录（质检/置信度台账/缺口/引文核验/冲突/规则自检/对抗）
  折叠进 <details>，点开展开完整审计；marked 缺失 → 纯文本兜底

结构化指标（extract_core_metrics）同时供给：
- 后端 node_deliver → done.result.metrics → 前端聊天交付物头部（CSS 横向条）
- 本作品页 meta（前端导出时把已有 metrics 传入；外部调用不带 meta 也自洽，从 md 再解析）
"""
from __future__ import annotations

import html
import json
import re
from typing import Any

# 不可点/泛化的"来源机构"标签：指标表来源列出现即判"待核"（数字背书纪律）
_JUNK_SOURCE = (
    # ⚠️ **与另外两张表对齐**（2026-09-17 实跑病案）：这张表此前**一条「反方检索」都没有**，
    # 而 `graph._GENERIC_SRC` 只收了「反方检索**数据**」、`core/industry_kb._LOW_TRUST` 收的是
    # 「反方检索**资料**」——**同一个概念三张表、三种内容，谁也没对齐谁。**
    # 结果那一跑的核心指标表上印着「28台 | 反方检索资料 | L2媒体与咨询」，
    # 而表下面挂着的"来源标注修正"提示举的例子恰是「反方检索数据」。
    # 教训不是"又漏了一个词"，是**同一个概念不该有三张手工维护的词表**。
    "反方检索资料", "反方检索数据", "反方检索", "反方观点",
    "宏观经济数据", "宏观经济", "行业数据", "行业基线数据", "行业基线", "行业估算",
    "行业观察", "联网搜索", "网络检索", "实时检索", "实时检索数据", "搜索引擎",
    "行业资料", "白皮书", "行业分析", "数据整理", "综合整理", "公开资料",
    "历史基线", "基线数据",
)

# 审计附录标题关键词（标题行命中即从该处把正文切成 main/audit）。
# 用"多字短语"避免与正文模块标题（宏观环境/市场规模/竞争格局…）撞车。
AUDIT_HEADING_PATTERNS = (
    "质检与可验证性", "结论置信度披露", "证据链台账", "数据缺口披露",
    "引文核验", "数据冲突日志", "规则层自检", "对抗性数字校验",
    # 自主决策链是"过程披露"，与其它审计附录同级：必须折叠，不能混进正文被逐字吐出来
    "自主决策链",
    "多角色辩论",
    "AI 使用与透明度", "研究假设与验证", "指标一致性核对",
    # 「结论状态·双源配对矩阵」这类诊断表以前靠"排在别的附录标题之后"被顺带切掉，
    # 顺序一变就漏进来——实测它会让指标台账把"✅ 满足 | 51"当成指标、报出捏造的冲突。
    "结论状态", "配对矩阵",
)

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__</title>
__AIGC_META__
<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<style>
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; margin: 0; background: #eef1f5; color: #1f2328; }
  .wrap { max-width: 960px; margin: 0 auto; padding: 22px 18px 90px; }
  .card { background: #fff; border-radius: 14px; box-shadow: 0 2px 14px rgba(0,0,0,.06); }

  /* 封面 */
  .cover { padding: 26px 30px 22px; margin-bottom: 14px; border-top: 4px solid #3b82f6; }
  .brand { color: #2563eb; font-weight: 700; font-size: 13px; letter-spacing: .6px; }
  .cover h1 { margin: 10px 0 6px; font-size: 25px; line-height: 1.4; }
  .cover .q { color: #6b7280; font-size: 13px; }
  .cover-meta { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; align-items: center; }
  .chip { display: inline-flex; align-items: center; gap: 5px; padding: 4px 11px; border-radius: 999px; font-size: 12px; background: #f3f4f6; color: #374151; }
  .chip.go { background: #d1fae5; color: #065f46; font-weight: 600; }
  .chip.nogo { background: #fee2e2; color: #991b1b; font-weight: 600; }
  .chip.validate { background: #fef3c7; color: #92400e; font-weight: 600; }
  .chip .lv { background: #e5e7eb; border-radius: 6px; padding: 0 5px; font-size: 11px; color: #4b5563; }

  /* hero */
  .hero { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 14px; }
  .hero-card { flex: 1 1 280px; padding: 14px 18px; }
  .hero-card h3 { margin: 0 0 8px; font-size: 12px; color: #6b7280; font-weight: 700; letter-spacing: .4px; }
  .verdict-big { font-size: 26px; font-weight: 800; }
  .verdict-big.go { color: #059669; } .verdict-big.nogo { color: #dc2626; } .verdict-big.validate { color: #d97706; }
  .task-mini { font-size: 13px; color: #374151; margin: 4px 0; line-height: 1.6; }
  .task-mini b { color: #111827; }
  .cond-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
  .cond { background: #eff6ff; color: #1d4ed8; padding: 3px 9px; border-radius: 999px; font-size: 12px; }

  /* stat tiles */
  .stat-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 10px; margin-bottom: 14px; }
  .stat { padding: 12px 14px; }
  .stat .k { font-size: 12px; color: #6b7280; line-height: 1.35; }
  .stat .v { font-size: 21px; font-weight: 800; color: #111827; margin: 4px 0 2px; }
  .stat .v .u { font-size: 12px; color: #6b7280; font-weight: 500; margin-left: 2px; }
  .stat .s { font-size: 11px; color: #9ca3af; display: flex; gap: 6px; align-items: center; }
  .bar { height: 4px; border-radius: 3px; background: #eef2f7; margin-top: 8px; overflow: hidden; }
  .bar i { display: block; height: 100%; background: #3b82f6; border-radius: 3px; }

  /* charts */
  #charts { display: flex; flex-direction: column; gap: 14px; margin-bottom: 14px; }
  .chart-box { padding: 14px 18px; }
  .chart-box h3 { margin: 0 0 6px; font-size: 14px; color: #444; }
  .chart-inner { width: 100%; height: 300px; }

  /* 正文 + 审计折叠 */
  article#report { padding: 26px 30px; }
  #report h1, #report h2 { border-bottom: 1px solid #eceef1; padding-bottom: 6px; margin-top: 26px; }
  #report h2 { font-size: 18px; }
  #report h3 { font-size: 15px; }
  #report table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 13px; }
  #report th, #report td { border: 1px solid #e5e7eb; padding: 6px 10px; text-align: left; }
  #report th { background: #f3f4f6; font-weight: 600; }
  #report blockquote { border-left: 3px solid #d1d5db; margin: 10px 0; padding: 6px 14px; color: #6b7280; background: #f9fafb; border-radius: 0 6px 6px 0; }
  #report code { background: #f3f4f6; padding: 1px 5px; border-radius: 4px; font-size: 12px; }
  #report a { color: #2563eb; }
  .md-fallback { white-space: pre-wrap; font-size: 13px; }

  details.audit-fold { margin-top: 14px; overflow: hidden; }
  details.audit-fold summary { cursor: pointer; padding: 14px 18px; font-weight: 700; color: #374151; user-select: none; list-style: none; display: flex; align-items: center; gap: 6px; font-size: 13px; }
  details.audit-fold summary::-webkit-details-marker { display: none; }
  details.audit-fold summary .arr { display: inline-block; transition: transform .15s; color: #6b7280; }
  details.audit-fold[open] summary .arr { transform: rotate(90deg); }
  details.audit-fold .inner { padding: 0 20px 18px; }
  details.audit-fold .inner #audit { line-height: 1.75; font-size: 13px; }
  details.audit-fold .inner #audit table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 12px; }
  details.audit-fold .inner #audit th, details.audit-fold .inner #audit td { border: 1px solid #e5e7eb; padding: 5px 8px; text-align: left; }
  details.audit-fold .inner #audit th { background: #f3f4f6; font-weight: 600; }
  @media print { details.audit-fold { display: block; } details.audit-fold summary { display: none; } body { background: #fff; } .card { box-shadow: none; } }
</style>
</head>
<body>
<div class="wrap">
  <div class="card cover">
    <div class="brand">研策 · 市场调研数字员工</div>
    <h1>__COVER_TITLE__</h1>
    <div class="q" id="coverQuestion"></div>
    <div class="cover-meta" id="coverMeta"></div>
  </div>

  <div class="hero" id="hero"></div>
  <div class="stat-grid" id="stats"></div>
  <div id="charts"></div>

  <article class="card" id="report"></article>
  <details class="card audit-fold" id="auditFold">
    <summary><span class="arr">▶</span> 审计附录（质检 / 证据链台账 / 引文核验 / 数据缺口 / 冲突 / 规则自检 / 对抗校验）— 点击展开</summary>
    <div class="inner"><article id="audit"></article></div>
  </details>
</div>

<script type="text/plain" id="main-md">__MAIN_MD__</script>
<script type="text/plain" id="audit-md">__AUDIT_MD__</script>
<script type="text/plain" id="meta-json">__META_JSON__</script>
<script>
  var META = __META_JSON__;
  var METRICS = META.metrics || [];
  var VERDICT = META.verdict || '';
  var esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); };
  var vInfo = { 'Go': { t: '可行 · Go', c: 'go' }, 'No-Go': { t: '放弃 · No-Go', c: 'nogo' }, 'Validate': { t: '审慎验证 · Validate', c: 'validate' } };

  function md2html(md) {
    var esc = function (s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); };
    var inline = function (s) {
      s = s.replace(/<ref>(\d+)<\/ref>/g, '[<span class="ref">$1</span>]').replace(/<\/?span[^>]*>/g, '');
      s = s.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
      s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
      s = s.replace(/!\[([^\]]*)\]\(([^)]*)\)/g, '$1');
      s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
      return s;
    };
    var lines = (md || '').split('\n');
    var out = [];
    var inUl = false;
    function closeUl() { if (inUl) { out.push('</ul>'); inUl = false; } }
    var i = 0;
    while (i < lines.length) {
      var s = (lines[i] || '').trim();
      if (!s) { i++; closeUl(); continue; }
      if (/^#{1,6}\s+\S/.test(s)) {
        closeUl();
        var lvl = s.match(/^#+/)[0].length;
        var tag = lvl <= 3 ? 'h2' : 'h3';
        out.push('<' + tag + '>' + inline(s.replace(/^#+\s*/, '')) + '</' + tag + '>');
      } else if (/^\|/.test(s) && s.split('|').length >= 3) {
        // 收集连续表格行，首行作表头 → 规范 <table>
        closeUl();
        var block = [];
        while (i < lines.length && /^\|/.test(lines[i].trim()) && lines[i].split('|').length >= 3) { block.push(lines[i].trim()); i++; }
        var parsed = [];
        for (var b = 0; b < block.length; b++) {
          var cs = block[b].replace(/^\||\|$/g, '').split('|');
          var isSep = cs.every(function (c) { var t = (c || '').trim(); return !t || /^:?-{2,}:?$/.test(t); });
          if (isSep) continue;
          parsed.push(cs.map(function (c) { return (c || '').trim(); }));
        }
        if (parsed.length) {
          out.push('<table><thead><tr>' + parsed[0].map(function (c) { return '<th>' + inline(c) + '</th>'; }).join('') + '</tr></thead><tbody>');
          for (var r = 1; r < parsed.length; r++) { out.push('<tr>' + parsed[r].map(function (c) { return '<td>' + inline(c) + '</td>'; }).join('') + '</tr>'); }
          out.push('</tbody></table>');
        }
      } else if (/^[-*]\s+/.test(s)) {
        if (!inUl) { inUl = true; out.push('<ul>'); }
        out.push('<li>' + inline(s.replace(/^[-*]\s+/, '')) + '</li>');
        i++;
      } else if (/^>\s?/.test(s)) {
        closeUl();
        out.push('<blockquote>' + inline(s.replace(/^>\s?/, '')) + '</blockquote>');
        i++;
      } else if (/^(-{3,}|\*{3,})$/.test(s)) {
        closeUl(); out.push('<hr/>'); i++;
      } else {
        closeUl();
        out.push('<p>' + inline(s) + '</p>');
        i++;
      }
    }
    closeUl();
    return out.join('\n');
  }

  function md2el(root, md) {
    var el = document.getElementById(root);
    if (window.marked && typeof marked.parse === 'function') { el.innerHTML = marked.parse(md || ''); }
    else { el.innerHTML = md2html(md || ''); }   // 离线/CDN 失效兜底：表格与正文仍排版，不吐 md 源码
  }

  // 封面 meta
  document.getElementById('coverQuestion').textContent = (META.question && META.question !== META.title) ? ('调研问题：' + META.question) : '';
  var coverMeta = document.getElementById('coverMeta');
  if (VERDICT) { var vi = vInfo[VERDICT] || { t: VERDICT, c: 'validate' }; coverMeta.appendChild(chip(vi.t, vi.c)); }
  // 「0 条」与「没统计」不能同形（2026-09-16）：`|| '—'` 把 **0 也当成缺失**，
  // 于是"本次 0 条证据"在作品页封面上印成"— 条证据"，读起来像"我们没数"。
  // 0 是一个读数（而且是一个**该被追问**的读数），不是缺席。
  coverMeta.appendChild(chip((META.evidence_count == null ? '—' : META.evidence_count) + ' 条证据', ''));
  coverMeta.appendChild(chip((META.citation_count == null ? '—' : META.citation_count) + ' 条数据来源', ''));
  coverMeta.appendChild(chip('生成 ' + new Date().toLocaleDateString('zh-CN'), ''));
  function chip(t, c) { var s = document.createElement('span'); s.className = 'chip ' + (c || ''); s.textContent = t; return s; }

  // hero：决策卡 + 验证任务 + 监控
  var hero = document.getElementById('hero');
  var vCard = document.createElement('div'); vCard.className = 'card hero-card';
  vCard.innerHTML = '<h3>决策判定</h3>' + (VERDICT ? '<div class="verdict-big ' + (vInfo[VERDICT] ? vInfo[VERDICT].c : 'validate') + '">' + esc((vInfo[VERDICT] ? vInfo[VERDICT].t : VERDICT)) + '</div>' : '<div style="color:#9ca3af;font-size:14px">本报告未走决策框架</div>');
  hero.appendChild(vCard);
  if (META.tasks && META.tasks.length) {
    var tCard = document.createElement('div'); tCard.className = 'card hero-card';
    var lines = META.tasks.slice(0, 3).map(function (t) {
      return '<div class="task-mini"><b>' + esc(t.id || '') + '</b> ' + esc(t.task || '') + (t.kpi ? ' <span style="color:#6b7280">· KPI ' + esc(t.kpi) + '</span>' : '') + '</div>';
    }).join('');
    tCard.innerHTML = '<h3>下一步 · 验证任务（共 ' + META.tasks.length + '）</h3>' + lines;
    hero.appendChild(tCard);
  }
  if (META.conditions && META.conditions.length) {
    var cCard = document.createElement('div'); cCard.className = 'card hero-card';
    cCard.innerHTML = '<h3>持续监控</h3><div class="cond-chips">' + META.conditions.map(function (c) { return '<span class="cond">' + esc(c) + '</span>'; }).join('') + '</div>';
    hero.appendChild(cCard);
  }

  // stat tiles（纯 CSS，离线首屏也有作品感）
  var stats = document.getElementById('stats');
  METRICS.forEach(function (m) {
    var tile = document.createElement('div'); tile.className = 'card stat';
    tile.innerHTML = '<div class="k">' + esc(m.name || '') + '</div><div class="v">' + esc(m.raw || (m.value + '')) + '</div><div class="s"><span>' + esc(m.source || '—') + '</span>' + (m.level ? '<span class="lv">' + esc(m.level) + '</span>' : '') + (m.year ? '<span>' + esc(m.year) + '</span>' : '') + '</div>';
    stats.appendChild(tile);
  });

  // 指标对比：同单位组 ≥2 → 各画一张线性小图；全异构且总 ≥2 → 退对数轴单图
  var chartEl = document.getElementById('charts');
  function buildChart(list, unit, logAxis) {
    var box = document.createElement('div'); box.className = 'card chart-box';
    box.innerHTML = '<h3>📊 ' + esc(unit || '核心指标') + '对比</h3><div class="chart-inner"></div>';
    chartEl.appendChild(box);
    var inner = box.querySelector('.chart-inner');
    if (window.echarts) {
      var chart = echarts.init(inner);
      chart.setOption({
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: function (ps) { var m = list[ps[0].dataIndex] || {}; return '<b>' + esc(m.name || '') + '</b><br/>数值：' + esc(m.raw || '') + '<br/>来源：' + esc(m.source || '—'); } },
        grid: { left: 90, right: 40, top: 30, bottom: 44 },
        xAxis: { type: 'category', data: list.map(function (m) { return m.name; }), axisLabel: { rotate: 20, fontSize: 11 } },
        yAxis: { type: logAxis ? 'log' : 'value' },
        series: [{ type: 'bar', data: list.map(function (m) { return m.value; }), barMaxWidth: 52,
          itemStyle: { color: '#3b82f6', borderRadius: [4, 4, 0, 0] },
          label: { show: true, position: 'top', fontSize: 10, formatter: function (p) { return esc((list[p.dataIndex] || {}).raw || ''); } } }]
      });
      window.addEventListener('resize', function () { chart.resize(); });
    } else {
      var tbl = document.createElement('table');
      tbl.innerHTML = '<tr><th>指标</th><th>数值</th><th>来源</th></tr>' + list.map(function (m) { return '<tr><td>' + esc(m.name || '') + '</td><td>' + esc(m.raw || '') + '</td><td>' + esc(m.source || '—') + '</td></tr>'; }).join('');
      inner.outerHTML = '<div></div>'; box.appendChild(tbl);
    }
  }
  var groups = {};
  // **区间行不进图**（见 extract_core_metrics 的 is_range 病案）：`22.3% - 27.3%` 画一根
  // 到 22.3 的柱子，等于把区间说成点估计。它们仍在上面的 stat tile 与降级表里按原文显示。
  METRICS.forEach(function (m) { if (!(m.value > 0) || m.is_range) return; var u = m.unit || '_'; (groups[u] = groups[u] || []).push(m); });
  var sameUnit = [];
  Object.keys(groups).forEach(function (u) { if (groups[u].length >= 2) sameUnit.push(groups[u]); });
  var chartable = METRICS.filter(function (m) { return m.value > 0 && !m.is_range; });
  if (sameUnit.length) { sameUnit.forEach(function (list) { buildChart(list, list[0].unit, false); }); }
  else if (chartable.length >= 2) { buildChart(chartable, '全部', true); }

  // 正文 main + 审计折叠
  var mdText = document.getElementById('main-md').textContent;
  md2el('report', mdText);
  var auditMd = document.getElementById('audit-md').textContent;
  if (auditMd && auditMd.trim()) { md2el('audit', auditMd); }
  else { document.getElementById('auditFold').style.display = 'none'; }
</script>
</body>
</html>"""


def extract_core_metrics(md: str, max_rows: int = 10) -> list[dict[str, Any]]:
    """从报告"核心指标一览表"提取**结构化指标行**（供给交付物头部与作品页）。

    schema: {name, value(数字), unit, raw(原单元格), source, level, year, note}
    容错：不硬锚 '## 📊' 标题——命中"表头含『指标』与『数值』"的数据表即开始解析
    （LLM 偶发 emoji 变体/空行）；'|---|' 分隔行跳过；遇非表格行停。
    value/unit 分离：单元格去千分位取首个数字，余串 trim 首尾空白/标点即为 unit
    （覆盖 亿元/万亿元/万元/元/万家/家/万人/人/座/杯/次/单/%/百分点/周…）。
    展示一律用 raw，不做显示层二次单位换算。
    """
    if not md:
        return []
    lines = (md or "").split("\n")
    metrics: list[dict[str, Any]] = []
    started = False
    for ln in lines:
        s = ln.strip()
        if not s.startswith("|"):
            if started:
                break  # 表格结束
            continue
        cells = [c.strip().strip("*`").strip() for c in s.strip("|").split("|")]
        if not cells:
            continue
        if not started:
            # 表头判定：同时含"指标"与"数值"（分隔行 |---| 与说明行跳过）
            if len(cells) >= 2 and any("指标" in c for c in cells[:2]) and any("数值" in c for c in cells[:2]):
                started = True
            continue
        # 跳过 --- 分隔行
        if all(re.fullmatch(r"-{2,}|:{2,}-+|", c or "") for c in cells):
            continue
        if len(cells) < 2 or not cells[0]:
            continue
        name = cells[0][:16]
        val_text = cells[1]
        # —— 区间值：`22.3% - 27.3%` / `22.3%-27.3%` / `22.3~27.3` ——
        # 病案（2026-09-19）：从前只取"首个数字"，于是 `22.3% - 27.3%` 被解析成
        # value=22.3、unit=`"% - 27.3%"` —— **单位串里带着区间尾巴**。后果是这一行
        # 在单位分组里自成一族（永远 ≥2 的分组条件不成立），然后因为 unit 里含 `%`
        # 被当成百分比单行，**画出一条长度 = 22.3/100 的条**：读者会以为这是一个点估计。
        # 现在如实标成区间：unit 取**末位数字之后**的单位（`%`），并打 `is_range` 标记；
        # 展示层据此**不画比例条**——区间没有单一量级，画条就是把区间说成点。
        _rng = re.search(
            r"([\d,]+(?:\.\d+)?)\s*(?:%|％)?\s*(?:[-~～—至到]|--)\s*([\d,]+(?:\.\d+)?)",
            val_text,
        )
        m = re.search(r"([\d,]+(?:\.\d+)?)", val_text)
        if not m:
            continue  # 非数值行（备注/空行）跳过
        num = float(m.group(1).replace(",", ""))
        if num <= 0:
            continue
        is_range = False
        if _rng and abs(float(_rng.group(2).replace(",", "")) - num) > 1e-9:
            is_range = True
            unit = re.sub(r"[，,；;、()（）~～\s]*$", "", val_text[_rng.end():])
        else:
            unit = re.sub(r"[，,；;、()（）~～\s]*$", "", val_text[m.end():])
        unit = re.sub(r"^[，,；;、()（）~～\s]*", "", unit)
        # —— 数据背书纪律：来源列只留"可点机构名"，泛标签(宏观经济数据/行业数据等)一律判"待核"；
        #    数据级别只留 L1-L5 短标签，避免展示层截断成"L2媒体与咨"。 ——
        src_raw = cells[2][:20] if len(cells) > 2 else ""
        src_clean = src_raw
        if not src_clean or any(_j in src_clean for _j in _JUNK_SOURCE) or src_clean in _JUNK_SOURCE:
            src_clean = "待核"
        lv_raw = cells[3].strip() if len(cells) > 3 else ""
        lv = lv_raw[:2] if re.match(r"^L[1-5]", lv_raw) else lv_raw[:6]
        note = (cells[5][:26] if len(cells) > 5 else "")
        if src_clean == "待核" and note and "待核" not in note:
            note = note + "；来源待核"
        elif src_clean == "待核" and not note:
            note = "来源待核"
        metrics.append(
            {
                "name": name,
                "value": num,
                "unit": unit[:12],
                "raw": val_text[:18],
                # 区间标记：展示层据此**不画比例条**（区间没有单一量级）。
                "is_range": is_range,
                "source": src_clean,
                "level": lv,
                "year": cells[4][:8] if len(cells) > 4 else "",
                "note": note[:40],
            }
        )
        if len(metrics) >= max_rows:
            break
    return metrics


def _extract_metrics(md: str) -> list[dict[str, Any]]:
    """（兼容旧调用）核心指标 → 只含 ECharts 需要的 5 键。基于富解析投影。"""
    return [
        {k: m.get(k) for k in ("name", "value", "unit", "raw", "source")}
        for m in extract_core_metrics(md)
    ]


def _is_heading(line: str) -> bool:
    s = (line or "").strip()
    return s.startswith("#") and bool(re.match(r"^#+\s*\S", s))


def _heading_text(line: str) -> str:
    return re.sub(r"^#+\s*", "", (line or "").strip())


# ============================================================
# 全文「关键数字摘录」——执行摘要/核心指标表的**全文**视图
# ============================================================
def build_number_digest(md: str, budget: int = 14000) -> str:
    """把整份报告的**关键数字**按章节摘出来，供摘要/指标表生成器阅读。

    病案（2026-09-11 实拆报告(8)）：`graph._build_summary_and_metrics` 只把
    `report[:4500]` 喂给模型，而正文有 124,000 字符 —— 模型只看到全文的 **3.6%**
    （方法章 + 第一章开头），却被要求"从报告中提取最核心的 5-8 个指标"。
    于是核心指标表的数字**没有正文依据**，与正文各章各说各话：
    这正是"同一指标两套数"在摘要侧的最后一道入口（正文侧已由同维度合并堵住）。

    做法：按章切分（**去掉审计附录**），每章优先取表格行（数字最密、结构化），
    再补"含数字的句子"；按章**公平分配预算**，防止前面章节把额度吃光——
    第一版只取前 N 字符的教训就是"后面的章节等于不存在"。
    """
    if not md:
        return ""
    main, _audit = split_audit_tail(md)
    lines = (main or "").split("\n")

    secs: list[tuple[str, list[str]]] = []
    title, body = "", []
    for ln in lines:
        # 只按**二级标题**切章：`###` 是章内小节（实测报告 14 章被切成 71 段），
        # 切太碎会让每段分到的额度只有 200 字符——一张表都放不下，等于白摘。
        s = ln.strip()
        if re.match(r"^##\s+\S", s):
            if title or body:
                secs.append((title, body))
            title, body = _heading_text(s), []
        else:
            body.append(ln)
    if title or body:
        secs.append((title, body))
    if not secs:
        return ""

    picks: list[list[str]] = []
    for _t, _body in secs:
        rows = [l.strip() for l in _body if l.strip().startswith("|")]
        sents: list[str] = []
        for l in _body:
            s = l.strip()
            if not s or s.startswith(("|", "#", ">", "```")):
                continue
            # 时效补检块的来源链接不是分析内容，喂进去只会占额度、诱发"把链接当指标"
            if s.startswith("- ") and "](http" in s:
                continue
            if not re.search(r"\d", s):
                continue
            if len(s) > 400:
                s = s[:400]
            if s not in sents:
                sents.append(s)
        picks.append(rows + sents)

    # 预算分配：先按章均分，剩余额度再轮转补给（防止前面章节把额度吃光——
    # "只取前 N 字符"的教训就是后面的章节等于不存在）。
    # **硬上限**：任何一步都不能突破 budget。第一版给了 `max(600, …)` 的下限，
    # 章节一多（实测 35 节）下限就反超预算，digest 涨到 31,783 字符。
    out: list[str] = []
    used = 0
    leftover: list[tuple[str, list[str]]] = []
    quota = max(200, budget // max(1, len(secs)))
    for i, (t, _b) in enumerate(secs):
        buf: list[str] = []
        size = 0
        for item in picks[i]:
            if used + size + len(item) + 1 > budget:
                break
            if buf and size + len(item) > quota:
                break
            buf.append(item)
            size += len(item) + 1   # +1 = join 时的换行符，不记就会整体超预算
        leftover.append((t, picks[i][len(buf):]))
        if t and used + len(t) + 5 <= budget:
            out.append(f"### {t}")
            used += len(t) + 5
        out.extend(buf)
        used += size
    # 第二轮：还有额度就轮转补给各章。补进来的条目带上章名前缀——否则一堆数字堆在末尾，
    # 模型分不清哪个数属于哪一章（比"看不到"更容易产出错归属）。
    progress = True
    while used < budget and progress:
        progress = False
        for t, lst in leftover:
            if not lst:
                continue
            item = lst.pop(0)
            tagged = f"[{t}] {item}" if t else item
            if used + len(tagged) + 1 > budget:
                continue  # 放不下就丢，不要卡住同一轮里的小条目
            out.append(tagged)
            used += len(tagged) + 1
            progress = True
    return "\n".join(out)


def _num_tokens(text: str) -> set[str]:
    """文本里的数字归一成 token 集合（去千分位/全角/尾点），供**精确**比对。

    用集合成员判断而不是子串包含：`131` 用子串会在 `1310` 里命中，
    误判成"正文有依据"——那比漏判更糟（把没依据的数字放行）。
    """
    t = (text or "").replace("，", ",").replace("．", ".").replace("　", " ")
    t = re.sub(r"(?<=\d)[,\s](?=\d{3}(?!\d))", "", t)   # 千分位 6,562 → 6562
    out: set[str] = set()
    for m in re.finditer(r"\d+(?:\.\d+)?", t):
        out.add(m.group(0).rstrip("."))
    return out


def annotate_unbacked_metrics(summary: str, body: str) -> tuple[str, list[str]]:
    """核心指标表**正文背书校验**：表里的数值必须在正文里真的出现过。

    病案（2026-09-11）：提示词早就写了"所有数值必须逐字来自报告原文"，但**没有执行机制**——
    和"政府来源背书企业微观数据"是同一类问题：写了规则，没人检查。实测报告(8) 的核心指标表
    由 `report[:4500]`（全文 3.6%）生成，却声称"从报告中提取最核心的 5-8 个指标"。

    只做**如实标注**，不擅自改数值、不改来源机构：正文里找不到同值数字的行，
    在备注列追加「待核（正文未见同值数字）」——让人一眼看出哪几个数是表里自产的。
    返回 (改写后的 summary, 被标注的指标名列表)。
    """
    if not summary or not body:
        return summary, []
    body_tokens = _num_tokens(body)
    lines = summary.split("\n")
    flagged: list[str] = []
    head_idx = -1
    note_col = -1
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s.startswith("|"):
            if head_idx >= 0:
                break
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if head_idx < 0:
            if len(cells) >= 2 and any("指标" in c for c in cells[:2]) and any("数值" in c for c in cells[:2]):
                head_idx = i
                for j, c in enumerate(cells):
                    if any(k in c for k in ("备注", "说明", "注释")):
                        note_col = j
                        break
            continue
        if note_col < 0:
            continue
        if all(re.fullmatch(r":?-{2,}:?|:?-+:?", c or "") for c in cells):
            continue
        if len(cells) <= note_col or not cells[0]:
            continue
        nums = _num_tokens(cells[1])
        # 只检查"单元格只有一个数"的行：区间（¥80万-¥120万）/多值行无法用单值判定，
        # 标了也是噪声
        if len(nums) != 1:
            continue
        v = next(iter(nums))
        if v in body_tokens:
            continue
        name = cells[0].strip("*` ")[:20]
        note = cells[note_col].strip()
        note = "" if note in ("-", "—", "–") else note
        if "正文未见同值数字" in note:
            continue
        cells[note_col] = (note + "；" if note else "") + "待核（正文未见同值数字）"
        lines[i] = "| " + " | ".join(cells) + " |"
        flagged.append(name)
    return ("\n".join(lines), flagged) if flagged else (summary, [])


def _aigc_meta_tags(md: str, title: str) -> str:
    """HTML 交付物的**隐式标识**（文件元数据层）。

    作品页是可以被单独转发、被平台抓取的一份文件——它比聊天窗口更需要机器可读的
    "这是 AI 生成内容 + 谁生成的 + 编号"。显式标识已随正文渲染（见 explicit_tail），
    这里补的是隐式层。生成失败返回空串：宁可少一层元数据，也不能让导出整个失败。
    """
    try:
        from core.ai_label import html_meta_tags

        return html_meta_tags(md, title=title)
    except Exception:
        return ""


def split_audit_tail(md: str) -> tuple[str, str]:
    """把报告切成 (main_md, audit_md)：审计附录（质检/置信度台账/缺口/引文核验/冲突/
    规则自检/对抗校验…含参赛版追加的 Rubric/自评/AI 透明）从首个命中标题处切到尾。

    只扫**标题行**（避免正文段落里出现关键词被误切）；正文模块标题
    （宏观环境/市场规模/竞争格局/目标市场/进入壁垒…）不含这些多字短语。
    无命中 → 返回 (md, "")。
    """
    if not md:
        return "", ""
    lines = (md or "").split("\n")
    cut = None
    for i, ln in enumerate(lines):
        if not _is_heading(ln):
            continue
        txt = _heading_text(ln)
        if any(k in txt for k in AUDIT_HEADING_PATTERNS):
            cut = i
            break
    if cut is None:
        return md, ""
    return "\n".join(lines[:cut]), "\n".join(lines[cut:])


def build_html_report(md: str, title: str = "研策 · 市场调研报告", meta: dict | None = None) -> str:
    """markdown 报告 → 自包含研报作品页（HTML）。

    meta（可选 dict，前端导出时传入，缺则自洽）：
      question, decision_verdict, validation_tasks[], monitor_conditions[],
      evidence_count, citation_count, metrics[]（缺 → 从 md 解析）。
    """
    meta = dict(meta or {})
    metrics = meta.get("metrics") or extract_core_metrics(md)
    main_md, audit_md = split_audit_tail(md or "")
    title = title or "研策 · 市场调研报告"
    question = (meta.get("question") or "").strip()
    cover_title = question or title
    json_meta = {
        "title": title,
        "question": question,
        "verdict": (meta.get("decision_verdict") or "").strip(),
        "tasks": meta.get("validation_tasks") or [],
        "conditions": meta.get("monitor_conditions") or [],
        # **0 不许被 `or ""` 吃掉**（2026-09-16）：`0 or ""` 得 `""`，
        # 于是封面上「0 条证据」印成「 条证据」（`"" == null` 为假，连"—"都拿不到）。
        # 用 `is not None` 取原值：没传 → None（页面上印"— 未统计"），传了 0 → 0。
        "evidence_count": meta.get("evidence_count"),
        "citation_count": meta.get("citation_count"),
        "metrics": metrics,
    }
    # ⚠️ **META 也必须做 `</script>` 转义**。`__MAIN_MD__` / `__AUDIT_MD__` 一直是转义的，
    # 只有这一处漏了（2026-09-16 全量审计）。META 里带用户问题与指标名——只要出现
    # 一个 `</script>`，`<script type="text/plain" id="meta-json">` 就提前闭合，
    # 后面的 JSON 变成正文 HTML、**整页 JS 源码会被当文本吐在页面上**。
    # 同一个模板里三种注入，转义了两种漏了一种 = 没有转义。
    _meta_js = json.dumps(json_meta, ensure_ascii=False).replace("</script>", "<\\/script>")
    return (
        _TEMPLATE.replace("__TITLE__", html.escape(title))
        .replace("__AIGC_META__", _aigc_meta_tags(md, title))
        .replace("__COVER_TITLE__", html.escape(cover_title))
        .replace("__MAIN_MD__", (main_md or "").replace("</script>", "<\\/script>"))
        .replace("__AUDIT_MD__", (audit_md or "").replace("</script>", "<\\/script>"))
        .replace("__META_JSON__", _meta_js)
    )
