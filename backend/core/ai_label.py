"""研策 · AI 生成合成内容标识（法定合规层）

## 为什么它必须存在，而不是"可选的企业披露"

《人工智能生成合成内容标识办法》**2025-09-01 已施行**（配套强制性国标
GB 45438-2025《网络安全技术 人工智能生成合成内容标识方法》同日实施）。
它要求服务提供者对生成合成内容添加**两类标识**：

- **显式标识**（第五条）：文本应在**起始、末尾或中间适当位置**添加文字提示，
  让用户**感知到**这是 AI 生成内容。对本产品即"这份报告是 AI 生成的"必须明写在正文里。
- **隐式标识**（第六条）：在**文件元数据**中添加生成合成内容属性信息、
  **服务提供者名称或编码**、**内容编号**。对本产品即导出的 docx/pdf/pptx/html/md
  须带这三个字段，以便传播平台与监管可核验。

## 与 `core/transparency.py` 的分工（两件事，别混）

| | transparency（AI 使用与透明度附录） | 本模块（法定标识） |
|---|---|---|
| 内容 | 用了哪个模型、哪些工具、哪些方法、参与边界 | 一句话：这是 AI 生成的 + 谁生成的 + 编号 |
| 归谁 | **参赛/学术**要求（ICC/ESOMAR、正大杯 AI 规范） | **法律**要求，对企业商用同样强制 |
| 默认 | 只进"参赛对照版"，企业研报不附 | **两种模式都必须有** |

企业模式此前返回空串（"不自爆"）——那是对**透明度附录**的正确取舍，
但**不能延伸到法定标识**：客户拿一份无标识的 AI 报告去对外发布，
风险落在客户身上（《办法》第七条还要求传播平台核验标识，无标识内容会被限流/下架）。
所以两者的边界在本模块里写死：**附录可省，标识不可省。**

## 标识长什么样（克制版）

报告**开头**一行：用户一眼看到"这是 AI 生成的"（显式，可感知）。
报告**末尾**一段：服务提供者 + 内容编号 + 人工复核边界 + 隐式标识说明（完整声明）。
导出**文件元数据**：同三个字段（隐式，机器可读）。

刻意**不**写满屏免责声明——标识的目的是"让读者知道"，不是"把责任推给读者"。
一份把免责写得比结论还长的报告，本身就是不专业的。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

# 服务提供者（隐式标识必填项之一：名称或编码）
SERVICE_PROVIDER = "研策（Yancec）· 市场调研数字员工"
PROVIDER_CODE = "YANCEC-RESEARCH-AGENT"
# 生成合成内容属性信息（隐式标识必填项之一）
CONTENT_PROPERTY = "AI-generated"
LABEL_BASIS = (
    "《人工智能生成合成内容标识办法》（2025-09-01 施行）"
    "及 GB 45438-2025《网络安全技术 人工智能生成合成内容标识方法》"
)


def content_id(md: str) -> str:
    """内容编号：正文 SHA-256 前 16 位。

    用**内容哈希**而非随机号/时间戳，是为了让编号本身有核查价值：
    同一份内容重新导出，编号不变（可复现）；正文被改动一个字符，编号就变
    （能证明"你手上的这份"和"我们归档的那份"是不是同一份）。
    随机号做不到这两点，只能算个流水号。
    """
    h = hashlib.sha256((md or "").encode("utf-8")).hexdigest()[:16].upper()
    return f"AIGC-YC-{h}"


# 正文里已有的编号（引擎在交付定型时算的那一个）
_CID_IN_TEXT = re.compile(r"AIGC-YC-([0-9A-F]{8,32})")


def canonical_content_id(md: str) -> str:
    """**优先复用正文里已有的编号**，没有才算。导出管线必须走这条。

    病案（2026-09-15 深访实跑，Claude 评审单独点名）：同一份 `.md` 里出现**两个编号**——

        L1  HTML 注释：   content-id: AIGC-YC-126309FFED3B41B5
        L8  正文标识节：  内容编号：AIGC-YC-B067DCE847825486

    根因是**两处各算了一次哈希，而且算的不是同一个字符串**：
      · 正文那一个是引擎在交付定型时对 `report_final` 算的（**不含**导出期内联的图表）；
      · 注释那一个是 `app.py` 导出时对 `content` 算的（**已含** base64 图表）。
    正文改一个字符编号就变，所以两个哈希必然不同。

    为什么这条对参赛作品特别要命：编号是我们**唯一一个能被第三方当场证伪**的承诺
    （"同一份内容重导出编号不变、改一字即变"）。而"你同一份文件两个编号，拿哪个复算？"
    是一句话就能拆掉的质问——**可证伪的叙事最怕自己先不自洽**。

    修法是结构性的：**导出侧不许再算，只许复用**。正文里已经印了编号，那就是这份内容的
    编号；注释承载的是同一个编号的机器可读副本。没有正文编号时（如无标识的旧报告）
    才回退到计算——那时它也不会与谁冲突，因为文件里只有这一个。
    """
    m = _CID_IN_TEXT.search(md or "")
    if m:
        return f"AIGC-YC-{m.group(1)}"
    return content_id(md)


def implicit_meta(md: str, *, title: str = "", question: str = "") -> dict[str, str]:
    """隐式标识（文件元数据三件套 + 若干可核验字段）

    ⚠️ `content_id` **必须走 `canonical_content_id`（复用），不能走 `content_id`（重算）**。
    病案（2026-09-16 核验三份外部方案时发现）：v0.9.82 把「导出侧只许复用、不许再算」
    这条纪律只落到了 `.md` 那一个面上（`app.py`），而**本函数是 docx / pptx / pdf / html
    四个导出面的共同入口**——于是那四个格式的元数据里印的仍是**重算出来的另一个编号**，
    与正文标识节里那个对不上。**同一个病、四个面、只治了一个。**

    为什么它比 md 那次更隐蔽：文件元数据要打开属性面板才看得到，正文标识节要滚动才看得到，
    **两者几乎不会同时出现在一屏里**——不像 md 的注释就在第 1 行、与第 8 行的正文编号隔着 7 行,
    一眼能撞见。所以这个不一致可以一直活到复核者去翻属性。

    判据与 md 面完全一致：正文里已有编号就复用它；没有（无标识的旧报告）才回退到计算——
    那时文件里只有这一个编号，不会与谁冲突。
    """
    return {
        "AIGC": CONTENT_PROPERTY,
        "content_producer": PROVIDER_CODE,
        "producer_name": SERVICE_PROVIDER,
        "content_id": canonical_content_id(md),
        "label_basis": LABEL_BASIS,
        "title": (title or "")[:120],
        "question": (question or "")[:200],
    }


def _marker(cid: str) -> str:
    return f"🤖 **AI 生成合成内容**｜服务提供者：{SERVICE_PROVIDER}｜内容编号：`{cid}`"


def explicit_head(md: str, cid: str = "") -> str:
    """显式标识（起始位置）：正文开头一行，读者一眼可感知。

    放在报告水印行**正下方**——不是埋在文末脚注里。理由：标识如果只有翻到最后
    才看得见，那它服务的是"合规检查"而不是"读者知情"，而条文要的是后者。

    `cid` 可显式传入：交付流程里正文还在被各附录拼接，
    头部与尾部必须用**同一个**内容编号，否则同一份报告出现两个编号 = 标识自相矛盾。
    """
    return (
        f"> {_marker(cid or canonical_content_id(md))}\n"
        f"> 依{LABEL_BASIS}添加标识；内容由 AI 参与生成，关键决策请结合报告内『一手验证方案』人工复核。"
    )


def explicit_tail(md: str, cid: str = "") -> str:
    """显式标识（末尾位置）：完整声明，含隐式标识字段披露，便于传播平台/监管核对。"""
    cid = cid or canonical_content_id(md)
    return "\n".join(
        [
            "\n\n---\n\n# 🤖 AI 生成合成内容标识",
            f"> {_marker(cid)}",
            "",
            f"- **生成合成内容属性**：{CONTENT_PROPERTY}（本报告由 AI 检索、核验、分析并生成初稿）",
            f"- **服务提供者**：{SERVICE_PROVIDER}（编码 `{PROVIDER_CODE}`）",
            f"- **内容编号**：`{cid}`（正文 SHA-256 前 16 位——同内容重导出编号不变，正文被改动则编号改变）",
            f"- **标识依据**：{LABEL_BASIS}",
            "",
            "> **本报告已做与未做**：数据取自公开发布来源，逐条引用经真实 HTTP 探测（存活/证书/内容页判据），"
            "关键数字做多源交叉与冲突并列；**一手问卷/访谈/实地采集为已设计方案、尚未执行**。"
            "AI 生成内容可能存在错误或过时之处，正式对外传播前请人工复核。"
            "导出文件（docx/pdf/pptx/html）的**元数据**中已写入上述属性信息、服务提供者与内容编号（隐式标识）。",
        ]
    )


def html_meta_tags(md: str, *, title: str = "", question: str = "") -> str:
    """隐式标识 → HTML `<meta>` 标签（HTML 交付物的机器可读层）"""
    m = implicit_meta(md, title=title, question=question)
    rows = [
        f'<meta name="AIGC" content="{_esc(m["AIGC"])}">',
        f'<meta name="content-producer" content="{_esc(m["content_producer"])}">',
        f'<meta name="producer-name" content="{_esc(m["producer_name"])}">',
        f'<meta name="content-id" content="{_esc(m["content_id"])}">',
        f'<meta name="aigc-label-basis" content="{_esc(m["label_basis"])}">',
        '<meta name="generator" content="研策 Yancec · 市场调研数字员工">',
    ]
    return "\n".join(rows)


def _esc(s: str) -> str:
    import html as _h

    return _h.escape(str(s or ""), quote=True)


def apply_file_meta(props: Any, md: str, *, title: str = "", question: str = "") -> None:
    """把隐式标识写进 python-docx / python-pptx 的 core_properties（尽力而为）。

    做成"传对象进来就地写"而不是返回新对象：docx 与 pptx 的 core_properties
    字段名一致但类型不同（一个 `CoreProperties`、一个 `CoreProperties` 同名异库），
    就地写省掉一层类型耦合，且失败只影响元数据、不影响文件本身。
    """
    try:
        m = implicit_meta(md, title=title, question=question)
        props.comments = (
            f"[{m['AIGC']}] 服务提供者：{m['producer_name']}（{m['content_producer']}）"
            f"｜内容编号：{m['content_id']}｜依据：{m['label_basis']}"
        )
        props.category = "AI 生成合成内容"
        # **作者字段也必须写**（2026-09-17 修）。
        #
        # 病案：本函数此前只写 `comments`/`category`/`keywords`，**没写 `author`**，
        # 于是 python-docx / python-pptx 的**库默认值**原样留在交付物里——
        # 实测发布件 `创意描述_成品_20260915.docx` 与 `方案书_v5_成品_20260915.docx`
        # 的 `dc:creator` 都是 **`python-docx`**，两份 pptx 的 `cp:lastModifiedBy`
        # 都是 **`Steve Canny`**（python-pptx 作者）。
        # 两层问题：① 复核者只要右键看一眼文件属性，看到的就是"一个脚本生成的"；
        # ② 更要紧的是**法定标识本身**——`dc:creator` 记的是**生成者**，
        # 而它记成了一个软件库，等于把"服务提供者是谁"这一格让给了工具链。
        # `pdf_meta()` 与 `xlsx_meta()` 两条路径都写了 creator，**只有 docx/pptx 这条漏了**。
        props.author = m["producer_name"]
        try:
            props.last_modified_by = m["producer_name"]
        except Exception:
            pass
        try:
            props.keywords = f"AIGC; {m['content_id']}; 研策"
        except Exception:
            pass
    except Exception:
        pass


def pdf_meta(md: str, *, title: str = "", question: str = "") -> dict[str, str]:
    """隐式标识 → reportlab `SimpleDocTemplate` 的元数据参数"""
    m = implicit_meta(md, title=title, question=question)
    return {
        "title": title or "研策 · 市场调研报告",
        "author": m["producer_name"],
        "subject": f"[{m['AIGC']}] 内容编号 {m['content_id']}｜{m['label_basis']}",
        "creator": f"{m['producer_name']}（{m['content_producer']}）",
    }


def xlsx_meta(md: str, *, title: str = "", question: str = "") -> dict[str, str]:
    """xlsx 隐式标识字段（openpyxl `DocumentProperties` 的字段名与 docx 不同，单独映射）。

    病案（2026-09-15 外部审计）：xlsx 是**第六个导出格式**，而它**一条标识都没有**——
    实测导出后 `docProps/core.xml` 只有 `<dc:creator>openpyxl</dc:creator>`。
    材料原本只主张"五格式元数据（docx/pdf/pptx/html/md）"，所以严格讲不算"与材料矛盾"；
    但**同一份报告的第六种导出物裸奔**，对一个把法定标识当卖点的产品是说不过去的。
    两条路：要么明写"xlsx 不导出"，要么补上。**我们选补上**——
    覆盖面比"材料写准一点"更值钱，而且补上之后材料可以主张六格式。

    返回值由调用方 `setattr` 到 `wb.properties`（不在本函数里 import openpyxl：
    xlsx 是可选依赖，标识模块不该因为缺它而导入失败）。
    """
    m = implicit_meta(md, title=title, question=question)
    _desc = (
        f"[{m['AIGC']}] 服务提供者：{m['producer_name']}（{m['content_producer']}）"
        f"｜内容编号：{m['content_id']}｜依据：{m['label_basis']}"
    )
    return {
        "title": (title or "研策调研报告")[:120],
        "creator": m["producer_name"],
        "lastModifiedBy": m["producer_name"],
        "subject": f"[{m['AIGC']}] 内容编号：{m['content_id']}",
        "description": _desc[:900],
        "keywords": f"{m['AIGC']}; 内容编号 {m['content_id']}",
        "category": m["AIGC"],
    }


def label_summary(md: str = "") -> dict[str, Any]:
    """标识信息（供 /health、方案书、说明材料直接引用）"""
    return {
        "basis": LABEL_BASIS,
        "effective": "2025-09-01",
        "explicit": ["报告起始：一行 AI 生成合成内容提示", "报告末尾：完整声明（属性/服务提供者/内容编号/依据）"],
        "implicit": ["docx/pptx core_properties", "pdf 文档元数据", "html <meta>", "md 尾部声明"],
        "content_id": canonical_content_id(md),
        "service_provider": SERVICE_PROVIDER,
        "provider_code": PROVIDER_CODE,
        "note": "法定标识两种模式（企业/参赛）都必须有；与『AI 使用与透明度附录』是两件事——后者可省，前者不可省。",
    }
