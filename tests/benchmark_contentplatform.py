"""Real benchmark suite against ContentPlatform.

20 queries with expected results. Measures:
- Recall@3: Is the expected file in top 3 results?
- Recall@5: Is the expected file in top 5 results?
- MRR (Mean Reciprocal Rank): Average 1/rank of first correct result
- Hit@1: Is the expected file the #1 result?

Run: python -m tests.benchmark_contentplatform
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from neurodex.config import load_config
from neurodex.registry import Registry
from neurodex.search import SearchEngine

# The repo ID for ContentPlatform (detected during init)
CONTENT_PLATFORM_REPO_ID = "6ed6aeb21072"
CONTENT_PLATFORM_NAME = "ContentPlatform"
CP = "/Users/karanpanchal/GitPoroject/ContentPlatform"


@dataclass
class BenchmarkQuery:
    """A benchmark query with expected results."""
    id: int
    query: str
    expected_files: list[str]  # Any of these files is a correct hit
    category: str  # For grouping results


# 20 real queries a developer/LLM would ask in a new session
BENCHMARK_QUERIES: list[BenchmarkQuery] = [
    # --- Documentation / Architecture ---
    BenchmarkQuery(
        id=1,
        query="what is the ContentPlatform project and its architecture",
        expected_files=[f"{CP}/CLAUDE.md"],
        category="docs",
    ),
    BenchmarkQuery(
        id=2,
        query="engineering principles and coding conventions",
        expected_files=[f"{CP}/CLAUDE.md"],
        category="docs",
    ),
    BenchmarkQuery(
        id=3,
        query="Celery task queue configuration with priority queues",
        expected_files=[f"{CP}/app/tasks/config.py", f"{CP}/docs/TASKS.md"],
        category="infra",
    ),

    # --- Authentication ---
    BenchmarkQuery(
        id=4,
        query="user authentication login register service",
        expected_files=[f"{CP}/app/auth/service.py"],
        category="auth",
    ),
    BenchmarkQuery(
        id=5,
        query="JWT token creation and password hashing security",
        expected_files=[f"{CP}/app/auth/security.py"],
        category="auth",
    ),
    BenchmarkQuery(
        id=6,
        query="Google and Apple OAuth provider integration",
        expected_files=[f"{CP}/app/auth/oauth.py"],
        category="auth",
    ),
    BenchmarkQuery(
        id=7,
        query="User model with email and password fields",
        expected_files=[f"{CP}/app/auth/models.py"],
        category="auth",
    ),

    # --- Brand & Persona ---
    BenchmarkQuery(
        id=8,
        query="brand persona builder generation",
        expected_files=[f"{CP}/app/brands/persona_builder.py"],
        category="brands",
    ),
    BenchmarkQuery(
        id=9,
        query="BrandProfile model with voice colors fonts",
        expected_files=[f"{CP}/app/brands/models.py"],
        category="brands",
    ),
    BenchmarkQuery(
        id=10,
        query="brand RAG retriever chunker embedder",
        expected_files=[
            f"{CP}/app/brands/rag/retriever.py",
            f"{CP}/app/brands/rag/chunker.py",
            f"{CP}/app/brands/rag/embedder.py",
        ],
        category="brands",
    ),

    # --- Content & Workflow ---
    BenchmarkQuery(
        id=11,
        query="content post workflow state machine transitions",
        expected_files=[f"{CP}/app/content/workflow.py"],
        category="content",
    ),
    BenchmarkQuery(
        id=12,
        query="PostService create update submit review approve",
        expected_files=[f"{CP}/app/content/service.py"],
        category="content",
    ),
    BenchmarkQuery(
        id=13,
        query="ContentPost PostVersion PostMedia database models",
        expected_files=[f"{CP}/app/content/models.py"],
        category="content",
    ),

    # --- Generation ---
    BenchmarkQuery(
        id=14,
        query="AI generation AgentRuntime execute generate stream",
        expected_files=[f"{CP}/app/generation/runtime.py"],
        category="generation",
    ),
    BenchmarkQuery(
        id=15,
        query="caption generation skill with brand persona voice",
        expected_files=[f"{CP}/app/generation/skills/generate_caption.py"],
        category="generation",
    ),
    BenchmarkQuery(
        id=16,
        query="generation Celery background task execute job",
        expected_files=[f"{CP}/app/tasks/generation.py"],
        category="generation",
    ),

    # --- Infrastructure ---
    BenchmarkQuery(
        id=17,
        query="FastAPI application factory main router middleware",
        expected_files=[f"{CP}/app/main.py"],
        category="infra",
    ),
    BenchmarkQuery(
        id=18,
        query="SQLAlchemy database session engine get_db",
        expected_files=[f"{CP}/app/core/database.py"],
        category="infra",
    ),
    BenchmarkQuery(
        id=19,
        query="base repository CRUD operations pagination",
        expected_files=[
            f"{CP}/app/common/base_repository.py",
            f"{CP}/app/common/pagination.py",
        ],
        category="infra",
    ),

    # --- Publishing & Chat ---
    BenchmarkQuery(
        id=20,
        query="post scheduling publishing platform adapter",
        expected_files=[
            f"{CP}/app/publishing/service.py",
            f"{CP}/app/publishing/adapters/base.py",
            f"{CP}/app/publishing/adapters/bundle_adapter.py",
        ],
        category="publishing",
    ),
]


def run_benchmark(
    search_engine: SearchEngine,
    repo_ids: list[str],
    repo_names: dict[str, str],
    label: str = "BASELINE",
) -> dict:
    """Run all benchmark queries and compute metrics."""
    results = {
        "label": label,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "queries": [],
        "metrics": {},
    }

    hit_at_1 = 0
    recall_at_3 = 0
    recall_at_5 = 0
    reciprocal_ranks: list[float] = []
    category_scores: dict[str, list[bool]] = {}

    for bq in BENCHMARK_QUERIES:
        search_results = search_engine.search(
            query=bq.query,
            repo_ids=repo_ids,
            repo_names=repo_names,
            max_results=5,
            max_tokens=999999,  # Don't limit by tokens for benchmark
        )

        result_files = [r.file_path for r in search_results]

        # Check if any expected file is in results
        found_at = None
        for rank, rf in enumerate(result_files, 1):
            if rf in bq.expected_files:
                found_at = rank
                break

        # Metrics
        is_hit_1 = found_at == 1
        is_recall_3 = found_at is not None and found_at <= 3
        is_recall_5 = found_at is not None and found_at <= 5
        rr = 1.0 / found_at if found_at else 0.0

        if is_hit_1:
            hit_at_1 += 1
        if is_recall_3:
            recall_at_3 += 1
        if is_recall_5:
            recall_at_5 += 1
        reciprocal_ranks.append(rr)

        # Category tracking
        if bq.category not in category_scores:
            category_scores[bq.category] = []
        category_scores[bq.category].append(is_recall_3)

        status = "PASS" if is_recall_3 else ("PARTIAL" if is_recall_5 else "FAIL")

        results["queries"].append({
            "id": bq.id,
            "query": bq.query,
            "category": bq.category,
            "status": status,
            "found_at_rank": found_at,
            "expected": [Path(f).name for f in bq.expected_files],
            "got": [Path(f).name for f in result_files[:5]],
        })

    n = len(BENCHMARK_QUERIES)
    results["metrics"] = {
        "total_queries": n,
        "hit_at_1": hit_at_1,
        "hit_at_1_pct": round(hit_at_1 / n * 100, 1),
        "recall_at_3": recall_at_3,
        "recall_at_3_pct": round(recall_at_3 / n * 100, 1),
        "recall_at_5": recall_at_5,
        "recall_at_5_pct": round(recall_at_5 / n * 100, 1),
        "mrr": round(sum(reciprocal_ranks) / n, 3),
        "category_recall_at_3": {
            cat: round(sum(scores) / len(scores) * 100, 1)
            for cat, scores in category_scores.items()
        },
    }

    return results


def print_results(results: dict) -> None:
    """Pretty-print benchmark results."""
    m = results["metrics"]

    print(f"\n{'='*70}")
    print(f"  NEURODEX BENCHMARK: {results['label']}")
    print(f"  {results['timestamp']}")
    print(f"{'='*70}")

    print(f"\n  OVERALL METRICS")
    print(f"  {'─'*40}")
    print(f"  Hit@1:      {m['hit_at_1']:2d}/{m['total_queries']}  ({m['hit_at_1_pct']:5.1f}%)")
    print(f"  Recall@3:   {m['recall_at_3']:2d}/{m['total_queries']}  ({m['recall_at_3_pct']:5.1f}%)")
    print(f"  Recall@5:   {m['recall_at_5']:2d}/{m['total_queries']}  ({m['recall_at_5_pct']:5.1f}%)")
    print(f"  MRR:        {m['mrr']:.3f}")

    print(f"\n  CATEGORY BREAKDOWN (Recall@3)")
    print(f"  {'─'*40}")
    for cat, pct in sorted(m["category_recall_at_3"].items()):
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        print(f"  {cat:15s} {bar} {pct:5.1f}%")

    print(f"\n  PER-QUERY RESULTS")
    print(f"  {'─'*40}")
    for q in results["queries"]:
        icon = "✓" if q["status"] == "PASS" else ("~" if q["status"] == "PARTIAL" else "✗")
        rank_str = f"@{q['found_at_rank']}" if q["found_at_rank"] else "miss"
        print(f"  {icon} Q{q['id']:2d} [{q['category']:12s}] {rank_str:5s} | {q['query'][:50]}")
        if q["status"] == "FAIL":
            print(f"       Expected: {q['expected']}")
            print(f"       Got:      {q['got'][:3]}")

    print(f"\n{'='*70}\n")


def main():
    config = load_config()
    registry = Registry(config)
    search_engine = SearchEngine(config)

    repo_ids = [CONTENT_PLATFORM_REPO_ID]
    repo_names = {CONTENT_PLATFORM_REPO_ID: CONTENT_PLATFORM_NAME}

    label = sys.argv[1] if len(sys.argv) > 1 else "BASELINE"
    results = run_benchmark(search_engine, repo_ids, repo_names, label)
    print_results(results)

    # Save results to file
    out_path = Path(__file__).parent / f"benchmark_{label.lower().replace(' ', '_')}.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"  Results saved to: {out_path}")

    search_engine.close_all()
    registry.close()

    return results


if __name__ == "__main__":
    main()
