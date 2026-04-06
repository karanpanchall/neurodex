"""Cross-project benchmark: GakkoDeck Backend + Frontend + Monorepo + Flutter.

Tests three things:
1. Per-project search accuracy (does search find the right file?)
2. Brain quality (does the brain capture what matters?)
3. Cross-project routing (can the LLM figure out which project to query?)

Run: python tests/benchmark_gakkodeck.py
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engram.brain import generate_brain, render_brain
from engram.config import load_config
from engram.registry import Registry
from engram.search import SearchEngine
from engram.store import RepoStore

# Repo IDs (from engram status)
REPOS = {
    "backend": ("b5b44a092bca", "GakkoDeck-Backend", "/Users/karanpanchal/GitPoroject/GakkoDeck-Backend"),
    "frontend": ("368383db37ee", "GakkoDeck-Frontend", "/Users/karanpanchal/GitPoroject/GakkoDeck-Frontend"),
    "monorepo": ("a8daa0bed0da", "GakkoDeck-Monorepo", "/Users/karanpanchal/GitPoroject/GakkoDeck-Monorepo"),
    "flutter": ("4788cf4fd839", "GakkoDeck-Flutter", "/Users/karanpanchal/GitPoroject/GakkoDeck-Flutter"),
}


@dataclass
class Query:
    id: int
    query: str
    target_repo: str  # Which repo has the answer
    expected_files: list[str]  # Partial path matches (filename or dir/filename)
    category: str


# 20 queries that test cross-project understanding
QUERIES: list[Query] = [
    # --- Backend ---
    Query(1, "flashcard study set creation API endpoint", "backend",
          ["routes.py", "study_sets", "flashcards"], "backend-api"),
    Query(2, "Stripe subscription billing webhook handler", "backend",
          ["stripe", "billing", "webhook", "subscription"], "backend-billing"),
    Query(3, "AI agent chat completion with memory", "backend",
          ["agent", "memory", "chat"], "backend-ai"),
    Query(4, "user authentication login JWT token", "backend",
          ["auth", "login", "token", "jwt"], "backend-auth"),
    Query(5, "database migration SQL schema", "backend",
          ["migration", ".sql", "alembic"], "backend-db"),

    # --- Frontend ---
    Query(6, "React flashcard flip animation component", "frontend",
          ["FlashcardFlip", "flashcard", "Flip"], "frontend-ui"),
    Query(7, "chat conversation message list component", "frontend",
          ["ChatConversation", "ChatMessageList", "chat"], "frontend-ui"),
    Query(8, "admin dashboard layout and data table", "frontend",
          ["AdminLayout", "AdminDataTable", "admin"], "frontend-admin"),
    Query(9, "authentication protected route guard", "frontend",
          ["ProtectedRoute", "AdminProtectedRoute", "auth"], "frontend-auth"),
    Query(10, "sidebar navigation app layout", "frontend",
           ["AppSidebar", "AppLayout", "layout"], "frontend-layout"),

    # --- Monorepo ---
    Query(11, "React Native mobile app entry point", "monorepo",
          ["App.tsx", "index.js", "app"], "monorepo-mobile"),
    Query(12, "mobile chat bot floating panel component", "monorepo",
          ["FloatingBotPanel", "FloatingChatBot", "bot"], "monorepo-ui"),
    Query(13, "API client configuration and endpoints", "monorepo",
          ["client.ts", "api", "fetch"], "monorepo-api"),
    Query(14, "mobile authentication login screen", "monorepo",
          ["auth", "login", "screen", "AccountLink"], "monorepo-auth"),
    Query(15, "app store submission and provisioning", "monorepo",
          ["SUBMISSION", "store", "AppDelegate", "entitlements"], "monorepo-config"),

    # --- Flutter ---
    Query(16, "Dart flashcard widget study screen", "flutter",
          ["flashcard", "study", "widget", ".dart"], "flutter-ui"),
    Query(17, "Flutter API service HTTP client", "flutter",
          ["api", "service", "http", "client", ".dart"], "flutter-api"),
    Query(18, "Flutter state management provider", "flutter",
          ["provider", "state", "store", "notifier"], "flutter-state"),
    Query(19, "iOS and Android build configuration", "flutter",
          ["build.gradle", "Podfile", "Runner", "ios", "android"], "flutter-config"),
    Query(20, "push notification handler Firebase", "flutter",
          ["notification", "firebase", "push", "fcm", "messaging"], "flutter-notif"),
]


def run_benchmark():
    config = load_config()
    registry = Registry(config)
    search_engine = SearchEngine(config)

    print(f"\n{'='*70}")
    print(f"  GAKKODECK CROSS-PROJECT BENCHMARK")
    print(f"  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}")

    # --- Part 1: Brain Stats ---
    print(f"\n  BRAIN STATS")
    print(f"  {'─'*40}")
    total_brain_tokens = 0
    for key, (repo_id, name, path) in REPOS.items():
        db_path = config.repo_db_path(repo_id)
        if not db_path.exists():
            print(f"  {name:25s} NOT INDEXED")
            continue
        store = RepoStore(db_path, repo_id, name)
        brain = generate_brain(store, config)
        rendered = render_brain(brain, store)
        tokens = len(rendered) // 4
        total_brain_tokens += tokens
        print(f"  {name:25s} {tokens:6,} tokens | {len(brain.modules):3d} modules | {store.get_file_count():4d} files")
        store.close()

    print(f"  {'─'*40}")
    print(f"  {'TOTAL':25s} {total_brain_tokens:6,} tokens (all 4 projects)")
    print(f"  {'LLM context usage':25s} {total_brain_tokens/200_000*100:.1f}% of 200k window")

    # --- Part 2: Per-Project Search Accuracy ---
    print(f"\n  SEARCH ACCURACY (per project)")
    print(f"  {'─'*40}")

    hit_at_1 = 0
    recall_at_3 = 0
    recall_at_5 = 0
    reciprocal_ranks: list[float] = []
    results_detail = []

    for q in QUERIES:
        repo_id, repo_name, _ = REPOS[q.target_repo]
        repo_names = {repo_id: repo_name}

        search_results = search_engine.search(
            query=q.query,
            repo_ids=[repo_id],
            repo_names=repo_names,
            max_results=5,
            max_tokens=999999,
        )

        result_files = [Path(r.file_path).name for r in search_results]
        result_paths = [r.file_path for r in search_results]

        # Check if any expected pattern matches any result
        found_at = None
        for rank, rf_path in enumerate(result_paths, 1):
            rf_name = Path(rf_path).name
            rf_lower = rf_path.lower()
            for expected in q.expected_files:
                if expected.lower() in rf_lower or expected.lower() in rf_name.lower():
                    found_at = rank
                    break
            if found_at:
                break

        is_hit_1 = found_at == 1
        is_recall_3 = found_at is not None and found_at <= 3
        is_recall_5 = found_at is not None and found_at <= 5
        rr = 1.0 / found_at if found_at else 0.0

        if is_hit_1: hit_at_1 += 1
        if is_recall_3: recall_at_3 += 1
        if is_recall_5: recall_at_5 += 1
        reciprocal_ranks.append(rr)

        status = "PASS" if is_recall_3 else ("PARTIAL" if is_recall_5 else "FAIL")
        icon = "✓" if status == "PASS" else ("~" if status == "PARTIAL" else "✗")
        rank_str = f"@{found_at}" if found_at else "miss"

        results_detail.append((q, status, found_at, result_files[:3]))
        print(f"  {icon} Q{q.id:2d} [{q.target_repo:10s}] {rank_str:5s} | {q.query[:45]}")
        if status == "FAIL":
            print(f"       Expected: {q.expected_files[:3]}")
            print(f"       Got: {result_files[:3]}")

    n = len(QUERIES)
    mrr = sum(reciprocal_ranks) / n

    print(f"\n  OVERALL METRICS")
    print(f"  {'─'*40}")
    print(f"  Hit@1:      {hit_at_1:2d}/{n}  ({hit_at_1/n*100:5.1f}%)")
    print(f"  Recall@3:   {recall_at_3:2d}/{n}  ({recall_at_3/n*100:5.1f}%)")
    print(f"  Recall@5:   {recall_at_5:2d}/{n}  ({recall_at_5/n*100:5.1f}%)")
    print(f"  MRR:        {mrr:.3f}")

    # Per-repo breakdown
    print(f"\n  PER-REPO RECALL@3")
    print(f"  {'─'*40}")
    for key in REPOS:
        repo_queries = [r for r in results_detail if r[0].target_repo == key]
        repo_hits = sum(1 for r in repo_queries if r[1] == "PASS")
        total = len(repo_queries)
        pct = repo_hits / total * 100 if total else 0
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        print(f"  {key:12s} {bar} {repo_hits}/{total} ({pct:.0f}%)")

    # --- Part 3: Cross-Project Routing Test ---
    print(f"\n  CROSS-PROJECT ROUTING TEST")
    print(f"  {'─'*40}")
    print(f"  Can a brain-equipped LLM figure out which repo to query?")
    print()

    routing_queries = [
        ("fix the flashcard API returning 500 errors", "backend",
         "Backend has the API (FastAPI + routes). Error is server-side."),
        ("the chat UI is showing messages out of order", "frontend",
         "Frontend has ChatMessageList.tsx, ChatConversation.tsx."),
        ("push notifications not arriving on Android", "flutter",
         "Flutter has Firebase/FCM integration."),
        ("update the mobile app store screenshots", "monorepo",
         "Monorepo has app-store-previews/ directory."),
        ("the flashcard flip animation is janky on mobile", "monorepo",
         "Monorepo has the React Native mobile app with components."),
    ]

    routing_correct = 0
    for query, expected_repo, reason in routing_queries:
        # Search ALL repos and see which one ranks highest
        all_results = []
        for key, (repo_id, repo_name, _) in REPOS.items():
            results = search_engine.search(
                query=query, repo_ids=[repo_id],
                repo_names={repo_id: repo_name},
                max_results=1, max_tokens=999999,
            )
            if results:
                all_results.append((key, results[0].final_score))

        if all_results:
            all_results.sort(key=lambda x: x[1])  # Lower = more relevant
            top_repo = all_results[0][0]
            correct = top_repo == expected_repo
            if correct: routing_correct += 1
            icon = "✓" if correct else "✗"
            print(f"  {icon} \"{query[:50]}\"")
            print(f"    Expected: {expected_repo} | Got: {top_repo} | {reason}")
        else:
            print(f"  ✗ \"{query[:50]}\" — no results from any repo")

    print(f"\n  Routing accuracy: {routing_correct}/{len(routing_queries)} ({routing_correct/len(routing_queries)*100:.0f}%)")

    # --- Final Summary ---
    print(f"\n{'='*70}")
    print(f"  SUMMARY")
    print(f"{'='*70}")
    print(f"  Brain: {total_brain_tokens:,} tokens for 4 projects ({sum(r.file_count for r in registry.list_repos() if r.name.startswith('Gakko'))} files)")
    print(f"  Search: {recall_at_3/n*100:.0f}% Recall@3 | {hit_at_1/n*100:.0f}% Hit@1 | {mrr:.3f} MRR")
    print(f"  Routing: {routing_correct}/{len(routing_queries)} correct cross-project routing")
    print(f"{'='*70}\n")

    search_engine.close_all()
    registry.close()


if __name__ == "__main__":
    run_benchmark()
