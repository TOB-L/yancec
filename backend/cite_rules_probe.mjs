// 引文可点性判据 · 行为探针（由 `_fulltest.py` 第 110 节调用）
//
// ## 为什么需要它
//
// 2026-09-19 录演示时用户当场发现：「打开原网页」按钮会指向
//   ① robots 明令 Disallow 的路径（提示写着"按红线不访问"，按钮却在请你点它）
//   ② `api.worldbank.org/...?format=json` 这类**数据接口**（核验打 HTTP 200，点开是一坨 JSON）
//
// 根因不是漏了一处判断，而是**同一个判据有四处各自实现**：
// 列表域名 / 详情 chip / 「打开原网页」按钮 / 执行流证据链接。
// 09-16 修好了 chip 那一处，另外三处照旧——**一次只补一个洞，洞就会在原地重开**。
//
// 所以这里验的不是"某个按钮改了没有"，而是**那把尺本身**：
// 直接从 `App.jsx` 源码里抠出判据来跑（而不是在本文件里重抄一遍），
// 于是"判据被谁改坏了"与"判据没被用上"都会在这里红。
//
// ## 为什么语料用真实存档
//
// 手搓样本只能证明"实现符合我的理解"，不能证明"它挡得住真实数据"。
// 主断言用真实存档那 13 条，正反样本用于防"永远返回 false"的假绿——
// 一个恒假的实现能通过所有"该挡的挡住了"的断言。
import { readFileSync, existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP = resolve(HERE, "../frontend/src/App.jsx");
const ARCH = resolve(HERE, "data/reports/20260919_124407_89044e.json");

const src = readFileSync(APP, "utf8");

// 起点必须包含 `isBadUrl`：`hasDisplayUrl` 依赖它，只抠后半段会 ReferenceError。
const markers = src.match(/const BAD_URL_MARKERS = \[[\s\S]*?\];/);
const helpers = src.match(
  /const isBadUrl = \(u\) => \{[\s\S]*?hasDisplayUrl\(c, badUrls\) && !isUnprobed\(c\) && !isDataApiUrl\(c\.url\);/,
);
if (!markers || !helpers) {
  // **必须响亮地失败**：抠不出来说明源码结构变了，而不是"检查通过"。
  console.error("!! 从 App.jsx 抠不出引文判据块 —— 源码结构变了，先修探针（不要放行）");
  process.exit(2);
}
const { hasDisplayUrl, citeOpenable, isDataApiUrl, isUnprobed } = new Function(
  `${markers[0]}\n${helpers[0]}\nreturn { hasDisplayUrl, citeOpenable, isDataApiUrl, isUnprobed };`,
)();

let fails = 0;

// ── ① 正反样本：先证伪"恒真/恒假"这类假绿 ───────────────────────────────
// 下面四条**逐字抄自真实存档** `20260919_124407_89044e.json`——就是录演示时
// 用户当场看见"网页有问题"的那四条。写死在样本里而不是从存档现读：
// 存档是会被清理的，**判据的正确性不该随一个数据文件的去留而变**。
const SAMPLES = [
  { n: "巨潮年报 PDF", c: { url: "https://static.cninfo.com.cn/finalpage/2026-03-28/1225045351.PDF" }, want: true },
  { n: "中汽协页面", c: { url: "https://www.caam.org.cn/xqxk/gzdt/202504/t20250418_11592.html" }, want: true },
  { n: "普通新闻页(带 query)", c: { url: "https://k.sina.com.cn/article_7880068201_xxx.html?from=auto" }, want: true },
  // ── 以下四类必须挡住：就是这次录演示时被看见的那四类 ──
  {
    n: "真实·robots 未探测（红线）",
    c: {
      url: "https://www.sohu.com/a/989036540_114835?scm=10001.341_14-200000.0.0-0-0-0-0.",
      url_note: "未探测（站点 robots.txt 明令 Disallow 该路径）；按红线不访问，不据此判死",
    },
    want: false,
  },
  { n: "真实·世行 GDP 接口", c: { url: "https://api.worldbank.org/v2/country/CHN/indicator/NY.GDP.MKTP.CD?format=json&per_page=60&date=2020:2026" }, want: false },
  { n: "真实·IMF datamapper", c: { url: "https://www.imf.org/external/datamapper/api/v1/NGDPD" }, want: false },
  { n: "真实·世行人口接口", c: { url: "https://api.worldbank.org/v2/country/CHN/indicator/SP.POP.TOTL?format=json&per_page=60&date=2020:2026" }, want: false },
  { n: "坏域名(视频站)", c: { url: "https://www.bilibili.com/video/BV1xx" }, want: false },
  { n: "无 url", c: { url: "" }, want: false },
];
console.log("① 正反样本：");
for (const s of SAMPLES) {
  const got = citeOpenable(s.c, new Set());
  if (got !== s.want) {
    fails++;
    console.log(`   ✗ ${s.n}：期望 ${s.want} 实得 ${got}`);
  } else {
    console.log(`   ✓ ${s.n}`);
  }
}

// ── ② 真实存档语料：附加信息。判据的正确性已由 ① 独立承担 ──────────────
if (!existsSync(ARCH)) {
  // 不静默通过：说清楚跳过了什么。但不判失败——存档会被清理，
  // 而"这条检查依赖一个数据文件的去留"本身就不该成立。
  console.log(`\n② 真实语料：**未跑**（找不到 ${ARCH.split(/[\\/]/).pop()}）——主断言由 ① 承担`);
} else {
  const rep = JSON.parse(readFileSync(ARCH, "utf8"));
  const r = rep.result ?? rep;
  const cits = r.citations;
  const bad = new Set(r.bad_urls || []);
  const openable = cits.filter((c) => citeOpenable(c, bad)).length;
  console.log(`\n② 真实语料（${cits.length} 条引文 / ${bad.size} 条坏 URL）：可点 ${openable} 条`);

  // **不写死下标**——语料重跑一次顺序就会变。改成从数据本身推导：
  // 凡是"未探测"或"数据接口"的，一律不该拿到按钮，一条都不许漏。
  const leaked = cits.filter((c) => citeOpenable(c, bad) && (isDataApiUrl(c.url) || isUnprobed(c)));
  if (leaked.length) {
    fails += leaked.length;
    for (const c of leaked) console.log(`   ✗ 漏放：${String(c.title).slice(0, 34)} · ${c.url}`);
  } else {
    console.log(`   ✓ 未探测/数据接口一律无按钮（本份语料里此类共 ${cits.filter((c) => isDataApiUrl(c.url) || isUnprobed(c)).length} 条）`);
  }
  // 反向：可点条数不能是 0——否则"全挡"也能通过上面那条
  if (openable === 0) {
    fails++;
    console.log("   ✗ 一条都不给点：判据写太宽了（把能读的页面也挡了）");
  }
}

console.log(fails ? `\n!! ${fails} 项不符` : "\n全部通过");
process.exit(fails ? 1 : 0);
