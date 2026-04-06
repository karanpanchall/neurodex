"""Code vocabulary expansion for BM25 search.

Bridges the gap between natural language queries and code identifiers.
Example: searching "login" also matches "authenticate", "signin", "session".
"""

from __future__ import annotations

SYNONYMS: dict[str, list[str]] = {
    "auth": ["authentication", "authorize", "authorization", "login", "signin", "sign_in",
             "logout", "signout", "sign_out", "session", "jwt", "oauth", "token", "credential",
             "password", "principal", "identity"],
    "login": ["auth", "authenticate", "signin", "sign_in", "logon", "log_on"],
    "logout": ["signout", "sign_out", "logoff", "log_off", "revoke"],
    "permission": ["rbac", "role", "access", "authorize", "acl", "policy", "scope"],

    "db": ["database", "query", "sql", "orm", "model", "migration", "schema", "table",
           "postgres", "mysql", "sqlite", "mongo", "redis"],
    "query": ["select", "insert", "update", "delete", "find", "filter", "where", "join"],
    "migration": ["migrate", "alembic", "schema", "alter", "evolve"],
    "cache": ["redis", "memcache", "memoize", "invalidate", "ttl", "expire"],

    "api": ["endpoint", "route", "handler", "controller", "request", "response", "rest",
            "graphql", "grpc"],
    "endpoint": ["route", "path", "url", "handler", "view", "controller"],
    "request": ["req", "http", "fetch", "call", "invoke"],
    "response": ["res", "reply", "result", "output"],
    "middleware": ["interceptor", "hook", "filter", "pipe", "guard"],

    "test": ["spec", "unittest", "pytest", "jest", "mocha", "assert", "expect",
             "mock", "stub", "fixture", "snapshot"],
    "mock": ["stub", "fake", "spy", "patch", "monkeypatch"],

    "config": ["settings", "env", "environment", "dotenv", "configuration", "options",
               "preferences", "params", "parameters"],
    "env": ["environment", "dotenv", "config", "settings", "variable"],

    "error": ["exception", "raise", "throw", "catch", "try", "except", "finally",
              "fault", "failure", "crash", "panic"],
    "log": ["logger", "logging", "print", "debug", "info", "warn", "error", "trace",
            "console", "stdout"],

    "async": ["await", "promise", "future", "coroutine", "concurrent", "parallel",
              "thread", "worker", "task", "background"],
    "queue": ["job", "worker", "celery", "rabbitmq", "kafka", "pub", "sub", "message",
              "event", "dispatch"],

    "list": ["array", "slice", "vector", "collection", "items"],
    "map": ["dict", "dictionary", "hash", "object", "record", "hashmap"],
    "parse": ["deserialize", "decode", "unmarshal", "read", "load", "from_json", "from_str"],
    "serialize": ["encode", "marshal", "dump", "to_json", "to_str", "format"],

    "file": ["path", "directory", "folder", "read", "write", "stream", "io", "fs"],
    "upload": ["multipart", "form_data", "attachment", "blob", "file"],
    "download": ["fetch", "stream", "export", "save"],

    "component": ["widget", "element", "view", "template", "render"],
    "style": ["css", "theme", "design", "layout", "tailwind", "styled"],
    "state": ["store", "redux", "context", "reactive", "signal", "ref"],
    "event": ["handler", "listener", "callback", "on_click", "on_change", "emit"],

    "deploy": ["release", "publish", "ship", "ci", "cd", "pipeline", "build"],
    "container": ["docker", "kubernetes", "k8s", "pod", "image"],
}


def expand_query(query: str) -> str:
    """Expand a query with synonyms for broader BM25 matching.

    Short queries (1-3 words) get expanded. Longer queries are
    assumed to be specific enough already.
    """
    words = query.lower().split()

    if len(words) > 5:
        return query

    expanded_terms: list[str] = list(words)

    for word in words:
        if word in SYNONYMS:
            expanded_terms.extend(SYNONYMS[word][:3])
        else:
            for key, synonym_list in SYNONYMS.items():
                if word in synonym_list:
                    expanded_terms.append(key)
                    break

    seen: set[str] = set()
    unique: list[str] = []
    for term in expanded_terms:
        if term not in seen:
            seen.add(term)
            unique.append(term)

    return " ".join(unique)


def build_project_vocabulary(symbols: list[str]) -> dict[str, list[str]]:
    """Build project-specific vocabulary from indexed symbol names.

    Splits camelCase and snake_case symbols into searchable terms.
    Example: getUserProfile -> ["get", "user", "profile"]
    """
    vocab: dict[str, list[str]] = {}

    for symbol in symbols:
        parts = _split_identifier(symbol)
        if len(parts) > 1:
            vocab[symbol.lower()] = [part.lower() for part in parts]
            for part in parts:
                key = part.lower()
                if key not in vocab:
                    vocab[key] = []
                if symbol.lower() not in vocab[key]:
                    vocab[key].append(symbol.lower())

    return vocab


def _split_identifier(name: str) -> list[str]:
    """Split a camelCase or snake_case identifier into words.

    Examples:
        getUserProfile -> ["get", "User", "Profile"]
        get_user_profile -> ["get", "user", "profile"]
        HTTPClient -> ["HTTP", "Client"]
    """
    if "_" in name:
        return [segment for segment in name.split("_") if segment]

    parts: list[str] = []
    current: list[str] = []

    for i, char in enumerate(name):
        if char.isupper():
            if current and (not current[-1].isupper()):
                parts.append("".join(current))
                current = [char]
            elif current and current[-1].isupper() and i + 1 < len(name) and name[i + 1].islower():
                parts.append("".join(current))
                current = [char]
            else:
                current.append(char)
        else:
            current.append(char)

    if current:
        parts.append("".join(current))

    return parts if len(parts) > 1 else [name]
