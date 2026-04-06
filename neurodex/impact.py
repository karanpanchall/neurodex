"""Blast-radius impact analysis.

Given a file (or set of changed files), determines what other files,
functions, and tests are affected. Uses bidirectional BFS through
typed edges with risk scoring.

Inspired by code-review-graph's approach but implemented independently
using NEURODEX's edge system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from neurodex.store import RepoStore


SECURITY_KEYWORDS = frozenset({
    "auth", "login", "token", "jwt", "oauth", "session", "password",
    "secret", "key", "encrypt", "decrypt", "hash", "credential",
    "permission", "rbac", "role", "scope", "acl", "security",
    "sanitize", "validate", "csrf", "xss", "injection", "sql",
})


@dataclass
class ImpactResult:
    """Result of blast-radius analysis."""
    source_file: str
    source_lines: tuple[int, int] | None = None
    seed_symbols: list[str] = field(default_factory=list)
    affected: list[AffectedSymbol] = field(default_factory=list)
    affected_files: int = 0
    untested_count: int = 0
    risk_score: float = 0.0
    edge_stats: dict[str, int] = field(default_factory=dict)


@dataclass
class AffectedSymbol:
    """A symbol affected by a change -- function/class level precision."""
    file_path: str
    symbol: str
    qualified_name: str
    kind: str
    line_start: int
    line_end: int
    signature: str
    distance: int
    via: str
    direction: str
    has_tests: bool = False
    risk: float = 0.0


def analyze_impact(
    store: RepoStore,
    file_path: str,
    max_depth: int = 3,
    max_nodes: int = 50,
    changed_lines: tuple[int, int] | None = None,
) -> ImpactResult:
    """Analyze blast radius at symbol level.

    If changed_lines is given, starts from exact symbols at those lines.
    Returns affected symbols with precise file:line locations and risk.
    """
    raw = store.impact_bfs(
        file_path, max_depth=max_depth, max_nodes=max_nodes,
        changed_lines=changed_lines,
    )

    seed_symbols = []
    if changed_lines:
        seeds = store.get_nodes_at_lines(file_path, changed_lines[0], changed_lines[1])
        seed_symbols = [f"{seed['name']} (L{seed['line_start']}-{seed['line_end']})" for seed in seeds]
    else:
        seeds = store.get_nodes_in_file(file_path)
        seed_symbols = [seed["name"] for seed in seeds[:5]]

    affected: list[AffectedSymbol] = []
    untested = 0
    seen_files: set[str] = set()

    for item in raw:
        file_path_affected = item.get("file", item.get("file_path", ""))
        test_edges = store.get_test_edges(file_path_affected)
        has_tests = len(test_edges) > 0
        if not has_tests:
            untested += 1
        seen_files.add(file_path_affected)

        distance = item.get("distance", 1)
        risk = _compute_risk(store, file_path_affected, distance, has_tests)

        affected.append(AffectedSymbol(
            file_path=file_path_affected,
            symbol=item.get("symbol", item.get("name", Path(file_path_affected).stem)),
            qualified_name=item.get("qualified_name", file_path_affected),
            kind=item.get("kind", "file"),
            line_start=item.get("line_start", 0),
            line_end=item.get("line_end", 0),
            signature=item.get("signature", ""),
            distance=distance,
            via=item.get("via", "IMPORTS"),
            direction=item.get("direction", "forward"),
            has_tests=has_tests,
            risk=risk,
        ))

    affected.sort(key=lambda entry: (-entry.risk, entry.distance))

    overall_risk = _compute_overall_risk(store, file_path, affected, untested)

    return ImpactResult(
        source_file=file_path,
        source_lines=changed_lines,
        seed_symbols=seed_symbols,
        affected=affected,
        affected_files=len(seen_files),
        untested_count=untested,
        risk_score=overall_risk,
        edge_stats=store.get_edge_stats(),
    )


def _compute_risk(
    store: RepoStore,
    file_path: str,
    distance: int,
    has_tests: bool,
) -> float:
    """Compute risk score for a single affected file.

    Factors:
    - Distance: closer = higher risk
    - Test coverage: untested = +0.25
    - Security sensitivity: security-related code = +0.20
    - Caller diversity: many callers from different files = +0.15
    """
    risk = 0.0

    risk += max(0, 0.3 - (distance - 1) * 0.1)

    if not has_tests:
        risk += 0.25

    fp_lower = file_path.lower()
    if any(kw in fp_lower for kw in SECURITY_KEYWORDS):
        risk += 0.20

    file_stem = Path(file_path).stem
    callers = store.get_callers(file_stem)
    unique_caller_files = len(set(caller["source_file"] for caller in callers))
    if unique_caller_files > 3:
        risk += 0.15
    elif unique_caller_files > 1:
        risk += 0.05

    return min(risk, 1.0)


def _compute_overall_risk(
    store: RepoStore,
    source_file: str,
    affected: list[AffectedSymbol],
    untested: int,
) -> float:
    """Compute overall risk score for the change.

    Factors:
    - File spread: how many files affected (30%)
    - Security: is the source file security-related (25%)
    - Test gaps: fraction of affected files without tests (25%)
    - Max individual risk: worst-case affected file (20%)
    """
    risk = 0.0

    num_affected = len(affected)
    if num_affected >= 10:
        risk += 0.30
    elif num_affected >= 5:
        risk += 0.20
    elif num_affected >= 2:
        risk += 0.10

    if any(kw in source_file.lower() for kw in SECURITY_KEYWORDS):
        risk += 0.25

    if affected:
        untested_ratio = untested / len(affected)
        risk += untested_ratio * 0.25

    if affected:
        risk += max(entry.risk for entry in affected) * 0.20

    return min(risk, 1.0)


def render_impact(result: ImpactResult) -> dict:
    """Render symbol-level impact analysis for MCP response."""
    output: dict = {
        "source_file": result.source_file,
        "risk_score": round(result.risk_score, 2),
        "risk_level": (
            "critical" if result.risk_score > 0.7
            else "high" if result.risk_score > 0.5
            else "medium" if result.risk_score > 0.3
            else "low"
        ),
        "affected_symbols": len(result.affected),
        "affected_files": result.affected_files,
        "untested": result.untested_count,
    }

    if result.source_lines:
        output["changed_lines"] = f"{result.source_lines[0]}-{result.source_lines[1]}"
    if result.seed_symbols:
        output["seed_symbols"] = result.seed_symbols

    output["blast_radius"] = [
        {
            "file": entry.file_path.rsplit("/app/", 1)[-1] if "/app/" in entry.file_path else Path(entry.file_path).name,
            "symbol": entry.symbol,
            "kind": entry.kind,
            "lines": f"{entry.line_start}-{entry.line_end}" if entry.line_start else None,
            "signature": entry.signature[:80] if entry.signature else None,
            "distance": entry.distance,
            "via": entry.via,
            "direction": entry.direction,
            "has_tests": entry.has_tests,
            "risk": round(entry.risk, 2),
        }
        for entry in result.affected[:25]
    ]

    return output
