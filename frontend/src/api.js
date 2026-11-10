// 研策 · 前端 API 封装
// 与后端 FastAPI 对接

export const apiBase = import.meta.env.VITE_API_BASE || "http://127.0.0.1:8000";

async function request(path, options = {}) {
  const resp = await fetch(`${apiBase}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`API错误 ${resp.status}: ${detail}`);
  }
  return resp.json();
}

export const api = {
  // 提问入口：返回澄清问题 或 研究计划
  ingest: (question, context) =>
    request("/api/ingest", {
      method: "POST",
      body: JSON.stringify({ question, context: context || {} }),
    }),

  // 确认澄清 + 生成计划
  confirm: (question, context) =>
    request("/api/confirm", {
      method: "POST",
      body: JSON.stringify({ question, context }),
    }),

  // 执行调研（携带用户编辑后的计划）
  execute: (question, context, plan) =>
    request("/api/execute", {
      method: "POST",
      body: JSON.stringify({ question, context, plan: plan || null }),
    }),

  // 追问闭环：对报告某句深挖
  deepDive: (claim, userIntent, context) =>
    request("/api/deep-dive", {
      method: "POST",
      body: JSON.stringify({ claim, user_intent: userIntent || "", context: context || {} }),
    }),

  // 查询任务状态
  task: (taskId) => request(`/api/task/${taskId}`),

  // 数据源体系（注册表：已接入/规划中 + 受阻台账 + 结论类型→必选源层）
  sourceRegistry: () => request("/api/source-registry"),

  // 数据源**健康台账**（静态声明 vs 此刻是否活着，两件事）：
  // 默认读台账（不打扰任何站点，瞬时返回）；probe=1 → 现场逐个探活（约 40 秒，会真的发包）。
  // 巡检比注册表更硬：注册表答"我们声明接了哪些源"，巡检答"这些源此刻活不活"。
  sourcesHealth: (probe = 0, region = 1) =>
    request(`/api/sources/health?probe=${probe ? 1 : 0}&region=${region ? 1 : 0}`),

  // 多模态：上传附件（文件 → 抽取内容 → 纳入调研上下文）
  upload: (file) => {
    const form = new FormData();
    form.append("file", file);
    return fetch(`${apiBase}/api/upload`, { method: "POST", body: form }).then((r) =>
      r.ok ? r.json() : r.text().then((t) => Promise.reject(new Error(`上传失败 ${r.status}: ${t}`)))
    );
  },

  // —— 报告存档（服务端兜底找回：浏览器 localStorage 写满时凭它恢复报告正文）——
  // 列表只回短字段（id/question/ts/verdict/chars），不含正文
  listReports: (limit = 20) => request(`/api/reports?limit=${encodeURIComponent(limit)}`),

  // 单份正文。404 = 该报告没在服务端落档（老报告/存档被清），属正常情况 → 返回 null 让调用方静默跳过，
  // 不要 throw（否则启动补齐会走异常分支，把正常情况当错误处理）
  getReport: async (id) => {
    const resp = await fetch(`${apiBase}/api/reports/${encodeURIComponent(id)}`);
    if (resp.status === 404) return null;
    if (!resp.ok) throw new Error(`API错误 ${resp.status}: ${await resp.text()}`);
    return resp.json();
  },
};

// 报告导出：拼接下载地址（旧 GET 方式，仅兼容保留）
export function exportUrl(fmt, content, title) {
  return `${apiBase}/api/export/${fmt}?content=${encodeURIComponent(content)}&title=${encodeURIComponent(title || "研策调研报告")}`;
}

// 报告导出：POST body 传输（正文几十KB，GET query 会超 uvicorn/h11 请求行上限
// → 后端返回 "Invalid HTTP request received."）。body 无长度限制，稳定下载。
// meta：仅 html（作品页）导出携带 {question,decision_verdict,validation_tasks,
//       monitor_conditions,evidence_count,citation_count,metrics}；缺则后端自解析。
export async function downloadExport(fmt, content, title = "研策调研报告", meta = null) {
  const resp = await fetch(`${apiBase}/api/export/${fmt}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      content: content || "",
      title: title || "研策调研报告",
      ...(meta ? { meta } : {}),
    }),
  });
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const j = await resp.json();
      detail = j.detail || detail;
    } catch {
      detail = (await resp.text().catch(() => "")) || detail;
    }
    throw new Error(`导出失败(${fmt}): ${detail}`);
  }
  const blob = await resp.blob();
  const u = URL.createObjectURL(blob);
  const a = document.createElement("a");
  let fn = `${title || "研策调研报告"}.${fmt === "html" ? "html" : fmt}`;
  const cd = resp.headers.get("Content-Disposition") || "";
  const m = cd.match(/filename\*=UTF-8''([^;]+)/i) || cd.match(/filename="?([^";]+)"?/i);
  if (m && m[1]) {
    try {
      fn = decodeURIComponent(m[1]);
    } catch {
      fn = m[1];
    }
  }
  a.href = u;
  a.download = fn;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(u);
}
