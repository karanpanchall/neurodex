"""Cross-project API contract detection.

Bridges between projects by detecting:
- Backend: API endpoints (routes, schemas, response types)
- Frontend/Mobile: API client calls (fetch URLs, TypeScript types, SDK methods)
- Shared: model/type names that appear in multiple projects

When a backend endpoint changes, this module finds every frontend/mobile
file that consumes it.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from neurodex.config import EngramConfig
from neurodex.registry import Registry
from neurodex.store import RepoStore


@dataclass
class ApiEndpoint:
    """A backend API endpoint."""
    method: str
    path: str
    file: str
    line: int
    handler: str
    request_schema: str | None = None
    response_schema: str | None = None
    repo_id: str = ""
    repo_name: str = ""


@dataclass
class ApiConsumer:
    """A frontend/mobile reference to an API endpoint."""
    url_pattern: str
    method: str | None
    file: str
    line: int
    context: str
    repo_id: str = ""
    repo_name: str = ""


@dataclass
class ContractMatch:
    """A matched API contract between backend and consumer."""
    endpoint: ApiEndpoint
    consumers: list[ApiConsumer] = field(default_factory=list)


@dataclass
class CrossProjectImpact:
    """Impact of a backend change across all consuming projects."""
    changed_file: str
    changed_repo: str
    affected_endpoints: list[ApiEndpoint] = field(default_factory=list)
    affected_contracts: list[ContractMatch] = field(default_factory=list)
    shared_types: list[dict] = field(default_factory=list)


def extract_api_endpoints(store: RepoStore) -> list[ApiEndpoint]:
    """Extract API endpoints from a backend project."""
    endpoints: list[ApiEndpoint] = []

    rows = store._conn.execute(
        """SELECT file_path, content, line_start FROM chunks
        WHERE chunk_type='code' AND (file_path LIKE '%router%' OR file_path LIKE '%route%')"""
    ).fetchall()

    for row in rows:
        content = row["content"] or ""
        file_path = row["file_path"]
        base_line = row["line_start"] or 0

        for match in re.finditer(
            r'@\w+\.(get|post|put|patch|delete)\(\s*["\']([^"\']+)["\']',
            content, re.IGNORECASE,
        ):
            method = match.group(1).upper()
            path = match.group(2)
            line = base_line + content[:match.start()].count("\n")

            rest = content[match.end():]
            handler_match = re.search(r"(?:async\s+)?def\s+(\w+)", rest)
            handler = handler_match.group(1) if handler_match else ""

            handler_block = rest[:500]
            req_schema = _find_schema(handler_block, "request|body|payload")
            resp_schema = _find_schema(handler_block, "response|return")

            endpoints.append(ApiEndpoint(
                method=method, path=path, file=file_path, line=line,
                handler=handler, request_schema=req_schema,
                response_schema=resp_schema,
                repo_id=store.repo_id, repo_name=store.repo_name,
            ))

        for match in re.finditer(
            r'(?:app|router)\.(get|post|put|patch|delete)\(\s*["\']([^"\']+)',
            content, re.IGNORECASE,
        ):
            endpoints.append(ApiEndpoint(
                method=match.group(1).upper(), path=match.group(2),
                file=file_path, line=base_line + content[:match.start()].count("\n"),
                handler="", repo_id=store.repo_id, repo_name=store.repo_name,
            ))

    return endpoints


def extract_api_consumers(store: RepoStore) -> list[ApiConsumer]:
    """Extract API client calls from a frontend/mobile project."""
    consumers: list[ApiConsumer] = []

    rows = store._conn.execute(
        "SELECT file_path, content, line_start FROM chunks WHERE chunk_type='code'"
    ).fetchall()

    for row in rows:
        content = row["content"] or ""
        file_path = row["file_path"]
        base_line = row["line_start"] or 0

        url_patterns = [
            (r"""(?:fetch|axios|http|api)\s*[.(]\s*['"`]([^'"`]+)['"`]""", None),
            (r"""\.(get|post|put|patch|delete)\s*\(\s*['"`]([^'"`]+)['"`]""", "method"),
            (r"""api\w*\.\w+\s*\(\s*['"`]([^'"`]+)['"`]""", None),
            (r"""`[^`]*(?:/api/|/v1/)([^`]+)`""", None),
        ]

        for pattern, method_group in url_patterns:
            for match in re.finditer(pattern, content, re.IGNORECASE):
                if method_group == "method":
                    method = match.group(1).upper()
                    url = match.group(2)
                else:
                    method = None
                    url = match.group(1)

                if not url or url.startswith("http") and "api" not in url.lower():
                    continue
                if any(skip in url for skip in [".js", ".css", ".png", ".svg", "localhost"]):
                    continue

                line = base_line + content[:match.start()].count("\n")
                context_start = max(0, match.start() - 50)
                context_end = min(len(content), match.end() + 50)
                context = content[context_start:context_end].strip()

                consumers.append(ApiConsumer(
                    url_pattern=url, method=method, file=file_path, line=line,
                    context=context[:100],
                    repo_id=store.repo_id, repo_name=store.repo_name,
                ))

    return consumers


def find_shared_types(
    stores: dict[str, RepoStore],
) -> list[dict]:
    """Find type/model names that appear in multiple projects.

    These are the implicit contracts -- when backend has StudySetResponse
    and frontend has StudySetResponse, they're coupled.
    """
    type_names: dict[str, set[str]] = {}

    for repo_name, store in stores.items():
        names = set()
        nodes = store.get_all_nodes("class")
        for node in nodes:
            name = node["name"]
            if name.startswith("Test") or name.startswith("_"):
                continue
            if name in ("BaseModel", "Base", "Meta", "Config"):
                continue
            names.add(name)
        type_names[repo_name] = names

    shared: list[dict] = []
    all_names: set[str] = set()
    for names in type_names.values():
        all_names |= names

    for name in sorted(all_names):
        repos_with = [repo for repo, names in type_names.items() if name in names]
        if len(repos_with) >= 2:
            shared.append({
                "type_name": name,
                "found_in": repos_with,
            })

    return shared


def match_contracts(
    endpoints: list[ApiEndpoint],
    consumers: list[ApiConsumer],
) -> list[ContractMatch]:
    """Match backend endpoints to frontend consumers by URL pattern."""
    matches: list[ContractMatch] = []

    for endpoint in endpoints:
        endpoint_key = _normalize_path(endpoint.path)

        matched_consumers: list[ApiConsumer] = []
        for consumer in consumers:
            consumer_key = _normalize_path(consumer.url_pattern)
            if _paths_match(endpoint_key, consumer_key):
                if consumer.method is None or consumer.method == endpoint.method:
                    matched_consumers.append(consumer)

        if matched_consumers:
            matches.append(ContractMatch(endpoint=endpoint, consumers=matched_consumers))

    return matches


def analyze_cross_project_impact(
    changed_file: str,
    changed_repo_id: str,
    config: EngramConfig,
    registry: Registry,
) -> CrossProjectImpact:
    """Analyze cross-project impact of a backend change.

    1. Find which API endpoints are in the changed file
    2. Find all consumers of those endpoints in other projects
    3. Find shared type names that might break
    """
    repos = registry.list_repos()
    stores: dict[str, RepoStore] = {}

    result = CrossProjectImpact(
        changed_file=changed_file,
        changed_repo=changed_repo_id,
    )

    for repo in repos:
        db_path = config.repo_db_path(repo.id)
        if db_path.exists():
            stores[repo.name] = RepoStore(db_path, repo.id, repo.name)

    try:
        changed_store = None
        changed_name = ""
        for repo in repos:
            if repo.id == changed_repo_id:
                changed_store = stores.get(repo.name)
                changed_name = repo.name
                break

        if not changed_store:
            return result

        all_endpoints = extract_api_endpoints(changed_store)
        affected_endpoints = [
            endpoint for endpoint in all_endpoints
            if changed_file.endswith(endpoint.file.split("/")[-1]) or endpoint.file == changed_file
        ]
        if not affected_endpoints:
            affected_endpoints = all_endpoints
        result.affected_endpoints = affected_endpoints

        all_consumers: list[ApiConsumer] = []
        for repo_name, store in stores.items():
            if repo_name == changed_name:
                continue
            consumers = extract_api_consumers(store)
            all_consumers.extend(consumers)

        result.affected_contracts = match_contracts(affected_endpoints, all_consumers)

        result.shared_types = find_shared_types(stores)

    finally:
        for store in stores.values():
            store.close()

    return result


def render_cross_project_impact(result: CrossProjectImpact) -> dict:
    """Render cross-project impact for MCP response."""
    output: dict = {
        "changed_file": result.changed_file,
        "changed_repo": result.changed_repo,
        "endpoints_affected": len(result.affected_endpoints),
        "cross_project_contracts": [],
        "shared_types": result.shared_types[:10],
    }

    for contract in result.affected_contracts:
        endpoint = contract.endpoint
        output["cross_project_contracts"].append({
            "endpoint": f"{endpoint.method} {endpoint.path}",
            "backend_file": endpoint.file.split("/app/")[-1] if "/app/" in endpoint.file else endpoint.file.split("/")[-1],
            "backend_handler": endpoint.handler,
            "backend_line": endpoint.line,
            "request_schema": endpoint.request_schema,
            "response_schema": endpoint.response_schema,
            "consumers": [
                {
                    "repo": consumer.repo_name,
                    "file": Path(consumer.file).name,
                    "line": consumer.line,
                    "url": consumer.url_pattern,
                    "context": consumer.context[:80],
                }
                for consumer in contract.consumers[:10]
            ],
        })

    return output


def _normalize_path(path: str) -> list[str]:
    """Normalize a URL path to key segments for matching.

    /api/v1/study-sets/{id}/flashcards → ['study-sets', 'flashcards']
    /study_sets → ['study-sets']
    """
    path = re.sub(r"https?://[^/]+", "", path)
    path = path.split("?")[0]

    segments = []
    for seg in path.split("/"):
        seg = seg.strip()
        if not seg:
            continue
        if seg in ("api", "v1", "v2", "v3"):
            continue
        if seg.startswith("{") or seg.startswith("$"):
            continue
        if seg.startswith(":"):
            continue
        seg = seg.replace("_", "-").lower()
        segments.append(seg)

    return segments


def _paths_match(segments_a: list[str], segments_b: list[str]) -> bool:
    """Check if two normalized path segment lists match."""
    if not segments_a or not segments_b:
        return False
    set_a = set(segments_a)
    set_b = set(segments_b)
    return bool(set_a & set_b)


def _find_schema(text: str, keyword_pattern: str) -> str | None:
    """Find a schema/type name near a keyword in code text."""
    for match in re.finditer(rf"({keyword_pattern})\s*[:=]\s*(\w+)", text, re.IGNORECASE):
        name = match.group(2)
        if name[0].isupper() and len(name) > 3:
            return name
    for match in re.finditer(r":\s*([A-Z]\w+(?:Response|Request|Create|Update|Schema))", text):
        return match.group(1)
    return None
