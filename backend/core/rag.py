"""研策 · 知识层（RAG + 资产沉淀）

对应 v3 方案：
- 知识层：RAG（行业报告/资产库）+ 长短记忆
- 资产飞轮：历史调研沉淀成行业档案库，跨任务复用检索结果

技术：
- 向量库：Chroma（本地轻量）
- 嵌入：bge-m3（中文检索好、本地可跑）

核心功能：
1. 资产入库：调研结果/检索内容 → 存入行业档案库
2. 资产检索：新任务先查历史档案，复用已有结果（省token、更准）
3. 资产飞轮：用得越多越懂该行业
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import get_settings

logger = logging.getLogger(__name__)

# 内存态存储（开发期；Chroma 持久化后续接）
_collection = None
_collection_lock = threading.Lock()  # 防并发竞态：多子Agent并行时只初始化一次


def _get_collection():
    """获取 Chroma 集合（惰性初始化，持久化到磁盘；失败降级内存）"""
    global _collection
    if _collection is not None:
        return _collection
    with _collection_lock:
        if _collection is not None:
            return _collection
        try:
            import chromadb

            from core import safe_io

            settings = get_settings()
            # **离线模式不进持久库**（2026-09-16 · 第十处生产写点）。
            #
            # `data/chroma` 是 13MB 的生产向量库，写它的是第三方
            # `chromadb.PersistentClient(path=...)`，而路径来自 `settings.vector_db_path`
            # ——**不是我们拼的常量**。所以它既不在"含 `data` 字面量的模块常量"枚举里，
            # 也没有闸门：对抗核验员据此**把这条缺陷判为不存在**（实测仍在，盘上就有）。
            # 离线时直接走内存降级：跑测试不再往生产向量库里灌测试语料。
            if safe_io.is_offline():
                logger.info("离线模式：向量库走内存降级，不碰生产 Chroma 库")
                _collection = _MemoryCollection()
                return _collection
            client = chromadb.PersistentClient(path=settings.vector_db_path)
            _collection = client.get_or_create_collection(
                name="yancec_assets",
            )
            return _collection
        except Exception as e:
            logger.warning("Chroma 初始化失败，降级为内存存储: %s", e)
            # 降级：内存字典存储
            _collection = _MemoryCollection()
            return _collection


class _MemoryCollection:
    """Chroma 不可用时的内存降级（保证不翻车）"""

    def __init__(self):
        self._docs: list[dict] = []
        self._count = 0
        self._last_query_tokens: list[str] = []
        self._recent_texts: list[str] = []

    def add(self, ids, documents, metadatas, embeddings=None):
        for i, doc_id in enumerate(ids):
            self._docs.append(
                {
                    "id": doc_id,
                    "document": documents[i],
                    "metadata": metadatas[i] if metadatas else {},
                }
            )
        self._count += len(ids)

    def query(self, query_embeddings, n_results, where=None):
        # 内存降级：无向量检索，按关键词匹配打分返回最相关的
        # （比"返回最近插入"强：避免不同子任务都命中同一条资产 → 报告雷同）
        try:
            query_text = " ".join(self._recent_texts)
        except Exception:
            query_text = ""
        scored = []
        for d in self._docs:
            score = 0.0
            # 简单打分：查询词在文档中出现的次数/位置
            for token in self._last_query_tokens:
                if token and token in d["document"]:
                    score += 1
            scored.append((score, d))
        scored.sort(key=lambda x: x[0], reverse=True)
        results = [d for s, d in scored[:n_results] if s > 0] or self._docs[-n_results:]
        return {
            "ids": [[d["id"] for d in results]],
            "documents": [[d["document"] for d in results]],
            "metadatas": [[d["metadata"] for d in results]],
        }

    def query_tokens(self, tokens):
        """外部调用传入查询关键词，供内存降级打分"""
        self._last_query_tokens = tokens
        self._recent_texts = tokens

    def count(self):
        return self._count


# ============================================================
# 嵌入（bge-m3，本地跑；不可用时降级哈希）
# ============================================================
_embedder = None
_embedder_lock = threading.Lock()  # 防并发竞态：16个子Agent并行检索时，模型必须只加载一次


def _get_embedder():
    """获取 bge-m3 嵌入模型（本地路径加载，离线可用）

    双检锁：ThreadPool 多子Agent并发首次调用时，若无锁会各自实例化一个模型
    （bge-m3 约2GB/实例）→ 内存爆 → 后端被杀 → 前端 network error。
    """
    global _embedder
    if _embedder is not None:
        return _embedder
    with _embedder_lock:
        if _embedder is not None:
            return _embedder
        try:
            from FlagEmbedding import BGEM3FlagModel

            # 本地模型路径：默认 <仓库根>/models/bge-m3，可用环境变量覆盖
            local_model_path = os.environ.get("YANCEC_EMBED_MODEL") or str(
                Path(__file__).resolve().parents[2] / "models" / "bge-m3"
            )
            _embedder = BGEM3FlagModel(local_model_path, use_fp16=False)
        except Exception as e:
            logger.warning("bge-m3 加载失败，降级为哈希嵌入: %s", e)
            _embedder = _HashEmbedder()
        return _embedder


class _HashEmbedder:
    """bge-m3 不可用时的降级（特征哈希，保证向量库可跑）"""

    def encode(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        vectors = []
        for text in texts:
            vec = [0.0] * 256
            for token in re.findall(r"[一-龥]{1,4}|\w+", text)[:200]:
                idx = abs(hash(token)) % 256
                vec[idx] += 1.0
            norm = sum(v * v for v in vec) ** 0.5
            vectors.append([v / norm if norm else v for v in vec])
        return vectors


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量嵌入文本"""
    try:
        embedder = _get_embedder()
        if isinstance(embedder, _HashEmbedder):
            return embedder.encode(texts)
        # bge-m3
        import numpy as np

        outputs = embedder.encode(texts, batch_size=8)
        embeddings = outputs["dense_vecs"]
        return embeddings.tolist()
    except Exception as e:
        logger.warning("嵌入失败，降级哈希: %s", e)
        return _HashEmbedder().encode(texts)


def embed_text(text: str) -> list[float]:
    """嵌入单条文本"""
    return embed_texts([text])[0]


# ============================================================
# 资产入库
# ============================================================
def add_asset(
    industry: str,
    content: str,
    source_type: str = "retrieval",
    source_url: str = "",
    source_title: str = "",
    evidence: dict | None = None,
) -> str:
    """把调研结果/检索内容存入行业档案库（资产沉淀）"""
    collection = _get_collection()
    asset_id = f"ast_{abs(hash(content)) % 100000000}_{len(str(collection.count()))}"
    metadata = {
        "industry": industry,
        "source_type": source_type,
        "source_url": source_url,
        "source_title": source_title,
        "created_at": datetime.now().isoformat(),
        "evidence": str(evidence) if evidence else "",
    }
    try:
        if isinstance(collection, _MemoryCollection):
            # 内存降级：无需向量嵌入（省内存）
            collection.add(
                ids=[asset_id],
                documents=[content[:2000]],
                metadatas=[metadata],
            )
        else:
            emb = embed_text(content[:1000])
            collection.add(
                ids=[asset_id],
                documents=[content[:2000]],
                metadatas=[metadata],
                embeddings=[emb],
            )
        logger.info("资产已入库: %s [%s] %s", industry, source_type, source_title[:30])
        return asset_id
    except Exception as e:
        logger.warning("资产入库失败: %s", e)
        return ""


# ============================================================
# 资产检索（跨任务复用）
# ============================================================
def _asset_rank(item: dict[str, Any]) -> float:
    """资产排序权重：来源权威度(L1>L2>L3) + 时效(新>旧)

    检索增强的"增强"：同批命中资产里，优先返回权威官方来源 + 更近的数据。
    """
    url = item.get("source_url", "") or ""
    rank = 1.0
    if url:
        if any(d in url for d in ["gov", "stat", "stats", "edu", "cas", "org.cn"]):
            rank = 3.0  # L1 官方
        elif any(d in url for d in [".com", ".cn", "media", "news", ".org"]):
            rank = 2.0  # L2 媒体/咨询/企业
        # 否则 L3 保持 1.0
    ts = item.get("created_at", "")
    age_bonus = 0.0
    if ts:
        try:
            from datetime import datetime

            d = datetime.fromisoformat(ts)
            days = (datetime.now() - d).days
            age_bonus = max(0.0, 1.0 - days / 365.0) * 0.2  # 近1年内的资产额外加权
        except Exception:
            pass
    return rank + age_bonus


def search_assets(query: str, industry: str = "", n_results: int = 5) -> list[dict[str, Any]]:
    """检索历史档案：新任务先查资产库，复用已有结果

    - 资产飞轮：同行业第二次调研成本更低、更准
    - 省 token：命中资产则少检索外部数据
    """
    collection = _get_collection()
    try:
        # 内存降级：关键词打分（不需要向量嵌入），跳过 bge-m3 加载，省内存防并发爆
        tokens = [t for t in re.findall(r"[一-龥]{2,6}|\w+", query) if len(t) > 1]
        if isinstance(collection, _MemoryCollection):
            collection.query_tokens(tokens)
            result = collection.query(query_embeddings=[], n_results=n_results)
        else:
            emb = embed_text(query)
            result = collection.query(query_embeddings=[emb], n_results=n_results)
        items = []
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        for i, doc_id in enumerate(ids):
            meta = metas[i] if i < len(metas) else {}
            # 同行业过滤（如果指定了行业）
            if industry and meta.get("industry") != industry:
                continue
            items.append(
                {
                    "id": doc_id,
                    "content": docs[i] if i < len(docs) else "",
                    "industry": meta.get("industry", ""),
                    "source_type": meta.get("source_type", ""),
                    "source_url": meta.get("source_url", ""),
                    "source_title": meta.get("source_title", ""),
                    "created_at": meta.get("created_at", ""),
                }
            )
        # 检索增强：权威来源(L1官方优先) + 时效(近数据加分) 重排
        items.sort(key=_asset_rank, reverse=True)
        return items
    except Exception as e:
        logger.warning("资产检索失败: %s", e)
        return []


# ============================================================
# 顶层接口：查询时结合资产库（RAG）
# ============================================================
def rag_search(query: str, industry: str = "") -> dict[str, Any]:
    """RAG 检索：先查资产库，返回命中的历史档案

    返回值：
    - hits: 命中的历史资产
    - from_assets: 是否从资产库命中（命中则优先用历史，省外部检索）
    """
    hits = search_assets(query, industry=industry, n_results=3)
    return {
        "hits": hits,
        "from_assets": len(hits) > 0,
    }


if __name__ == "__main__":
    import sys

    sys.path.insert(0, ".")
    # 自测：入库 + 检索
    print("=== 资产入库 ===")
    add_asset("咖啡", "杭州咖啡市场规模约120-160亿元，美团报告显示订单量全国第4", "retrieval", "https://meituan.com", "美团咖啡报告")
    print("=== 资产检索 ===")
    hits = search_assets("杭州咖啡市场规模", industry="咖啡")
    for h in hits[:3]:
        print(f"  [{h['industry']}] {h['content'][:50]}... (来源:{h['source_title']})")
