"""BM25 search engine with smart ranking.

Searches across one or multiple repo stores with:
- FTS5 BM25 keyword search
- Synonym expansion for vocabulary bridging
- Re-ranking by recency, chunk type, and symbol match
- Deduplication (best chunk per file)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from neurodex.config import EngramConfig
from neurodex.store import RepoStore, SearchResult
from neurodex.synonyms import expand_query


@dataclass
class RankedResult:
    """Search result with composite ranking score."""
    chunk_id: str
    file_path: str
    repo_id: str
    repo_name: str
    symbol_name: str | None
    symbol_type: str | None
    chunk_type: str
    language: str | None
    content: str
    summary: str | None
    line_start: int | None
    line_end: int | None
    bm25_score: float
    final_score: float
    more_in_file: int


RECENCY_WEIGHT = 0.15
CLAUDE_MD_BOOST = 3.0
TYPE_BOOST_DOC = 1.5
TYPE_BOOST_INSIGHT = 2.0
SYMBOL_MATCH_BONUS = 2.0
FILE_PATH_MATCH_BONUS = 1.5

PRIORITY_DOC_NAMES = {"CLAUDE.md", "AGENTS.md", "README.md", "ARCHITECTURE.md"}


class SearchEngine:
    """Searches across repo stores with ranking."""

    def __init__(self, config: EngramConfig) -> None:
        self._config = config
        self._stores: dict[str, RepoStore] = {}

    def get_or_open_store(self, repo_id: str, repo_name: str) -> RepoStore:
        """Get a store, opening it if needed."""
        if repo_id not in self._stores:
            db_path = self._config.repo_db_path(repo_id)
            if not db_path.exists():
                raise FileNotFoundError(f"No index found for repo {repo_id}")
            self._stores[repo_id] = RepoStore(db_path, repo_id, repo_name)
        return self._stores[repo_id]

    def close_all(self) -> None:
        for store in self._stores.values():
            store.close()
        self._stores.clear()

    def search(
        self,
        query: str,
        repo_ids: list[str],
        repo_names: dict[str, str] | None = None,
        max_results: int = 5,
        max_tokens: int = 3000,
        expand_synonyms: bool = True,
    ) -> list[RankedResult]:
        """Search across multiple repos with ranking.

        Args:
            query: Search query
            repo_ids: List of repo IDs to search
            repo_names: Optional mapping of repo_id → human name
            max_results: Max results to return
            max_tokens: Max total tokens in results (approximate)
            expand_synonyms: Whether to expand query with synonyms
        """
        repo_names = repo_names or {}

        search_query = expand_query(query) if expand_synonyms else query

        all_results: list[SearchResult] = []
        for repo_id in repo_ids:
            name = repo_names.get(repo_id, repo_id)
            try:
                store = self.get_or_open_store(repo_id, name)
                results = store.search_bm25(search_query, limit=max_results * 2)
                all_results.extend(results)
            except FileNotFoundError:
                continue

        if not all_results:
            return []

        ranked = self._rerank(all_results, query)

        deduped = self._deduplicate(ranked)

        final: list[RankedResult] = []
        token_count = 0
        chars_per_token = 4

        for result in deduped[:max_results * 2]:
            chunk_tokens = len(result.content) // chars_per_token
            if token_count + chunk_tokens > max_tokens and final:
                break
            token_count += chunk_tokens
            final.append(result)
            if len(final) >= max_results:
                break

        return final

    def search_compact(
        self,
        query: str,
        repo_ids: list[str],
        repo_names: dict[str, str] | None = None,
        max_results: int = 10,
    ) -> list[RankedResult]:
        """Compact search -- returns metadata only, no content."""
        results = self.search(
            query, repo_ids, repo_names,
            max_results=max_results, max_tokens=999999,
        )
        for result in results:
            result.content = ""
        return results

    def search_symbols(
        self,
        pattern: str,
        repo_ids: list[str],
        repo_names: dict[str, str] | None = None,
        max_results: int = 50,
    ) -> list[RankedResult]:
        """Search for symbols (function/class names) by pattern."""
        repo_names = repo_names or {}
        all_results: list[RankedResult] = []

        for repo_id in repo_ids:
            name = repo_names.get(repo_id, repo_id)
            try:
                store = self.get_or_open_store(repo_id, name)
                chunks = store.search_symbols(pattern, limit=max_results)
                for chunk in chunks:
                    all_results.append(RankedResult(
                        chunk_id=chunk.id,
                        file_path=chunk.file_path,
                        repo_id=repo_id,
                        repo_name=name,
                        symbol_name=chunk.symbol_name,
                        symbol_type=chunk.symbol_type,
                        chunk_type=chunk.chunk_type,
                        language=chunk.language,
                        content="",
                        summary=chunk.summary,
                        line_start=chunk.line_start,
                        line_end=chunk.line_end,
                        bm25_score=0.0,
                        final_score=0.0,
                        more_in_file=0,
                    ))
            except FileNotFoundError:
                continue

        return all_results[:max_results]

    def _rerank(
        self, results: list[SearchResult], original_query: str
    ) -> list[RankedResult]:
        """Re-rank results with composite scoring."""
        query_words = set(original_query.lower().split())
        ranked: list[RankedResult] = []
        now = time.time()

        for result in results:
            score = result.bm25_score

            file_basename = result.chunk.file_path.rsplit("/", 1)[-1] if "/" in result.chunk.file_path else result.chunk.file_path
            if file_basename in PRIORITY_DOC_NAMES:
                score *= CLAUDE_MD_BOOST
            elif result.chunk.chunk_type == "doc":
                score *= TYPE_BOOST_DOC
            elif result.chunk.chunk_type == "insight":
                score *= TYPE_BOOST_INSIGHT

            if result.chunk.symbol_name:
                sym_lower = result.chunk.symbol_name.lower().replace("_", " ").replace("/", " ").replace(".", " ")
                sym_words = set(sym_lower.split())
                overlap = query_words & sym_words
                if overlap:
                    match_ratio = len(overlap) / max(len(query_words), 1)
                    score *= (1.0 + match_ratio * (SYMBOL_MATCH_BONUS - 1.0))

            fp_lower = result.chunk.file_path.lower().replace("/", " ").replace("_", " ").replace(".", " ")
            fp_words = set(fp_lower.split())
            fp_overlap = query_words & fp_words
            if fp_overlap:
                score *= FILE_PATH_MATCH_BONUS

            if result.chunk.last_modified:
                age_hours = (now - result.chunk.last_modified) / 3600
                if age_hours < 168:
                    score *= (1 + RECENCY_WEIGHT)

            ranked.append(RankedResult(
                chunk_id=result.chunk.id,
                file_path=result.chunk.file_path,
                repo_id=result.repo_id,
                repo_name=result.repo_name,
                symbol_name=result.chunk.symbol_name,
                symbol_type=result.chunk.symbol_type,
                chunk_type=result.chunk.chunk_type,
                language=result.chunk.language,
                content=result.chunk.content,
                summary=result.chunk.summary,
                line_start=result.chunk.line_start,
                line_end=result.chunk.line_end,
                bm25_score=result.bm25_score,
                final_score=score,
                more_in_file=0,
            ))

        ranked.sort(key=lambda x: x.final_score)
        return ranked

    def _deduplicate(self, results: list[RankedResult]) -> list[RankedResult]:
        """Keep best chunk per file, track how many more matched."""
        seen_files: dict[str, int] = {}
        deduped: list[RankedResult] = []

        for result in results:
            key = f"{result.repo_id}:{result.file_path}"
            if key not in seen_files:
                seen_files[key] = 0
                deduped.append(result)
            else:
                seen_files[key] += 1

        for result in deduped:
            key = f"{result.repo_id}:{result.file_path}"
            result.more_in_file = seen_files.get(key, 0)

        return deduped
