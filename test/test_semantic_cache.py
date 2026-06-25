"""
语义缓存功能验证脚本

测试三个核心场景并统计缓存收益：
1. 核心功能 — 存取、语义相似、namespace 隔离
2. 真实 Tavily 搜索 — 多次查询观察缓存命中/未命中的延迟差异
3. RAGFlow 模拟 — 知识库检索的缓存加速

运行方式：
    conda run -n deepsearch-agent --no-capture-output python test/test_semantic_cache.py

前提条件：
    - Redis 正在运行
    - deepsearch-agent conda 环境已安装依赖
    - TAVILY_API_KEY 已配置（场景 2 需要）
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from dotenv import load_dotenv

load_dotenv(_project_root / ".env")


# ══════════════════════════════════════════════════════════════════════════════
# 工具函数 & 统计
# ══════════════════════════════════════════════════════════════════════════════

def _hdr(title: str) -> None:
    print(f"\n{'─' * 60}\n  {title}\n{'─' * 60}")


@dataclass
class CacheStats:
    """延迟统计"""
    records: list[dict] = field(default_factory=list)

    def add(self, query: str, hit: bool, ms: float, source: str) -> None:
        self.records.append({"query": query, "hit": hit, "ms": ms, "source": source})

    def _hits(self):   return [r for r in self.records if r["hit"]]
    def _misses(self): return [r for r in self.records if not r["hit"]]
    def _api(self):    return [r for r in self.records if r["source"] == "tavily_api"]
    def _cache(self):  return [r for r in self.records if r["source"] == "cache"]

    def _p50(self, vals):
        if not vals: return 0
        return sorted(vals)[len(vals) // 2]

    def _avg(self, vals):
        return sum(vals) / len(vals) if vals else 0

    def report(self) -> None:
        api_ms   = [r["ms"] for r in self._api()]
        cache_ms = [r["ms"] for r in self._cache()]
        speedup  = self._avg(api_ms) / self._avg(cache_ms) if api_ms and cache_ms else 0

        _hdr("缓存收益报告")
        print(f"""
    查询总量: {len(self.records)}     命中: {len(self._hits())}     未命中: {len(self._misses())}     命中率: {len(self._hits())/max(len(self.records),1)*100:.1f}%

    ┌────────────┬──────────┬──────────┬──────────┬──────────┐
    │ 来源       │ 次数     │ 平均     │ P50      │ 范围     │
    ├────────────┼──────────┼──────────┼──────────┼──────────┤
    │ Tavily API │ {len(self._api()):>8} │ {self._avg(api_ms):>7.0f}ms │ {self._p50(api_ms):>7.0f}ms │ {min(api_ms) if api_ms else 0:>6.0f}-{max(api_ms) if api_ms else 0:<5.0f}ms │
    │ Cache 命中 │ {len(self._cache()):>8} │ {self._avg(cache_ms):>7.0f}ms │ {self._p50(cache_ms):>7.0f}ms │ {min(cache_ms) if cache_ms else 0:>6.0f}-{max(cache_ms) if cache_ms else 0:<5.0f}ms │
    └────────────┴──────────┴──────────┴──────────┴──────────┘

    加速比: {speedup:.1f}x  →  缓存将外部 API 调用延迟从 ~{self._avg(api_ms):.0f}ms 降至 ~{self._avg(cache_ms):.0f}ms
    跳过 API 调用: {len(self._cache())} 次 (节省约 ${len(self._cache())*0.005:.3f} Tavily 费用)
""")


_stats = CacheStats()

# 全局共享的 store 实例，避免每次场景都重新加载模型
_shared_store = None


async def _get_store():
    """获取或初始化共享的 SemanticCacheStore"""
    global _shared_store
    if _shared_store is None:
        from app.persistence.cache_store import SemanticCacheStore
        _shared_store = SemanticCacheStore()
        await _shared_store.start()
    return _shared_store


async def _inject_store():
    """将共享 store 注入全局单例，使 check_cache/save_to_cache 可用"""
    import app.persistence.cache_store as cs_module
    cs_module._cache_store = await _get_store()


async def _cleanup_store():
    """从全局单例中移除 store 引用（不关闭，后续场景继续用）"""
    import app.persistence.cache_store as cs_module
    cs_module._cache_store = None


# ══════════════════════════════════════════════════════════════════════════════
# 场景 1: 核心功能验证
# ══════════════════════════════════════════════════════════════════════════════

async def scenario_1_core():
    """验证 SemanticCacheStore 的基本存取、语义相似、namespace 隔离"""
    store = await _get_store()

    # 1a. 生命周期 + embedding
    emb = await store._get_embedding("测试")
    assert len(emb) == 384, f"维度异常: {len(emb)}"
    print("  ✅ 模型加载正常 (384维)")

    # 1b. 存取 + 相同命中
    await store.redis.delete("insighter:cache:s1")
    hit, _ = await store.lookup("s1", "随便问")
    assert not hit, "空 ns 应 miss"

    t0 = time.perf_counter()
    await store.store("s1", "2026 年 AI 发展趋势", "缓存结果内容...")
    store_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    hit, result = await store.lookup("s1", "2026 年 AI 发展趋势")
    lookup_ms = (time.perf_counter() - t0) * 1000
    assert hit and result == "缓存结果内容..."
    print(f"  ✅ 相同查询命中 — store={store_ms:.0f}ms, lookup={lookup_ms:.0f}ms")

    # 1c. 语义相似匹配
    await store.redis.delete("insighter:cache:s1_sim")
    await store.store("s1_sim", "新能源汽车补贴政策 2026", "补贴搜索结果...")

    hit, _ = await store.lookup("s1_sim", "2026年电动车购置税减免")
    tag = "✅" if hit else "❌"
    print(f"  {tag} 语义相似: '电动车购置税减免' → {'hit' if hit else 'miss'} (相似度阈值的预期行为)")

    hit, _ = await store.lookup("s1_sim", "MySQL 数据库索引优化技巧")
    assert not hit, "不相关查询应 miss"
    print(f"  ✅ 语义不相关: 'MySQL索引' → miss (正确隔离)")

    # 1d. namespace 隔离
    await store.redis.delete("insighter:cache:ns_a")
    await store.redis.delete("insighter:cache:ns_b")
    await store.store("ns_a", "气候变暖影响", "结果A")
    await store.store("ns_b", "气候变暖影响", "结果B")

    _, ra = await store.lookup("ns_a", "气候变暖影响")
    _, rb = await store.lookup("ns_b", "气候变暖影响")
    assert ra != rb, "不同 ns 应返回不同结果"
    print(f"  ✅ namespace 隔离: ns_a≠ns_b")

    # 清理
    for k in ("s1", "s1_sim", "ns_a", "ns_b"):
        await store.redis.delete(f"insighter:cache:{k}")


# ══════════════════════════════════════════════════════════════════════════════
# 场景 2: 真实 Tavily 搜索 + 缓存
# ══════════════════════════════════════════════════════════════════════════════

async def scenario_2_tavily():
    """真实 Tavily 搜索 — 展示缓存命中/未命中的延迟差异"""
    tavily_api_key = os.getenv("TAVILY_API_KEY")
    if not tavily_api_key or "你的" in tavily_api_key:
        print("  ⚠️ 未配置 TAVILY_API_KEY，跳过")
        return

    from tavily import TavilyClient
    from app.persistence.cache_store import check_cache, save_to_cache

    await _inject_store()
    store = await _get_store()
    await store.redis.delete("insighter:cache:tavily")
    tavily = TavilyClient(api_key=tavily_api_key)
    ns = "tavily"

    groups = [
        # 1. 测试长尾/具体细节查询（验证缓存能否覆盖细粒度问题）
        ("节假日", "2026年中国法定节假日放假安排", "general", 5, [
            "中国2026年节假日放假时间表",
            "2026国庆节放假几天",
            "2026年调休安排表",
            "2026年春节法定假期是哪几天",
            "2026国亲节放假安排",  # 错别字测试
            "2026年 国庆节 放假 几天？",  # 包含空格和标点
            "2025年节假日安排",  # 过去的时间
            "2027年放假安排",  # 未来的时间
        ]),
        # 2. 测试模糊查询与口语化表达（验证语义匹配/缓存泛化能力）
        ("新能源政策", "2026年中国新能源汽车补贴政策", "general", 5, [
            "明年买电车还免税吗",
            "2026年新能源车还有补贴吗",
            "现在买新能源车政策怎么样",
            "电车补贴",
        ]),
        # 3. 测试多意图/复合查询（验证缓存能否拆解或匹配复杂Query）
        ("量子计算", "2025年2026年量子计算最新突破进展", "news", 5, [
            "量子计算最新进展及国内研究机构排名",
            "2026年量子计算商业化落地情况",
        ]),
        # 4. 测试强负样本（完全不相关的查询，验证缓存防误命中机制）
        ("不相关", "Python asyncio 协程编程最佳实践 2026", "general", 3, [
            "今天武汉天气怎么样",
            "红烧肉怎么做才好吃",
            "马斯克最新推特发言",
        ])
    ]

    print(f"\n  {'组':<10} {'轮':>4}  {'延迟':>10}  {'结果':<8}  {'说明'}")
    print(f"  {'─'*10} {'─'*4} {'─'*10} {'─'*8} {'─'*35}")

    for label, query, topic, mr, follow_ups in groups:
        cq = f"{query}|||{topic}|||{mr}"

        # --- 第 1 轮: 真实 API ---
        t0 = time.perf_counter()
        cached = await check_cache(ns, cq)
        check_ms = (time.perf_counter() - t0) * 1000

        if cached is None:
            t0 = time.perf_counter()
            result = tavily.search(query=query, topic=topic, max_results=mr)
            api_ms = (time.perf_counter() - t0) * 1000
            n = len(result.get("results", []))
            await save_to_cache(ns, cq, json.dumps(result, ensure_ascii=False, default=str))
            _stats.add(query[:40], False, api_ms, "tavily_api")
            print(f"  {label:<10} {'1':>4}  {api_ms:>8.0f}ms  MISS     API调用 ({n}条结果) → 已缓存")
        else:
            _stats.add(query[:40], True, check_ms, "cache")
            print(f"  {label:<10} {'1':>4}  {check_ms:>8.0f}ms  HIT      缓存命中 (已预热)")

        # --- 第 2 轮: 相同查询 → 必须命中 ---
        t0 = time.perf_counter()
        cached = await check_cache(ns, cq)
        hit2_ms = (time.perf_counter() - t0) * 1000
        assert cached is not None, f"相同查询应命中: {query}"
        _stats.add(f"[重复]{query[:30]}", True, hit2_ms, "cache")
        print(f"  {label:<10} {'2':>4}  {hit2_ms:>8.0f}ms  HIT      相同查询命中")

        # --- 第 3+ 轮: 语义相似追问 ---
        for i, fu in enumerate(follow_ups, 3):
            fu_cq = f"{fu}|||{topic}|||{mr}"
            t0 = time.perf_counter()
            cached = await check_cache(ns, fu_cq)
            fu_check_ms = (time.perf_counter() - t0) * 1000

            if cached is not None:
                _stats.add(f"[相似]{fu[:30]}", True, fu_check_ms, "cache")
                print(f"  {label:<10} {i:>4}  {fu_check_ms:>8.0f}ms  HIT      语义相似命中: {fu[:30]}...")
            else:
                t0 = time.perf_counter()
                try:
                    result = tavily.search(query=fu, topic=topic, max_results=mr)
                    fu_api_ms = (time.perf_counter() - t0) * 1000
                    await save_to_cache(ns, fu_cq, json.dumps(result, ensure_ascii=False, default=str))
                    _stats.add(f"[相似]{fu[:30]}", False, fu_api_ms, "tavily_api")
                    print(f"  {label:<10} {i:>4}  {fu_api_ms:>8.0f}ms  API      语义相关但未命中: {fu[:30]}...")
                except Exception as e:
                    print(f"  {label:<10} {i:>4}  {'─':>8}  ERR      {e}")

    await store.redis.delete("insighter:cache:tavily")
    await _cleanup_store()
    print(f"\n  ✅ Tavily 搜索测试完成")


# ══════════════════════════════════════════════════════════════════════════════
# 场景 3: RAGFlow 模拟缓存
# ══════════════════════════════════════════════════════════════════════════════

async def scenario_3_ragflow():
    """模拟 RAGFlow 知识库查询 — 展示缓存加速效果"""
    from app.persistence.cache_store import check_cache, save_to_cache

    await _inject_store()
    store = await _get_store()
    await store.redis.delete("insighter:cache:ragflow")
    ns = "ragflow"

    cases = [
        ("电商助手", "如何制定2026年电商平台AI应用路线图？"),
        ("供应链助手", "库存预测模型的选型和部署方案是什么？"),
        ("客服助手", "智能客服系统NLU模块的优化方法有哪些？"),
    ]

    RAGFLOW_LATENCY = 3000  # 模拟 RAGFlow 典型延迟 3s

    print(f"\n  {'助手':<8} {'轮':>4}  {'延迟':>12}  {'说明'}")
    print(f"  {'─'*8} {'─'*4} {'─'*12} {'─'*35}")

    for chat_name, question in cases:
        cq = f"{chat_name}|||{question}"

        # 第1次 — 模拟真实 RAGFlow 调用
        t0 = time.perf_counter()
        cached = await check_cache(ns, cq)
        check_ms = (time.perf_counter() - t0) * 1000

        if cached is None:
            await asyncio.sleep(0.05)  # 快速模拟，不计入真实延迟
            answer = f"[模拟回答] {chat_name} 对 '{question[:20]}...' 的知识库检索结果"
            await save_to_cache(ns, cq, answer)
            _stats.add(question[:40], False, RAGFLOW_LATENCY, "ragflow")
            print(f"  {chat_name:<8} {'1':>4}  {RAGFLOW_LATENCY:>8.0f}ms  MISS → 已缓存 (模拟RAGFlow {RAGFLOW_LATENCY}ms)")
        else:
            _stats.add(question[:40], True, check_ms, "cache")
            print(f"  {chat_name:<8} {'1':>4}  {check_ms:>8.0f}ms  HIT (已预热)")

        # 第2次 — 必须命中
        t0 = time.perf_counter()
        cached = await check_cache(ns, cq)
        h2 = (time.perf_counter() - t0) * 1000
        assert cached is not None, f"相同查询应命中: {question}"
        _stats.add(f"[重复]{question[:30]}", True, h2, "cache")
        print(f"  {chat_name:<8} {'2':>4}  {h2:>8.0f}ms  HIT 相同查询命中 (加速 {RAGFLOW_LATENCY/h2:.0f}x)")

    await store.redis.delete("insighter:cache:ragflow")
    await _cleanup_store()
    print(f"\n  ✅ RAGFlow 模拟测试完成")


# ══════════════════════════════════════════════════════════════════════════════
# 主函数
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    print("=" * 60)
    print("  语义缓存验证")
    print(f"  Redis: {os.getenv('REDIS_HOST', 'localhost')}:{os.getenv('REDIS_PORT', '6379')}"
          f"  | 阈值: {os.getenv('CACHE_SIMILARITY_THRESHOLD', '0.92')}"
          f"  | TTL: {os.getenv('CACHE_TTL_SECONDS', '3600')}s")
    print("=" * 60)

    global _shared_store

    try:
        # ---- 场景 1: 核心功能 ----
        # _hdr("场景 1: 核心功能验证")
        # await scenario_1_core()

        # ---- 场景 2: 真实 Tavily 搜索 ----
        _hdr("场景 2: 真实 Tavily 搜索 + 缓存加速对比")
        await scenario_2_tavily()

        # ---- 场景 3: RAGFlow 模拟 ----
        # _hdr("场景 3: RAGFlow 知识库缓存 (模拟)")
        # await scenario_3_ragflow()

        # ---- 汇总统计 ----
        _stats.report()
    finally:
        # 释放全局实例
        if _shared_store is not None:
            await _shared_store.stop()
            _shared_store = None

if __name__ == "__main__":
    asyncio.run(main())
