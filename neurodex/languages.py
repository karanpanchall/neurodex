"""Per-language configuration for AST extraction.

Each language defines which tree-sitter node types represent:
- classes (and class-like constructs)
- functions (and method definitions)
- imports
- calls (function invocations)
- tests (test function patterns)

Adding a new language = adding one entry to LANGUAGE_CONFIG.
Inspired by code-review-graph's multi-language approach but implemented independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LanguageConfig:
    """Tree-sitter node type configuration for a single language."""

    treesitter_module: str | None
    class_types: frozenset[str] = frozenset()
    function_types: frozenset[str] = frozenset()
    import_types: frozenset[str] = frozenset()
    call_types: frozenset[str] = frozenset()
    test_file_patterns: tuple[str, ...] = ()
    test_func_patterns: tuple[str, ...] = ()
    name_node_types: frozenset[str] = frozenset({"identifier", "name", "property_identifier", "type_identifier"})


EXTENSION_MAP: dict[str, str] = {
    ".py": "python", ".pyw": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hxx": "cpp",
    ".cs": "csharp",
    ".rb": "ruby", ".rake": "ruby", ".gemspec": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala", ".sc": "scala",
    ".dart": "dart",
    ".lua": "lua",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".ex": "elixir", ".exs": "elixir",
    ".hs": "haskell",
    ".sql": "sql",
    ".md": "markdown", ".mdx": "markdown", ".rst": "rst", ".txt": "text",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".json": "json",
    ".xml": "xml", ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "scss", ".less": "less",
    ".vue": "vue", ".svelte": "svelte",
    ".ipynb": "jupyter",
}


LANGUAGE_CONFIG: dict[str, LanguageConfig] = {
    "python": LanguageConfig(
        treesitter_module="tree_sitter_python",
        class_types=frozenset({"class_definition", "decorated_definition"}),
        function_types=frozenset({"function_definition", "async_function_definition"}),
        import_types=frozenset({"import_statement", "import_from_statement"}),
        call_types=frozenset({"call", "attribute"}),
        test_file_patterns=("test_", "_test.py", "conftest.py"),
        test_func_patterns=("test_", "setUp", "tearDown"),
    ),
    "javascript": LanguageConfig(
        treesitter_module="tree_sitter_javascript",
        class_types=frozenset({"class_declaration", "class"}),
        function_types=frozenset({
            "function_declaration", "function_expression", "arrow_function",
            "method_definition", "generator_function_declaration",
        }),
        import_types=frozenset({"import_statement", "call_expression"}),
        call_types=frozenset({"call_expression", "new_expression"}),
        test_file_patterns=(".test.", ".spec.", "__tests__/"),
        test_func_patterns=("test", "it(", "describe(", "beforeEach", "afterEach"),
    ),
    "typescript": LanguageConfig(
        treesitter_module="tree_sitter_typescript",
        class_types=frozenset({
            "class_declaration", "abstract_class_declaration",
            "interface_declaration", "type_alias_declaration", "enum_declaration",
        }),
        function_types=frozenset({
            "function_declaration", "function_expression", "arrow_function",
            "method_definition", "method_signature",
        }),
        import_types=frozenset({"import_statement"}),
        call_types=frozenset({"call_expression", "new_expression"}),
        test_file_patterns=(".test.", ".spec.", "__tests__/"),
        test_func_patterns=("test", "it(", "describe(", "beforeEach"),
    ),
    "go": LanguageConfig(
        treesitter_module="tree_sitter_go",
        class_types=frozenset({"type_declaration", "type_spec"}),
        function_types=frozenset({"function_declaration", "method_declaration"}),
        import_types=frozenset({"import_declaration", "import_spec"}),
        call_types=frozenset({"call_expression"}),
        test_file_patterns=("_test.go",),
        test_func_patterns=("Test", "Benchmark"),
    ),
    "rust": LanguageConfig(
        treesitter_module="tree_sitter_rust",
        class_types=frozenset({
            "struct_item", "enum_item", "trait_item", "impl_item",
            "type_item", "union_item",
        }),
        function_types=frozenset({"function_item", "function_signature_item"}),
        import_types=frozenset({"use_declaration"}),
        call_types=frozenset({"call_expression", "macro_invocation"}),
        test_file_patterns=("tests/", "_test.rs"),
        test_func_patterns=("test_",),
    ),
    "java": LanguageConfig(
        treesitter_module="tree_sitter_java",
        class_types=frozenset({
            "class_declaration", "interface_declaration", "enum_declaration",
            "annotation_type_declaration", "record_declaration",
        }),
        function_types=frozenset({"method_declaration", "constructor_declaration"}),
        import_types=frozenset({"import_declaration"}),
        call_types=frozenset({"method_invocation", "object_creation_expression"}),
        test_file_patterns=("Test.java", "Tests.java", "test/"),
        test_func_patterns=("test", "@Test", "@Before", "@After"),
    ),
    "c": LanguageConfig(
        treesitter_module="tree_sitter_c",
        class_types=frozenset({"struct_specifier", "enum_specifier", "union_specifier", "type_definition"}),
        function_types=frozenset({"function_definition", "declaration"}),
        import_types=frozenset({"preproc_include"}),
        call_types=frozenset({"call_expression"}),
    ),
    "cpp": LanguageConfig(
        treesitter_module="tree_sitter_cpp",
        class_types=frozenset({
            "struct_specifier", "class_specifier", "enum_specifier",
            "type_definition", "namespace_definition", "template_declaration",
        }),
        function_types=frozenset({"function_definition", "declaration"}),
        import_types=frozenset({"preproc_include", "using_declaration"}),
        call_types=frozenset({"call_expression"}),
    ),
    "csharp": LanguageConfig(
        treesitter_module="tree_sitter_c_sharp",
        class_types=frozenset({
            "class_declaration", "interface_declaration", "struct_declaration",
            "enum_declaration", "record_declaration",
        }),
        function_types=frozenset({"method_declaration", "constructor_declaration", "local_function_statement"}),
        import_types=frozenset({"using_directive"}),
        call_types=frozenset({"invocation_expression", "object_creation_expression"}),
        test_file_patterns=("Test.cs", "Tests.cs"),
        test_func_patterns=("[Test]", "[Fact]", "[Theory]"),
    ),
    "ruby": LanguageConfig(
        treesitter_module="tree_sitter_ruby",
        class_types=frozenset({"class", "module", "singleton_class"}),
        function_types=frozenset({"method", "singleton_method"}),
        import_types=frozenset({"call"}),
        call_types=frozenset({"call", "command_call"}),
        test_file_patterns=("_test.rb", "_spec.rb", "test/", "spec/"),
        test_func_patterns=("test_", "it ", "describe ", "context "),
    ),
    "php": LanguageConfig(
        treesitter_module="tree_sitter_php",
        class_types=frozenset({"class_declaration", "interface_declaration", "trait_declaration", "enum_declaration"}),
        function_types=frozenset({"function_definition", "method_declaration"}),
        import_types=frozenset({"namespace_use_declaration"}),
        call_types=frozenset({"function_call_expression", "member_call_expression", "scoped_call_expression"}),
        test_file_patterns=("Test.php", "test/", "tests/"),
        test_func_patterns=("test", "@test"),
    ),
    "swift": LanguageConfig(
        treesitter_module="tree_sitter_swift",
        class_types=frozenset({
            "class_declaration", "struct_declaration", "enum_declaration",
            "protocol_declaration", "extension_declaration",
        }),
        function_types=frozenset({"function_declaration", "init_declaration", "subscript_declaration"}),
        import_types=frozenset({"import_declaration"}),
        call_types=frozenset({"call_expression"}),
        test_file_patterns=("Tests.swift", "Test.swift", "Tests/"),
        test_func_patterns=("test", "func test"),
    ),
    "kotlin": LanguageConfig(
        treesitter_module="tree_sitter_kotlin",
        class_types=frozenset({
            "class_declaration", "object_declaration", "interface_declaration",
            "enum_class_body",
        }),
        function_types=frozenset({"function_declaration"}),
        import_types=frozenset({"import_header"}),
        call_types=frozenset({"call_expression"}),
        test_file_patterns=("Test.kt", "Tests.kt", "test/"),
        test_func_patterns=("test", "@Test"),
    ),
    "dart": LanguageConfig(
        treesitter_module="tree_sitter_dart",
        class_types=frozenset({"class_definition", "enum_declaration", "mixin_declaration", "extension_declaration"}),
        function_types=frozenset({"function_signature", "method_signature", "function_body"}),
        import_types=frozenset({"import_or_export"}),
        call_types=frozenset({"function_expression_body"}),
        test_file_patterns=("_test.dart", "test/"),
        test_func_patterns=("test(", "testWidgets(", "group("),
    ),
    "scala": LanguageConfig(
        treesitter_module="tree_sitter_scala",
        class_types=frozenset({"class_definition", "object_definition", "trait_definition"}),
        function_types=frozenset({"function_definition", "function_declaration"}),
        import_types=frozenset({"import_declaration"}),
        call_types=frozenset({"call_expression"}),
        test_file_patterns=("Spec.scala", "Test.scala", "Suite.scala"),
        test_func_patterns=("test(", "\"should", "it("),
    ),
    "elixir": LanguageConfig(
        treesitter_module="tree_sitter_elixir",
        class_types=frozenset({"call"}),
        function_types=frozenset({"call"}),
        import_types=frozenset({"call"}),
        call_types=frozenset({"call"}),
        test_file_patterns=("_test.exs", "test/"),
        test_func_patterns=("test ", "describe "),
    ),
    "lua": LanguageConfig(
        treesitter_module="tree_sitter_lua",
        class_types=frozenset(),
        function_types=frozenset({"function_declaration", "function_definition", "local_function"}),
        import_types=frozenset({"function_call"}),
        call_types=frozenset({"function_call", "method_index_expression"}),
    ),
    "haskell": LanguageConfig(
        treesitter_module="tree_sitter_haskell",
        class_types=frozenset({"type_class", "data_type", "newtype", "type_synonym"}),
        function_types=frozenset({"function", "signature"}),
        import_types=frozenset({"import"}),
        call_types=frozenset({"function_application"}),
    ),
    "sql": LanguageConfig(treesitter_module=None),
    "bash": LanguageConfig(treesitter_module=None),
    "markdown": LanguageConfig(treesitter_module=None),
    "rst": LanguageConfig(treesitter_module=None),
    "text": LanguageConfig(treesitter_module=None),
    "yaml": LanguageConfig(treesitter_module=None),
    "toml": LanguageConfig(treesitter_module=None),
    "json": LanguageConfig(treesitter_module=None),
    "xml": LanguageConfig(treesitter_module=None),
    "html": LanguageConfig(treesitter_module=None),
    "css": LanguageConfig(treesitter_module=None),
    "scss": LanguageConfig(treesitter_module=None),
    "less": LanguageConfig(treesitter_module=None),
    "vue": LanguageConfig(treesitter_module=None),
    "svelte": LanguageConfig(treesitter_module=None),
    "jupyter": LanguageConfig(treesitter_module=None),
}


def get_language(file_path: str) -> str | None:
    """Detect language from file extension."""
    extension = Path(file_path).suffix.lower()
    return EXTENSION_MAP.get(extension)


def get_config(language: str) -> LanguageConfig | None:
    """Get language config, or None if unknown."""
    return LANGUAGE_CONFIG.get(language)


def has_ast_support(language: str) -> bool:
    """Check if we have tree-sitter AST parsing for this language."""
    config = LANGUAGE_CONFIG.get(language)
    return config is not None and config.treesitter_module is not None


def get_definition_types(language: str) -> frozenset[str]:
    """Get all node types that represent definitions (classes + functions)."""
    config = LANGUAGE_CONFIG.get(language)
    if not config:
        return frozenset()
    return config.class_types | config.function_types


def is_test_file(file_path: str, language: str) -> bool:
    """Check if a file is a test file based on language-specific patterns."""
    config = LANGUAGE_CONFIG.get(language)
    if not config or not config.test_file_patterns:
        return False
    filename = Path(file_path).name
    path_lower = file_path.lower()
    return any(pattern in filename or pattern in path_lower for pattern in config.test_file_patterns)


def is_test_function(func_name: str, language: str) -> bool:
    """Check if a function name looks like a test."""
    config = LANGUAGE_CONFIG.get(language)
    if not config or not config.test_func_patterns:
        return False
    return any(func_name.startswith(pattern) or pattern in func_name for pattern in config.test_func_patterns)
