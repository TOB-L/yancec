import { Component, useEffect, useMemo, useRef, useState } from "react";
import { marked } from "marked";
import { api, apiBase, downloadExport } from "./api.js";
import "./App.css";

// 前端版本标识：刷新后 F12 控制台可确认加载的是不是最新代码
// 若控制台无此行或版本不是本文件顶部 FE_VERSION → 浏览器缓存了旧模块 → Ctrl+Shift+R 硬刷新
// ⚠️ 若用 8000 一体模式（FastAPI 托管 frontend/dist）演示：改完 src 后必须 `npm run build`
//    重建 dist，否则跑的仍是旧包（旧包含 iframe 预览 → 艾瑞等站"一直加载中"）。
// ⚠️ **改 src 必须同步 bump 这一行**（2026-09-16 病案：它停在 v0.9.78 而引擎已 v0.9.82，
//    四个版本的前端改动都没反映——于是"文件写 v0.9.78、控制台也印 v0.9.78"这个本该报警的
//    组合变成了**假通过**：你无法区分"加载了最新模块"和"加载了四天前的缓存"。
//    这道自检的全部价值就在于那个数会变；数不变，检查就退化成一句装饰。
//    `_fulltest.py:test_frontend_version_tracks_engine` 会挡住"引擎升、前端不升"这种漂移。）
const FE_VERSION = "v0.9.90";
console.info(`[研策前端] ${FE_VERSION} · 引擎基线见交付报告头水印`);

// 报告正文里的外链一律"新标签页"打开（v0.9.62）。
// 病案（用户原话）："点来源链接跳转后返回，数据源面板不该折叠"——根因是 <a> 默认**同标签**跳转，
// 整个 SPA 被卸载重载，回来时 React 状态（展开的哪条引文、滚动到哪）全部从零开始，
// 于是"刚点的是哪一条"永远丢了。文档级 click 拦截（见下方 useEffect）只能兜住 JSX 里写的链接，
// 兜不住 marked 从报告 markdown 里生成的链接（时效块内链、来源超链接都在这一段）。
// 因此从**生成源头**改：覆盖 marked 的 link 渲染器，让吐出来的 HTML 自带 target/rel。
// 只给 http(s):// 与 //host 协议相对的外链加；站内相对链接（#锚点等）保持原样。
marked.use({
  renderer: {
    link(token) {
      const text = this.parser.parseInline(token.tokens || []);
      const href = token.href || "";
      const title = token.title ? ` title="${token.title}"` : "";
      const ext = /^(https?:)?\/\//i.test(href);
      const attrs = ext ? ' target="_blank" rel="noopener noreferrer"' : "";
      return `<a href="${href}"${title}${attrs}>${text}</a>`;
    },
  },
});

// 错误边界：渲染崩溃时显示可读错误，而不是白屏
export class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, errMsg: "", errStack: "" };
  }
  static getDerivedStateFromError(err) {
    return {
      hasError: true,
      errMsg: String(err?.message || err),
      errStack: String(err?.stack || ""),
    };
  }
  componentDidCatch(err, info) {
    console.error("渲染崩溃:", err, info);
  }
  render() {
    if (this.state.hasError) {
      return (
        <div className="crash-screen">
          <h3>⚠️ 界面渲染出错</h3>
          <p>{this.state.errMsg}</p>
          {this.state.errStack && <pre className="crash-stack">{this.state.errStack}</pre>}
          <p className="crash-hint" style={{ color: "#f0b429", fontSize: 12 }}>
            若反复出现此错误：请<strong>重启前端服务</strong>（终端 Ctrl+C 后重新 npm run dev），再按 Ctrl+Shift+R 硬刷新 —— 通常是浏览器缓存了旧代码。
          </p>
          <button onClick={() => window.location.reload()}>刷新页面</button>
          <button
            onClick={() => {
              // 只清当前对话，保留历史会话（避免一次崩溃丢光所有历史）
              try {
                localStorage.removeItem("yancec_msgs");
              } catch {}
              window.location.reload();
            }}
          >
            仅清当前对话并刷新
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

// 对话模式：像 DeepSeek 一样，一条条消息流
// 证据来源URL → 可读域名（展示"查看原文·xxx.com"，点开跳转）
const domainOf = (url) => {
  try {
    return new URL(url).hostname.replace(/^www\./, "").replace(/^m\./, "");
  } catch {
    return "来源";
  }
};

// 坏URL闸门：游戏站/搜索聚合一律不渲染链接（防"数据源全是我的世界"）
// 与后端 _is_clean_url 对齐；历史消息里的旧数据也在这里被拦截
const BAD_URL_MARKERS = [
  "minecraft", "mojang", "fandom", "java.net",
  "mc.163.com", "game.163.com", "yx.qq.com", "4399.com", "7k7k.com",
  "bilibili.com/video", "/video/BV", "youtube.com", "youtu.be",
  "youku.com", "iqiyi.com", "v.qq.com", "douyin.com", "kuaishou.com",
  "music.163.com", "y.qq.com", "kugou.com", "kuwo.cn", "xiami.com",
  "baidu.com", "bing.com", "bingj.com", "sogou.com", "so.com", "microsoft.com",
];

// 防旧模块缓存崩溃：旧版编译产物里某 updater 引用未声明的 msgId（ReferenceError）。
// 这里声明模块级兜底，旧模块引用它得到 undefined 而非报错（React updater 里 ===undefined 恒 false，安全跳过）。
// 不影响正常代码——handleExecute 里的 `const msgId = Date.now()` 会局部遮蔽它。
var msgId = undefined;
const isBadUrl = (u) => {
  if (!u) return true;
  const x = String(u).toLowerCase();
  return BAD_URL_MARKERS.some((m) => x.includes(m));
};

// ───「这条引文能不能点开」——一把尺，四处共用 ───────────────────────────────
// 病案（2026-09-19 录演示时被用户当场看见）：
//   详情卡上的 chip **在 09-16 就修过这个 bug**（那段解释还留在下面），
//   而同一张卡上的「打开原网页」按钮是**同一个 bug，从来没修**：
//   chip 印着「🚪 未探测（按合规主动不访问）」，按钮却在请你点开它。
//   ——**同一屏两个互相否定的动作**，而且出现在本项目最强调"可核验"的地方。
// 修法不是把 09-16 那次的补丁再抄一遍（那就是第二次"一个判据两处实现"），
// 而是把判据收成**一处**，四处渲染全部改成调用它。
const DATA_API_RE = /(^|\/\/)api\.|\/api\/|datamapper|[?&]format=(json|xml)\b|\.(json|xml)(\?|$)/i;

// 数据接口 ≠ 网页。链接核验打的是 **HTTP 200**，而 200 正是 JSON API 的正常返回——
// 「链接活着」与「点开人能读」是两件事，这里判的是后者。
// 演示时点开 api.worldbank.org 得到的是原始 JSON，而面板上写着「✓ 已核验可访问」。
const isDataApiUrl = (u) => !!u && DATA_API_RE.test(String(u));
const isUnprobed = (c) => /未探测/.test(String((c && c.url_note) || ""));

// ① 列表 / 详情要不要**显示域名**：有 url，且不是垃圾域名、不在坏 URL 集里。
const hasDisplayUrl = (c, badUrls) =>
  !!c && !!c.url && !isBadUrl(c.url) && !(badUrls && badUrls.has(c.url));

// ② 详情要不要给**「打开原网页」按钮**：在 ① 之上再加两道闸。
//    合规红线：robots 明令 Disallow 的路径，我们自己不访问，**也不给读者递刀**。
//    数据接口：给了按钮就等于承诺一个"点开是人能读的页面"，而它是 JSON。
const citeOpenable = (c, badUrls) =>
  hasDisplayUrl(c, badUrls) && !isUnprobed(c) && !isDataApiUrl(c.url);

// 净化历史消息里的坏链接/坏来源（升级后旧数据自动清洗，不残留在界面）
const sanitizeMsgUrl = (obj) => {
  if (Array.isArray(obj)) return obj.map(sanitizeMsgUrl);
  if (obj && typeof obj === "object") {
    const out = { ...obj };
    if (typeof out.url === "string" && isBadUrl(out.url)) out.url = "";
    for (const k of Object.keys(out)) out[k] = sanitizeMsgUrl(out[k]);
    return out;
  }
  return obj;
};

// ═══════════════════════════════════════════════════════════════════════════
// 存储容量治理（用户实测 bug：跑完报告 → 刷新 → 报告整段消失）
// 为什么：一份报告正文 ~120KB，citations/evidences/trace/plan 再叠几百KB；
// 旧代码把同一份消息数组既写 yancec_msgs 又写 yancec_sessions_msgs[sessionId]，
// 且历史会话只增不减 → localStorage ~5MB 配额写满 → setItem 抛 QuotaExceededError
// → 被裸 `catch {}` 静默吞掉 → 报告没落盘，用户毫无察觉，刷新即丢。
// 对策三条：①写入前按预算逐级瘦身重试；②失败必须可见（返回 false + toast，绝不静默）；
//          ③最后一条报告正文三级兜底（本地快照 yancec_last_report + 服务端存档找回）。
// ═══════════════════════════════════════════════════════════════════════════
const STORAGE_BUDGET = 3_500_000; // 字符预算，给 5MB 配额留余量
const SESSIONS_MSG_KEY = "yancec_sessions_msgs";
// 历史会话快照的**体积**预算（不是条数上限）。
// 为什么不用"只留最近 8 条"：那是拿一个"内容不见了"换另一个——用户点开旧会话得到空页面，
// 和刷新丢掉报告是同一类体验事故。按体积裁，配额没用满就都留着（实测一份报告正文约 124KB，
// 1.5M 字符够放十几份），只有真的逼近配额时才从最旧的开始丢。
const SESSIONS_MSG_BUDGET = 1_500_000;
const SESSIONS_MSG_MIN = 3; // 再省也要留最近 3 个会话
const REPORT_INDEX_KEY = "yancec_report_index";
const REPORT_INDEX_MAX = 20;
const LAST_REPORT_KEY = "yancec_last_report";
// —— 会话级（sessionStorage）的两个恢复键，v0.9.62 新增。刻意**不走 localStorage**：
//    它们恢复的是"这次浏览到哪儿了"，是新开一个标签页就该从头来过的东西；
//    而 localStorage 是全标签页共享的，会把上一个窗口的阅读位置串到新窗口里。
//    与既有 key 无任何语义重叠（都是新名字，yancec_ 前缀）。
const CITE_PANEL_SS_KEY = "yancec_cite_panel"; // 展开的是哪条引文：{msgId, idx}
const CHAT_SCROLL_SS_KEY = "yancec_scroll"; // .chat-main 的 scrollTop
const TRUNC_LIMIT = 4000;
const TRUNC_NOTE =
  "\n\n> （本条历史报告正文为节省浏览器存储已截断，完整内容请用导出功能或历史会话获取）";
// 归档预览：比 TRUNC_LIMIT 更短——归档的正文**不指望本地留着**，只留一眼能认出是哪份报告的量，
// 真正的全文在服务端存档里（打开会话时自动取回）。留得短，就能多留几个会话。
const PREVIEW_LIMIT = 700;
const ARCHIVE_NOTE =
  "\n\n> 📦 本条报告正文已归档到服务端（本地只留摘要）。正在自动取回全文，稍候片刻；若未出现，请在右侧「历史报告（服务端存档）」中重新载入。";

// 写失败只提示一次：配额满时 messages 每次变更都会重试，不按住会刷屏；
// 恢复成功（下次写入成功）后重置，让下一次真实写满仍能提醒。
let _quotaWarned = false;

// 绝不静默：返回 true/false，失败一律留痕
function safeSet(key, value) {
  try {
    localStorage.setItem(key, typeof value === "string" ? value : JSON.stringify(value));
    return true;
  } catch (e) {
    console.warn("[存储] 写入失败", key, e);
    return false;
  }
}

// 最后一条"正文非空"的 report 下标 —— 用户刚要的成果，任何降级都不得触碰
function lastReportIdx(msgs) {
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i];
    if (m && m.type === "report" && m.report) return i;
  }
  return -1;
}

// 消息瘦身：只做减法，不改原对象。
// keepEvidenceFor：末尾 N 条保留 evidences（其余剥掉）；0=全剥，Infinity=全留
// fullReportFor：末尾 N 条保留完整 report 正文（其余截断）；Infinity=全留
// 铁律：最后一条 report 的正文永不截断、永不丢弃（截了等于换个姿势再丢一次）
function stripHeavy(msgs, { keepEvidenceFor = 0, fullReportFor = Infinity, archiveReportFor = Infinity } = {}) {
  const list = Array.isArray(msgs) ? msgs : [];
  const n = list.length;
  const exempt = lastReportIdx(list); // 豁免下标
  // 报告条目的倒数序号（0=最新一条报告）——"保留全文"按**报告条数**算，不按消息条数算
  const reportOrder = new Map();
  let seenR = 0;
  for (let i = n - 1; i >= 0; i--) {
    const m = list[i];
    if (m && m.type === "report" && m.report) reportOrder.set(i, seenR++);
  }
  return list.map((m, i) => {
    if (!m || typeof m !== "object") return m;
    const fromEnd = n - 1 - i; // 距末尾还有几条（0=最后一条）
    const keepEv = fromEnd < keepEvidenceFor;
    const keepFullReport = i === exempt || fromEnd < fullReportFor;
    const out = { ...m };
    delete out.trace;
    if (!keepEv && out.evidences) delete out.evidences;
    if (!keepFullReport && typeof out.report === "string" && out.report.length > TRUNC_LIMIT) {
      const ord = reportOrder.has(i) ? reportOrder.get(i) : Infinity;
      if (ord < archiveReportFor) {
        out.report = out.report.slice(0, TRUNC_LIMIT) + TRUNC_NOTE;
      } else {
        // **归档**而不是截断：只留一小段预览 + 服务端存档 id，打开这条会话时自动取回全文。
        // 病案（用户实测）：原来一律截断到 4000 字——一份 124KB 的报告"大部分都消失了"，
        // 而服务端其实完整存着。截断是**不可恢复的丢失**，留指针才是可恢复的。
        out.report = out.report.slice(0, PREVIEW_LIMIT) + ARCHIVE_NOTE;
        out._bodyArchived = true; // 打开会话时凭 serverReportId 取回全文
      }
    }
    return out;
  });
}

// 取末尾 6 条，但必须把"最后一条报告"一并带上——否则报告后面又跟了几条追问时，
// 降级到 L4 会把用户刚要的正文整条切掉
function tailWithReport(msgs, keep = 6) {
  const tail = msgs.slice(-keep);
  const li = lastReportIdx(msgs);
  if (li >= 0 && li < msgs.length - keep) tail.unshift(msgs[li]);
  return tail;
}

// 两级降写：yancec_msgs（主，阶梯降级）+ yancec_sessions_msgs（历史快照，精简更狠）
// notify：可选，配额写满时用来弹 toast（模块级函数拿不到组件内的 showToast）
function persistMessages(msgs, sessionId, notify) {
  const list = Array.isArray(msgs) ? msgs : [];

  // —— 主键 yancec_msgs：L0 原样 → L1 去 trace → L2 末2条外去 evidences
  //    → L3 末2条外截断 report → L4 只留末6条（仍保最后一条报告全文）——
  const ladder = [
    () => list, // L0
    () => stripHeavy(list, { keepEvidenceFor: Infinity }), // L1
    () => stripHeavy(list, { keepEvidenceFor: 2 }), // L2
    () => stripHeavy(list, { keepEvidenceFor: 2, fullReportFor: 2, archiveReportFor: 2 }), // L3
    () => stripHeavy(tailWithReport(list, 6)), // L4
  ];
  let saved = false;
  for (let lvl = 0; lvl < ladder.length; lvl++) {
    let text;
    try {
      text = JSON.stringify(ladder[lvl]());
    } catch (e) {
      console.warn("[存储] yancec_msgs 序列化失败，跳过该档位", lvl, e);
      continue;
    }
    // 超自我预算的档位直接跳过：即便侥幸写进去，也会把会话快照/报告索引挤爆，下一轮照样写不进
    const overBudget = text.length > STORAGE_BUDGET;
    if (!overBudget && safeSet("yancec_msgs", text)) {
      saved = true;
      // L0 原样写进去了 = 存储已恢复 → 解除"已提醒"，下次真写满还能再提醒
      if (lvl === 0) _quotaWarned = false;
      break;
    }
    // L0 就没写进去 = 配额确已写满（后面几级是抢救，不是正常路径）
    // → 必须让用户知道并导出备份；按住只弹一次，避免每条流式消息都弹
    if (lvl === 0 && !_quotaWarned) {
      _quotaWarned = true;
      console.warn(`[存储] yancec_msgs 写入受阻（体积 ${text.length}），开始降级`);
      if (typeof notify === "function") {
        notify("浏览器存储已满：本次报告未能自动保存，请立即导出备份");
      }
    }
  }
  if (!saved) console.warn("[存储] yancec_msgs 降级到底仍写不进去，报告仅存于内存");

  // —— 历史快照 yancec_sessions_msgs[sessionId]：去 trace/evidences，report 只末条留全文 ——
  if (sessionId) {
    safeSet("yancec_cur_session", sessionId); // 几字节的会话指针；走 safeSet 只为不静默吞错
    try {
      // 旧报告的正文**归档**（只留指针+预览）而不是截断——历史会话里"内容消失"
      // 的根因就是这里。打开会话时 openSession 会自动把全文取回来。
      const slim = stripHeavy(list, { keepEvidenceFor: 0, fullReportFor: 1, archiveReportFor: 1 });
      let all = {};
      try {
        const parsed = JSON.parse(localStorage.getItem(SESSIONS_MSG_KEY) || "{}");
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) all = parsed;
      } catch {
        all = {};
      }
      delete all[sessionId]; // 先删后加：让当前会话排到插入顺序末尾（=最新的一个）
      all[sessionId] = slim;
      // 按**体积**从最旧的开始丢（保留 floor 个兜底），再逐步收紧重试。
      // 只按条数裁会让"配额明明够放"的旧会话白丢——那也是一种"内容不见了"。
      // 注意 left 必须自己数：Object.keys 是快照，删了元素它长度也不变，判 floor 会永远不成立。
      const pruneTo = (budget, floor) => {
        const keys = Object.keys(all);
        let total = keys.reduce((s, k) => s + JSON.stringify(all[k]).length, 0);
        let left = keys.length;
        for (const k of keys) {
          if (total <= budget || left <= floor) break;
          total -= JSON.stringify(all[k]).length;
          delete all[k];
          left--;
        }
        return left;
      };
      for (const [budget, floor] of [
        [SESSIONS_MSG_BUDGET, SESSIONS_MSG_MIN],
        [SESSIONS_MSG_BUDGET / 2, 1],
        [SESSIONS_MSG_BUDGET / 4, 1],
      ]) {
        if (pruneTo(budget, floor) > 0 && safeSet(SESSIONS_MSG_KEY, all)) break;
      }
    } catch (e) {
      console.warn("[存储] 历史会话快照写入失败", e);
    }
  }
}

// 报告索引：只记 {id, localId, question, ts} 短字段，绝不存正文。
// 用途：消息体被配额吞掉后，还能凭 localId 找到服务端存档 id 把正文捞回来。
function readReportIndex() {
  try {
    const arr = JSON.parse(localStorage.getItem(REPORT_INDEX_KEY) || "[]");
    return Array.isArray(arr) ? arr.filter((x) => x && x.id) : [];
  } catch {
    return [];
  }
}
function rememberReportIndex(entry) {
  if (!entry || !entry.id) return;
  // 追加到末尾（末尾=最新），超上限丢最旧的
  const next = [...readReportIndex(), entry].slice(-REPORT_INDEX_MAX);
  safeSet(REPORT_INDEX_KEY, next);
}

// 服务端存档 → 报告消息（字段名从下划线协议映射到前端驼峰，恢复路径只补正文，不改状态机）
function reportMsgFromServer(data, fallback = {}) {
  return {
    role: "assistant",
    type: "report",
    _reveal: false, // 恢复出来的报告一律不重播打字机
    localId: fallback.localId || `recovered_server_${fallback.id || "x"}`,
    serverReportId: fallback.id || null,
    report: data.report || "",
    citations: data.citations || [],
    metrics: data.metrics || [],
    entailment: data.entailment || {},
    // **`?? null` 而不是 `|| 0`**（2026-09-15）：后端在"一条引用都没敲过门"时
    // 返回 `null`（未判定）。`|| 0` 会把它变成 0，于是角标把"没测过"印成"0%"——
    // 正是本产品在批评的「把缺席当结论」。
    citationPassRate: data.citation_pass_rate ?? null,
    // 通过率的分母（真的敲过门的条数）与未探测条数——与实时路径同一套字段名。
    // 少了这两个，恢复出来的报告角标 tooltip 会把"没测"说成"0%"。
    citationProbed: data.citation_report?.probed ?? null,
    citationSkipped: data.citation_report?.skipped ?? null,
    // 证据条数与仲裁处数：头部那行「N 条证据 · M 处仲裁」的两个数。
    // 原先恢复路径不带它们，于是历史里打开的报告头部是「 条证据 ·  处仲裁」。
    evidenceCount: data.evidence_count ?? (data.evidences || []).length ?? 0,
    conflicts: data.conflicts ?? (data.conflict_results || []).length ?? 0,
    // 仲裁分母：不带它，tooltip 会对一份**刚跑完的报告**说「本次未产出冲突检测读数（旧报告）」
    conflictScan: data.conflict_scan ?? null,
    // 坏链集与域名回退：丢了它们，恢复出来的报告里死链照旧可点
    badUrls: data.bad_urls || [],
    urlFixes: data.url_fixes || {},
    // 闸门结论（存档里一直有，恢复路径此前没映射）：
    // 少了它，被闸门 Go→Validate 降过的报告，从历史打开时看不出被降过
    integrityGate: data.integrity_gate || null,
    stopCard: data.stop_card || null,
    // 决策清单降级标记（v0.9.75）：非空 = 这一节是模板不是模型判定，头部必须打角标
    sopFallback: data.sop_fallback || {},
    decisionChain: data.decision_chain || [],
    question: data.question || fallback.question || "",
    decisionVerdict: data.decision_verdict || "",
    validationTasks: data.validation_tasks || [],
    monitorConditions: data.monitor_conditions || [],
    auditBundle: data.audit_bundle || null,
    _recovered: true, // 仅作标记：这条不是本次跑的，是从服务端捞回的
  };
}
// 把"正文不在本地"的报告消息从服务端取回：空正文（被配额吞掉）或**已归档**（本地只留预览）。
// 病案（用户实测）：历史记录里的旧报告曾被一律截断到 4000 字——一份 124KB 的报告
// "内容大部分都消失了"，而服务端其实完整存着。截断是**不可恢复的丢失**；留指针+预览、
// 打开时自动取回才是可恢复的。本地因此只花几百字符/份，能留下多得多的历史会话。
async function healReportBodies(msgs) {
  const list = Array.isArray(msgs) ? msgs : [];
  const need = list.filter(
    (m) => m && m.type === "report" && (m._bodyArchived || !(m.report && m.report.length)),
  );
  if (!need.length) return { list, healed: 0, failed: 0 };
  const patch = new Map();
  let failed = 0;
  for (const m of need) {
    try {
      let id = m.serverReportId;
      if (!id) {
        const hit = readReportIndex().find((x) => x.localId && x.localId === m.localId);
        id = hit && hit.id;
      }
      if (!id) {
        failed += 1;
        continue;
      }
      const data = await api.getReport(id);
      if (!data || !data.report) {
        failed += 1;
        continue;
      }
      patch.set(m.localId != null ? m.localId : m.serverReportId, data);
    } catch (e) {
      failed += 1;
      console.warn("[存储] 报告正文取回失败（忽略）", m && m.localId, e);
    }
  }
  if (!patch.size) return { list, healed: 0, failed };
  return {
    list: list.map((m) => {
      const d = m && patch.get(m.localId != null ? m.localId : m.serverReportId);
      return d && m.type === "report" ? patchReportFromServer(m, d) : m;
    }),
    healed: patch.size,
    failed,
  };
}

// 会话切换令牌：取回是异步的，用户可能已经开了另一个会话——旧回包必须丢掉，不能盖到新会话上
let _sessionSeq = 0;

// 补进已有空报告消息：服务端有值就用服务端的，服务端缺字段则保留本地已有的
function patchReportFromServer(msg, data) {
  const orKeep = (v, prev) => (Array.isArray(v) ? (v.length ? v : prev) : v || prev);
  return {
    ...msg,
    report: data.report || msg.report || "",
    citations: orKeep(data.citations, msg.citations) || [],
    metrics: orKeep(data.metrics, msg.metrics) || [],
    entailment: data.entailment || msg.entailment || {},
    citationPassRate: data.citation_pass_rate ?? msg.citationPassRate ?? null,
    // 以下一组与 `reportMsgFromServer` 同源（2026-09-16）：**补正文这条路上也会掉读数**。
    // 注意数值字段用 `??` 不用 `orKeep`——`orKeep` 对非数组走 `v || prev`，
    // 而这里的 0 是合法读数（"0 处仲裁"是一个有信息的 0）。
    citationProbed: data.citation_report?.probed ?? msg.citationProbed ?? null,
    citationSkipped: data.citation_report?.skipped ?? msg.citationSkipped ?? null,
    evidenceCount: data.evidence_count ?? msg.evidenceCount ?? 0,
    conflicts: data.conflicts ?? msg.conflicts ?? 0,
    conflictScan: data.conflict_scan || msg.conflictScan || null,
    badUrls: orKeep(data.bad_urls, msg.badUrls) || [],
    urlFixes: data.url_fixes || msg.urlFixes || {},
    integrityGate: data.integrity_gate || msg.integrityGate || null,
    stopCard: data.stop_card || msg.stopCard || null,
    sopFallback: data.sop_fallback || msg.sopFallback || {},
    decisionChain: data.decision_chain || msg.decisionChain || [],
    question: data.question || msg.question || "",
    decisionVerdict: data.decision_verdict || msg.decisionVerdict || "",
    validationTasks: orKeep(data.validation_tasks, msg.validationTasks) || [],
    monitorConditions: orKeep(data.monitor_conditions, msg.monitorConditions) || [],
    auditBundle: data.audit_bundle || msg.auditBundle || null,
    _reveal: false,
  };
}

// ═══════════════════════════════════════════════════════════════════════════
// 历史会话的时间分组 + 搜索（v0.9.61）
// 病案（用户原话）："历史记录里的时间信息进行相关的细分，全是只有具体时间点的"。
// 根因：会话只存了 ts = toLocaleTimeString()（形如 "15:37:02"）——**连日期都没有**，
// 于是列表里全是同款时分秒，既看不出是哪天的，也无从按时间归组。
// 对策两条：①新会话补一个毫秒级 at（老会话没有 at → 一律沉"更早"，绝不给它编造日期）；
//          ②加搜索框——同一份报告常常记不住是在哪个会话里跑的。
// 另注：at 是**新增字段**，不改 ts 的名字/语义 → 老 localStorage 数据原样可用，不需要迁移。
// ═══════════════════════════════════════════════════════════════════════════
// 分组区间（v0.9.62 调整）：用户原话"分为今天、昨天、七天内、一月内这几个区间"——
// 原来的「本周/本月」按自然周/月切，跨周一时"3 天前"会跳到"本周"，与"几天前"的直觉不符；
// 改成纯天数区间后，组名自己就把天数量级说清楚了。
// 另注：DOM 上组名只在文档里出现一次，改字符串不影响任何持久化数据（会话存的是 at 时间戳，不是组名）。
const HIST_GROUPS = ["今天", "昨天", "七天内", "一月内", "更早"]; // 渲染顺序：从上到下，不可打乱
const DAY_MS = 86_400_000;

const _pad2 = (n) => String(n).padStart(2, "0");

// 本地"自然日"零点。为什么不用"距今小时数"：凌晨 00:10 看一份 3 天前 23:50 的会话，
// 按时长算是 24.3 小时 → 会被塞进"昨天"；而用户眼里它就是 3 天前。
// 按日历日相减才符合"今天/昨天"的直觉；DST 造成的 23/25 小时日由 Math.round 吸收。
function _dayStart(t) {
  const d = new Date(t);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

// 分组判定。无 at 的老会话一律"更早"——它们都创建于本次升级之前，本就最旧。
function histBucket(at) {
  if (!at) return "更早";
  const days = Math.round((_dayStart(Date.now()) - _dayStart(at)) / DAY_MS);
  if (days <= 0) return "今天"; // <=0 而非 ===0：容错系统时钟回拨/未来时间戳
  if (days === 1) return "昨天";
  if (days <= 7) return "七天内"; // 2~7 天
  if (days <= 30) return "一月内"; // 8~30 天
  return "更早";
}

// 展示时间：今天/昨天补到 HH:MM 即可（同一天不必再写日期）；
// 更早的必须带上 MM-DD，否则又回到"看不出是哪天"的老问题。
// 老会话（无 at）原样返回它自己的 ts 字符串——没有可信日期就不臆造一个。
function histTimeText(s) {
  if (!s.at) return s.ts || "";
  const d = new Date(s.at);
  const hhmm = `${_pad2(d.getHours())}:${_pad2(d.getMinutes())}`;
  const b = histBucket(s.at);
  if (b === "今天" || b === "昨天") return hhmm;
  return `${_pad2(d.getMonth() + 1)}-${_pad2(d.getDate())} ${hhmm}`;
}

// 分组 + 组内倒序（新的在上）。at 缺失视作 0 → 老会话稳定沉到"更早"末尾（stable sort 保序）。
function histGroupSessions(sessions) {
  const g = {};
  for (const name of HIST_GROUPS) g[name] = [];
  for (const s of sessions) (g[histBucket(s.at)] || g["更早"]).push(s);
  for (const name of HIST_GROUPS) g[name].sort((a, b) => (b.at || 0) - (a.at || 0));
  return g;
}

// 会话 → 可搜索文本。标题只截了前 20 字，用户记得的关键词常落在**存进消息里的完整问题**上
// （report/executing 等消息带 question 字段，落在 yancec_sessions_msgs[sessionId] 里）。
// 所以搜索范围 = 标题 ∪ 该会话所有消息的 question。
// 只在抽屉打开/会话列表变化时解析一次（本地读取，不发请求）：逐键解析 >1MB 的 JSON
// 会明显卡输入，因此钉在 useMemo 里，而不是放进 onChange。
function histSearchIndex() {
  const out = {};
  try {
    const all = JSON.parse(localStorage.getItem(SESSIONS_MSG_KEY) || "{}");
    for (const sid of Object.keys(all || {})) {
      const msgs = all[sid];
      if (!Array.isArray(msgs)) continue;
      out[sid] = msgs
        .map((m) => (m && typeof m === "object" && m.question ? String(m.question) : ""))
        .filter(Boolean)
        .join("\n");
    }
  } catch (e) {
    // 解析失败 = 没有索引 → 退化为只按标题匹配。搜索是锦上添花，不该因为读不到快照就报错
    console.warn("[历史搜索] 会话消息索引解析失败（本次退化为只搜标题）", e);
  }
  return out;
}

export default function App() {
  const [messages, setMessages] = useState(() => {
    try {
      const msgs = sanitizeMsgUrl(JSON.parse(localStorage.getItem("yancec_msgs") || "[]") || []);
      // 刷新后：把"执行中"的残留消息标记为中断（进程已死，不能继续思考）
      // 并给缺少 localId 的旧消息补唯一id（引用面板按消息精确开合需要）
      const restored = msgs.map((m, i) => {
        const base =
          m.type === "executing" && m.status === "running"
            ? {
                // 刷新页面 ≠ 任务作废：后端有 checkpoint（MemorySaver），thread_id=localId 已持久化，
                // 点「▶ 继续执行」即从断点续跑，不重跑不重复扣积分（后端 checkpoint 丢失时自动从头，也不二次计费）。
                ...m,
                localId: m.localId ?? `stored_${i}`,
                status: "paused",
                error: "",
              }
            : m.localId != null
              ? m
              : { ...m, localId: `stored_${i}` };
        // 刷新恢复的历史报告不再重播打字机（_reveal 只在"本次刚跑完"置位，持久化/恢复一律剥掉）
        return base.type === "report" ? { ...base, _reveal: false } : base;
      });
      // 本地兜底恢复：若历史里一条带正文的报告都没有（上次 setItem 被配额吞掉），
      // 就把 yancec_last_report 的精简副本补回末尾——至少让用户看到刚跑完的成果。
      const hasReport = restored.some((m) => m.type === "report" && m.report && m.report.length);
      if (!hasReport) {
        try {
          const lr = sanitizeMsgUrl(JSON.parse(localStorage.getItem(LAST_REPORT_KEY) || "null"));
          if (lr && lr.report && lr.report.length) {
            restored.push({
              role: "assistant",
              type: "report",
              _reveal: false, // 恢复出来的报告不重播打字机
              localId: "recovered_local",
              report: lr.report,
              citations: lr.citations || [],
              metrics: lr.metrics || [],
              question: lr.question || "",
              decisionVerdict: lr.decisionVerdict || "",
              validationTasks: lr.validationTasks || [],
              monitorConditions: lr.monitorConditions || [],
              auditBundle: lr.auditBundle || null,
              _recovered: true, // 仅作标记：这条是从本地快照捞回来的
            });
            console.warn("[存储] 已从本地快照 yancec_last_report 恢复报告正文");
          }
        } catch (e) {
          console.warn("[存储] 本地兜底报告恢复失败（忽略）", e);
        }
      }
      return restored;
    } catch {
      return [];
    }
  });
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const toastTimer = useRef(null);
  // 轻提示：2.6秒自动消失（不常驻在主界面）
  function showToast(msg) {
    setToast(msg);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(""), 2600);
  }
  const [expandedEvidence, setExpandedEvidence] = useState(null);
  const [panelW, setPanelW] = useState(430); // 右侧数据源面板宽度（可拖拽）
  const [expandedCitation, setExpandedCitation] = useState(null); // 报告引用角标 → 右侧弹层
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [assetsOpen, setAssetsOpen] = useState(false);
  const [assets, setAssets] = useState(null); // 资产库数据
  // 行业基线卡（沉淀下来的"越用越懂"）：与"证据资产"是两回事——
  // 后者是一条条证据，前者是**归档成行业的基线**，下次同行业调研时整段注入。
  const [industryCards, setIndustryCards] = useState(null);
  const [runStatsOpen, setRunStatsOpen] = useState(false); // 运行台账抽屉
  const [runStats, setRunStats] = useState(null);
  // 自主性图谱抽屉（2026-09-16 补界面入口）。
  // 病案：`core/autonomy_map.py` 与 `GET /api/autonomy-map` 早就在，
  // 而**前端一处入口都没有**——Demo 分镜 L28 把它写成「**必拍**」的弹层
  // （"16 行节点表…停在 deliver 行"），PPT P7B 也把它做成了主力技术页。
  // 后端有数据、纸面有镜头、**产品里点不开**：这一拍根本拍不出来。
  // 一张只在材料里存在的表，比没有这张表更危险——它把"说了"当成了"演了"。
  const [autonomyOpen, setAutonomyOpen] = useState(false);
  const [autonomy, setAutonomy] = useState(null);
  // 工具合同抽屉（2026-09-19 补界面入口）。
  // 病案：`GET /api/tools` 返回十张工具合同（目的/输入/输出/权限/失败处理/审计），
  // 方案书与说明材料里反复引用它当卖点（"工具=可审计的业务合同"），
  // 而**全仓零消费者**——它只存在于接口和纸面上，产品里点不开。
  // 这正是本仓库最高频的断链形态：「实现了 ≠ 能被用上」。
  const [toolsOpen, setToolsOpen] = useState(false);
  const [tools, setTools] = useState(null);
  // 记忆抽屉（同上）。`GET /api/memory` 一直能读、也一直真的在影响下一次调研
  // （行业/偏好/会话三张表被 recall 消费），却同样没有任何界面能让人看见它。
  // **数据有、披露无**：读者没法知道"它记住了我什么"，也就没法判断这是不是好事。
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [memory, setMemory] = useState(null);
  const [board, setBoard] = useState(null); // 最小复购看板四指标
  // 决策回访（v0.9.75）：当初的判断，今天动了没有。
  // 它是**报告交付之后**才发生的事，所以不在报告里，而是独立一个面。
  const [outcomeOpen, setOutcomeOpen] = useState(false);
  const [outcome, setOutcome] = useState(null);
  const [outcomeBusy, setOutcomeBusy] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [srvReports, setSrvReports] = useState(null); // 服务端存档报告列表（null=还没拉过；[]=拉过但为空）
  const [srvTick, setSrvTick] = useState(0); // 刷新按钮的计数器：+1 就重拉一次列表
  const [histQuery, setHistQuery] = useState(""); // 历史抽屉的搜索词（只在抽屉内有意义，关抽屉即清空）

  // ── 抽屉互斥：**唯一收口**（2026-09-15 修）────────────────────────────
  // 病案：此前每个入口**手工罗列**"要关掉谁"——settings 只关 history、
  // history 只关 settings、assets 只关 history+settings……五个入口五份名单，
  // 结果"先开 A 再开 B"必然重叠。而漏掉的那个恰好是**后加的**「决策回访」抽屉：
  // `setOutcomeOpen(false)` 全仓只出现在它自己身上，别的入口没人关它，
  // 于是"开回访 → 开历史"两个面板物理叠在一起、且无从关闭——**评审点两下就能复现**。
  //
  // 手工维护互斥关系，漏一个就是一个界面故障；所以改成**一处定义、全员走它**：
  // 新增抽屉时只需往 DRAWERS 里加一行，互斥自动成立。
  const DRAWERS = {
    history: setHistoryOpen,
    settings: setSettingsOpen,
    assets: setAssetsOpen,
    runStats: setRunStatsOpen,
    outcome: setOutcomeOpen,
    autonomy: setAutonomyOpen,
    tools: setToolsOpen,
    memory: setMemoryOpen,
  };
  const openDrawer = (keep) => {
    Object.entries(DRAWERS).forEach(([k, set]) => set(k === keep));
  };
  const [pointsBalance, setPointsBalance] = useState(null); // 积分余额（档位+积分计量）
  // `/api/billing` 的**完整**载荷。2026-09-17 之前只取了 `balance` 一个字段，
  // 而接口同时返回 mode_points / tiers / default_mode / ledger——**后端给了、前端不读**，
  // 正是本项目最高频的那类断链。现在整包留下，设置抽屉里的档位表由它渲染（不再手写死值）。
  const [billing, setBilling] = useState(null);
  const [sessions, setSessions] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem("yancec_sessions") || "[]");
    } catch {
      return [];
    }
  }); // 历史对话列表（持久化）
  const [sessionId, setSessionId] = useState(() => {
    try {
      return localStorage.getItem("yancec_cur_session") || null;
    } catch {
      return null;
    }
  }); // 当前会话id（持久化，刷新不丢）
  const [depth, setDepth] = useState("标准"); // 深度档（默认标准：5维度全Agent覆盖；免费档只有2维度会让部分Agent空跑）
  const [modeMenuOpen, setModeMenuOpen] = useState(false); // Gemini式模式下拉
  const [attachments, setAttachments] = useState([]); // [{name, summary}] 已解析附件
  const [streamAbort, setStreamAbort] = useState(null); // 暂停控制器
  const [deepDiveTarget, setDeepDiveTarget] = useState(null); // 深挖模式：待深挖的报告（null=正常模式）
  const endRef = useRef(null);
  const inputRef = useRef(null);
  const fileInputRef = useRef(null);
  const modeMenuRef = useRef(null); // 档位下拉：点外部任意空白即关闭

  // —— 滚动控制：.chat-main 是真正滚动容器；"贴底跟随 + 新问题强制到底 + 一键到底悬浮钮" ——
  const chatMainRef = useRef(null);
  const [showDown, setShowDown] = useState(false); // 距底部>阈值(用户上滑)时显示"↓到底"
  const stickRef = useRef(true); // 用户是否"贴底"：近底=跟随新内容；上滑阅读=暂停跟随
  const scrollSaveTimer = useRef(null); // 滚动位置写盘节流计时器（见 handleChatScroll）
  function scrollChat(instant = true) {
    const sc = chatMainRef.current;
    if (!sc) return;
    const go = () => {
      sc.scrollTop = sc.scrollHeight;
    };
    if (instant) go();
    else requestAnimationFrame(go);
  }
  function nearBottom() {
    const sc = chatMainRef.current;
    if (!sc) return true;
    return sc.scrollHeight - sc.scrollTop - sc.clientHeight < 160;
  }
  function handleChatScroll() {
    const nb = nearBottom();
    stickRef.current = nb;
    setShowDown(!nb);
    // 滚动位置持久化（v0.9.62 兜底）：走 sessionStorage，会话级。
    // 节流 200ms 再写——scroll 事件一帧能来好几次，每次都同步写 sessionStorage 会拖出滚动卡顿；
    // 只有真的停下来时才写一次，恢复精度对"回到刚才读到的地方"完全够。
    const sc = chatMainRef.current;
    if (!sc || scrollSaveTimer.current) return;
    scrollSaveTimer.current = setTimeout(() => {
      scrollSaveTimer.current = null;
      try {
        sessionStorage.setItem(CHAT_SCROLL_SS_KEY, String(sc.scrollTop));
      } catch {}
    }, 200);
  }
  // 挂载恢复（兜底之二）：把上次离开时的滚动位置放回去。
  // 必须等 DOM 渲染完一帧再赋值——挂载瞬间 scrollHeight 还是 0，此时设 scrollTop 会被浏览器钳成 0，
  // 等于没恢复（这是这类"恢复滚动"最常踩的坑）。
  // 同时先把贴底开关按掉：下面的 ResizeObserver 只看 stickRef，若不先按掉，报告卡片异步长高会
  // 立刻把位置又拽回底部，恢复白做。
  useEffect(() => {
    const sc = chatMainRef.current;
    if (!sc) return;
    let v = 0;
    try {
      v = Number(sessionStorage.getItem(CHAT_SCROLL_SS_KEY)) || 0;
    } catch {}
    if (!(v > 0)) return; // 首次进入 / 本来就在顶部 → 保持默认的"贴底"
    stickRef.current = false;
    requestAnimationFrame(() => {
      sc.scrollTop = v;
      const nb = nearBottom();
      stickRef.current = nb;
      setShowDown(!nb); // 恢复出来若不在底部，同步亮出"↓到底"，否则用户没有回底部的入口提示
    });
    // 只在挂载时跑一次：这是"进入页面"的一次性动作，不是响应某个状态
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 贴底加固：当用户处于底部、而内容高度被异步增长(打字机逐块 reveal/长卡片布局)撑高时，
  // 主动重新钉底——即使没有消息数组变更(如 ReportCard 内部动画)也不会滞留半路。
  useEffect(() => {
    const sc = chatMainRef.current;
    if (!sc) return;
    const ro = new ResizeObserver(() => {
      if (stickRef.current) sc.scrollTop = sc.scrollHeight;
    });
    ro.observe(sc);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 档位下拉：点击档位菜单/触发按钮之外的区域（含发送、输入框）→ 直接收起
  useEffect(() => {
    if (!modeMenuOpen) return;
    const close = (e) => {
      const r = modeMenuRef.current;
      if (!r) return;
      const inMenu = r.querySelector(".mode-menu")?.contains(e.target);
      const inTrigger = r.querySelector(".mode-select")?.contains(e.target);
      if (!inMenu && !inTrigger) setModeMenuOpen(false);
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [modeMenuOpen]);

  // 会话持久化：消息与历史列表写入 localStorage（刷新不丢）
  // 容量感知写入：persistMessages 内部按预算逐级瘦身重试，写不进去会 toast 告知
  //（旧代码是裸 setItem + `catch {}`，配额写满后静默失败 → 刷新丢报告）
  useEffect(() => {
    persistMessages(messages, sessionId, showToast);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages, sessionId]);
  useEffect(() => {
    try {
      localStorage.setItem("yancec_sessions", JSON.stringify(sessions));
    } catch {}
  }, [sessions]);
  // 本地兜底快照：最后一条报告单独存一份精简副本（报告正文 + 头部结构化字段，不含 trace/evidences 明细）。
  // 主键 yancec_msgs 被配额降级或写失败时，启动时能凭它把正文捞回来（服务端存档是第二道兜底）。
  useEffect(() => {
    const last = [...messages].reverse().find((m) => m && m.type === "report" && m.report);
    if (!last) return;
    const snap = {
      report: last.report,
      citations: last.citations || [],
      metrics: last.metrics || [],
      question: last.question || "",
      decisionVerdict: last.decisionVerdict || "",
      validationTasks: last.validationTasks || [],
      monitorConditions: last.monitorConditions || [],
      auditBundle: last.auditBundle || null,
      ts: Date.now(),
    };
    // 写不下就退化为"只保正文"：正文是这个 key 存在的唯一理由
    if (!safeSet(LAST_REPORT_KEY, snap)) {
      safeSet(LAST_REPORT_KEY, { report: last.report, question: last.question || "" });
    }
  }, [messages]);

  // 档位+积分计量：挂载时拉取积分余额（头部徽章显示，任务中由 billing 事件实时更新）
  useEffect(() => {
    try {
      fetch(`${apiBase}/api/billing`)
        .then((r) => r.json())
        .then((b) => {
          if (b && typeof b.balance === "number") setPointsBalance(b.balance);
          if (b) setBilling(b);
        })
        .catch(() => {});
    } catch {}
  }, []);

  // 启动补齐（第二道兜底）：报告正文没落进 localStorage 时，凭服务端存档把正文捞回来。
  // 触发场景：上次写入被配额吞掉 → 消息里 report 字段为空（或压根没有 report 消息）。
  // 全程静默：失败只 console.warn，绝不弹错误、绝不影响正常启动（404=老报告没存档，属正常）。
  useEffect(() => {
    (async () => {
      try {
        const empty = messages.filter(
          (m) => m && m.type === "report" && (m._bodyArchived || !(m.report && m.report.length)),
        );
        if (empty.length) {
          // 逐条补：先认消息自带 serverReportId，没有再用 localId 查本地索引
          for (const m of empty) {
            try {
              let id = m.serverReportId;
              if (!id) {
                const hit = readReportIndex().find((x) => x.localId && x.localId === m.localId);
                id = hit?.id;
              }
              if (!id) continue;
              const data = await api.getReport(id);
              if (!data || !data.report) continue;
              setMessages((cur) =>
                cur.map((x) =>
                  x.type === "report" && x.localId === m.localId ? patchReportFromServer(x, data) : x
                )
              );
              showToast("已从服务器恢复报告正文");
            } catch (e) {
              console.warn("[存储] 报告正文补齐失败（忽略）", m?.localId, e);
            }
          }
          return;
        }
        // 连一条 report 消息都没有（消息体整个没存下来）→ 取索引里最新一条补成一条报告
        if (messages.some((m) => m && m.type === "report")) return;
        const idx = readReportIndex();
        const lastEntry = idx[idx.length - 1];
        if (!lastEntry) return;
        const data = await api.getReport(lastEntry.id);
        if (!data || !data.report) return;
        setMessages((cur) =>
          cur.some((m) => m && m.type === "report") ? cur : [...cur, reportMsgFromServer(data, lastEntry)]
        );
        showToast("已从服务器恢复报告正文");
      } catch (e) {
        console.warn("[存储] 启动补齐失败（忽略）", e);
      }
    })();
    // 只在挂载时跑一次：用挂载瞬间的初始消息做体检
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 服务端存档报告：历史抽屉一打开就拉一次列表（只回短字段：id/question/ts/verdict/chars，不含正文）。
  // 全程静默：接口挂了/空都只显示"暂无服务端存档报告"一行灰字，绝不弹错误——
  // 它是会话列表之外的**补充入口**，不是主流程，服务端不可用时不该打断用户手头的操作。
  useEffect(() => {
    if (!historyOpen) return undefined;
    let alive = true; // 抽屉关得比请求快时丢弃回包，避免往已卸载的视图写状态
    (async () => {
      try {
        const data = await api.listReports(20);
        if (alive) setSrvReports(Array.isArray(data && data.reports) ? data.reports : []);
      } catch (e) {
        console.warn("[历史报告] 服务端存档列表拉取失败（静默）", e);
        if (alive) setSrvReports([]);
      }
    })();
    return () => {
      alive = false;
    };
  }, [historyOpen, srvTick]);

  // 关抽屉即清空搜索词。为什么用 effect 而不是在 ✕ 按钮里 setHistQuery("")：
  // 关抽屉的入口有七八个（✕ / 右上 ☰ 再点一次 / 打开某个会话 / 新建对话 / 设置 / 资产库 / 运行台账 /
  // 载入存档报告），逐个补一句迟早漏掉一个——漏掉的那个就会让下次打开还带着过滤，
  // 用户看到"历史是空的"会先怀疑数据丢了。
  useEffect(() => {
    if (!historyOpen) setHistQuery("");
  }, [historyOpen]);

  // 新消息贴底：本就在底部时跟随流式更新；用户上滑阅读则不强拉（有悬浮"↓到底"按钮可手动回）
  useEffect(() => {
    if (!nearBottom()) return;
    const sc = chatMainRef.current;
    if (sc) requestAnimationFrame(() => { sc.scrollTop = sc.scrollHeight; });
  }, [messages]);

  // ── 数据源面板的"定格"（v0.9.62）───────────────────────────────────────────
  // 用户原话："点其中一个链接跳走，返回时整块折叠了，于是不知道刚点的是哪一条"。
  // 治本是对外链加 target="_blank"（见文件头 marked 渲染器 + 两处 <a>），这里做兜底：
  // 万一还是发生了整页重载（用户手动刷新 / 浏览器策略），把"开着哪条引文"从会话存储里捡回来。
  //
  // 防串台是这里的重点：sessionStorage 是**标签页级**的，用户可能已经切到另一个会话再刷新，
  // 而那个会话里根本没有 msgId 这条消息。所以恢复时必须三重校验：消息还在、类型还是 report、
  // idx 还在该报告的引文范围内；任一不满足就**丢弃并清记录**，绝不凭空开一个面板
  // （开错面板比不开更糟：用户会以为这是当前这轮调研的来源）。
  useEffect(() => {
    let raw = null;
    try {
      raw = sessionStorage.getItem(CITE_PANEL_SS_KEY);
    } catch {}
    if (!raw) return;
    let ok = false;
    try {
      const s = JSON.parse(raw);
      const msg = messages.find((m) => m && m.type === "report" && m.localId === s?.msgId);
      if (msg && Number.isInteger(s.idx) && s.idx >= 0 && s.idx < (msg.citations || []).length) {
        setExpandedCitation({ msgId: s.msgId, idx: s.idx });
        ok = true;
      }
    } catch {}
    if (!ok) {
      // 串台 / 记录损坏 / 那条引文早已不在 → 丢掉，不留着它下次再误导一次
      try {
        sessionStorage.removeItem(CITE_PANEL_SS_KEY);
      } catch {}
    }
    // 只在挂载时读一次：之后面板的开合由用户点击驱动，反过来再同步会把用户的手点覆盖掉
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 面板开合 → 写会话存储。用 effect 而不是往 setExpandedCitation 的四个调用点各补一句：
  // 关面板的入口有报告角标、面板 ✕、新建对话、切换会话等好几处，逐个补必漏；这里一处收口。
  // 关面板只清**面板**记录，滚动位置故意留着（用户可能只是想关掉面板继续读那段）。
  useEffect(() => {
    try {
      if (expandedCitation) {
        sessionStorage.setItem(CITE_PANEL_SS_KEY, JSON.stringify(expandedCitation));
      } else {
        sessionStorage.removeItem(CITE_PANEL_SS_KEY);
      }
    } catch {}
  }, [expandedCitation]);

  // 全局错误兜底：任何未捕获错误/Promise拒绝 → toast 显示（消灭"白屏却不知错在哪"）
  useEffect(() => {
    const onErr = (e) => {
      console.error("未捕获错误:", e.error || e.message || e);
      showToast(`运行出错：${e.message || e.error?.message || String(e.error || e)}`);
    };
    const onRej = (e) => {
      console.error("未捕获Promise拒绝:", e.reason);
      showToast(`请求异常：${e.reason?.message || String(e.reason)}`);
    };
    window.addEventListener("error", onErr);
    window.addEventListener("unhandledrejection", onRej);
    return () => {
      window.removeEventListener("error", onErr);
      window.removeEventListener("unhandledrejection", onRej);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 外链一律"新标签页"打开：点数据源返回后，聊天内容/滚动位置/展开面板原样保留（不再整页刷新回到最初）
  useEffect(() => {
    const onDocClick = (e) => {
      if (e.defaultPrevented || e.button !== 0) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const tgt = e.target && e.target.nodeType === 1 ? e.target : e.target && e.target.parentElement;
      const a = tgt && tgt.closest ? tgt.closest("a[href]") : null;
      if (!a) return;
      const href = a.getAttribute("href") || "";
      if (!/^(https?:)?\/\//i.test(href)) return; // http(s):// 与 //host 协议相对链接都拦
      const t = (a.getAttribute("target") || "").toLowerCase();
      if (t && t !== "_self") return; // 已有新标签意图 → 交给浏览器
      e.preventDefault();
      window.open(href, "_blank", "noopener");
    };
    document.addEventListener("click", onDocClick, true);
    return () => document.removeEventListener("click", onDocClick, true);
  }, []);

  // 提交提问（附件解析内容纳入调研上下文）
  async function handleSend() {
    const q = input.trim();
    if (!q || loading) return;
    // 深挖/划词询问优先：处于深挖模式时，输入都路由到"对所选内容的追问"，不误续旧任务
    if (deepDiveTarget) {
      const target = deepDiveTarget;
      setDeepDiveTarget(null); // 退出深挖模式
      setInput("");
      setError("");
      await handleDeepDive(target.claim, q);
      return;
    }
    // 继续指令：存在"已暂停"的执行消息时，发"继续"就恢复执行（不是新问题）
    const resumeMsg = messages.find(
      (m) => m.type === "executing" && m.status === "paused"
    );
    if (resumeMsg && /^(继续|继续执行|接着做|接着|resume|继续跑|继续吧)$/i.test(q)) {
      setInput("");
      await handleExecute(resumeMsg.plan, {}, { resume: true, msgId: resumeMsg.localId });
      return;
    }
    setInput("");
    setError("");
    setLoading(true);
    // 附件内容摘要并入提问（多模态：价目表/文档 → 调研上下文）
    //
    // ⚠️ **截断必须披露**（2026-09-16 全量审计）：后端 `_parse_upload` 只回
    // `content[:500]`，而这里**静默**把它拼进提问——实测一份 37,260 字的附件
    // 只有 500 字到达模型，用户和模型都以为"附件给了"。
    // 现在：① 后端回 `chars_total/chars_used`；② 这里把差额写进提问本身
    // ——**模型也需要知道它看的是节选**，否则它会把"节选里没有"答成"资料里没有"。
    const attachSummaries = attachments
      .map((a) => {
        const used = a.summary || a.name;
        const total = Number(a.chars_total || 0);
        if (total > used.length) {
          return `${used}\n（注：本附件共 ${total.toLocaleString()} 字，此处仅带入前 ${used.length} 字节选，其余未参与本次分析）`;
        }
        return used;
      })
      .join("\n");
    const fullQuestion = attachSummaries ? `${q}\n\n[附件资料]\n${attachSummaries}` : q;
    setMessages((m) => [...m, { role: "user", content: fullQuestion, attachments }]);
    setAttachments([]);
    requestAnimationFrame(() => scrollChat(true)); // 新问题 → 立即把视野沉到最新处（长对话顶部回跳）
    // 记录会话（新会话时才创建，用于历史列表标题）
    // at = 真毫秒时间戳（新增字段）：ts 只有时分秒、连日期都没有，没法据它分组，
    // ts 保持不变（老数据只有 ts，向后兼容 + 显示兜底）。
    if (!sessionId) {
      const newId = `sess_${Date.now()}`;
      setSessionId(newId);
      setSessions((s) => [{ id: newId, title: q.slice(0, 20), ts: new Date().toLocaleTimeString(), at: Date.now() }, ...s]);
    } else {
      setSessions((s) => s.map((x) => (x.id === sessionId ? { ...x, title: q.slice(0, 20) } : x)));
    }
    // 渐进式执行：直接进入思考过程（不再先出"确认计划"阻塞卡）。
    // 计划由后端在流内生成 → 以 plan 事件内联展示（可暂停编辑/继续）
    await handleExecute(null, {}, {}, fullQuestion);
  }

  // 打开资产库（行业档案沉淀可见）
  async function openAssets() {
    openDrawer("assets");
    try {
      const res = await fetch(`${apiBase}/api/assets`).then((r) => r.json());
      setAssets(res);
      // 两张网并排取：出问题也不互相拖累（一张挂了另一张照显）
      fetch(`${apiBase}/api/industry-cards`)
        .then((r) => r.json())
        .then(setIndustryCards)
        .catch((e) => setIndustryCards({ count: 0, cards: [], __error: String(e?.message || e) }));
    } catch (e) {
      // ⚠️ **读失败 ≠ 0**（2026-09-16 全量审计）：这里原先回 `{count: 0, ...}`，
      // 界面于是印「资产库 0 个」——把"我们没读到"说成了"你没有"。
      // 与本仓库"缺测显示 — 不显示 0"是同一条纪律，只是这次发生在前端。
      setAssets({ count: 0, groups: {}, items: [], __error: String(e?.message || e) });
    }
  }

  // 打开自主性图谱（每个编排节点：谁在决定 / 影响多大 / 有没有护栏 / 可不可逆）
  async function openAutonomy() {
    openDrawer("autonomy");
    try {
      const res = await fetch(`${apiBase}/api/autonomy-map`).then((r) => r.json());
      setAutonomy(res);
    } catch (e) {
      setAutonomy({ summary: null, nodes: [], __error: String(e?.message || e) });
    }
  }

  // 打开运行台账（真实 API 执行账本：档位/耗时/成本/引文/判定）
  async function openRunStats() {
    openDrawer("runStats");
    try {
      const res = await fetch(`${apiBase}/api/run-stats`).then((r) => r.json());
      setRunStats(res);
    } catch (e) {
      // **读失败不许伪装成「0 笔」**（2026-09-16）：原来这里回 `{total: 0, rows: []}`，
      // 抽屉于是印出「共 0 笔真实运行」+「暂无账本——跑 3 笔以上深入档后回填」。
      // 对着一屏"0 笔"，复核者和用户都只会得出一个结论：**你没跑过**。
      // 而真相可能是后端没起来。台账是我们唯一敢让人当场核对的东西，这里不能含糊。
      setRunStats({ total: 0, count: 0, rows: [], __error: String(e?.message || e) });
    }
    // 最小复购看板（四指标）：赛道培训里点名建议带上台的东西，
    // 与台账同屏——台账是原始行，看板是从同一批行按**公开口径**算出来的结论。
    try {
      const b = await fetch(`${apiBase}/api/repurchase-board`).then((r) => r.json());
      setBoard(b);
    } catch (e) {
      setBoard({ __error: String(e?.message || e) });
    }
  }

  // 演示账户充值（2026-09-19 接通）。
  // 病案：`RECHARGE_TIERS` 一直随 `/api/billing` 送到前端，界面上却**没有任何入口
  // 能改变余额**——价目表是摆着看的。现在后端有 `POST /api/recharge`，这里接上。
  // **不接真实支付，也不假装接了**：返回里的 `simulated` 决定措辞。
  async function doRecharge(amount) {
    try {
      const r = await fetch(`${apiBase}/api/recharge`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ amount }),
      }).then((x) => x.json());
      if (r?.ok === false) {
        showToast(`充值未生效：${r.reason || "未知原因"}`);
        return;
      }
      if (r?.billing) {
        setBilling(r.billing);
        setPointsBalance(r.billing.balance);
      }
      showToast(`演示账户已到账 +${r?.added ?? 0} 积分（未接入真实支付）`);
    } catch (e) {
      showToast(`充值请求失败：${e?.message || e}`);
    }
  }

  // 打开工具合同（十张合同的四维：目的/输入输出/权限/失败处理 + 审计）
  async function openTools() {
    openDrawer("tools");
    try {
      setTools(await fetch(`${apiBase}/api/tools`).then((r) => r.json()));
    } catch (e) {
      // 与其余抽屉同一条纪律：**读失败不许印成「0 张」**（那会被读成"你没做"）
      setTools({ tools: [], __error: String(e?.message || e) });
    }
  }

  // 打开记忆（它记住了什么：行业 / 偏好 / 会话 / 决策）
  async function openMemory() {
    openDrawer("memory");
    try {
      setMemory(await fetch(`${apiBase}/api/memory`).then((r) => r.json()));
    } catch (e) {
      setMemory({ __error: String(e?.message || e) });
    }
  }

  // 决策回访：打开只拉历史（零成本，不发起检索）；「现场回访」才真的去查
  async function openOutcome() {
    openDrawer("outcome");
    try {
      setOutcome(await fetch(`${apiBase}/api/outcome-review`).then((r) => r.json()));
    } catch {
      setOutcome({ in_flight: [], reviews: [], current: null });
    }
  }
  async function runOutcome() {
    setOutcomeBusy(true);
    try {
      const r = await fetch(`${apiBase}/api/outcome-review?run_now=1&max_conds=3`, { method: "POST" });
      setOutcome(await r.json());
    } catch (e) {
      setError?.(String(e));
    } finally {
      setOutcomeBusy(false);
    }
  }

  // 提交澄清答案（单选/多选按钮）→ 生成计划
  async function handleConfirm(answers) {
    setLoading(true);
    setError("");
    try {
      // 把多选数组转成字符串（后端 context 需要 str）：单选取第一个，多选用逗号连接
      const strAnswers = {};
      for (const [key, val] of Object.entries(answers)) {
        strAnswers[key] = Array.isArray(val) ? val.join("、") : String(val);
      }
      strAnswers.depth = depth; // 带上深度档
      const res = await api.confirm(
        messages.filter((m) => m.role === "user").at(-1)?.content || "",
        strAnswers
      );
      setMessages((m) => [
        ...m,
        { role: "assistant", type: "plan", plan: res.plan, depth },
      ]);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  // 执行调研（SSE 流式，可暂停）——携带用户编辑后的计划
  // extraContext：Stateful HITL——用户手动补参（rework_manual）随重跑注入
  async function handleExecute(plan, extraContext = {}, opts = {}, question = "") {
    // opts.resume=true：暂停后继续——用同一 thread_id 从后端断点续跑，复用旧执行消息
    const isResume = !!opts.resume;
    // 互斥闸：已有流在执行时禁止再开第二条（否则暂停手柄/loading 会互相踩踏）
    if (loading) {
      showToast("有调研在执行中，请先等它完成或暂停，再继续 / 重新生成");
      return;
    }
    setLoading(true);
    setError("");
    if (!isResume) {
      // 点击执行 → 自动收起研究计划（让思考过程成为主角）
      setMessages((msgs) =>
        msgs.map((m) => (m.type === "plan" ? { ...m, collapsed: true } : m))
      );
    }
    try {
      const q = question || messages.filter((m) => m.role === "user").at(-1)?.content || "";
      const msgId = opts.msgId || Date.now();
      if (!isResume) {
        setMessages((m) => [
          ...m,
          {
            role: "assistant",
            type: "executing",
            localId: msgId,
            question: q,
            plan,
            trace: [],
            evidences: [],
            conflicts: [],
            status: "running",
          },
        ]);
      } else {
        // 恢复：旧执行消息状态改回 running，后续事件继续追加（不新增消息、不重复已完成的步骤）
        setMessages((msgs) =>
          msgs.map((m) =>
            m.localId === msgId && m.type === "executing"
              ? { ...m, status: "running" }
              : m
          )
        );
      }
      if (!isResume) requestAnimationFrame(() => scrollChat(true)); // 新任务开跑 → 视野沉底看新流

      const controller = new AbortController();
      setStreamAbort(controller);

      const resp = await fetch(`${apiBase}/api/execute-stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question: q,
          context: { depth, ...plan?.clarified_context, ...extraContext },
          plan: plan || null, // 把用户编辑后的计划传回后端（真正生效）
          thread_id: String(msgId), // 暂停后继续用同一id，后端从checkpoint续跑
          resume: isResume,
        }),
        signal: controller.signal,
      });
      if (!resp.ok) throw new Error(`API错误 ${resp.status}`);
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split("\n\n");
        buffer = events.pop() || "";
        for (const evText of events) {
          const line = evText.split("\n").find((l) => l.startsWith("data: "));
          if (!line) continue;
          let ev;
          try {
            ev = JSON.parse(line.slice(6));
          } catch {
            continue;
          }
          updateExecutingMsg(msgId, ev, q);
        }
      }
    } catch (e) {
      if (e.name === "AbortError") {
        // 用户暂停 / 重跑：标记为已暂停（但不要覆盖"已用补充数据重跑"状态）
        setMessages((msgs) =>
          msgs.map((m) =>
            m.localId === msgId
              ? m.status === "restarted"
                ? m
                : { ...m, status: "paused" }
              : m
          )
        );
      } else {
        // ⚠️ **传输层失败必须把执行卡片落下**（2026-09-16 全量审计）。
        // 病案：这里原先只 `setError(e.message)`——而**后端发来的 error 事件**走的是
        // `updateExecutingMsg` 的 `ev.type === "error"` 分支（那条是好的）。
        // 两条路分得很清：网络断开 / HTTP 5xx / `!resp.ok` 时**根本没有 ev**，
        // 于是执行消息永远停在 `status: "running"` ——卡片一直转着
        // 「研策 · 正在执行」，而任务早已不在跑了。**演示现场一次抖动就定格成这样。**
        // 页面顶部那行 error 提示会在下一次操作时被清掉，卡片却会一直留着。
        setError(e.message);
        setMessages((msgs) =>
          msgs.map((m) =>
            m.localId === msgId && m.type === "executing" && m.status === "running"
              ? { ...m, status: "failed", error: `连接中断：${e.message}` }
              : m
          )
        );
      }
    } finally {
      setLoading(false);
      setStreamAbort(null);
    }
  }

  // 重新生成：对当前报告不满意 → 用原问题从头再跑一轮（结果以最新为准）
  async function handleRegenerate(question) {
    const q = (question || "").trim();
    if (!q || loading) return;
    setError("");
    await handleExecute(null, {}, {}, q);
  }

  // 一手验证方案（AI 深访/问卷设计）：生成问卷+深访+实地方案，报告卡内联展示
  async function handlePrimaryResearch(msgId, report) {
    setMessages((msgs) =>
      msgs.map((m) =>
        m.localId === msgId
          ? { ...m, primaryOpen: true, primaryLoading: true, primaryError: "", simMd: "", simCsv: "", simLoading: false, simError: "" }
          : m
      )
    );
    try {
      const res = await fetch(`${apiBase}/api/primary-research`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ report: report || "" }),
      }).then((r) => r.json());
      setMessages((msgs) =>
        msgs.map((m) =>
          m.localId === msgId
            ? { ...m, primaryLoading: false, primaryMd: res.md || "", primaryDesign: res.design || null }
            : m
        )
      );
    } catch (e) {
      setMessages((msgs) =>
        msgs.map((m) =>
          m.localId === msgId
            ? { ...m, primaryLoading: false, primaryError: String(e?.message || e) }
            : m
        )
      );
    }
  }

  // 一手研究·模拟问卷数据（演示闭环）：问卷设计好 → 生成模拟样本+分析 → 数据反哺结论
  async function handleSimulateSurvey(msgId, report, design) {
    setMessages((msgs) =>
      msgs.map((m) =>
        m.localId === msgId ? { ...m, simLoading: true, simError: "" } : m
      )
    );
    try {
      const res = await fetch(`${apiBase}/api/primary-research/simulate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ report: report || "", design: design || null }),
      }).then((r) => r.json());
      setMessages((msgs) =>
        msgs.map((m) =>
          m.localId === msgId ? { ...m, simLoading: false, simMd: res.md || "", simCsv: res.csv || "" } : m
        )
      );
    } catch (e) {
      setMessages((msgs) =>
        msgs.map((m) =>
          m.localId === msgId ? { ...m, simLoading: false, simError: String(e?.message || e) } : m
        )
      );
    }
  }

  // 暂停执行
  function handlePause() {
    try {
      if (streamAbort) streamAbort.abort();
    } catch (e) {
      console.warn("停止流失败:", e);
    }
    // 乐观更新：即使 abort 抛错，界面也立刻反映"已暂停"，避免渲染异常
    setMessages((msgs) =>
      msgs.map((m) => (m.type === "executing" && m.status === "running" ? { ...m, status: "paused" } : m))
    );
  }

  // Stateful HITL：用户在 StopCard 手动补数据 → 停掉当前流，带补参重新执行
  async function handleRerunWithManual(msgId, plan, manualInput) {
    try {
      if (!manualInput?.trim()) {
        // 无输入 → 等价于"继续返工"（收起卡片，后台返工继续）
        setMessages((msgs) =>
          msgs.map((x) => (x.localId === msgId ? { ...x, stopDismissed: true } : x))
        );
        return;
      }
      if (streamAbort) streamAbort.abort(); // 停掉当前流
      setStreamAbort(null);
      // 旧执行消息标记为"已用补充数据重跑"，隐藏其停止卡片
      setMessages((msgs) =>
        msgs.map((x) => (x.localId === msgId ? { ...x, status: "restarted" } : x))
      );
      await handleExecute(plan, { rework_manual: manualInput.trim() });
    } catch (e) {
      // 任何异常：显示 toast 而非白屏
      showToast(`重跑失败：${e?.message || e}`);
      setLoading(false);
    }
  }

  // 更新执行中卡片（整体防御：内部异常不拖垮 React，转 toast + 日志）
  // question：本次调研的原始问题（流循环里已知，随事件带进来——组件闭包里的 messages 是本次执行开始前的旧值，查不到刚入队的执行消息）
  function updateExecutingMsg(msgId, ev, question = "") {
    // 防弹：msgId 无效（undefined/null/NaN）直接跳过，绝不崩 React
    if (msgId === undefined || msgId === null || Number.isNaN(Number(msgId))) {
      console.warn("updateExecutingMsg 收到无效 msgId，跳过事件", ev?.type);
      return;
    }
    try {
      setMessages((msgs) =>
        msgs.map((m) => {
          if (!(m.localId === msgId && m.type === "executing")) return m;
          const next = { ...m };
          if (ev.type === "trace") {
            next.trace = [...(m.trace || []), { node: ev.node, detail: ev.detail, status: ev.status }];
          } else if (ev.type === "evidence") {
            next.evidences = [...(m.evidences || []), ev.evidence];
          } else if (ev.type === "conflict") {
            next.conflicts = [...(m.conflicts || []), ev.conflict];
          } else if (ev.type === "plan") {
            next.plan = ev.plan; // 渐进式：计划在流内生成 → 思考流中内联展示
          } else if (ev.type === "billing") {
            next.billing = ev.preview; // 档位+积分计量：预计消耗/余额
            if (ev.preview && typeof ev.preview.balance === "number") {
              setPointsBalance(ev.preview.balance); // 头部积分徽章实时更新
            }
          } else if (ev.type === "budget") {
            next.budgetWarning = ev; // 预算预警：弹"继续/中止"
          } else if (ev.type === "stop") {
            next.stopCard = ev.stop_card; // 人工接管：质检BLOCK → 显示停止卡片
          } else if (ev.type === "done") {
            next.status = "done";
            next.report = ev.result?.report_final || "";
            next.badUrls = ev.result?.bad_urls || []; // 坏URL集：彻底失效的来源据此隐藏链接
            next.urlFixes = ev.result?.url_fixes || {}; // 域名回退映射：失效页→机构官网可点链接
            // 关键：用后端核验并清洗后的 evidences 覆盖流式原始证据（死链 url 已被清空），
            // 否则执行中累积的"未核验来源链接"在完成后仍可点404
            next.evidences = ev.result?.evidences || m.evidences || [];
          } else if (ev.type === "error") {
            next.status = "failed";
            next.error = ev.error;
          }
          return next;
        })
      );
      if (ev.type === "done") {
        const doneCits = ev.result?.citations || [];
        if (!doneCits.length) console.warn("done事件citations为空（后端未产出引用）");
        const serverReportId = ev.result?.report_id || null; // 服务端存档 id：正文丢了可凭它找回
        const doneQuestion = question || ""; // 流循环带进来的原始问题（与执行消息上的 question 同源）
        setMessages((msgs) => [
          ...msgs,
          {
            role: "assistant",
            type: "report",
            _reveal: true, // 刚跑完的新报告 → ReportCard 打字机逐块展示；历史回放会剥掉此项
            localId: msgId, // 与执行消息同 id：引用面板按消息精确开合，互不串扰
            serverReportId, // 服务端存档 id（没落盘时凭它找回正文）
            report: ev.result?.report_final || "",
            citations: doneCits,
            badUrls: ev.result?.bad_urls || [],
            urlFixes: ev.result?.url_fixes || {},
            billing: msgs.find((x) => x.localId === msgId && x.type === "executing")?.billing || null,
            metrics: ev.result?.metrics || [], // 结构化核心指标 → 交付物头 CSS 条 / 作品页导出
            // 引用可信度两层读数（交付物头部角标；也是本版唯一"能被当场证伪"的指标）
            entailment: ev.result?.entailment || {},
            citationPassRate: ev.result?.citation_pass_rate ?? null,
            // 通过率的**分母**（真的敲过门的条数）与未探测条数：角标要能区分
            // 「0% 全坏」与「一条都没敲」。
            citationProbed: ev.result?.citation_report?.probed ?? null,
            citationSkipped: ev.result?.citation_report?.skipped ?? null,
            // 降级可见（v0.9.75）：决策清单走兜底时头部要打红角标——报告其余部分正常，
            // 只有这一节是模板，没有角标就没人会知道
            sopFallback: ev.result?.sop_fallback || {},
            decisionChain: ev.result?.decision_chain || [], // 自主决策链 → 交付物头「🧠 决策链」
            question: doneQuestion,
            evidenceCount: ev.result?.evidences?.length || 0,
            conflicts: ev.result?.conflict_results?.length || 0,
            // 仲裁的**分母**（trunk_matched / pairs_compared）。没有它，「0 处仲裁」读不出
            // 是"查过没有"还是"根本没查"——而评审会直接问这一处。
            conflictScan: ev.result?.conflict_scan || null,
            // 决策完整性闸门结论（PASS/WARN/BLOCK + 是否降级）。有它才能在交付物头部
            // 打出"本次判定被闸门降过"的角标——正文改了而头部不显示，等于读者第一屏看不到。
            integrityGate: ev.result?.integrity_gate || null,
            validationTasks: ev.result?.validation_tasks || [],
            monitorConditions: ev.result?.monitor_conditions || [],
            decisionVerdict: ev.result?.decision_verdict || "",
            // 可复算包元信息（bundle_id/文件清单/大小）→ 报告页"📦 可复算包"按钮下载
            auditBundle: ev.result?.audit_bundle || null,
          },
        ]);
        // 索引只记短字段（绝不存正文）：刷新后消息体若被配额吞掉，凭 localId → id 找回正文
        if (serverReportId) {
          rememberReportIndex({
            id: serverReportId,
            localId: msgId,
            question: doneQuestion,
            ts: new Date().toISOString(),
          });
        }
        // 报告生成后自动打开右侧数据源面板（引用非空时），让用户一眼看到真实来源
        if (doneCits.length) {
          setExpandedCitation({ msgId, idx: 0 });
        }
      }
    } catch (err) {
      console.error("updateExecutingMsg 崩溃:", err);
      showToast(`处理执行事件出错：${err?.message || err}`);
    }
  }

  // 追问闭环：对报告某句发起深挖
  async function handleDeepDive(claim, userIntent) {
    setLoading(true);
    setError("");
    try {
      // 用户深挖意图入对话流
      setMessages((m) => [
        ...m,
        {
          role: "user",
          content: `🔍 再深挖：「${claim.slice(0, 60)}」${userIntent ? `\n关注点：${userIntent}` : ""}`,
        },
      ]);
      setMessages((m) => [...m, { role: "assistant", type: "typing", content: "正在深挖该结论…" }]);
      const res = await api.deepDive(claim, userIntent);
      setMessages((m) => m.filter((x) => x.type !== "typing"));
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          type: "deep_dive",
          claim,
          conclusion: res.deep_conclusion || "",
          sub_questions: res.sub_questions || [],
          evidenceCount: res.new_evidences?.length || 0,
        },
      ]);
    } catch (e) {
      setMessages((m) => m.filter((x) => x.type !== "typing"));
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  // 进入深挖模式：点击"再深挖"后，输入框左侧出现标识，用户自由输入想深挖的内容
  function enterDeepDive(claim) {
    setDeepDiveTarget({ claim });
    setError("");
    if (inputRef.current) inputRef.current.focus();
  }

  // 可编辑子任务
  function updateSubtask(msgIndex, stId, field, value) {
    setMessages((msgs) =>
      msgs.map((m, i) =>
        i === msgIndex && m.type === "plan"
          ? {
              ...m,
              plan: {
                ...m.plan,
                subtasks: m.plan.subtasks.map((st) =>
                  st.id === stId ? { ...st, [field]: value } : st
                ),
              },
            }
          : m
      )
    );
  }

  function addSubtask(msgIndex) {
    setMessages((msgs) =>
      msgs.map((m, i) => {
        if (i !== msgIndex || m.type !== "plan") return m;
        if ((m.plan?.subtasks || []).length >= 12) return m; // 上限：防滥用
        return {
          ...m,
          plan: {
            ...m.plan,
            subtasks: [
              ...m.plan.subtasks,
              { id: `st_${Date.now()}`, agent_type: "", description: "", search_queries: [] },
            ],
          },
        };
      })
    );
  }

  function removeSubtask(msgIndex, stId) {
    setMessages((msgs) =>
      msgs.map((m, i) =>
        i === msgIndex && m.type === "plan"
          ? {
              ...m,
              plan: {
                ...m.plan,
                subtasks: m.plan.subtasks.filter((st) => st.id !== stId),
              },
            }
          : m
      )
    );
  }

  // 折叠/展开研究计划（箭头收放）
  function togglePlanCollapse(msgIndex) {
    setMessages((msgs) =>
      msgs.map((m, i) =>
        i === msgIndex && m.type === "plan" ? { ...m, collapsed: !m.collapsed } : m
      )
    );
  }

  // 上传文件 → 后端解析内容（多模态）
  async function handleFileUpload(e) {
    const files = Array.from(e.target.files || []);
    if (fileInputRef.current) fileInputRef.current.value = "";
    for (const f of files) {
      try {
        const parsed = await api.upload(f);
        setAttachments((prev) => [
          ...prev,
          {
            name: parsed.filename,
            summary: parsed.summary || parsed.content || "",
            structured: parsed.structured || [],
            // 附件原始字数：提问里要据此披露"只带了前 N 字"（见 send() 处病案）
            chars_total: parsed.chars_total || 0,
          },
        ]);
      } catch (err) {
        setError(err.message);
        // 解析失败也保留文件名，至少用户知道已选择
        setAttachments((prev) => [...prev, { name: f.name, summary: "", structured: [] }]);
      }
    }
  }

  // 新建对话
  function handleNewChat() {
    setMessages([]);
    setSessionId(null);
    try {
      localStorage.removeItem("yancec_cur_session");
    } catch {}
    setError("");
    // 关闭所有抽屉：任一个残留都会让 drawer-open class 常驻
    // → 主内容区 margin-left:320px 永久左移（用户报的"输入框左移/内容全在左边"）。
    // **不再手工罗列**（2026-09-16）：这里原先是四行 setXxxOpen(false) 的硬编码清单，
    // 而清单必然会漏掉后加的抽屉（漏掉的那个就是「决策回访」，后来又漏了「自主性图谱」）
    // ——`openDrawer(null)` 走 DRAWERS 唯一收口，加抽屉时这里一个字都不用改。
    openDrawer(null);
    setDeepDiveTarget(null);
    setExpandedCitation(null);
  }

  // 打开历史会话（恢复该会话的消息 + 持久化当前会话）
  function openSession(id) {
    let loaded = [];
    try {
      const all = JSON.parse(localStorage.getItem("yancec_sessions_msgs") || "{}");
      // 历史回放不重播打字机：剥掉 _reveal（报告/深挖等消息全文直接展示）
      loaded = (all[id] || []).map((x) => ({ ...x, _reveal: false }));
    } catch {
      loaded = [];
    }
    setMessages(loaded);
    setSessionId(id);
    try {
      localStorage.setItem("yancec_cur_session", id);
    } catch {}
    setHistoryOpen(false);
    setSettingsOpen(false);
    setAssetsOpen(false);
    setRunStatsOpen(false);
    setDeepDiveTarget(null);
    setExpandedCitation(null);
    // 本地只留了预览/指针的报告 → 从服务端把全文取回来（内容不该因为浏览器配额而消失）
    const seq = ++_sessionSeq;
    healReportBodies(loaded).then(({ list, healed, failed }) => {
      if (seq !== _sessionSeq) return; // 用户已切到别的会话 → 丢弃这次回包
      if (healed) {
        // ⚠️ **不要拿 openSession 当时那份 `list` 整体覆盖**（2026-09-16 全量审计）。
        // 取回正文是异步的（逐份请求，几十份就要几秒），这期间用户可能已经发了新消息、
        // 或流式事件已经追加了新的执行卡片——`setMessages(list)` 会把它们**整段抹掉**，
        // 而且 `list` 是**载入时**的快照。
        // `_sessionSeq` 那道守卫只防"切了会话"，防不住"同一会话里来了新消息"。
        // 改成**按 localId 把补好的那份并回当前消息**：只补内容，不动数组结构。
        const byId = new Map(list.map((x) => [x.localId, x]));
        setMessages((cur) =>
          cur.map((m) => {
            const patched = byId.get(m.localId);
            return patched && m.type === "report" ? { ...m, ...patched } : m;
          })
        );
      }
      if (failed) showToast(`有 ${failed} 份历史报告正文已不在存档中（超过保留期）`);
    });
  }

  // 载入一条服务端存档报告：追加到**当前对话末尾**（不改状态机；历史一律不重播打字机）。
  // 与"启动补齐"是同一件事的两个入口：那边是本地消息体被配额吞掉后自动补，这边是用户主动捞。
  // localId 用 srv_<id>：与本地消息（stored_*/本次跑的 uuid/recovered_*）天然不撞；
  // serverReportId 也一并带上 → 万一这次追加的消息体又被配额吞掉，下次刷新仍能凭它补回正文。
  async function loadSrvReport(id) {
    try {
      const data = await api.getReport(id);
      if (!data || !data.report) {
        // 404 走这里：服务端没落档或已被清理，属正常情况（不是异常），提示一句即可
        console.warn("[历史报告] 该存档已不在服务端（可能已被清理）", id);
        showToast("载入失败：报告可能已被清理");
        return;
      }
      setMessages((cur) => [
        ...cur,
        reportMsgFromServer(data, { id, localId: `srv_${id}`, question: data.question || "" }),
      ]);
      openDrawer(null);   // 唯一收口：不手工罗列（见 DRAWERS 处病案）
      showToast("已载入服务端存档报告");
    } catch (e) {
      console.warn("[历史报告] 服务端存档载入失败", id, e);
      showToast("载入失败：报告可能已被清理");
    }
  }

  // —— 历史抽屉：搜索词 / 分组 / 服务端存档过滤（纯本地计算，不发任何请求） ——
  // 索引只在抽屉打开、会话列表变化时解析一次；抽屉关闭时直接给空对象（短路，不读 localStorage）。
  // 依赖里挂 sessions 是**故意**的缓存失效键（回调本体不读它，所以 lint 会报"多余依赖"）：
  // 发了新问题/换会话后快照变了，不重建索引就会拿着旧问题搜不到。
  const histIndex = useMemo(
    () => (historyOpen ? histSearchIndex() : {}),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [historyOpen, sessions]
  );
  const histKw = histQuery.trim().toLowerCase();
  // 匹配范围：标题 ∪ 该会话消息里的完整 question（标题只截了 20 字，够不着用户记得的那半句）
  const histMatched = histKw
    ? sessions.filter((s) =>
        `${s.title || ""}\n${histIndex[s.id] || ""}`.toLowerCase().includes(histKw)
      )
    : [];
  // 分组只在无搜索词时用（搜索态刻意平铺，见下方渲染注释）
  const histGroups = histGroupSessions(sessions);
  // 服务端存档同一份词过滤：**只过滤已拉回的数组**，绝不因搜索再打一次接口
  // （拉取时机仍是"抽屉打开 / 点刷新"，见上面的 srvReports effect）
  const srvMatched = histKw
    ? (srvReports || []).filter((r) => String(r.question || "").toLowerCase().includes(histKw))
    : srvReports || [];
  // 会话条目渲染（分组视图与搜索平铺视图共用，避免两处 JSX 漂移）
  const renderSession = (s) => (
    <button key={s.id} className="history-item" onClick={() => openSession(s.id)}>
      <span className="history-title">{s.title}</span>
      <span className="history-time">{histTimeText(s)}</span>
    </button>
  );

  return (
    <ErrorBoundary>
      <div
        className={`chat-app ${expandedCitation ? "panel-open" : ""} ${historyOpen || settingsOpen || assetsOpen || runStatsOpen || outcomeOpen || autonomyOpen || toolsOpen || memoryOpen ? "drawer-open" : ""}`}
        style={{ "--panel-w": `${panelW}px` }}
      >
      {/* 顶栏：左=历史，中=品牌，右=新对话 */}
      <header className="chat-header">
        <div className="header-left">
          {/* ☰ 必须走 `openDrawer` 唯一收口（2026-09-16 修）。
              病案：这里原先是 `setHistoryOpen(!historyOpen)`——**绕过了互斥收口**。
              于是"先开资产库、再点 ☰"就会两个抽屉物理叠在一起（评审点两下即可复现），
              而且它写的注释就在收口函数上方，专讲"手工维护互斥关系，漏一个就是一个界面故障"。
              **注释写了不等于接线了**：收口函数存在，但有一个调用点没走它。 */}
          <button
            className="header-btn"
            onClick={() => (historyOpen ? openDrawer(null) : openDrawer("history"))}
            title="历史对话"
          >
            ☰
          </button>
        </div>
        <div className="header-title">
          <span className="chat-brand">研策</span>
          <span className="chat-brand-sub">市场调研数字员工</span>
        </div>
        <button className="header-newchat" onClick={handleNewChat} title="新建对话">
          <span>✚</span>
          <span className="btn-text">新建对话</span>
        </button>
      </header>

      {/* 历史对话面板（左侧） */}
      {historyOpen && (
        <div className="history-drawer">
          <div className="history-head">
            <span>历史对话</span>
            <button onClick={() => setHistoryOpen(false)}>✕</button>
          </div>
          {/* 搜索框放在滚动容器**外**：跟着列表一起滚走，就等于每搜一次都要先滚回顶部 */}
          <input
            className="hist-search"
            type="text"
            value={histQuery}
            onChange={(e) => setHistQuery(e.target.value)}
            placeholder="搜索历史对话…"
            aria-label="搜索历史对话"
          />
          <div className="history-list">
            {sessions.length === 0 ? (
              // 全部为空时保持原空状态文案，不因新增搜索而改写既有观感
              <p className="history-empty">暂无历史对话</p>
            ) : histKw ? (
              // 搜索态：**刻意不分组**。过滤后组标题会碎成"今天：1 条 / 更早：1 条"，
              // 反而把本来连续的结果切开；平铺 + 一句"共 N 条"最省眼力。
              histMatched.length === 0 ? (
                <p className="hist-empty">没有匹配的对话</p>
              ) : (
                <>
                  {histMatched.map(renderSession)}
                  <p className="hist-count">共 {histMatched.length} 条</p>
                </>
              )
            ) : (
              // 分组态：空组连标题一起不渲染（否则会出现"昨天"下面空一片）
              HIST_GROUPS.map((name) =>
                histGroups[name].length === 0 ? null : (
                  <div className="hist-group" key={name}>
                    <div className="hist-group-title">{name}</div>
                    {histGroups[name].map(renderSession)}
                  </div>
                )
              )
            )}
            {/* 服务端存档报告：本地会话被清/换设备后仍能取回正文的补充入口。
                列表不带正文 → 抽屉打开只花一次轻请求，正文等点开那一下再按 id 单取。 */}
            <div className="srv-report-section">
              <div className="srv-report-head">
                <span className="srv-report-head-title">
                  📚 历史报告（服务端存档）
                  {/* 同一份报告常常记不住是在哪个会话里跑的 → 搜索词同时过滤这一区，
                      命中数摆在标题旁（搜索框在头顶，两处结果同词，用户不用猜为什么少了几条） */}
                  {histKw ? <span className="hist-srv-count">命中 {srvMatched.length}</span> : null}
                </span>
                <button
                  className="srv-report-refresh"
                  onClick={() => setSrvTick((t) => t + 1)}
                  title="刷新服务端存档列表"
                >
                  ⟳
                </button>
              </div>
              {!srvReports || srvReports.length === 0 ? (
                <p className="srv-report-empty">暂无服务端存档报告</p>
              ) : srvMatched.length === 0 ? (
                <p className="srv-report-empty">没有匹配的存档报告</p>
              ) : (
                srvMatched.map((r) => (
                  <button
                    key={r.id}
                    className="history-item srv-report-item"
                    onClick={() => loadSrvReport(r.id)}
                    title={r.question || ""}
                  >
                    <span className="srv-report-title">{_cut2(r.question || "（无问题描述）", 24)}</span>
                    <span className="srv-report-meta">
                      {String(r.ts || "").replace("T", " ").slice(0, 16)} · {r.verdict || "—"} · {r.chars || 0} 字
                    </span>
                  </button>
                ))
              )}
            </div>
          </div>
          <div className="history-foot">
            <button className="settings-btn" onClick={() => openDrawer("settings")}>
              ⚙️ 设置
            </button>
            <button className="settings-btn" onClick={openAssets}>
              📚 资产库
            </button>
            <button className="settings-btn" onClick={openRunStats} title="真实运行账本（耗时/成本/引文/判定）">
              📊 运行台账
            </button>
            <button className="settings-btn" onClick={openAutonomy} title="每个编排节点：谁在决定 / 影响多大 / 有没有护栏 / 可不可逆">
              🧭 自主性图谱
            </button>
            <button className="settings-btn" onClick={openOutcome} title="当初的判断，今天动了没有">
              🔁 决策回访
            </button>
            <button className="settings-btn" onClick={openTools} title="十张工具合同：目的/输入/输出/权限/失败处理/审计">
              🧰 工具合同
            </button>
            <button className="settings-btn" onClick={openMemory} title="它记住了你什么：行业 / 偏好 / 会话 / 决策">
              🧠 记忆
            </button>
          </div>
        </div>
      )}

      {/* 对话区 */}
      <main
        className="chat-main"
        ref={chatMainRef}
        onScroll={handleChatScroll}
      >
        {messages.length === 0 ? (
            <div className="chat-welcome">
            <h1>你想调研什么市场？</h1>
            <p>描述需求，研策会自主拆解、并行检索、生成带证据链的研究报告。点下方真实商业案例即可体验。</p>
            <div className="welcome-chips">
              <button onClick={() => setInput("星巴克等咖啡品牌下沉三四线城市该不该做？下沉门店增速为什么放缓？")}>☕ 星巴克下沉（增速放缓）</button>
              <button onClick={() => setInput("蜜雪冰城 vs 喜茶，平价茶饮进县城该不该扩？单店模型怎么算？")}>🧋 蜜雪冰城 vs 喜茶</button>
              <button onClick={() => setInput("比亚迪10万级电车下沉到县城，能否吃掉燃油车？五菱还守得住吗？")}>🚗 比亚迪 vs 五菱（下沉）</button>
              <button onClick={() => setInput("元气森林等无糖饮料增速放缓，该不该重仓线下渠道？")}>🥤 无糖饮料线下渠道</button>
            </div>
          </div>
        ) : (
          <div className="msg-list">
            {messages.map((m, i) => (
              <Message
                key={i}
                m={m}
                loading={loading}
                onConfirm={handleConfirm}
                onExecute={() => handleExecute(m.plan)}
                onResume={(plan, msgId) => handleExecute(plan, {}, { resume: true, msgId })}
                onPause={handlePause}
                onDismissBudget={(localId) =>
                  setMessages((msgs) =>
                    msgs.map((x) =>
                      x.localId === localId ? { ...x, budgetDismissed: true } : x
                    )
                  )
                }
                onDismissStop={(localId) =>
                  setMessages((msgs) =>
                    msgs.map((x) =>
                      x.localId === localId ? { ...x, stopDismissed: true } : x
                    )
                  )
                }
                onRerunWithManual={(localId, plan, manualInput) =>
                  handleRerunWithManual(localId, plan, manualInput)
                }
                onUpdateSubtask={(stId, f, v) => updateSubtask(i, stId, f, v)}
                onAddSubtask={() => addSubtask(i)}
                onRemoveSubtask={(stId) => removeSubtask(i, stId)}
                onTogglePlanCollapse={() => togglePlanCollapse(i)}
                onEnterDeepDive={enterDeepDive}
                onPrimaryResearch={handlePrimaryResearch}
                onSimulateSurvey={handleSimulateSurvey}
                onRegenerate={m.question ? () => handleRegenerate(m.question) : undefined}
                expandedEvidence={expandedEvidence}
                setExpandedEvidence={setExpandedEvidence}
                expandedCitation={expandedCitation}
                setExpandedCitation={setExpandedCitation}
                panelW={panelW}
                setPanelW={setPanelW}
              />
            ))}
          </div>
        )}
        {error && <div className="chat-error">⚠️ {error}</div>}
        {toast && <div className="chat-toast">{toast}</div>}
        <div ref={endRef} />
      </main>

      {/* 底部输入框（DeepSeek 模式 + 上传 + 居中发送 + 暂停） */}
      <footer className="chat-inputbar">
        <div className="inputbar-inner">
          {/* 回到最新消息：用户上滑阅读时，浮现在输入框右上方（圆形浅底 + 实心下箭头） */}
          {showDown && messages.length > 0 && (
            <button
              type="button"
              className="down-fab"
              title="回到最新消息"
              aria-label="回到最新消息"
              onClick={() => {
                scrollChat(true);
                setShowDown(false);
              }}
            >
              <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true">
                <path
                  d="M6 9.5l6 6 6-6"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="3.2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </button>
          )}
          <div className="input-shell">
            {deepDiveTarget && (
              <span className="deepdive-badge" onClick={() => setDeepDiveTarget(null)} title="点击退出深挖模式">
                🔍 深挖 <em>✕</em>
              </span>
            )}
            <button className="attach-btn" onClick={() => fileInputRef.current?.click()} title="上传文件/图片">
              <span>＋</span>
            </button>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              hidden
              onChange={handleFileUpload}
            />
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  handleSend();
                }
              }}
              placeholder={deepDiveTarget ? "输入你想深挖的内容…" : "输入你想调研的市场…"}
              rows={1}
            />
            {/* Gemini 式：模式选择（紧凑箭头）+ 发送 */}
            <div className="input-right" ref={modeMenuRef}>
              <div className="mode-select" onClick={() => setModeMenuOpen(!modeMenuOpen)} title="选择调研深度">
                <span className="mode-select-current">{depth}</span>
                <span className={`mode-select-arrow ${modeMenuOpen ? "open" : ""}`} />
              </div>
              {modeMenuOpen && (
                <div className="mode-menu">
                  {[
                    { key: "免费", price: "¥0", points: 10, desc: "市场概览" },
                    { key: "标准", price: "¥39", points: 50, desc: "市场全景" },
                    { key: "深入", price: "¥99", points: 150, desc: "+竞争 +用户" },
                    { key: "定制", price: "¥299", points: 400, desc: "+决策SOP" },
                  ].map((opt) => (
                    <button
                      key={opt.key}
                      className={`mode-option ${depth === opt.key ? "active" : ""}`}
                      onClick={() => { setDepth(opt.key); setModeMenuOpen(false); }}
                    >
                      <span className="mode-option-main">
                        <span className="mode-option-name">{opt.key}</span>
                        <span className="mode-option-desc">{opt.desc}</span>
                      </span>
                      <span className="mode-option-price">{opt.points}积分/次</span>
                      {depth === opt.key && <span className="mode-option-check">✓</span>}
                    </button>
                  ))}
                </div>
              )}
              {loading && streamAbort ? (
                <button type="button" className="stop-btn" onClick={handlePause} title="暂停">
                  ■
                </button>
              ) : (
                <button type="button" className="send-btn" onClick={handleSend} disabled={loading || !input.trim()}>
                  ➤
                </button>
              )}
            </div>
          </div>
          {attachments.length > 0 && (
            <div className="attach-list">
              {attachments.map((a, i) => (
                <span key={i} className="attach-chip" title={a.summary || a.name}>
                  📎 {a.name}
                  {a.structured?.length ? ` · 已解析 ${a.structured.length} 项` : ""}
                </span>
              ))}
            </div>
          )}
        </div>
        <span className="points-badge" title="积分余额（档位+积分计量，模式越深消耗越快）">
          ⚡ {pointsBalance ?? "…"}
        </span>
        <span
          className="ver-badge"
          title="点击检查后端版本"
          onClick={async () => {
            try {
              const h = await (await fetch(`${apiBase}/health`)).json();
              showToast(`前端 ${FE_VERSION} · 后端 ${h?.version || "未知"}`);
            } catch {
              showToast("后端未连接");
            }
          }}
        >
          {FE_VERSION}
        </span>
      </footer>

      {/* 左下角设置面板 */}
      {settingsOpen && (
        <div className="settings-drawer">
          <div className="settings-head">
            <span>设置</span>
            <button
              title="返回历史"
              onClick={() => openDrawer("history")}
            >←</button>
          </div>
          <div className="settings-body">
            <div className="settings-group">
              <div className="settings-label">调研深度档位</div>
              {/* 2026-09-17：这里原本硬编码三个人民币价（¥39 / ¥99 / ¥299），
                  而**同一个抽屉往下 40 行**刚写着「计量单位是档位积分，不是人民币——
                  模型单价未配置之前，本系统不报任何钱数」。同一档位在两寸之内出现两个价，
                  且那个 ¥ 是被 K17 明令禁止报的钱数。
                  现在改为读 /api/billing 的 mode_points——**与后端 core/billing.MODE_POINTS 同源**，
                  不再在 JSX 里维护第二张价目表（"同一动作两处实现"是本仓库的老病）。 */}
              {[
                { key: "标准", desc: "市场全景" },
                { key: "深入", desc: "+竞争 +用户" },
                { key: "定制", desc: "+决策SOP" },
              ].map((opt) => (
                <button
                  key={opt.key}
                  className={`settings-option ${depth === opt.key ? "active" : ""}`}
                  onClick={() => setDepth(opt.key)}
                >
                  <div>
                    <div className="opt-name">{opt.key}</div>
                    <div className="opt-desc">{opt.desc}</div>
                  </div>
                  <div className="opt-price">
                    {billing?.mode_points?.[opt.key] != null
                      ? `${billing.mode_points[opt.key]} 积分`
                      : "—"}
                  </div>
                </button>
              ))}
            </div>

            <div className="settings-group">
              <div className="settings-label">积分与档位</div>
              {/*
                2026-09-17 重写。原文是「会员与充值」：三档会员价（¥19/¥99/¥299 每月）
                + 四个按钮，而**四个按钮点了都是「功能开发中」**；余额那一行还是
                **硬编码的「当前余额 ¥0」**（人民币），与头部徽章的真实积分余额
                **同一个量、两个值、两个单位**。
                三档价格另有三个问题：① 不在《口径对销总表》K17 的冻结口径内；
                ② 与后端 core/billing.RECHARGE_TIERS（¥30/¥99/¥299 买积分）**是两套互不相干的价目**，
                   还共用 ¥99/¥299 却含义完全不同；③ 其中的「无限调研+深度档」
                   **在成本上不可能兑现**（深入档单次实测 21 万 tokens）。
                现在改为只展示**真实存在的口径**：积分余额（取自 /api/billing）+ 档位消耗表
                （同一接口的 mode_points，不再手写死值），并如实说明账户的演示性质。
              */}
              <div className="settings-membership">
                <div className="member-plan">
                  <div>
                    <div className="opt-name">积分余额</div>
                    <div className="opt-desc">
                      {typeof pointsBalance === "number"
                        ? `${pointsBalance.toLocaleString()} 积分`
                        : "读取中…"}
                    </div>
                  </div>
                </div>
                {billing?.mode_points && (
                  <div className="member-plan">
                    <div>
                      <div className="opt-name">单次消耗（按档位）</div>
                      <div className="opt-desc">
                        {Object.entries(billing.mode_points)
                          .map(([k, v]) => `${k} ${v}`)
                          .join(" · ")}
                      </div>
                    </div>
                  </div>
                )}
                {/* 充值入口（2026-09-19 接通）。
                    从前 `RECHARGE_TIERS` 一直随 /api/billing 送到前端，
                    而**没有任何入口能改变余额**——价目表摆着看。
                    「实现了 ≠ 能被用上」：字段有、接口有、承重的那个动作没有。
                    ⚠️ 措辞如实：这是**单机演示账户**，点一下即时到账，
                    不产生任何支付行为（后端返回 simulated:true）。 */}
                {(billing?.tiers || []).length > 0 && (
                  <div className="member-plan">
                    <div style={{ width: "100%" }}>
                      <div className="opt-name">补充积分（演示账户 · 即时到账）</div>
                      <div className="recharge-row">
                        {(billing.tiers || []).map((t) => (
                          <button
                            key={t.amount}
                            className="recharge-btn"
                            onClick={() => doRecharge(t.amount)}
                            title={`演示账户充值：+${t.points} 积分（不接入真实支付）`}
                          >
                            ¥{t.amount} → {t.points} 分
                            {t.bonus ? <span className="recharge-bonus">{t.bonus}</span> : null}
                          </button>
                        ))}
                      </div>
                    </div>
                  </div>
                )}
                <p className="member-note">
                  计量单位是<b>档位积分</b>，不是人民币——模型单价未配置之前，本系统不报任何钱数。
                  当前为<b>单机演示账户</b>：余额存于进程内，重启回到初始值（{billing?.start_balance ?? "—"} 分）；
                  上面的充值按钮<b>即时到账、不接入真实支付通道</b>，仅用于演示计量闭环。
                </p>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* 资产库抽屉（行业档案沉淀可见） */}
      {assetsOpen && (
        <div className="settings-drawer">
          <div className="settings-head">
            <span>📚 资产库 · 行业档案</span>
            <button
              title="返回历史"
              onClick={() => openDrawer("history")}
            >←</button>
          </div>
          <div className="settings-body">
            <div className="settings-group">
              <div className="settings-label">
                {assets?.__error ? (
                  <>
                    ⚠️ 资产库读取失败：{assets.__error}
                    <br />
                    （**这不是「已沉淀 0 条」**——是我们没读到）
                  </>
                ) : (
                  <>已沉淀 {(assets?.count || 0)} 条行业证据 · 跨任务复用（资产飞轮）</>
                )}
              </div>
              {assets?.groups && Object.keys(assets.groups).length > 0 && (
                <div className="asset-groups">
                  {Object.entries(assets.groups).map(([ind, n]) => (
                    <span key={ind} className="asset-group-chip">🏷 {ind} · {n}条</span>
                  ))}
                </div>
              )}
            </div>
            <div className="settings-group">
              <div className="settings-label">最近沉淀</div>
              {(assets?.items || []).length === 0 || assets?.__error ? (
                <p className="history-empty">
                  {assets?.__error
                    ? "读不到资产库，本节无内容可显示（**不是「没有沉淀」**）。"
                    : "暂无沉淀——执行调研后，证据会自动进入资产库，下次同行业提问直接复用。"}
                </p>
              ) : (
                assets.items.map((it) => (
                  <div key={it.id} className="asset-item">
                    <div className="asset-industry">[{it.industry}] {it.source_title}</div>
                    <div className="asset-preview">{it.content}</div>
                  </div>
                ))
              )}
            </div>
            {/* 行业基线卡（2026-09-15 加）：**这条护城河此前在产品里没有入口**。
                `industry_kb` 的卡一直在沉淀、也一直在被注入新报告，却没有任何界面能让人看见它——
                数据有、披露无。而它恰恰是「越用越懂」那句叙事唯一的实物。
                `brief` 就是**注入报告的那段文本**，逐字一致：所见即所注。 */}
            <div className="settings-group">
              <div className="settings-label">
                行业基线卡 {(industryCards?.count || 0)} 张（下次同行业调研时**自动注入**下面的文本）
              </div>
              {(industryCards?.cards || []).length === 0 ? (
                <p className="history-empty">暂无基线卡——同行业调研跑过一次之后会在这里出现。</p>
              ) : (
                (industryCards.cards || []).map((c) => (
                  <details key={c.industry} className="ai-trans-fold">
                    <summary>
                      🏷 {c.industry}
                      {c.market_size ? ` · 规模 ${c.market_size}` : ""}
                      {` · ${c.n_numbers} 项指标`}
                    </summary>
                    <div className="asset-preview" style={{ whiteSpace: "pre-wrap" }}>
                      {c.brief || "（本次为空——卡片存在但未通过注入护栏）"}
                    </div>
                  </details>
                ))
              )}
            </div>
          </div>
        </div>
      )}
      {/* 工具合同抽屉（2026-09-19 补界面入口）。
          病案：`GET /api/tools` 的十张合同（目的/输入/输出/权限/失败处理/审计）
          一直是方案书里的卖点（"工具 = 可审计的业务合同"），接口也一直在，
          而**前端一处入口都没有**——复核者想看一眼"工具合同长什么样"，
          只能去看 PPT。写在纸上的能力，产品里必须点得开。 */}
      {toolsOpen && (
        <div className="settings-drawer">
          <div className="settings-head">
            <span>🧰 工具合同 · 每个工具的四维约定</span>
            <button title="返回历史" onClick={() => openDrawer("history")}>←</button>
          </div>
          <div className="settings-body">
            {tools?.__error ? (
              <p className="history-empty">
                ⚠️ 工具合同读取失败：{tools.__error}
                <br />
                （**这不是「没有工具」**——是我们没读到。查后端是否在跑：`GET /api/tools`）
              </p>
            ) : !(tools?.tools || []).length ? (
              <p className="history-empty">暂无工具合同（GET /api/tools）。</p>
            ) : (
              <>
                <div className="settings-group">
                  <div className="settings-label">
                    共 {tools.tools.length} 张合同 · 每张写明<b>目的 / 输入 / 输出 / 权限 / 失败处理</b>
                    {tools.tools.filter((t) => t.audit).length > 0 &&
                      ` + 审计落点（${tools.tools.filter((t) => t.audit).length} 张有）`}
                  </div>
                </div>
                {(tools.tools || []).map((t) => (
                  <details key={t.name} className="ai-trans-fold">
                    <summary>
                      🔧 {t.name}
                      <span className="tool-purpose"> {_cut2(t.purpose || "", 26)}</span>
                    </summary>
                    <div className="tool-contract">
                      <div><b>目的</b>{t.purpose}</div>
                      <div><b>输入</b>{t.inputs}</div>
                      <div><b>输出</b>{t.outputs}</div>
                      <div><b>权限</b>{t.permissions}</div>
                      <div><b>失败处理</b>{t.failure_handling}</div>
                      {t.audit && <div><b>审计</b>{t.audit}</div>}
                    </div>
                  </details>
                ))}
              </>
            )}
          </div>
        </div>
      )}

      {/* 记忆抽屉（2026-09-19 补界面入口）。
          `/api/memory` 一直在被 `recall()` 消费（影响下一次调研的行业/偏好），
          而人看不见它。**看不出它记住了什么，就没法判断它记错了什么**——
          "越用越懂"这句叙事需要一个能被当场核对的面。 */}
      {memoryOpen && (
        <div className="settings-drawer">
          <div className="settings-head">
            <span>🧠 记忆 · 它记住了你什么</span>
            <button title="返回历史" onClick={() => openDrawer("history")}>←</button>
          </div>
          <div className="settings-body">
            {memory?.__error ? (
              <p className="history-empty">
                ⚠️ 记忆读取失败：{memory.__error}
                <br />
                （**这不是「没有记忆」**——是我们没读到。查后端是否在跑：`GET /api/memory`）
              </p>
            ) : (
              <>
                <div className="settings-group">
                  <div className="settings-label">
                    行业 {(memory?.industries || []).length} 个 · 会话 {(memory?.sessions || []).length} 条
                  </div>
                  <div className="asset-groups">
                    {(memory?.industries || []).map((i) => (
                      <span key={i} className="asset-group-chip">🏷 {i}</span>
                    ))}
                    {!(memory?.industries || []).length && (
                      <span className="history-empty">还没有行业记忆——跑过一次调研后会出现。</span>
                    )}
                  </div>
                </div>
                <div className="settings-group">
                  <div className="settings-label">偏好（下一次调研会带上）</div>
                  {Object.entries(memory?.prefs || {}).length === 0 ? (
                    <p className="history-empty">暂无偏好记录。</p>
                  ) : (
                    Object.entries(memory.prefs || {}).map(([k, v]) => (
                      <div key={k} className="asset-item">
                        <div className="asset-industry">{k}</div>
                        <div className="asset-preview">{String(v)}</div>
                      </div>
                    ))
                  )}
                </div>
                <div className="settings-group">
                  <div className="settings-label">最近会话</div>
                  {(memory?.sessions || []).length === 0 ? (
                    <p className="history-empty">暂无会话记录。</p>
                  ) : (
                    (memory.sessions || []).slice(0, 12).map((s, i) => (
                      <div key={`${s.ts}-${i}`} className="asset-item">
                        <div className="asset-industry">[{s.industry || "—"}] {s.ts || ""}</div>
                        <div className="asset-preview">{s.question}</div>
                      </div>
                    ))
                  )}
                </div>
                <p className="member-note">
                  记忆是<b>本机文件</b>（<code>core/memory.py</code>），用于下次同行业调研时带入上下文。
                  它不含任何账号数据；可在设置里清除。
                </p>
              </>
            )}
          </div>
        </div>
      )}
      {/* 自主性图谱抽屉（2026-09-16 补）——Demo 分镜 L28 的「必拍」弹层。
          它回答的是那个必问的问题："你的智能体自主在哪？拓扑不是写死的吗？"
          与其辩护，不如把每个节点摊开：谁在决定、影响面多大、有没有监督、可不可逆。
          **高影响 × 低监督**的行单独标出来——那是我们自己找出来的弱点；
          这张表存在的意义就是让它们无处可藏，也是"不自称完全自主"的凭据。 */}
      {autonomyOpen && (
        <div className="settings-drawer">
          <div className="settings-head">
            <span>🧭 自主性图谱 · 每个节点谁在决定</span>
            <button title="返回历史" onClick={() => openDrawer("history")}>←</button>
          </div>
          <div className="settings-body">
            {autonomy?.__error && (
              <p className="history-empty">
                ⚠️ 自主性图谱读取失败：{autonomy.__error}
                <br />
                （**这不是「没有节点」**——是我们没读到。查后端是否在跑：`GET /api/autonomy-map`）
              </p>
            )}
            {!autonomy?.nodes?.length ? (
              <p className="history-empty">暂无节点数据（GET /api/autonomy-map）。</p>
            ) : (
              <>
                <div className="settings-group">
                  <div className="settings-label">自主性画像</div>
                  <p className="autonomy-summary">
                    共 <b>{autonomy.summary?.nodes ?? 0}</b> 个编排节点 ·
                    模型自主 {autonomy.summary?.by_who?.["模型"] ?? 0} ·
                    模型提议+规则裁决 {autonomy.summary?.by_who?.["混合"] ?? 0} ·
                    确定性规则 {autonomy.summary?.by_who?.["规则"] ?? 0} ·
                    人类决定 {autonomy.summary?.by_who?.["人"] ?? 0}
                    <br />
                    护栏覆盖率 <b>{((autonomy.summary?.guard_coverage ?? 0) * 100).toFixed(0)}%</b>
                    {" "}（监督强度 ≥6 的节点占比）
                  </p>
                  {/* 自陈等级：**只说形态，不锚定档位**——锚一个档位就等于把可被当场推翻的
                      引文挂在墙上（K21）。 */}
                  <p className="autonomy-self">
                    本系统自陈：<b>{autonomy.summary?.self_level}</b>
                  </p>
                </div>
                <div className="settings-group">
                  <div className="settings-label">
                    {autonomy.nodes.length} 个节点逐个摊开（按影响面降序，最该被盯的排最前）
                  </div>
                  <div className="autonomy-table">
                    {autonomy.nodes.map((n) => {
                      const risky = (autonomy.summary?.high_impact_low_oversight || []).includes(n.node);
                      return (
                        <div
                          className={`autonomy-row${n.reversible ? "" : " irrev"}${risky ? " risky" : ""}`}
                          key={n.node}
                        >
                          <div className="autonomy-line1">
                            <code>{n.node}</code>
                            <span className="autonomy-who">{n.who_label}</span>
                            {!n.reversible && <span className="autonomy-chip irrev-chip">不可逆</span>}
                            {risky && <span className="autonomy-chip risk-chip">高影响×低监督</span>}
                          </div>
                          <div className="autonomy-what">{n.decides}</div>
                          <div className="autonomy-line2">
                            影响面 <b>{n.impact}</b>/10　监督 <b>{n.oversight}</b>/10
                            {n.reversible ? "可逆" : "**不可逆**"}
                          </div>
                          <div className="autonomy-guard">护栏：{n.guard}</div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* 运行台账抽屉（真实 API 执行账本） */}
      {runStatsOpen && (
        <div className="settings-drawer">
          <div className="settings-head">
            <span>📊 运行台账 · 真实执行账本</span>
            <button
              title="返回历史"
              onClick={() => openDrawer("history")}
            >←</button>
          </div>
          <div className="settings-body">
            {/* **读失败必须说"读失败"，不许印成"0 笔"**（2026-09-16 全量审计）。
                下面那句"暂无账本"是给**真的没有数据**准备的；后端没起来时它也照样出现，
                而"共 0 笔真实运行"对复核者的含义只有一个：你没跑过。 */}
            {runStats?.__error && (
              <p className="history-empty">
                ⚠️ 运行台账读取失败：{runStats.__error}
                <br />
                （**这不是「0 笔」**——是我们没读到。查后端是否在跑：`GET /api/run-stats`）
              </p>
            )}
            {board?.__error && (
              <p className="history-empty">
                ⚠️ 最小复购看板读取失败：{board.__error}（**这不是「没有指标」**）
              </p>
            )}
            {/* 最小复购看板：主办方点名建议带上台的四个指标。
                每项旁边标出分子/分母——**没有分母的百分比在 2026 年会被当场问死**；
                未测的显示"—"，不显示 0（缺测不是零）。 */}
            {board?.metrics && (
              <div className="settings-group">
                <div className="settings-label">📈 最小复购看板（口径见 tooltip，可复算）</div>
                <div className="board-grid">
                  {Object.values(board.metrics).map((x) => (
                    <div className={`board-card ${x.low_sample ? "low-n" : ""}`} key={x.label} title={x.definition + (x.note ? `\n⚠️ ${x.note}` : "") + (x.display_note ? `\n· ${x.display_note}` : "")}>
                      <div className="board-label">{x.label}</div>
                      <div className={`board-value ${x.display === "—" ? "na" : ""}`}>
                        {x.display || "—"}
                        {x.display_unit && x.display_unit !== "元/份" && (
                          <span className="board-unit"> {x.display_unit}</span>
                        )}
                      </div>
                      <div className="board-den">
                        {x.unit === "元/份"
                          ? x.display_unit === "元/份"
                            ? `成功 ${x.num_ok} 笔`
                            : `${x.num_ok} 笔成功运行`
                          : x.display === "—"
                            ? "未埋点"
                            : `${x.num}/${x.den}`}
                      </div>
                      {/* 分母太小就把话说在卡片上：否则被单独截图引用的，一定是那个裸百分比 */}
                      {x.low_sample && <div className="board-lowchip">样本不足</div>}
                    </div>
                  ))}
                </div>
                {board.caveat && <p className={`board-caveat ${board.sample_ok ? "" : "warn"}`}>{board.caveat}</p>}
              </div>
            )}
            <div className="settings-group">
              <div className="settings-label">
                共 {(runStats?.total ?? 0)} 笔真实运行（仅真实用户跑题落账；离线测试不污染）
              </div>
              {!(runStats?.rows || []).length ? (
                <p className="history-empty">
                  暂无账本——跑 3 笔以上「深入」档真实题后回填（GET /api/run-stats）。算账页与稳定性口径据此冻结，回测样本从首批起积累。
                </p>
              ) : (
                <div className="runstat-table">
                  {(runStats.rows || []).map((r, i) => (
                    <div className="runstat-row" key={i}>
                      <div className="runstat-line1">
                        <b>{String(r.theme || "—").slice(0, 40)}</b>
                        <span className="runstat-badge">{r.depth || "—"}档</span>
                        <span className="runstat-badge">{r.decision_verdict || "—"}</span>
                      </div>
                      <div className="runstat-line2">
                        {r.ts ? String(r.ts).replace("T", " ").slice(0, 16) : "—"}
                        {"　"}⏱ {r.elapsed_s != null ? `${Math.round(r.elapsed_s)}s` : "—"}
                        {"　"}⚡ {r.budget_spent != null ? `${r.budget_spent}积分` : "—"}
                        {"　"}🔗 {r.citation_count ?? "—"} 引文 / {r.domain_count ?? "—"} 域
                        {"　"}↻ {r.rework_count ?? 0} 返工
                      </div>
                      {/* 分模型 token（2026-09-15 加）。**这行以前没有，而且以前也没数据**：
                          `by_model` 一直在写，但检索通道走 httpx 直连、不过 LangChain 回调，
                          所以它**永远只有一个模型键**——"按模型分账"这个能力数据上从没成立过。
                          补上检索计量之后，它才第一次是**真的分组**（主模型 + 检索模型）。
                          成本被问到"钱花在哪"时，这就是那个答案。 */}
                      {(() => {
                        const tk = r.llm_tokens || {};
                        const bm = tk.by_model || {};
                        const keys = Object.keys(bm);
                        if (!tk.total_tokens) return null;
                        return (
                          <div className="runstat-line3">
                            🪙 {(tk.total_tokens / 10000).toFixed(1)} 万 tokens
                            {` · ${tk.calls ?? 0} 次调用`}
                            {keys.length > 0 && (
                              <>
                                {"　"}
                                {keys.map((k) => (
                                  <span key={k} className="runstat-badge">
                                    {k} {(((bm[k].prompt || 0) + (bm[k].completion || 0)) / 10000).toFixed(1)}万
                                  </span>
                                ))}
                              </>
                            )}
                            {costNote(r) && <span className="runstat-badge">{costNote(r)}</span>}
                          </div>
                        );
                      })()}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* 决策回访抽屉（v0.9.75）：当初的判断，今天动了没有。
          它是**报告交付之后**才发生的事，所以独立成面——回访不是报告的一节，
          而是"这个数字员工还在替你盯着"的持续服务。 */}
      {outcomeOpen && (
        <div className="settings-drawer">
          <div className="settings-head">
            <span>🔁 决策回访 · 当初的判断，今天还站得住吗</span>
            <button
              title="返回历史"
              onClick={() => openDrawer("history")}
            >←</button>
          </div>
          <div className="settings-body">
            <div className="settings-group">
              <div className="settings-label">
                在途决策基线 {(outcome?.in_flight || []).length} 条
                （原判定与基线值只读，回访不改写历史）
              </div>
              {/* **谁在盯着这件事，要说清楚**（2026-09-15 外部审计）：
                  回访**由人触发**（这个按钮 / CLI / API），不是后台定时任务——
                  本项目没有调度器。材料里一旦写成"系统会回来对账"，
                  读起来就像全自动，而实际上"系统记下了账、人来按下回访"。
                  这条说明不是免责，是**把自动化程度如实标出来**。 */}
              <div className="settings-hint">
                回访**由你触发**（本按钮 / CLI / API），不是后台定时任务。系统负责的是：
                每次交付**自动记下**判定与监控条件（标 <b>自动登记</b>），到期后由你按下回访。
              </div>
              {(outcome?.in_flight || []).map((b) => (
                <div className="runstat-row" key={b.id}>
                  <div className="runstat-line1">
                    <b>{String(b.question || "—").slice(0, 34)}</b>
                    <span className="runstat-badge">{b.verdict || "—"}</span>
                    <span className="runstat-badge">{b.decision_date || "—"}</span>
                    {/* 自动登记 vs 手工登记：两者分量不同，长得一样就等于没说 */}
                    <span className="runstat-badge" title="这条基线是系统在交付时自动记下的，还是人手工补的">
                      {String(b.source || "").startsWith("auto") ? "自动登记" : "手工登记"}
                    </span>
                  </div>
                  <div className="runstat-line2">
                    监控条件 {(b.monitor_conditions || []).length} 条
                    {"　"}应验日 {b.due_date || "—"}
                  </div>
                </div>
              ))}
              <button className="settings-btn" disabled={outcomeBusy} onClick={runOutcome}>
                {outcomeBusy ? "⏳ 现场回访中（逐条真实检索，约 1–2 分钟）…" : "▶ 现场回访（逐条查监控条件）"}
              </button>
            </div>
            {outcome?.current && (
              <div className="settings-group">
                <div className="settings-label">
                  本次回访：🔔 有进展 {outcome.current.changed} 条 ·
                  ⚠️ 未取得 {outcome.current.no_result} 条
                  {/* 「尚未到应验日」必须能被看见（2026-09-17）。
                      后端 core/outcome_track 明确返回 not_due 并写了理由：
                      **否则「有进展 0 条」会被读成「监控条件都没动」**，
                      而真实原因是「还没到该看的时候」。该自陈此前只印在 md 面
                      （仅 ?md=1 才走），交互面一个字都没有。 */}
                  {(outcome.current.not_due || 0) > 0 &&
                    ` · ⏳ ${outcome.current.not_due} 条尚未到应验日`}
                </div>
                {(outcome.current.reviews || []).map((r) => (
                  <div className="ovr-item" key={r.id + r.reviewed_at}>
                    <div className="ovr-q">{r.id}｜{String(r.question || "").slice(0, 40)}</div>
                    <div className="ovr-orig">原判定 {r.verdict_original}（{r.decision_date}）</div>
                    {(r.rows || []).map((x, i) => (
                      <div className="ovr-row" key={i}>
                        <span className={`ovr-tag ${x.verdict}`}>
                          {x.verdict === "changed" ? "🔔 有进展"
                            : x.verdict === "unchanged" ? "· 未见变化" : "⚠️ 未取得"}
                        </span>
                        <span className="ovr-cond">{x.condition}</span>
                        {x.why && <div className="ovr-why">依据：{x.why}</div>}
                        {(x.evidence || []).slice(0, 2).map((e, j) => (
                          <a className="ovr-ev" key={j} href={e.url} target="_blank" rel="noreferrer">
                            {String(e.title || e.url).slice(0, 60)}
                          </a>
                        ))}
                      </div>
                    ))}
                  </div>
                ))}
                <p className="board-caveat">
                  口径：未取得 ≠ 没变化（把「没查到」说成「没变化」等于自己给自己盖章）。
                  回访只回答「监控条件今天有没有可指认的进展」，不回答「原判断对不对」——
                  那是应验日到期后才有资格谈的事。
                </p>
              </div>
            )}
            {!(outcome?.current) && (outcome?.reviews || []).length > 0 && (
              <div className="settings-group">
                <div className="settings-label">历史回访记录（最近 {(outcome.reviews || []).length} 次）</div>
                {(outcome.reviews || []).slice(0, 5).map((r, i) => (
                  <div className="runstat-row" key={i}>
                    <div className="runstat-line1">
                      <b>{r.id}｜{String(r.question || "").slice(0, 30)}</b>
                      <span className="runstat-badge">🔔 {r.changed}</span>
                      <span className="runstat-badge">⚠️ {r.no_result}</span>
                    </div>
                    <div className="runstat-line2">{String(r.reviewed_at || "").replace("T", " ").slice(0, 16)}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
      </div>
    </ErrorBoundary>
  );
}

// ===== 消息渲染 =====
function Message({
  m,
  loading,
  onConfirm,
  onExecute,
  onResume,
  onPause,
  onDismissBudget,
  onDismissStop,
  onRerunWithManual,
  onUpdateSubtask,
  onAddSubtask,
  onRemoveSubtask,
  onTogglePlanCollapse,
  onEnterDeepDive,
  onPrimaryResearch,
  onSimulateSurvey,
  onRegenerate,
  expandedEvidence,
  setExpandedEvidence,
  expandedCitation,
  setExpandedCitation,
  panelW,
  setPanelW,
}) {
  // 思考过程折叠：每条执行卡片独立（执行中也能收起）
  const [thinkOpen, setThinkOpen] = useState(true);
  // 证据/信源面板折叠：思考流下"已检索证据·信源"独立收缩（箭头式）
  const [evidOpen, setEvidOpen] = useState(true);
  // Stateful HITL：停止卡片的「人工补参」输入（2026-09-17 接上；此前无此 state，
  // 因为**没有入口**——见下方 stop-card-manual 的注释）。
  // 用户消息
  if (m.role === "user") {
    return (
      <div className="msg msg-user">
        <div className="bubble user-bubble">
          {m.content}
          {m.attachments?.length > 0 && (
            <div className="attach-list-inline">
              {m.attachments.map((a, i) => (
                <span key={i} className="attach-chip" title={a.summary || a.name}>
                  📎 {a.name}
                  {a.structured?.length ? ` · ${a.structured.length} 项` : ""}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  }

  // 正在生成研究计划占位（发送后即时反馈，避免"几秒空白以为卡死"）
  if (m.type === "typing") {
    return (
      <div className="msg">
        <div className="bubble wide">
          <div className="ai-head">研策</div>
          <div className="typing-row"><span className="spin">◌</span> {m.content || "正在生成研究计划…"}</div>
        </div>
      </div>
    );
  }

  // 澄清问题（单选/多选按钮）
  if (m.type === "clarify") {
    return (
      <div className="msg">
        <div className="bubble wide">
          <div className="ai-head">研策</div>
          <p className="bubble-text">为了给你更精准的调研，请选择：</p>
          <ClarifyGroup questions={m.questions} onDone={onConfirm} loading={loading} />
        </div>
      </div>
    );
  }

  // 研究计划卡片（子任务逐个编辑，箭头收放）
  if (m.type === "plan") {
    const plan = m.plan;
    const collapsed = m.collapsed;
    return (
      <div className="msg">
        <div className="bubble wide">
          <button className="plan-head" onClick={onTogglePlanCollapse}>
            <span className={`think-arrow ${collapsed ? "" : "open"}`} />
            <span className="plan-head-title">📋 研究计划 · {plan.subtasks?.length || 0} 子任务</span>
            {/* ⚠️ **`price_estimate` 的单位是「档位积分」，不是人民币**（2026-09-16 全量审计）。
                后端字段自己的描述就是这么写的：`core/models.py` 的
                `price_estimate: ... description="预估消耗（单位=档位积分，与 core/billing.MODE_POINTS 同源；非人民币）"`。
                这里原写 `¥{plan.price_estimate}` —— 同一个「深入」档在别的界面印 150 积分/次，
                在这一张卡上印成 ¥150。**同一个数在两个地方是两种货币**，
                而"积分/元三处打架"本仓库在 v0.9.79 已经修过一轮。
                （另注：这条链路当前**没有生产者**——`type:"clarify"` 的消息已不再生成，
                计划卡因此不会被渲染；标签按正确口径写，是为了它哪天被重新接上时不会带着错的单位回来。） */}
            {plan.price_estimate > 0 && (
              <span className="plan-head-price">{plan.price_estimate} 积分</span>
            )}
          </button>
          {!collapsed && (
            <div className="plan-body">
          <div className="plan-meta">
            <span>{plan.dimensions?.length || 0} 维度</span>
          </div>
          <div className="plan-dims">
            {(plan.dimensions || []).map((d, i) => (
              <span key={i} className="dim-chip">{i + 1}. {d}</span>
            ))}
          </div>
          <div className="plan-subtasks">
            {(plan.subtasks || []).map((st) => (
              <div key={st.id} className="st-card">
                <div className="st-row">
                  <input
                    value={st.agent_type}
                    onChange={(e) => onUpdateSubtask(st.id, "agent_type", e.target.value)}
                    className="st-type"
                    placeholder="调研类型（如：行业扫描/竞品分析…）"
                  />
                  <input
                    value={st.description}
                    onChange={(e) => onUpdateSubtask(st.id, "description", e.target.value)}
                    className="st-input"
                    placeholder="子任务描述（自由编辑）"
                  />
                  <button className="st-del" onClick={() => onRemoveSubtask(st.id)}>✕</button>
                </div>
                <input
                  value={(st.search_queries || []).join("；")}
                  onChange={(e) =>
                    onUpdateSubtask(
                      st.id,
                      "search_queries",
                      e.target.value.split(/[；;]/).map((s) => s.trim()).filter(Boolean)
                    )
                  }
                  className="st-queries"
                  placeholder="检索关键词（用；分隔，可自由编辑）"
                />
              </div>
            ))}
            {plan.subtasks.length < 12 && (
              <button className="st-add" onClick={onAddSubtask}>+ 添加子任务（{plan.subtasks.length}/12）</button>
            )}
          </div>
          <button className="execute-btn" onClick={onExecute}>开始执行调研 →</button>
            </div>
          )}
        </div>
      </div>
    );
  }

  // 执行中卡片（思考过程折叠 + 可暂停）
  if (m.type === "executing") {
    const trace = m.trace || [];
    const evids = m.evidences || [];
    const conflicts = m.conflicts || [];
    const done = m.status === "done";
    const failed = m.status === "failed";
    const paused = m.status === "paused";
    const restarted = m.status === "restarted";
    // 子任务短标签：取『』里的维度名，否则去前缀截断
    const shortLabel = (d) => {
      const md = String(d || "").match(/[『「]([^』」]+)[』」]/);
      return md ? md[1] : String(d || "").replace(/^调研[『「]?/, "").slice(0, 14);
    };
    return (
      <div className="msg">
        <div className="bubble wide">
          <div className="ai-head">
            {done ? "研策 · 调研完成"
              : paused ? "研策 · 已暂停"
              : failed ? "研策 · 执行失败"
              : restarted ? "研策 · 已用补充数据重跑"
              : "研策 · 正在执行"}
            {!done && !paused && !failed && !restarted && <span className="spin">◌</span>}
            {paused && (
              <button
                className="resume-btn"
                onClick={() => onResume(m.plan, m.localId)}
                title="从上次中断处继续执行"
              >
                ▶ 继续执行
              </button>
            )}
            {/* 失败也要有**出口**（2026-09-16）：原先只有「已暂停」有继续按钮，
                失败态只印一行红字，用户能做的只有重新提问一遍（问题、档位、计划全丢）。
                断点续跑对传输层失败正合适——后端还留着 checkpoint。 */}
            {failed && (
              <button
                className="resume-btn"
                onClick={() => onResume(m.plan, m.localId)}
                title="从上次的中断处继续（后端仍有 checkpoint；拿不到就从头跑）"
              >
                ↻ 重试
              </button>
            )}
          </div>
          {m.billing && (
            <div className="points-chip" title="档位+积分计量：模式越深消耗越快">
              ⚡ 预计消耗 {m.billing.cost} 积分 · 余额 {m.billing.balance}
              {/* 2026-09-19 删掉了这里原有的那个「积分不足·本次体验价」角标。
                  它稳定出现在每一次深入/定制档演示里——因为演示账户的初始余额(100)
                  配得比最贵档位(400)还低。**那不是业务状态，是初始值没配够。**
                  现在初始余额 2000（≥ 最贵档位），这个状态在正常演示里到不了；
                  留一句"余额不够"的话术在现场，只会让复核者以为产品在打折卖。
                  积分口径本身没有变：`enough`/`short` 仍在 /api/billing 里如实返回。 */}
            </div>
          )}
          {failed && m.error && <div className="exec-error">⚠️ {m.error}</div>}
          <div className="think-block">
            <button
              className="think-toggle"
              onClick={() => setThinkOpen(!thinkOpen)}
            >
              <span className={`think-arrow ${thinkOpen ? "open" : ""}`} />
              {done ? "已深度思考" : failed ? "执行失败" : paused ? "执行已暂停" : restarted ? "已用补充数据重跑" : "正在思考…"}
              <span className={`think-pulse ${done || failed || paused || restarted ? "off" : ""}`} />
            </button>
            {/* 渐进式执行：计划在流内生成 → 内联展示 MECE 问题树（可暂停编辑，不阻塞思考） */}
            {m.plan?.subtasks?.length > 0 && (
              <div className="plan-strip">
                <span className="plan-strip-head">📋 MECE 问题树 · {m.plan.subtasks.length} 子任务</span>
                <span className="plan-strip-list">
                  {(Array.isArray(m.plan.problem_tree) ? m.plan.problem_tree : []).map((br, i) => (
                    <span key={i} className={`plan-strip-st mece-br mece-br-${i}`} title={br.question}>
                      {br.icon} {br.branch}·{br.subtask_ids?.length || 0}
                    </span>
                  ))}
                </span>
                <div className="plan-strip-sub">
                  {(m.plan.subtasks || []).slice(0, 8).map((st, i) => (
                    <span key={i} className="plan-strip-st">{shortLabel(st.description)}</span>
                  ))}
                </div>
              </div>
            )}
            {thinkOpen && (
              <div className="think-flow">
                {trace.map((ev, i) => (
                  <span key={i} className="think-frag">
                    {ev.detail}
                    {i < trace.length - 1 && "，"}
                  </span>
                ))}
                {!done && !failed && !paused && !restarted && (
                  <span className="think-frag thinking">思考中…</span>
                )}
              </div>
            )}
            {/* 工作流编排可视化：LangGraph 节点流水线（当前/完成/失败状态） */}
            {thinkOpen && <PipelineView trace={trace} />}
            {evids.length > 0 && (
              <div className="exec-panel">
                <button
                  type="button"
                  className="exec-panel-head exec-panel-toggle"
                  onClick={() => setEvidOpen(!evidOpen)}
                  title={evidOpen ? "收起证据列表" : "展开证据列表（点开每条可看来源与检索词）"}
                >
                  <span className={`think-arrow ${evidOpen ? "open" : ""}`} />
                  已检索证据 · 信源（{evids.length}）
                </button>
                {evidOpen && (
                <div className="exec-evid">
                  {evids.map((ev, i) => {
                    const src = ev.sources?.[0];
                    return (
                      <div
                        key={i}
                        className="exec-evid-card"
                        onClick={() => {
                          // 正在选字复制 → 不折叠（选中文本后松手会触发click）
                          const sel = window.getSelection();
                          if (sel && sel.toString().length > 0) return;
                          setExpandedEvidence(expandedEvidence === i ? null : i);
                        }}
                      >
                        <div className="exec-evid-head">
                          <span className="exec-evid-src">{src?.publisher || "实时检索"}</span>
                          <span className="exec-evid-claim">{ev.claim}</span>
                          {ev.confidence && (
                            <span className={`conf-chip conf-${ev.confidence_level || "low"}`}>
                              {ev.confidence}置信度
                            </span>
                          )}
                        </div>
                        {expandedEvidence === i && (
                          <div className="exec-evid-detail">
                            {ev.query && (
                              <div className="exec-query" title="本条证据来自这次联网检索（真实工具调用记录，非模型编造）">
                                🔎 来自检索：{ev.query}
                              </div>
                            )}
                            <div className="exec-meta">
                              <span>
                                来源：
                                {(ev.sources || []).map((s) => s?.publisher || domainOf(s?.url) || "未知").filter(Boolean).join("、") || "未知"}
                              </span>
                              {ev.sources?.[0]?.publish_date && <span>日期：{ev.sources[0].publish_date}</span>}
                            </div>
                            {ev.confidence_reasons?.length > 0 && (
                              <div className="exec-conf-reasons">
                                {ev.confidence_reasons.join(" · ")}
                              </div>
                            )}
                            {(ev.sources || []).length > 0 && (
                              <div className="exec-src-links">
                                {ev.sources.map((s, k) => {
                                  const u = (m.urlFixes || {})[s?.url] || s?.url;
                                  // `isUnprobed` 在这里**故意不加**：`url_note` 只挂在 citations 上
                                  //（`graph.py:3411-3475`），证据源从没有这个字段——加了就是永远为假的
                                  // 死代码，而"实现了≠能被用上"正是本项目最高频的缺陷形态。
                                  // `isDataApiUrl` 是纯 URL 判据，不依赖元数据，所以这里成立。
                                  if (!s?.url || (m.badUrls || []).includes(s.url) || isBadUrl(u) || isDataApiUrl(u)) return null;
                                  return (
                                    <a
                                      key={k}
                                      href={u}
                                      target="_blank"
                                      rel="noopener noreferrer"
                                      className="exec-link"
                                      title={u}
                                    >
                                      ↗ {domainOf(u)}{ev.sources.length > 1 ? ` · ${s?.publisher || ""}`.trim() : ""}
                                    </a>
                                  );
                                })}
                              </div>
                            )}
                            {ev.sources?.[0]?.excerpt && <p className="exec-excerpt">{ev.sources[0].excerpt}</p>}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
                )}
              </div>
            )}
          </div>
          {conflicts.length > 0 && (
            <div className="conflict-note">⚖️ 检测到 {conflicts.length} 处证据冲突，已自动仲裁</div>
          )}
          {m.budgetWarning && !done && !m.budgetDismissed && (
            <div className="budget-card">
              <div className="budget-msg">💰 {m.budgetWarning.message}</div>
              <div className="budget-actions">
                <button className="budget-btn" onClick={onPause}>中止</button>
                <button className="budget-btn continue" onClick={() => onDismissBudget(m.localId)}>
                  继续
                </button>
              </div>
            </div>
          )}
          {/* 人工接管卡片：质检 BLOCK → 研策主动停止，展示"为什么停/已知道/需要你做什么" */}
          {m.stopCard && !done && !restarted && !m.stopDismissed && (
            <div className="stop-card">
              <div className="stop-card-title">🛑 研策主动停止 · 需人工确认</div>
              {m.stopCard.risk_class && (
                <div className="stop-card-risk">
                  风险等级：<b>{m.stopCard.risk_class}</b> · {m.stopCard.disposition}
                </div>
              )}
              <div className="stop-card-reason">{m.stopCard.reason}</div>
              <div className="stop-card-list">
                {(Array.isArray(m.stopCard.why_stop) ? m.stopCard.why_stop : []).map((f, i) => (
                  <div key={i} className="stop-card-item">⚠️ {f}</div>
                ))}
              </div>
              <div className="stop-card-grid">
                <div className="stop-card-cell"><b>✅ 已知道</b>{m.stopCard.known}</div>
                <div className="stop-card-cell"><b>❓ 未确认</b>{m.stopCard.unknown}</div>
                <div className="stop-card-cell"><b>👤 需要你决定</b>{m.stopCard.need_user}</div>
                <div className="stop-card-cell"><b>⏱ 继续将消耗</b>{m.stopCard.cost_if_continue}</div>
                <div className="stop-card-cell"><b>↻ 如何恢复</b>{m.stopCard.how_to_resume}</div>
              </div>
              <div className="stop-card-actions">
                <button className="budget-btn" onClick={onPause}>终止调研</button>
                <button className="budget-btn continue" onClick={() => onDismissStop(m.localId)}>
                  继续返工
                </button>
              </div>
              {/* Stateful HITL · 人工补参重跑（2026-09-17 补上入口）
                  此前这条链**只有中段**：handleRerunWithManual（abort 当前流 → 标记旧消息
                  restarted → 带 rework_manual 重跑）实现是完整的，prop 也一路传进了本组件，
                  但停止卡片上**既没有输入框、也从不调用它**——能力整条不可达。
                  而《评分自评表_初赛》两处把它标成「✅ 可补参重跑」。
                  后端反而是通的：req.context → _normalize_context → clarified_context
                  → ctx（通用透传）→ researchers 读 context["rework_manual"]。
                  所以缺的只是这个入口。**留空即等价于「继续返工」**（handler 自带该分支），
                  也就是说：不用它时的行为与改动前完全一致。 */}
              <div className="stop-card-manual">
                <label className="stop-card-manual-label" htmlFor={`manual-${m.localId}`}>
                  你手上已有的数据（门店数 / 租金 / 样本量 / 内部口径…）填这里，带着它重跑：
                </label>
                <textarea
                  id={`manual-${m.localId}`}
                  className="stop-card-manual-input"
                  rows={2}
                  value={manualInput}
                  onChange={(e) => setManualInput(e.target.value)}
                  placeholder="留空则等价于「继续返工」"
                />
                <button
                  className="budget-btn manual"
                  onClick={() => onRerunWithManual(m.localId, m.plan, manualInput)}
                >
                  ↻ 带补充数据重跑
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    );
  }

  // 报告卡片（点引用符号 → 右侧弹网页/文件面板 + 全部数据源；报告左侧留位）
  if (m.type === "report") {
    const html = marked.parse(m.report || "");
    const citations = m.citations || [];
    const panelOpen = expandedCitation?.msgId === m.localId;
    const activeIdx = panelOpen ? expandedCitation.idx : null;
    const activeCite = panelOpen && activeIdx != null ? citations[activeIdx] || null : null;
    return (
      <ReportCard
        m={m}
        html={html}
        citations={citations}
        panelOpen={panelOpen}
        activeIdx={activeIdx}
        activeCite={activeCite}
        setExpandedCitation={setExpandedCitation}
        onEnterDeepDive={onEnterDeepDive}
        onPrimaryResearch={onPrimaryResearch}
        onSimulateSurvey={onSimulateSurvey}
        onRegenerate={onRegenerate}
        panelW={panelW}
        setPanelW={setPanelW}
      />
    );
  }

  // 深挖结果卡片
  if (m.type === "deep_dive") {
    const html = marked.parse(m.conclusion || "");
    return (
      <div className="msg">
        <div className="bubble wide">
          <div className="ai-head">研策 · 深挖结果 <span className="report-meta">{m.evidenceCount} 条新证据</span></div>
          <div className="deep-claim">针对：「{m.claim?.slice(0, 60)}」</div>
          <div className="report-body" dangerouslySetInnerHTML={{ __html: html }} />
        </div>
      </div>
    );
  }

  return null;
}

// ===== 澄清问题（按钮组，支持单选/多选） =====
function ClarifyGroup({ questions, onDone, loading }) {
  const [selections, setSelections] = useState({});
  const [cityInput, setCityInput] = useState("");
  const [submitted, setSubmitted] = useState(false); // 防重复提交

  // 单选：每个问题只能选一个选项
  const select = (key, opt) => {
    setSelections((s) => ({ ...s, [key]: opt }));
    // 选"特定城市"时清空旧的
    if (opt !== "特定城市") setCityInput("");
  };

  // 特定城市：需要用户填城市名
  const needsCity = selections.scope === "特定城市";

  // 提交失败后恢复按钮（loading 从 true → false 且未成功时）
  useEffect(() => {
    if (!loading) setSubmitted(false);
  }, [loading]);

  const allAnswered =
    questions.every((q) => selections[q.key]) &&
    (!needsCity || cityInput.trim());

  const submit = () => {
    if (!allAnswered || submitted || loading) return;
    setSubmitted(true); // 立即锁死，防止双击/重复触发
    const answers = { ...selections };
    if (needsCity) answers.scope = `特定城市:${cityInput.trim()}`;
    onDone(answers);
  };

  return (
    <div className="clarify-group">
      {questions.map((q) => (
        <div key={q.key} className="clarify-q-block">
          <div className="clarify-q">{q.question}</div>
          <div className="option-row">
            {(q.options || []).map((opt) => (
              <button
                key={opt}
                className={`option-btn ${selections[q.key] === opt ? "active" : ""}`}
                onClick={() => select(q.key, opt)}
              >
                {opt}
              </button>
            ))}
          </div>
          {q.key === "scope" && selections.scope === "特定城市" && (
            <input
              className="city-input"
              value={cityInput}
              onChange={(e) => setCityInput(e.target.value)}
              placeholder="请输入城市名，如：成都"
            />
          )}
        </div>
      ))}
      <button
        className="confirm-btn"
        onClick={submit}
        disabled={!allAnswered || submitted || loading}
      >
        {submitted || loading ? "正在生成计划…" : "确认并生成计划"}
      </button>
    </div>
  );
}

// 工作流编排可视化：LangGraph 节点流水线（idle/active/done/failed 状态）
const PIPELINE_NODES = [
  ["router", "意图解析"], ["human_confirm", "计划确认"], ["route_skill", "任务拆解"],
  ["industry", "行业扫描"], ["competition", "竞争格局"], ["user", "用户洞察"], ["statistics", "数据测算"],
  ["check_conflict", "冲突仲裁"], ["replan", "动态重规划"], ["summarize", "报告汇总"], ["budget_check", "预算检查"],
  ["quality_check", "质量检查"], ["rework_fix", "返工修复"], ["decision_sop", "决策清单"], ["asset_sink", "资产沉淀"], ["deliver", "报告交付"],
];
function PipelineView({ trace }) {
  const status = {};
  (trace || []).forEach((ev) => { status[ev.node] = ev.status; }); // 后写覆盖前写：done 覆盖 running
  return (
    <div className="pipeline">
      {PIPELINE_NODES.map(([node, label]) => {
        const st = status[node] || "idle";
        return (
          <span key={node} className={`pipeline-node ${st}`} title={label}>
            <span className="dot" />
            {label}
          </span>
        );
      })}
    </div>
  );
}

// 审计附录标题关键词（标题行命中 → 从该处把报告切成 正文/审计 两段）
//
// ⚠️ **这份表必须与后端 `core/report_html.AUDIT_HEADING_PATTERNS` 逐条一致**
// （2026-09-16 全量审计）。原先这里只有 12 条、后端有 15 条，少的三条是
// 「指标一致性核对」「结论状态」「配对矩阵」——于是**同一份报告**：
// 在 App 里它们是**正文**（大段诊断表直接铺在读者眼前），
// 在导出的作品页/Word/PDF 里它们被**折进审计附录**。
// 同一份交付物两种结构，读者对不上。两处由 `_fulltest` 的
// `test_audit_split_lists_agree` 机械锁住（漏一条就红）。
const AUDIT_HEADINGS = [
  "质检与可验证性", "结论置信度披露", "证据链台账", "数据缺口披露", "引文核验",
  "数据冲突日志", "规则层自检", "对抗性数字校验", "AI 使用与透明度", "研究假设与验证",
  "自主决策链",
  "多角色辩论",
  "指标一致性核对",
  "结论状态", "配对矩阵",
];
// 把整篇报告 md 切成 { main, audit, hasAudit }：只扫标题行（# 开头），
// 正文模块标题（宏观环境/市场规模/竞争格局…）不含这些多字短语 → 不会误切。
function splitAuditTail(md) {
  const lines = (md || "").split("\n");
  let cut = -1;
  for (let i = 0; i < lines.length; i++) {
    const s = lines[i].trim();
    if (!/^#{1,6}\s+\S/.test(s)) continue;
    const txt = s.replace(/^#+\s*/, "");
    if (AUDIT_HEADINGS.some((k) => txt.includes(k))) { cut = i; break; }
  }
  if (cut < 0) return { main: md || "", audit: "", hasAudit: false };
  return { main: lines.slice(0, cut).join("\n"), audit: lines.slice(cut).join("\n"), hasAudit: true };
}

// 「未探测」的五种理由：**表驱动**，与后端 `core/source_registry.SKIP_REASONS` 一一对应。
//
// 为什么做成表：这段渲染此前是两条 `if` 加一个兜底，兜底把"认不出的未探测"
// 一律印成中性的「N 个未探测」——于是**没名字的那种混在里面看不出来**。
// 实测就是 SEC 那条：后端分类器认不出它的 detail，落进空类，
// 而 `skipped_compliance + skipped_budget` 加起来对不上 `skipped`，两个数并排印在同一行。
// 表驱动 + 末尾显式对账（见下方 `rest`）之后，**加起来对不上是看得见的**。
const SKIP_KIND_CHIPS = [
  ["skipped_compliance", "🚪", "站点 robots 明令禁止（按红线不访问）"],
  ["skipped_config", "🔧", "本机配置缺口（未配联系方式 → 该源 fail-closed）"],
  ["skipped_switch", "⏸", "总开关已关闭"],
  ["skipped_budget", "⏳", "巡检预算用尽（**我们没跑完**，不是合规停用）"],
  ["skipped_unclassified", "❓", "**未分类**（出现即为缺陷：后端 SKIP_REASONS 缺一条）"],
];

const VERDICT_META = {
  Go: ["可行 · Go", "go"],
  "No-Go": ["放弃 · No-Go", "nogo"],
  Validate: ["审慎验证 · Validate", "validate"],
};

// 决策类型 → 展示元信息（与后端 core/decision_trace.KINDS 一一对应；顺序即分组展示顺序）
const DECISION_KINDS = [
  ["classify", "🧭", "问题定性"],
  ["decompose", "🌳", "问题拆解"],
  ["route", "🗂️", "源层级路由"],
  ["select_tool", "🧰", "工具选择"],
  ["replan", "🔄", "计划调整"],
  ["recall", "📚", "经验召回"],
  ["rework", "↩️", "返工判定"],
  ["arbitrate", "⚖️", "冲突仲裁"],
  ["degrade", "⬇️", "风险降级"],
  ["stop", "🛑", "主动停止"],
  ["fallback", "🧯", "降级兜底"],
];
const DECISION_TRIGGER_CLS = {
  "模型判断": "llm",
  "模型提议 + 规则校验": "hybrid",
  "确定性规则": "rule",
  "人工确认": "human",
};

// 自主决策链：把智能体做过的每一次取舍摊开（处境 → 选项 → 选择 → 理由 → 依据）。
// 为什么单独做一块：赛道上最常被问的一句是"你的智能体在哪里自主决策了"——
// 架构图答不了这个问题，**把每一次取舍摆出来才能答**。
function DecisionChain({ decisions, onClose }) {
  const ds = decisions || [];
  const byKind = {};
  ds.forEach((d) => (byKind[d.kind] = byKind[d.kind] || []).push(d));
  const llmN = ds.filter((d) => DECISION_TRIGGER_CLS[d.trigger] === "llm" || DECISION_TRIGGER_CLS[d.trigger] === "hybrid").length;
  return (
    <div className="reg-overlay" onClick={onClose}>
      <div className="reg-modal dec-modal" onClick={(e) => e.stopPropagation()}>
        <div className="reg-head">
          <span className="reg-title">🧠 自主决策链</span>
          <span className="dec-head-sub">
            本次共 {ds.length} 次取舍 · 其中 {llmN} 次由模型判断
          </span>
          <button className="cite-panel-close" onClick={onClose}>✕</button>
        </div>
        <div className="reg-body">
          <div className="dec-intro">
            一次调研里真正决定报告质量的是<strong>取舍</strong>——该查什么源、该不该返工、冲突该并列还是该取舍。
            这些取舍在传统产品里是看不见的，而<strong>看不见的判断无法被检验，也无法被信任</strong>。
            下面把每一次都摊开：面对什么处境、有哪些选项、选了哪个、凭什么。
          </div>
          {DECISION_KINDS.map(([kind, icon, label]) => {
            const group = byKind[kind] || [];
            if (!group.length) return null;
            return (
              <div className="dec-group" key={kind}>
                <div className="dec-group-head">
                  {icon} {label} <span className="dec-count">{group.length}</span>
                </div>
                {group.map((d, i) => (
                  <div className="dec-item" key={i}>
                    <div className="dec-row">
                      <span className="dec-key">处境</span>
                      <span className="dec-val">{d.situation}</span>
                    </div>
                    {!!(d.options || []).length && (
                      <div className="dec-row">
                        <span className="dec-key">选项</span>
                        <span className="dec-opts">
                          {(d.options || []).map((o, j) => (
                            <span
                              key={j}
                              className={`dec-opt ${String(d.chosen || "").includes(String(o).slice(0, 8)) ? "on" : ""}`}
                            >
                              {o}
                            </span>
                          ))}
                        </span>
                      </div>
                    )}
                    <div className="dec-row">
                      <span className="dec-key">选择</span>
                      <span className="dec-val dec-chosen">{d.chosen}</span>
                    </div>
                    <div className="dec-row">
                      <span className="dec-key">理由</span>
                      <span className="dec-val">{d.why}</span>
                    </div>
                    {d.evidence && (
                      <div className="dec-row">
                        <span className="dec-key">依据</span>
                        <span className="dec-val dec-ev">{d.evidence}</span>
                      </div>
                    )}
                    <div className="dec-foot">
                      <span className={`dec-tag ${DECISION_TRIGGER_CLS[d.trigger] || "rule"}`}>{d.trigger}</span>
                    </div>
                  </div>
                ))}
              </div>
            );
          })}
          {!ds.length && <div className="reg-loading">本次没有记录到决策（异常）</div>}
          <div className="dec-foot-note">
            这一节证明的是「取舍被记录且可核对」，<strong>不证明每个取舍都是对的</strong>。
            判错的取舍同样列在上面——若某次判断事后被质检或 Red Team 推翻，那条决策仍留在链上。
          </div>
        </div>
      </div>
    </div>
  );
}

// 报告抬头：从报告头水印抽取 引擎版本/生成时点（网页版"报告页眉"用）
function reportHeadMeta(md) {
  const m1 = String(md || "").match(/引擎基线\s*(v[\d.]+)/);
  const m2 = String(md || "").match(/生成时点：\s*([0-9-]+)/);
  return { engine: m1 ? m1[1] : "", date: m2 ? m2[1] : "" };
}

// 核心指标 → 每行 {…, _fill: number|null}。横向条只在"同单位组 ≥2 且同基数级"内画比例，
// 否则画数字大字（异构单位共轴=误导）；% 类单行填充≈value/100。
//
// **区间行一律不画条**（`is_range`，2026-09-19 补）：`22.3% - 27.3%` 这种单元格里
// 没有单一量级，画一条长度 = 22.3/100 的条等于把区间说成点估计。后端已把它的
// `unit` 修干净（原来会是 `"% - 27.3%"`），这里据此判掉并交给数字卡显示原文。
function metricRows(metrics) {
  const rows = (metrics || []).map((m, i) => ({ ...m, _i: i }));
  const groups = {};
  rows.forEach((r) => {
    if (!(Number(r.value) > 0) || r.is_range) return;
    const u = r.unit || "_";
    (groups[u] = groups[u] || []).push(r);
  });
  rows.forEach((r) => {
    const g = groups[r.unit || "_"] || [];
    const vals = g.filter((x) => Number(x.value) > 0).map((x) => Number(x.value));
    if (r.is_range) {
      r._fill = null; // 区间没有单一量级
    } else if (g.length >= 2 && vals.length) {
      const hi = Math.max(...vals);
      r._fill = hi > 0 ? Math.max(0.02, Number(r.value) / hi) : 0;
    } else if (Number(r.value) > 0 && /%|％/.test(r.unit || "")) {
      r._fill = Math.max(0.02, Math.min(1, Number(r.value) / 100));
    } else {
      r._fill = null;
    }
  });
  return rows;
}

// ===== 对话内图表：纯 SVG 柱状图（零依赖、离线稳），麦肯锡/Codex 式"一图一单位" =====
const _escAttr = (s) =>
  String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const _cut = (s, n) => {
  s = String(s == null ? "" : s);
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
};
// 柱顶值标签：取"数值+单位"前段（括号备注不占柱顶）
const _valLabel = (raw, value) => {
  const t = String(raw == null ? "" : raw);
  const cut = t.split(/[（(]|\s/)[0];
  return cut && cut.length <= 11 ? cut : String(value);
};

function metricChartSvg(rows) {
  const W = 640;
  const padX = 4;
  const baseY = 92;
  const topPad = 6;
  const H = baseY + 58;
  const vals = rows.map((r) => Number(r.value) || 0);
  const maxV = Math.max(1, ...vals);
  const step = (W - padX * 2) / rows.length;
  const colW = Math.max(16, step * 0.54);
  const bars = rows
    .map((r, idx) => {
      const v = Math.max(0, vals[idx]);
      const h = Math.max(2, (v / maxV) * (baseY - topPad - 14));
      const x = padX + step * idx + (step - colW) / 2;
      const y = baseY - h;
      const tip = `${r.name}：${r.raw || r.value}（${r.source || "—"}${r.year ? `，${r.year}` : ""}${r.note ? `；${r.note}` : ""}）`;
      return (
        `<g>` +
        `<title>${_escAttr(tip)}</title>` +
        `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${colW.toFixed(1)}" height="${h.toFixed(1)}" rx="3" fill="#3b82f6"/>` +
        `<text x="${(x + colW / 2).toFixed(1)}" y="${(y - 4).toFixed(1)}" text-anchor="middle" class="mcv">${_escAttr(_valLabel(r.raw, r.value))}</text>` +
        `<text x="${(x + colW / 2).toFixed(1)}" y="${(baseY + 13).toFixed(1)}" text-anchor="middle" class="mcn">${_escAttr(_cut(r.name, 8))}</text>` +
        `<text x="${(x + colW / 2).toFixed(1)}" y="${(baseY + 27).toFixed(1)}" text-anchor="middle" class="mcs">${_escAttr(_cut(r.source || "—", 7))}</text>` +
        `</g>`
      );
    })
    .join("");
  return (
    `<svg viewBox="0 0 ${W} ${H}" role="img" xmlns="http://www.w3.org/2000/svg">` +
    `<line x1="${padX}" y1="${baseY}" x2="${W - padX}" y2="${baseY}" class="mcb"/>` +
    bars +
    `</svg>`
  );
}

// ===== 正文"多层次图"：把可比的表格在 markdown 里就地渲染成 SVG 图（多一层图表） =====
// 通用判定：首列=名称列，任一数值列在 ≥2 行内**单位一致**且非纯文字列 → 出一张横向条图。
// （旧版只认"品牌/代表品牌"表头 + 特定列名关键词 → 绝大多数数据表画不出图 = "报告只有一张柱状图"的直接原因）
const _TEXT_HEADERS = [
  "说明", "依据", "来源", "条件", "停止", "负责", "谁", "备注", "对策", "风险",
  "时间", "做法", "方式", "口径", "章节", "位置",
];
const _extractNum = (s) => {
  const m = String(s == null ? "" : s).match(/([\d,]+(?:\.\d+)?)/);
  return m ? parseFloat(m[1].replace(/,/g, "")) : null;
};
const _cut2 = (s, n) => {
  s = String(s == null ? "" : s);
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
};
// 取单位：**表头括号里的单位优先**于单元格尾巴。与 core/report_charts._unit_of 保持同一套判定——
// 同一张表的单位一旦两边各写一套，聊天里画的图和导出文件里的图就会标出两个单位（必然漂移）。
// 病案（对齐后端 2026-09-11 修复）：表头写「2025年预估营收 (亿元)」、单元格写 `320.0 (估算)`，
// 旧逻辑一律从单元格尾巴取 → 单位成了"估算"，聊天里顶着「（估算）」就画出一张图——
// 比不出图更糟：出来一张**标错单位的图**。
const _UNIT_ANNOT_HDR = /^(估算|待核|预计|预测|约|左右|L\d)$/; // 表头括号里的可信度标注（不是单位）
const _UNIT_ANNOT_CELL = /^(估算|待核|预计|预测|约|左右|L\d|专家判断.*)$/; // 单元格尾巴的，多一个"专家判断…"
// 单位必须"像单位"（含这些字且够短）：防把「高线核心市场 (一线/新一线)」的括号内容当单位
const _UNIT_CHARS = ["元", "亿", "万", "%", "％", "家", "人", "杯", "个", "次", "倍", "天", "月", "年", "吨", "台", "辆", "平", "公里", "米", "分", "点", "美元", "港元", "人/", "家/", "次/"];
const _looksLikeUnit = (s) => {
  const t = (s || "").trim();
  return !!t && t.length <= 8 && _UNIT_CHARS.some((c) => t.includes(c));
};
// 无单位返回 null（调用方按 falsy 判定跳过该列）
const _unitOfCell = (s, header) => {
  // ① 先看表头括号：是单位就优先采信；是"估算/待核/预计"这类标注 → 忽略，回退到单元格尾巴
  const h = String(header == null ? "" : header).match(/[（(]\s*([^（()）]{1,8})\s*[)）]/);
  if (h && !_UNIT_ANNOT_HDR.test(h[1].trim())) {
    const hu = h[1].trim().slice(0, 6);
    // 不像单位（如 "(一线/新一线)"）→ 整列不出图：宁可少一张图，也不给图配个假单位
    return _looksLikeUnit(hu) ? hu : null;
  }
  // ② 回退：取单元格"数值之后的余串"当单位（去括号/约高低等修饰）
  const t = String(s == null ? "" : s);
  const m = t.match(/([\d,]+(?:\.\d+)?)/);
  if (!m) return null;
  let unit = t.slice(m.index + m[0].length);
  unit = unit.replace(/^[（(【\s]*/, "").replace(/[（(【】)）\s]*$/, "");
  unit = unit.replace(/^(约|约合|高于|低于|低|高)/, "");
  unit = unit.replace(/^\.+/, "");
  // ③ 尾巴本身是标注（`320.0 (估算)`）→ 视为无单位：同表多行才能按"同单位"聚在一起，
  //    而不是被这些字拆散、或让"估算"冒充成单位
  if (_UNIT_ANNOT_CELL.test(unit || "")) return null;
  const cut = unit.slice(0, 6);
  return _looksLikeUnit(cut) ? cut : null; // ④ 不像单位（散文尾巴）→ 无单位
};
const _grams2 = (s) => {
  const t = String(s == null ? "" : s).replace(/[\s，,。．.、:：()（）【】%％]/g, "");
  const g = new Set();
  for (let i = 0; i < t.length - 1; i++) g.add(t.slice(i, i + 2));
  return g;
};
// 组内名称是否至少共享一个连续 2 字（判"同一指标族"，防"同为 % 的渗透率/增速"共轴误导）
const _shareGram = (names) => {
  let acc = null;
  for (const n of names) {
    const g = _grams2(n);
    acc = acc === null ? g : new Set([...acc].filter((x) => g.has(x)));
    if (!acc.size) return false;
  }
  return !!(acc && acc.size);
};

function _chartInlineHtml(title, items, note) {
  const pseudo = items.map((x) => ({ name: x.name, value: x.value, raw: x.raw, unit: "", source: "", note: "" }));
  return (
    '<div class="report-chart-inline"><div class="report-chart-title">📊 ' +
    title +
    "</div>" +
    metricChartSvg(pseudo) +
    '<div class="report-chart-note">' +
    note +
    "</div></div>"
  );
}

function _chartHtmlForTable(blockLines) {
  const parsed = blockLines.map((b) =>
    b.replace(/^\||\|$/g, "").split("|").map((c) => (c || "").trim())
  );
  if (parsed.length < 3) return "";
  const hdr = parsed[0] || [];
  // 核心指标一览表已在交付物头部单独作图 → 正文不重复
  if (hdr.some((c) => c.includes("指标")) && hdr.some((c) => c.includes("数值"))) return "";
  const data = parsed
    .slice(1)
    .filter((r) => r.length >= 2 && (r[0] || "").trim() && !/^-{2,}/.test((r[0] || "").trim()));
  if (data.length < 2) return "";
  // 逐数值列尝试；首列视为名称列（表头命中名称族或未命中文字列均放行）
  for (let c = 1; c < hdr.length; c++) {
    const htxt = (hdr[c] || "").trim();
    if (_TEXT_HEADERS.some((k) => htxt.includes(k))) continue;
    const items = [];
    const units = new Set();
    let ok = true;
    for (const r of data) {
      const v = _extractNum(r[c]);
      const u = _unitOfCell(r[c], htxt); // 表头一起传进去：表头括号单位优先于单元格尾巴（与后端同判定）
      if (v == null || v <= 0 || !u) {
        ok = false;
        break;
      }
      units.add(u);
      items.push({ name: (r[0] || "").slice(0, 12), value: v, raw: (r[c] || "").slice(0, 12), unit: u });
    }
    if (!ok || items.length < 2 || units.size !== 1) continue;
    const u0 = [...units][0];
    // 异义不共轴：百分号组要求同指标族（共享连续 2 字），不满足则换下一列/不出图
    if (u0.includes("%") && !_shareGram(items.map((x) => x.name))) continue;
    return _chartInlineHtml(_cut2(htxt || "数值对比", 18) + "（" + u0 + "）", items, "数据来源：见下表");
  }
  return "";
}

function decorateReportMd(md) {
  const lines = (md || "").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const s = lines[i].trim();
    if (/^\|/.test(s) && s.split("|").length >= 3) {
      const block = [];
      while (i < lines.length && /^\|/.test(lines[i].trim()) && lines[i].split("|").length >= 3) {
        block.push(lines[i].trim());
        i++;
      }
      const dec = _chartHtmlForTable(block);
      if (dec) out.push(dec);
      block.forEach((b) => out.push(b));
      continue;
    }
    out.push(lines[i]);
    i++;
  }
  return out.join("\n");
}

// 交付物头（决策判定 + 核心指标图表）——立即渲染，不进打字机
function DeliverableHead({ m, metrics }) {
  // Hook 必须在**任何提前 return 之前**（Rules of Hooks）——本组件下面有 `if (!hasAnything) return null`，
  // 把 useState 放到它后面会在"无内容→有内容"的状态切换时抛 hook 顺序错误。
  const [decOpen, setDecOpen] = useState(false);
  const verdict = m.decisionVerdict || "";
  const _allRows = metricRows(metrics);
  // **截断必须留痕**（2026-09-16 全量审计）：这里原写 `metricRows(metrics).slice(0, 8)`，
  // 静默只画前 8 项——而报告正文里的《核心指标一览表》可能是 12 行。
  // 图里的点数 ≠ 表里的条数，且没有任何地方说少了什么（同一条纪律见
  // `report_charts` 的「图中 M 行，本组共 N 行」）。
  const rows = _allRows.slice(0, 8);
  const rowsHidden = Math.max(0, _allRows.length - rows.length);
  // 同单位 ≥2 → 一组柱状图（麦肯锡式对比）；异构/单值 → 数字卡（异量纲不共轴 = 不误导）
  const _groups = {};
  rows.forEach((r) => {
    if (!(Number(r.value) > 0) || r.is_range) return; // 区间行不画柱（见 metricRows 注释）
    const u = String(r.unit || "").trim() || "_";
    (_groups[u] = _groups[u] || []).push(r);
  });
  const chartGroups = Object.keys(_groups)
    .filter((u) => _groups[u].length >= 2)
    .map((u) => ({ unit: u === "_" ? "" : u, rows: _groups[u] }));
  const singletons = rows.filter((r) => {
    const u = String(r.unit || "").trim() || "_";
    return (_groups[u] || []).length < 2;
  });
  const hasAnything = Boolean(verdict) || rows.length > 0 || Boolean(m.entailment?.executed) || Boolean((m.decisionChain || []).length);
  if (!hasAnything) return null;
  const v = VERDICT_META[verdict];
  const chips = m.monitorConditions || [];
  const decisions = m.decisionChain || [];
  // 决策完整性闸门角标（2026-09-16 补）。
  // 病案：`integrityGate` 从 done 事件一路存进了消息对象，**而这里从没读过它**——
  // 上面那行注释写着"有它才能在交付物头部打出『本次判定被闸门降过』的角标"，
  // 角标却根本没造出来。于是：判定是**降级后**的 Validate，读者看到 Validate，
  // **看不出它原本是 Go、更看不出是谁降的**。闸门是本项目最想主张的那件东西，
  // 结果它在第一屏是隐形的（正文横幅要往下滚才看得到）。
  // 同时把「闸门未执行」单列：那与「查过且干净」是两件事，读者必须分得出来。
  const gate = m.integrityGate || null;
  const gateChip = !gate
    ? null
    : gate.executed === false
      ? {
          cls: "warn",
          text: "🛡️ 闸门未执行",
          tip:
            "决策完整性闸门本次未能执行（内部异常）。**这不等于「本次没有问题」**——" +
            "是这道闸门这次没跑起来，判定行未经核对，请当作「未经过决策安全闸门」来读。" +
            (gate.error ? `原因：${String(gate.error).slice(0, 160)}` : ""),
        }
      : gate.downgraded_from && gate.downgraded_to
        ? {
            cls: "bad",
            text: `🛡️ 判定被闸门降级 ${gate.downgraded_from}→${gate.downgraded_to}`,
            tip:
              "有高风险项落在决策区（执行摘要/核心指标/决策依据/测算），闸门按确定性规则" +
              `把判定由 ${gate.downgraded_from} 降为 ${gate.downgraded_to}。` +
              "**只降不升**——闸门没有资格把 Validate 抬成 Go。" +
              `阻断 ${(gate.blockers || []).length} 项，详见报告置顶横幅与文末审计附录。`,
          }
        : gate.level === "BLOCK"
          ? {
              cls: "warn",
              text: `🛡️ 闸门阻断 ${(gate.blockers || []).length} 项`,
              tip: "有高风险项落在决策区；本次判定本就低于 Validate，未被改动。详见报告置顶横幅。",
            }
          : gate.level === "WARN"
            ? {
                cls: "warn",
                text: `🛡️ 闸门 WARN ${(gate.warnings || []).length} 项`,
                tip: "发现问题但都不落在决策区——写在文末审计附录，不进入决策叙事。",
              }
            : null;
  // 引用可信度两层角标：**内容层（原文支不支持这句话）才是与外部研究同层可比的那个数**，
  // 链接层只是"链接活不活"。两个都显示、且标清各自含义，避免读者把高链接通过率读成"引用可信"。
  const ent = m.entailment || {};
  // ⚠️ **「未判定」不许渲染成「0%」**（2026-09-16）。
  // 后端在 `judged == 0` 时给的是 `pass_rate = 0.0` 且 `executed = true`
  // （这一层确实跑了，只是没有任何"可判定"的对——可配对的引用页全是登记线索/索引元数据）。
  // 而前端拿到 0.0 就渲染成**红色的「引用支撑率 0%」**——读者读到的是
  // "这份报告的引用一条都不支持结论"，而事实是"这一层这次没有可比对的内容"。
  // 报告正文的附录分支早就分开了（`if rep["judged"]` 那段写着"本次无可判定的主张，
  // 这不等于通过"），**但角标这一端没有**——同一个读数，正文说"未判定"、角标说"0%"。
  const entChip = ent.executed
    ? (ent.judged > 0
      ? {
        cls: ent.pass_rate >= 0.9 ? "ok" : ent.pass_rate >= 0.7 ? "warn" : "bad",
        text: `🧷 引用支撑率 ${(ent.pass_rate * 100).toFixed(0)}%`,
        tip:
          `引用蕴涵核验：${ent.judged} 组「正文主张 ↔ 引用页原文」逐条比对，` +
          `明确支撑 ${ent.counts?.support ?? 0} · 部分支撑 ${ent.counts?.partial ?? 0} · ` +
          `不相关 ${ent.counts?.unrelated ?? 0} · 相悖 ${ent.counts?.contradict ?? 0}` +
          (ent.index_only ? `；另有 ${ent.index_only} 条为登记线索（不计入通过率）` : "") +
          (ent.unjudged ? `；${ent.unjudged} 条未完成判定（不计入通过率，也不视为通过）` : "") +
          `。**样本 ${ent.judged} 组。**` +
          // 校准：支撑率的分母是我们自己判的 → 必须并排给出"核验官会不会把错的判成对的"
          (ent.calibration?.executed
            ? ` 核验器自校准（${ent.calibration.n_pos}正例+${ent.calibration.n_neg}反例，`
              + `${String(ent.calibration.ran_at || "").slice(0, 10)}）：`
              + `**严格假阳性率 ${(ent.calibration.fpr * 100).toFixed(1)}%**、`
              + `真阳性率 ${(ent.calibration.tpr * 100).toFixed(1)}%——`
              + "即「一组已知不支撑的样本里，有多少被误判成了支撑」。"
              + "HALLMARK 指出这才是决定核验器能否部署的数：**旗标多半是噪声还是真捕获，由它决定**。"
              + "（自测，非第三方认证；另见报告末尾《核验器自校准》。）"
            : " 尚未做假阳性率校准——公开基线（DeepTRACE 40–80%、AIO 11.0% 不被支持）说明"
              + "这个数不该被当成「核验器很准」的证据。") +
          " 判定由模型给出并公开理由与原文片段，可被人工推翻；详见报告末尾《引用蕴涵核验》。",
      }
      : {
          // `judged === 0`：这一层跑了，但**没有任何可判定的对**。
          // 用中性色（warn）而不是红色：红色是"有问题"，这里是"没测到"。
          cls: "warn",
          text: "🧷 引用支撑率 未判定",
          tip:
            `引用蕴涵核验：本次配出 ${ent.pairs ?? 0} 组，**可判定的 0 组**` +
            (ent.index_only ? `（${ent.index_only} 组为登记线索/索引元数据，无正文可比对）` : "") +
            (ent.unjudged ? `（${ent.unjudged} 组判定未完成）` : "") +
            "。**「未判定」既不是 0% 也不是 100%**——它不是一次测量结果，是这次没有可比对的内容。" +
            "正文与报告末尾的《引用蕴涵核验》一节写的是同一件事。",
        }
      )
    : null;
  // 遗漏层（v0.9.75）：链接层与内容层**只审已经存在的引用**。只报支撑率会被读成
  // "核验器太松"；把"还有多少数字根本没来源"并排报出来，叙事的性质才是自审。
  const om = ent.omission || {};
  const omChip = om.claims
    ? {
        cls: om.omitted === 0 ? "ok" : om.omitted / om.claims <= 0.3 ? "warn" : "bad",
        text: `🔍 无来源数字 ${om.omitted}/${om.claims}`,
        tip:
          `遗漏层：正文段落中带「数字+单位」的断言共 ${om.claims} 处 —— ` +
          `带来源标记 ${om.cited} · 无来源但**自陈「估算/待核/预测」** ${om.labeled} · ` +
          `本报告自算（TAM/SAM/SOM 等）${om.derived} · ` +
          `**既无来源也未自陈不确定（裸断言）${om.omitted}**。` +
          "链接层与内容层**只审已经存在的引用**，对「该引而没引」完全盲——" +
          "而行业实测（Google AI Overviews 98,020 条断言）显示**主导失败模式恰恰是遗漏而非引错**，" +
          "也就是说：一份「引文核验 100% 通过」的报告，可能有相当一部分数字根本没有来源。" +
          "裸断言不等于是错的（可能来自上文的引用、或已核验的测算引擎），" +
          "它说明的是**这些位置没有把来源摆出来**。判定为启发式，详见报告末尾《遗漏层》。",
      }
    : om.executed === false
      ? {
          // **"没数" ≠ "没有裸断言"**（2026-09-15 加，与链接核验的"未探测 ≠ 0%"同型）。
          // 没有这个分支时，"扫描崩了"与"claims=0（正文确实没有数字断言）"在角标上
          // **完全不可区分**，表现都是角标消失——读起来是"这次没什么问题"。
          cls: "warn",
          text: "🔍 遗漏层 未执行",
          tip:
            "本次遗漏层扫描**未产出结果**（异常跳过或未接线）。" +
            "**这不等于「没有裸断言」**——它是「没数」。" +
            "正文里的无来源数字可能仍然存在，只是这一层没给出读数。",
        }
      : null;
  // **0% 也要显示**（2026-09-15 修）：原判据 `> 0` 让链接核验最差时角标整块消失——
  // 等于**把最坏的消息渲染成没有消息**，与"链接层不通过率也照印"的诚实纪律直接冲突。
  //
  // 再修一层（同日外部审计）：后端现在在**一条引用都没敲过门**时返回 `null`（未判定），
  // 而不是 `0.0`。所以这里要分三种情况，不能把后两种混成一个：
  //   · 有通过率      → 印百分比（0% 也印，红角标）
  //   · 全部未探测    → 印「未探测」（**中性灰**，不是红）——"没敲门"不等于"门是坏的"
  //   · 确实没有引用  → 不显示
  const linkChip = m.citationPassRate != null
    ? {
        cls: m.citationPassRate >= 0.9 ? "ok" : m.citationPassRate >= 0.7 ? "warn" : "bad",
        text: `🔗 链接核验 ${(m.citationPassRate * 100).toFixed(0)}%`,
        tip:
          "引文核验（链接层）：每条引用的 URL 经真实 HTTP 探测——存活、证书是否被系统信任、" +
          "内容页是否已成软404/词典页/验证墙。**链接能打开 ≠ 内容支持结论**，" +
          "后者见「引用支撑率」角标。" +
          // **分母必须报**（2026-09-16）：`citationProbed` 此前存下来却没有任何读取点——
          // 于是一个「92%」看不出是「92/100」还是「11/12」。按本仓库一贯口径
          // （"一个带分母的 0 才是有信息的 0"），通过率不写分母就是不可核对。
          (m.citationProbed != null ? `　本次实测探测 ${m.citationProbed} 条。` : "") +
          (m.citationSkipped
            ? `　本次另有 ${m.citationSkipped} 条按合规姿态未访问（robots/SEC），` +
              "**不计入这个百分比**。"
            : ""),
      }
    : m.citationSkipped
      ? {
          cls: "warn",
          text: `🔗 链接核验 未探测（${m.citationSkipped} 条）`,
          tip:
            "本次全部引用都因合规姿态未访问（robots.txt Disallow / SEC 联系方式未配置），" +
            "**所以没有通过率可报**——这是「未判定」，不是「0% 全坏」，也不是「全部通过」。" +
            "我们不会用一个没做过的测量去描述这些链接。",
        }
      : null;
  // 降级角标（v0.9.75）：**这一条的存在理由是"报告其余部分正常"**。
  // 决策清单是整份报告最像"决策级"的一节，它可以静默变成规则模板，
  // 而读者从其余内容上看不出任何异样。所以必须有角标、且必须是红色。
  const degrad = m.sopFallback?.reason
    ? {
        text: "⚠️ 决策清单为兜底模板",
        tip:
          `本次「决策清单」未能走通模型生成路径（原因：${m.sopFallback.reason}` +
          (m.sopFallback.detail ? `｜${m.sopFallback.detail}` : "") +
          "），判定与行动建议来自确定性规则模板，**不是**基于本次证据链的个性化结论。" +
          "证据链、核验与审计部分不受影响。请勿据此直接拍板。",
      }
    : null;
  return (
    <div className="deliver-head">
      {degrad && (
        <div className="degrade-banner" title={degrad.tip}>
          {degrad.text}
          <span className="degrade-why">（原因：{m.sopFallback.reason}）</span>
        </div>
      )}
      {(verdict || chips.length > 0 || entChip || omChip || linkChip || decisions.length > 0) && (
        <div className="deliver-verdict-row">
          {verdict && (
            <span className={`verdict-badge ${v ? v[1] : "validate"}`}>
              {v ? v[0] : verdict}
            </span>
          )}
          {gateChip && (
            <span className={`cite-rate-chip ${gateChip.cls}`} title={gateChip.tip}>
              {gateChip.text}
            </span>
          )}
          {decisions.length > 0 && (
            <button
              className="cite-rate-chip dec-chip"
              onClick={() => setDecOpen(true)}
              title={`本次调研智能体做了 ${decisions.length} 次取舍（问题定性 / 拆解 / 源路由 / 返工 / 仲裁 / 降级…）——点开看每一次的处境、选项、选择与理由`}
            >
              🧠 自主决策链 {decisions.length}
            </button>
          )}
          {entChip && (
            <span className={`cite-rate-chip ${entChip.cls}`} title={entChip.tip}>
              {entChip.text}
            </span>
          )}
          {omChip && (
            <span className={`cite-rate-chip ${omChip.cls}`} title={omChip.tip}>
              {omChip.text}
            </span>
          )}
          {linkChip && (
            <span className={`cite-rate-chip ${linkChip.cls}`} title={linkChip.tip}>
              {linkChip.text}
            </span>
          )}
          {chips.slice(0, 3).map((c, i) => (
            <span key={i} className="monitor-cond">👁 {c}</span>
          ))}
          {/* 监控条件多于 3 条时，**说清还有几条**（2026-09-16）：
              原先静默只印前 3 条，读者以为这就是全部监控条件。 */}
          {chips.length > 3 && (
            <span className="monitor-cond more" title={chips.slice(3).join("\n")}>
              还有 {chips.length - 3} 条
            </span>
          )}
        </div>
      )}
      {(chartGroups.length > 0 || singletons.length > 0) && (
        <div className="deliver-charts">
          {chartGroups.map((g, gi) => (
            <div className="metric-chart" key={gi}>
              <div className="metric-chart-head">
                <span className="metric-chart-title">📊 核心指标对比</span>
                {g.unit && <span className="metric-chart-unit">单位：{g.unit}</span>}
              </div>
              <div
                className="metric-chart-svg"
                dangerouslySetInnerHTML={{ __html: metricChartSvg(g.rows) }}
              />
            </div>
          ))}
          {/* 指标多于 8 项时必须说清（2026-09-16）：图里画的是前 8 项。
              与后端 `report_charts` 的「图中 M 行，本组共 N 行」同一纪律——
              **图里的点数 ≠ 表里的条数**这句话，前后端都不能只在一边做。 */}
          {rowsHidden > 0 && (
            <div className="metric-more-note">
              本组指标共 {_allRows.length} 项，上列前 {rows.length} 项（另有 {rowsHidden} 项未画入，完整清单见正文《核心指标一览表》）
            </div>
          )}
          {singletons.length > 0 && (
            <div className="metric-tiles">
              {singletons.map((r) => (                <div
                  className="metric-tile"
                  key={r._i}
                  title={`${r.name}：${r.raw}${r.source ? `（${r.source}${r.year ? `，${r.year}` : ""}）` : ""}`}
                >
                  <div className="metric-tile-k">{r.name}</div>
                  <div className="metric-tile-v">
                    {r.raw}
                    {r.level && <span className="metric-lev">{r.level}</span>}
                  </div>
                  <div className="metric-tile-s">{r.source || "—"}{r.year ? ` · ${r.year}` : ""}</div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      {decOpen && <DecisionChain decisions={decisions} onClose={() => setDecOpen(false)} />}
    </div>
  );
}

// 作品页 HTML 导出需要的结构化 meta（后端 content/title 之外当 meta 传入，缺则后端自解析）
function buildHtmlMeta(m) {
  return {
    question: m.question || "",
    decision_verdict: m.decisionVerdict || "",
    validation_tasks: m.validationTasks || [],
    monitor_conditions: m.monitorConditions || [],
    // **0 不许被 or "" 吃掉**（2026-09-17 与后端对齐）：`0 || ""` 得 `""`，
    // 于是作品页封面把「0 条证据」印成「 条证据」——读者分不清"0 条"与"没统计"。
    // 后端 core/report_html.py 专门为这个边界写了 `== null ? '—' : 原值` 的分支，
    // 前端这一层若把 0 变回空串，等于把刚修好的边界重新打开。传 null 让后端走「—」分支。
    evidence_count: m.evidenceCount ?? null,
    citation_count: (m.citations || []).length ?? null,
    metrics: m.metrics || [],
  };
}

// 报告卡片（打字机输出 + 数字角标→跳网页 + "数据来源"按钮→右侧全高抽屉）
function ReportCard({
  m,
  citations,
  panelOpen,
  activeIdx,
  activeCite,
  setExpandedCitation,
  onEnterDeepDive,
  onPrimaryResearch,
  onSimulateSurvey,
  onRegenerate,
  panelW,
  setPanelW,
}) {
  const [copied, setCopied] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  // 数据源体系（注册表）：按权威梯度分层展示"已接入 / 规划中 / 受阻"——诚实两栏，不吹没接的
  const [registry, setRegistry] = useState(null);
  const [regOpen, setRegOpen] = useState(false);
  // 数据源**健康巡检**：注册表答"我们声明接了哪些源"，巡检答"这些源**此刻**活不活"。
  // 两件事分开渲染——否则界面上"规划中"和"刚才探测失败"会长得一模一样。
  const [health, setHealth] = useState(null);
  const [healthBusy, setHealthBusy] = useState(false);
  const openRegistry = () => {
    setRegOpen(true);
    if (!registry) {
      api.sourceRegistry().then(setRegistry).catch(() => setRegistry({ error: "读取失败" }));
    }
    if (!health) {
      // 先读台账（零打扰、瞬时）；现场巡检由用户点按钮触发
      api.sourcesHealth(0).then(setHealth).catch(() => setHealth({ error: "读取失败" }));
    }
  };
  const runPatrol = () => {
    if (healthBusy) return;
    setHealthBusy(true);
    api
      .sourcesHealth(1)
      .then(setHealth)
      .catch((e) => setHealth({ error: String(e.message || e) }))
      .finally(() => setHealthBusy(false));
  };
  // 划词询问：选中报告里一段话 → 弹出"💬 询问研策"
  const [askPop, setAskPop] = useState(null);
  const askPopRef = useRef(null);
  const askPopTouched = useRef(0); // 弹层内点击导致的滚动需豁免自动收起（防焦点滚动自关）
  // 产品决策：只交付"企业级干净研报"——参赛对照版板块整体移除（大赛自检只在材料侧，不进产品/报告）
  // 后端交付时核验出的坏URL集（证书无效/失效/幻觉）→ 此类来源不渲染可点链接
  const badUrls = new Set(m.badUrls || []);
  // 展示/导出的报告全文：企业级干净研报（不夹带评分/大赛痕迹）
  const showText = m.report || "";
  const headMeta = reportHeadMeta(showText);
  // —— 审计附录切分：只扫标题行，把正文与"质检/置信度/缺口/引文核验/冲突/规则/对抗"尾段分开 ——
  // 核心收益：审计段不进打字机、不进 innerHTML → 消灭"逐字 reveal 覆盖 DOM 冲掉折叠"的时序坑。
  const { main: mainText, audit: auditText, hasAudit } = splitAuditTail(showText || "");
  // —— 报告一次性全文输出（2026-09-18 用户要求撤掉打字机）——
  // 打字机在"边生成边吐字"上有意义，但**报告是一次性交付物**：逐字推进只会让读者等，
  // 而且那 7–90 秒的窗口里读者滚动/划词/点折叠都会被下一次 innerHTML 覆盖打断。
  // 保留常量与下面的实现以便回退：置 true 即恢复逐字。
  const REPORT_TYPEWRITER = false;
  const _total = (mainText || "").length;
  // 阅读节奏输出：默认约 300 字/分逐字推进（不再"几秒突突完"）；超长自动提速封顶，可点"直接显示全文"
  const _TICK_MS = 60;
  // 计时**基于真实时间**（非按次数累加）：后台标签页被浏览器节流后，切回时会一次补齐到应有位置，
  // 不会出现"回到页面才开始慢慢补吐字"（旧实现按 setInterval 次数累加 → 节流即拖尾）。
  // 速度：每字约 4ms（≈250 字/秒）；下限 7 秒、上限 90 秒（长报告也不会拖过一分半）。
  const _REVEAL_MS = Math.min(90000, Math.max(7000, _total * 4));
  const [revealShown, setRevealShown] = useState(Infinity);
  useEffect(() => {
    if (!REPORT_TYPEWRITER || !m._reveal || !_total) {
      setRevealShown(Infinity);
      return;
    }
    const start = Date.now();
    setRevealShown(0);
    const t = setInterval(() => {
      const frac = (Date.now() - start) / _REVEAL_MS;
      setRevealShown((v) => {
        if (!Number.isFinite(v)) return v;
        if (frac >= 1) {
          clearInterval(t);
          return Infinity;
        }
        const nv = Math.floor(_total * frac);
        return nv > v ? nv : v;
      });
    }, _TICK_MS);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 划词浮窗：点别处/滚动即收起；弹层内点击引起的焦点滚动短暂豁免
  useEffect(() => {
    if (!askPop) return;
    const onDown = (e) => {
      if (askPopRef.current && askPopRef.current.contains(e.target)) {
        askPopTouched.current = Date.now();
        return;
      }
      setAskPop(null);
    };
    const onScr = () => {
      if (Date.now() - askPopTouched.current < 300) return;
      setAskPop(null);
    };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("scroll", onScr, true);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("scroll", onScr, true);
    };
  }, [askPop]);
  const revealDone = !Number.isFinite(revealShown) || revealShown >= _total;
  const revealActive = m._reveal && !revealDone && revealShown >= 0;
  // 正文 = 只渲染 main 段（审计段在下方独立 <details> 渲染，从不被 reveal 的 innerHTML 覆盖）
  const effMainHtml = revealActive
    ? marked.parse((mainText || "").slice(0, Math.floor(revealShown)))
    : marked.parse(decorateReportMd(mainText || ""));

  // 数据源按来源类别分组（政府官方/研究报告/企业信息/社媒论坛/媒体报道），
  // 每类独立展示，不混在一个标签里（用户明确要求）
  const groupedCitations = {};
  (citations || []).forEach((c, i) => {
    const g = c.group || "其他来源";
    (groupedCitations[g] = groupedCitations[g] || []).push({ ...c, _idx: i });
  });

  // 数据源面板可拖拽宽度：左缘手柄拖动，左右两侧内容随宽度联动
  const startPanelDrag = (e) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = panelW;
    const onMove = (ev) => {
      const w = Math.max(340, Math.min(760, startW + (startX - ev.clientX)));
      setPanelW(w);
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  };

  // 审计段 HTML（独立渲染，不受打字机 innerHTML 覆盖）
  const auditHtml = hasAudit ? marked.parse(auditText || "") : "";
  // 报告正文（main 段）一次性全文渲染；reveal 只作用于这段正文
  const body = (
    <div
      className="report-body"
      dangerouslySetInnerHTML={{ __html: effMainHtml }}
      onMouseUp={(e) => {
        // 划选一段话 → 松手后在选中内容附近弹"询问研策"
        if (e.target && e.target.closest && e.target.closest("a,button,details,summary,.ref")) return;
        if (revealActive) return; // 打字机逐段重建 DOM 会清选区，读完再划词
        const sel = window.getSelection && window.getSelection();
        if (!sel || sel.isCollapsed || !sel.rangeCount) return;
        if (!e.currentTarget.contains(sel.anchorNode)) return;
        const txt = String(sel.toString()).replace(/\s+/g, " ").trim();
        if (txt.length < 4 || txt.length > 200) return;
        const rc = sel.getRangeAt(0).getBoundingClientRect();
        if (!rc || (!rc.width && !rc.height)) return;
        const vw = window.innerWidth || 1200;
        const vh = window.innerHeight || 800;
        const left = Math.max(6, Math.min(rc.left, vw - 230));
        // 底部选段：把浮窗翻到选中段上方，避免顶出视口下缘点不到
        const top = rc.bottom + 118 > vh ? Math.max(8, rc.top - 116) : rc.bottom + 8;
        setAskPop({ text: txt, left, top });
      }}
      onClick={(e) => {
        // 点击正文数字角标 [n] → 右侧数据源面板弹出，并定位到该来源（不直接新开网页）
        if (e.target.tagName === "REF" && citations.length) {
          const idx = parseInt(e.target.textContent, 10) - 1;
          if (idx >= 0 && idx < citations.length) {
            e.stopPropagation();
            setExpandedCitation({ msgId: m.localId, idx });
          }
        }
      }}
    />
  );

  function copyText(text) {
    if (!text) return;
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).catch(() => {});
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch {}
      document.body.removeChild(ta);
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1400);
  }

  function exportAs(fmt) {
    const md = m.report || "";
    if (fmt === "md") {
      // ⚠️ 必须走**后端**导出。图表内联（`core/report_charts.decorate_md_charts`）是在后端做的，
      // 而这里原来是纯前端 `downloadReport(md)` 直接下载原文——于是"md 导出没有图"怎么修都修不好：
      // 后端那段代码**压根没被执行过**（docx/pdf 走 doExport 所以有图，只有 md 被漏在外面）。
      setShareOpen(false);
      downloadExport("md", md, undefined, buildHtmlMeta(m)).catch((e) => {
        console.error("md 导出失败", e);
        alert(String((e && e.message) || e));
      });
      return;
    }
    if (fmt === "link") {
      copyText(activeCite?.url || activeCite?.excerpt || "");
      setShareOpen(false);
      return;
    }
    doExport(fmt, md, buildHtmlMeta(m));
    setShareOpen(false);
  }

  const copySource = [activeCite?.source, activeCite?.title, activeCite?.url, "", activeCite?.excerpt]
    .filter(Boolean).join("\n");

  return (
    <div
      className="msg"
    >
      <div className="bubble wide">
        <div className="ai-head">
          研策 · 研究报告
          {/* **仲裁数必须带分母**：只写"N 处仲裁"，0 的时候读者分不清是"查过没有"还是"根本没查"。
              这是评审会直接问的一处（本仓库在链接核验、遗漏层上已各栽过一次同型问题）。 */}
          {/* ⚠️ 2026-09-18 第十跑：**改叫「证据级冲突」**。原先两处都叫"仲裁"，
              而它们是两个完全不同的机制——
                · 这里（`conflicts` / `conflict_scan`）= **两条证据互相打架**（同主干、数值差>20%）；
                · 《自主决策链》里的「⚖️ 冲突仲裁（1 次）」= **多角色辩论**
                  （RedTeam 质疑 → 被质疑方申辩 → 第三方裁定）。
              后果实测：报告头印「0 处仲裁（比对 6 对）」，同一屏的决策链印「冲突仲裁（1 次）」——
              **读者会读成"系统自己都数不清"**，而这正是评审会当场指着问的那一类。
              两个机制各自都对，错的是共用一个名字。 */}
          <span
            className="report-meta"
            title={
              // ⚠️ **判据是「字段在不在」，不是「值真不真」**（2026-09-16）。
              // 原先写 `m.conflictScan?.pairs_compared ? ... : "…（旧报告）"`——
              // 一个**真实读数为 0** 的报告（本次没有任何"同主干且带数字"的证据对）
              // 会被当成"旧报告"，tooltip 反过来指责自己没产出数据。
              // 这正是本仓库反复出现的「0 与缺失不分」：**分母为 0 是一个结论，不是缺席**。
              // 而 `conflict_scan` 此前根本没进后端白名单，所以这句话在演示时**必然**出现。
              "**证据级冲突**（两条证据互相打架）——与《自主决策链》里的「冲突仲裁」"
                + "（多角色辩论：质疑→申辩→裁定）是**两个不同机制**，不要混读。"
                + (m.conflictScan
                    ? `判据：${m.conflictScan.criterion || "同主干且数值差>20%"}；`
                      + `去掉数字后主干相同的证据对 ${m.conflictScan.trunk_matched ?? 0} 对，`
                      + `其中 ${m.conflictScan.pairs_compared ?? 0} 对进入数值比对。`
                      + ((m.conflictScan.pairs_compared ?? 0) === 0
                          ? "**本次没有这样的证据对**——这是「查过且没有」，不是「没查」。"
                          : "")
                    : "本次未产出冲突检测读数（旧报告，或后端未回传该字段）")
            }
          >
            {m.evidenceCount} 条证据 · {m.conflicts} 处证据级冲突
            {m.conflictScan != null &&
              `（比对 ${m.conflictScan.pairs_compared ?? 0} 对）`}
          </span>
          {m.billing && (
            <span className="points-bill" title="档位+积分计量：本次任务账单">
              ⚡ 消耗 {m.billing.cost} 积分 · 余额 {m.billing.balance}
            </span>
          )}
          {citations.length > 0 && (
            <button
              className="cite-toggle-btn"
              onClick={() =>
                setExpandedCitation(panelOpen ? null : { msgId: m.localId, idx: 0 })
              }
            >
              {panelOpen ? "收起 »" : `数据来源 ${citations.length} «`}
            </button>
          )}
        </div>
        {/* 报告抬头（网页版页眉：题名 + 引擎/研究时点）——专业报告感，不是聊天气泡 */}
        {(m.question || headMeta.engine || headMeta.date) && (
          <div className="report-masthead">
            <div className="report-masthead-title">
              📄 市场调研报告{m.question ? ` · ${m.question}` : ""}
            </div>
            <div className="report-masthead-meta">
              {headMeta.engine && <span className="report-masthead-chip">引擎 {headMeta.engine}</span>}
              {headMeta.date && <span className="report-masthead-chip">研究时点 {headMeta.date}</span>}
            </div>
          </div>
        )}
        {/* 交付物头：决策判定 + 核心指标图表 + 验证任务（立即渲染，不进打字机） */}
        <DeliverableHead m={m} metrics={m.metrics || []} />
        {revealActive && (
          <div className="type-skip-row">
            <button type="button" className="type-skip" onClick={() => setRevealShown(Infinity)}>
              ⏩ 直接显示全文
            </button>
            <span className="type-skip-hint">按阅读节奏输出中…</span>
          </div>
        )}
        {body}
        {/* 审计附录折叠：正文打字结束后出现，展开即完整审计（质检/置信度/缺口/引文核验/冲突/规则/对抗） */}
        {hasAudit && revealDone && (
          <details className="ai-trans-fold audit-sec" open={false}>
            <summary>审计附录（质检 / 证据链置信度 / 数据缺口 / 引文核验 / 冲突 / 规则自检 / 对抗校验）· 点开展开</summary>
            <div className="ai-trans-fold-body" dangerouslySetInnerHTML={{ __html: auditHtml }} />
          </details>
        )}
        {m.report && (
          <div className="report-actions">
            <button className="ghost-btn" onClick={() => onEnterDeepDive(showText.slice(0, 80) || "这条结论")}>
              🔍 再深挖
            </button>
            {onRegenerate && (
              <button
                className="ghost-btn"
                onClick={onRegenerate}
                title="用同一问题重新生成一版报告"
              >
                ↻ 重新生成
              </button>
            )}
            {/* 导出 .md 必须**走后端**（2026-09-15 外部审计）：
                这里原先是纯前端 Blob 直下（`downloadReport(showText)`），于是**两条 md 入口行为分叉**——
                后端那条会内联图表、并写入法定 AI 隐式标识（HTML 注释形式），前端这条两样都没有。
                同一个按钮，从"报告卡"点与从"分享"点，导出的文件不是同一个东西。
                这类"同一个动作两条实现"正是本仓库反复出问题的地方，所以统一到 doExport。 */}
            <button className="ghost-btn" onClick={() => doExport("md", showText, buildHtmlMeta(m))}>导出 .md</button>
            <button className="ghost-btn" onClick={() => doExport("docx", showText, buildHtmlMeta(m))}>导出 Word</button>
            <button className="ghost-btn" onClick={() => doExport("pdf", showText, buildHtmlMeta(m))}>导出 PDF</button>
            <button className="ghost-btn" onClick={() => doExport("html", showText, buildHtmlMeta(m))}>📖 作品页 HTML</button>
            <button className="ghost-btn" onClick={() => doExport("pptx", showText, buildHtmlMeta(m))}>导出 PPT</button>
            {/* xlsx 是"六格式交付"里**唯一没有前端入口**的一格（2026-09-16 补）。
                病案：后端 `/api/export/xlsx` 早就在（连法定隐式标识都写全了），
                而这一排按钮只有 md/docx/pdf/html/pptx 五个、分享菜单只有 pdf/docx/md/link——
                于是材料里"六格式"那句话说出去，当场在界面上只能数出五个。
                **声称的格式数必须能在界面上数出来**，否则那是宣传不是能力。 */}
            <button className="ghost-btn" onClick={() => doExport("xlsx", showText, buildHtmlMeta(m))}>导出 Excel</button>
            <button className="ghost-btn" onClick={() => onPrimaryResearch(m.localId, m.report)}>🔬 一手验证方案</button>
            {m.auditBundle?.bundle_id && (
              <button
                className="ghost-btn audit-bundle-btn"
                title={`下载可复算包（${m.auditBundle.n_files} 个文件 / ${Math.round((m.auditBundle.bytes || 0) / 1024)} KB）——含原始取证记录、公开判定规则，以及两个纯标准库独立脚本：verify.py 重探每个引用链接是否还活着，compute.py 从原始序列重算 CAGR/同比。第三方解压即可复算，无需安装本项目。`}
                onClick={() => downloadAuditBundle(m.auditBundle.bundle_id)}
              >
                📦 可复算包
              </button>
            )}
          </div>
        )}
        {askPop && (
          <div ref={askPopRef} className="ask-pop" style={{ left: askPop.left, top: askPop.top }}>
            <div className="ask-pop-text">
              “{askPop.text.length > 90 ? `${askPop.text.slice(0, 90)}…` : askPop.text}”
            </div>
            <div className="ask-pop-actions">
              <button
                type="button"
                onClick={() => {
                  const claim = askPop.text.slice(0, 120);
                  setAskPop(null);
                  onEnterDeepDive(claim);
                }}
              >
                💬 询问研策
              </button>
              <button type="button" className="ask-pop-close" onClick={() => setAskPop(null)} title="关闭">✕</button>
            </div>
          </div>
        )}
        {/* AI 深访/问卷：一手验证方案（治"无一手研究"的差异化护城河） */}
        {m.primaryOpen && (
          <div className="primary-box">
            {m.primaryLoading ? (
              <div className="primary-loading">🔬 正在设计问卷+深访+实地方案…</div>
            ) : m.primaryError ? (
              <div className="exec-error">⚠️ 生成失败：{m.primaryError}</div>
            ) : m.primaryMd ? (
              <div className="primary-body" dangerouslySetInnerHTML={{ __html: marked.parse(m.primaryMd) }} />
            ) : (
              <div className="primary-loading">（无结果）</div>
            )}
            {/* 一手研究·模拟数据（演示闭环）：问卷设计好 → 模拟样本 → 分析反哺 */}
            {!m.primaryLoading && !m.primaryError && m.primaryDesign && !m.simMd && (
              <div className="sim-cta">
                <button
                  className="ghost-btn"
                  onClick={() => onSimulateSurvey(m.localId, m.report || "", m.primaryDesign)}
                >
                  🧪 生成模拟问卷数据并出结论（演示闭环）
                </button>
                <span className="sim-cta-hint">按报告结论设定样本分布，一键展示「问卷 → 样本 → 分析 → 结论反哺」；模拟数据非真实采样</span>
              </div>
            )}
            {m.simLoading && <div className="primary-loading">🧪 正在生成 120 份模拟样本并分析…</div>}
            {m.simError && <div className="exec-error">⚠️ 模拟数据生成失败：{m.simError}</div>}
            {m.simMd && (
              <div className="sim-box">
                <div className="sim-body" dangerouslySetInnerHTML={{ __html: marked.parse(m.simMd) }} />
                {m.simCsv && (
                  <div className="sim-actions">
                    <button className="ghost-btn" onClick={() => downloadCsv(m.simCsv)}>
                      ⬇ 下载模拟样本 CSV
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>

      {/* 右侧数据源抽屉：可拖拽宽度，左缘手柄拉动；左侧内容随宽度联动 */}
      {panelOpen && (
        <div className="cite-panel" style={{ width: panelW }}>
          <div className="cite-drag" onMouseDown={startPanelDrag} title="拖动调整宽度" />
          <div className="cite-panel-head">
            <span className="cite-panel-title">数据来源（{citations.length}）</span>
            <button className="cite-reg-btn" onClick={openRegistry} title="按权威梯度查看数据源体系：已接入 / 规划中 / 受阻">🗂️ 数据源体系</button>
            <button className="cite-panel-close" onClick={() => setExpandedCitation(null)}>✕</button>
          </div>
          <div className="cite-list">
            {Object.entries(groupedCitations).map(([group, items]) => (
              <div key={group} className="cite-group">
                <div className="cite-group-label">{group}</div>
                {items.map((c) => (
                  <button
                    key={c._idx}
                    className={`cite-item ${c._idx === activeIdx ? "active" : ""}`}
                    onClick={() => setExpandedCitation({ msgId: m.localId, idx: c._idx })}
                  >
                    <span className="cite-num">{c._idx + 1}</span>
                    <span className="cite-item-source">{c.source}</span>
                    {hasDisplayUrl(c, badUrls) ? <span className="cite-item-domain">· {domainOf(c.url)}</span> : null}
                  </button>
                ))}
              </div>
            ))}
          </div>
          <div className="cite-detail">
            {activeCite && (
              <>
                {activeCite.title && <div className="cite-panel-sub">{activeCite.title}</div>}
                <div className="cite-meta-row">
                  {activeCite.group && <span className="cite-meta-chip">{activeCite.group}</span>}
                  {activeCite.tier && <span className="cite-meta-chip">T{activeCite.tier}</span>}
                  {hasDisplayUrl(activeCite, badUrls) && (
                    // **必须看 `url_note`，不能只看"有没有 url"**（2026-09-16）。
                    // 原先这一枚 chip 的判据只是"url 非空且不是坏链"，于是：
                    // 一条按合规姿态**根本没敲过门**的引用（robots 禁止 / SEC 未配联系方式），
                    // 正文与引文核验表里清清楚楚印着「🚪 未探测（按合规姿态主动不访问）」，
                    // 到了这个详情面板却被盖上「✓ 已核验可访问」——
                    // **同一屏两个互相否定的称号**，而且它出现在本项目最强调"可核验"的地方。
                    isUnprobed(activeCite) ? (
                      <span className="cite-meta-chip cite-meta-warn" title={activeCite.url_note}>
                        🚪 未探测（按合规主动不访问）
                      </span>
                    ) : activeCite.url_note ? (
                      <span className="cite-meta-chip cite-meta-warn" title={activeCite.url_note}>
                        ⚠️ {activeCite.url_note}
                      </span>
                    ) : isDataApiUrl(activeCite.url) ? (
                      // 第三态（2026-09-19）：链接**确实活着**（核验打 200），但 200 是
                      // JSON API 的正常返回。盖「✓ 已核验可访问」是对的、也是误导的——
                      // 读者会以为点开是个网页。**"活着"与"人能读"必须分开说。**
                      <span
                        className="cite-meta-chip cite-meta-warn"
                        title="链接可用（HTTP 200），但返回机器可读数据（JSON/XML），不是可阅读的网页"
                      >
                        🔌 数据接口（非网页）
                      </span>
                    ) : (
                      <span className="cite-meta-chip cite-meta-ok">✓ 已核验可访问</span>
                    )
                  )}
                </div>
                {/* 数字护照：数据期 + 口径。这是"数字真、口径不对"这类事故的唯一可见位置——
                    抽不到就如实标"未标注"（后端不做推断填充），标黄提示引用前须核原文。 */}
                <div className="cite-meta-row cite-passport">
                  <span
                    className={`cite-meta-chip ${activeCite.data_period ? "" : "cite-meta-warn"}`}
                    title="数字护照 · 数据期：该来源数据所属报告期（如 2025年报 / 2026H1）。未标注=标题与摘录中无明确报告期，未做推断。"
                  >
                    📅 {activeCite.data_period || "数据期未标注"}
                  </span>
                  <span
                    className={`cite-meta-chip ${activeCite.caliber ? "" : "cite-meta-warn"}`}
                    title="数字护照 · 口径：该来源显式声明的统计口径（规上/限额以上/含加盟/不含税/零售额…）。未标注=原文未声明，引用前须核。"
                  >
                    ⚖️ 口径：{activeCite.caliber || "未标注"}
                  </span>
                </div>
                {activeCite.query && (
                  <div className="cite-query" title="该来源来自这次联网检索（真实工具调用记录）">
                    🔎 来自检索：{activeCite.query}
                  </div>
                )}
                {citeOpenable(activeCite, badUrls) ? (
                  <>
                    <a className="cite-panel-open" href={activeCite.url} target="_blank" rel="noopener noreferrer">
                      打开 {domainOf(activeCite.url)} 原网页 ↗
                    </a>
                    {activeCite.url_note && (
                      <div className="cite-url-note">⚠️ {activeCite.url_note}</div>
                    )}
                    {activeCite.excerpt && (
                      <div className="cite-excerpt-inline">
                        <div className="cite-excerpt-label">📄 来源摘录（该来源检索到的关键内容，优先以此为准）</div>
                        <div className="cite-panel-excerpt-wrap">
                          <p className="cite-panel-excerpt">{activeCite.excerpt}</p>
                        </div>
                      </div>
                    )}
                    {!activeCite.excerpt && (
                      <div className="cite-note cite-note-light">
                        该来源未提供正文摘录，请点上方"打开原网页"在新标签核验。
                      </div>
                    )}
                  </>
                ) : (
                  <div className="cite-note">
                    {isUnprobed(activeCite)
                      ? activeCite.url_note
                      : isDataApiUrl(activeCite.url)
                        ? "该来源是数据接口（API）：链接可用，但返回机器可读数据而非网页，仅展示文字摘要。"
                        : activeCite.url_note || "该来源未通过可点核验或未提供网页，仅展示文字摘要。"}
                  </div>
                )}
                {/* 摘录的落点必须跟着**同一个判据**走（2026-09-19）。
                    它原先的守卫是 `!activeCite.url`——于是"未探测"与"数据接口"这两类
                    **有 url 但不再给按钮**的条目会连摘录一起消失：修按钮反倒把内容弄丢了。
                    改成 `!citeOpenable(...)` 后，凡是上方没渲染内联摘录的，这里都兜得住。 */}
                {!citeOpenable(activeCite, badUrls) && activeCite.excerpt && (
                  <div className="cite-panel-excerpt-wrap">
                    <p className="cite-panel-excerpt">{activeCite.excerpt}</p>
                  </div>
                )}
              </>
            )}
          </div>
          {/* 底部操作：一键复制 + 分享（PDF/Word/md/链接） */}
          <div className="cite-panel-foot">
            <button className="cite-action" onClick={() => copyText(copySource)}>
              {copied ? "✓ 已复制" : "📋 一键复制"}
            </button>
            <div className="cite-share">
              <button className="cite-action share-toggle" onClick={() => setShareOpen(!shareOpen)}>
                📤 分享 {shareOpen ? "▴" : "▾"}
              </button>
              {shareOpen && (
                <div className="share-menu">
                  <button className="share-option" onClick={() => exportAs("pdf")}>生成 PDF</button>
                  <button className="share-option" onClick={() => exportAs("docx")}>生成 Word</button>
                  <button className="share-option" onClick={() => exportAs("md")}>生成 md</button>
                  {/* 材料里写的是「xlsx 走『分享』菜单」——那就让分享菜单里真的有它，
                      否则那句话是**在描述一个不存在的界面**（2026-09-16 补）。 */}
                  <button className="share-option" onClick={() => exportAs("xlsx")}>生成 Excel</button>
                  <button className="share-option" onClick={() => exportAs("link")}>
                    {activeCite?.url ? "复制链接" : "复制摘要"}
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* 数据源体系：注册表按权威梯度分层 + 结论类型→必选源层 + 受阻台账（诚实，不吹没接的） */}
      {regOpen && (
        <div className="reg-overlay" onClick={() => setRegOpen(false)}>
          <div className="reg-modal" onClick={(e) => e.stopPropagation()}>
            <div className="reg-head">
              <span className="reg-title">🗂️ 数据源体系</span>
              <button className="cite-panel-close" onClick={() => setRegOpen(false)}>✕</button>
            </div>
            <div className="reg-body">
              {!registry && <div className="reg-loading">读取中…</div>}
              {registry?.error && <div className="reg-loading">读取失败：{registry.error}</div>}
              {/* 巡检：把"我们接了 N 个源"（过去完成时）升级成"此刻 N 个活着"（现在时）。
                  这是整个数据源体系里**唯一一个能被复核者当场按下去、当场看到结果**的按钮。 */}
              <div className="reg-health">
                <div className="reg-health-head">
                  <span className="reg-health-title">📡 数据源健康巡检</span>
                  <button
                    className={`reg-probe-btn ${healthBusy ? "busy" : ""}`}
                    onClick={runPatrol}
                    disabled={healthBusy}
                    title="逐个探活 28 个源（含 16 个地方统计局），约 40 秒；期间会向各站发一次列表页请求（守 robots、每站 ≥1 秒间隔）"
                  >
                    {healthBusy ? "⏳ 正在逐个探活…" : "▶ 现场巡检"}
                  </button>
                </div>
                {!health && <div className="reg-loading">读取台账中…</div>}
                {health?.error && <div className="reg-loading">读取失败：{health.error}</div>}
                {health && !health.error && (
                  <>
                    <div className="reg-health-line">
                      {health.summary ? (
                        <>
                          <b className={health.summary.fail ? "warn" : "ok"}>
                            {health.summary.ok}/{health.summary.total}
                          </b>{" "}
                          个源可用
                          {/* **第三态要单独说**（2026-09-15）：`未探测`（按合规姿态主动不访问）
                              既不是"可用"也不是"失败"。少这一句，SEC 那种源会被算进失败，
                              把"我们没敲门"读成"门坏了"。 */}
                          {/* **五种"未探测"不许共用一句话**（2026-09-16 起，2026-09-17 补全）：
                              后端按理由分开计数（`_skip_kind`），这里表驱动逐个印。
                              混成一个数时，"没跑完"会被读者自动读成"主动不碰"，
                              而"没配好联系方式"会被读成"我们守规矩"——两类都不是事实。 */}
                          {SKIP_KIND_CHIPS.map(([key, mark, label]) => {
                            const n = health.summary[key];
                            return n ? ` · ${mark} ${n} 个未探测（${label}）` : "";
                          })}
                          {/* **对账**：五个具名子计数之和必须等于 `skipped`。
                              少的那部分**显式印出来**，不再被中性兜底吞掉——
                              这正是 SEC 那条漏掉的方式（两个数加起来对不上，且没人发现）。 */}
                          {(() => {
                            const named = SKIP_KIND_CHIPS.reduce(
                              (a, [k]) => a + (health.summary[k] || 0),
                              0
                            );
                            const rest = (health.summary.skipped || 0) - named;
                            return rest > 0
                              ? ` · ❓ ${rest} 个未探测（未分类——后端 SKIP_REASONS 可能缺一条）`
                              : "";
                          })()}
                          {health.summary.fail ? ` · ❌ ${health.summary.fail} 个失败` : ""}
                          {health.when ? ` · 实测于 ${health.when}` : " · 尚未巡检"}
                          {health.elapsed_s ? ` · 耗时 ${health.elapsed_s}s` : ""}
                          {health.stale ? " · ⚠️ 台账已过期，建议重新巡检" : ""}
                        </>
                      ) : (
                        "尚无台账"
                      )}
                    </div>
                    <div className="reg-health-rows">
                      {(health.results || []).map((r) => {
                        // 三态渲染：未探测 ≠ 失败（见后端 `_run_one` 的 `skipped`）
                        const skipped = r.skipped || /未探测/.test(String(r.detail || ""));
                        return (
                          <div
                            key={r.sid}
                            className={`reg-health-row ${skipped ? "skip" : r.ok ? "ok" : "bad"}`}
                          >
                            <span className="rh-mark">{skipped ? "🚪" : r.ok ? "✅" : "❌"}</span>
                            <span className="rh-layer">{r.layer}</span>
                            <span className="rh-name">{r.name}</span>
                            <span className="rh-note">{r.detail}</span>
                            <span className="rh-ms">{r.ms ? `${r.ms}ms` : ""}</span>
                          </div>
                        );
                      })}
                    </div>
                    <div className="reg-health-foot">
                      判据不只看 HTTP 200——政府站改版后常返回 200 的「页面已迁移」页，只看状态码会把死源记成活的，
                      所以每个源都带<b>内容判据</b>（页内须仍能匹配到文章链接）。
                      遇 403/412 立即停手并如实记「被拒绝」，不重试、不换 UA、不绕。
                      标记为 ❌ 的源不是「删掉它」的信号，是「这一条此刻不可用」的诚实标注——回空的源照列，不假装个个都有产出。
                    </div>
                  </>
                )}
                  </div>
              {registry?.layers && (
                <>
                  <div className="reg-desc">{registry.describe}</div>
                  <div className="reg-layers">
                    {Object.entries(registry.layers).map(([k, v]) => (
                      <span key={k} className="reg-layer-chip"><b>{k}</b> {v}</span>
                    ))}
                  </div>

                  <div className="reg-sec">
                    ✅ 已接入 <b>{registry.connected?.length || 0}</b> 个源
                    <span className="reg-hint">（代码里真的接了）</span>
                  </div>
                  <div className="reg-list">
                    {(registry.connected || []).map((s) => (
                      <div key={s.id} className="reg-item">
                        <div className="reg-item-top">
                          <span className="reg-layer-tag">{s.layer}</span>
                          <span className="reg-item-name">{s.name}</span>
                          <span className="reg-item-meta">{s.structure} · {s.access}</span>
                        </div>
                        <div className="reg-item-feeds">喂：{(s.feeds || []).join("、")}</div>
                        <div className="reg-item-note">{s.note}</div>
                      </div>
                    ))}
                  </div>

                  {!!(registry.planned || []).length && (
                    <>
                      <div className="reg-sec reg-sec-plan">
                        🧭 规划中 <b>{registry.planned.length}</b> 个源
                        <span className="reg-hint">（未接入，仅路线图）</span>
                      </div>
                      <div className="reg-list">
                        {(registry.planned || []).map((s) => (
                          <div key={s.id} className="reg-item reg-item-plan">
                            <div className="reg-item-top">
                              <span className="reg-layer-tag">{s.layer}</span>
                              <span className="reg-item-name">{s.name}</span>
                            </div>
                            <div className="reg-item-note">{s.note}</div>
                          </div>
                        ))}
                      </div>
                    </>
                  )}

                  {!!(registry.blocked || []).length && (
                    <>
                      <div className="reg-sec reg-sec-block">
                        🚫 实测受阻 <b>{registry.blocked.length}</b> 个源
                        <span className="reg-hint">（2026-09-11 实测不可自动化取，含原因）</span>
                      </div>
                      <div className="reg-table">
                        <div className="reg-tr reg-th"><span>源</span><span>受阻原因</span><span>替代</span></div>
                        {(registry.blocked || []).map((b, i) => (
                          <div key={i} className="reg-tr">
                            <span>{b.name}</span><span>{b.reason}</span><span>{b.alt}</span>
                          </div>
                        ))}
                      </div>
                    </>
                  )}

                  {/* **本机未启用**（2026-09-16 补）：后端一直在下发 `runtime_disabled`
                      （如未配真实联系方式时的 SEC EDGAR —— fail-closed 主动停用），
                      而**前端一个字都没提**：本机 SEC 在「已接入 / 规划中 / 受阻」三栏里
                      一栏都不出现，于是它既不在"能用"里、也不在"不能用"里——
                      **一个源凭空消失了**。
                      这一栏必须与「实测受阻」分开：受阻 = 我们按合规姿态主动不碰；
                      本机未启用 = 我们接了、但**本机这次跑不起来**（原因由后端给出）。
                      这恰恰是能讲的一段合规故事，藏起来反而像少接了一个源。 */}
                  {!!(registry.runtime_disabled || []).length && (
                    <>
                      <div className="reg-sec reg-sec-block">
                        ⚙️ 本机未启用 <b>{registry.runtime_disabled.length}</b> 个源
                        <span className="reg-hint">（已登记接入，但本机运行时不可用——原因见下）</span>
                      </div>
                      <div className="reg-table">
                        <div className="reg-tr reg-th"><span>源</span><span>本机状态</span><span>层级</span></div>
                        {(registry.runtime_disabled || []).map((b, i) => (
                          <div key={i} className="reg-tr">
                            <span>{b.name}</span>
                            <span>{b.why}</span>
                            <span>{b.authority || "—"}</span>
                          </div>
                        ))}
                      </div>
                    </>
                  )}

                  {!!(registry.claim_routes || []).length && (
                    <>
                      <div className="reg-sec">🔀 结论类型 → 必选源层（问答→源硬路由）</div>
                      <div className="reg-table">
                        <div className="reg-tr reg-th"><span>结论类型</span><span>必选源层</span><span>要求的验证对</span></div>
                        {(registry.claim_routes || []).map((r) => (
                          <div key={r.key} className="reg-tr">
                            <span>{r.label}</span>
                            <span>{r.required} {r.required_label}</span>
                            <span>{r.pair}</span>
                          </div>
                        ))}
                      </div>
                    </>
                  )}
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// POST body 导出（正文几十KB，GET query 会被 uvicorn/h11 拒 → "Invalid HTTP request received."）
// meta：仅 html（作品页）导出携带（判定/任务/监控/条数/结构化指标），缺则后端从正文自解析
async function doExport(fmt, text, meta) {
  try {
    await downloadExport(fmt, text || "", undefined, meta);
  } catch (e) {
    console.error("导出失败", e);
    alert(String((e && e.message) || e));
  }
}

// 可复算包下载（ZIP）：后端在交付末端生成并落盘（原始取证记录只存在于当次运行内存里，
// 必须在收尾那一刻落盘），此处按 bundle_id 取回。响应带 Content-Disposition，直接跳链下载。
function downloadAuditBundle(bundleId) {
  if (!bundleId) return;
  const a = document.createElement("a");
  a.href = `${apiBase}/api/audit-bundle/${encodeURIComponent(bundleId)}`;
  a.download = `研策_可复算包_${bundleId}.zip`;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// 【已删除】`downloadReport(md)`——纯前端 Blob 直下原始 markdown。
//
// 为什么删掉而不是留着：它是**同一个动作的第二条实现**，且少了后端那条的两样东西——
// ① 图表内联（`decorate_md_charts`：md 里能嵌 base64 图，Typora/VSCode/GitHub 都渲染）；
// ② 法定 AI 隐式标识（HTML 注释形式）。
// 留着它的结果就是：报告卡上的"导出 .md"与分享面板里的"生成 md"**导出两个不同的文件**，
// 而用户完全看不出来。这类"同一动作、两处实现、慢慢分叉"的病，
// 本仓库已经在 run_stats 落账、L5 路由、行业取名函数上各犯过一次——
// 所以这次不留兼容分支，只留这条注释说明它为什么不该回来。

// 成本口径提示（2026-09-15）：**未配置单价就不报钱数**（口径表 K17）。
// 有 token 就报 token——"没换算成钱"与"没测用量"是两件事，
// 都显示成"—"会把"我们没换算"说成"我们没测"，比不给数更糟。
function costNote(r) {
  const tk = (r && r.llm_tokens) || {};
  if (r && r.cost_cny != null) return `¥${r.cost_cny}/份`;
  if (tk.total_tokens) return "未配单价 → 只报 token 不报钱";
  return "";
}

function downloadCsv(text) {
  const blob = new Blob([text], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "模拟问卷样本.csv";
  a.click();
  URL.revokeObjectURL(url);
}
