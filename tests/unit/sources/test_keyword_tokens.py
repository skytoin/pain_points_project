from __future__ import annotations

from discovery.sources.keyword_tokens import MAX_TOKENS, decompose_keyword


class TestDecomposeKeyword:
    def test_two_tokens_pass_through(self) -> None:
        assert decompose_keyword("Personal CRM") == ["Personal", "CRM"]

    def test_drops_stopwords_case_insensitively(self) -> None:
        # `for` is a stopword regardless of the surrounding casing.
        assert decompose_keyword("X for Y") == ["X", "Y"]
        # Capitalised stopword also drops.
        assert decompose_keyword("The CRM workflow") == ["CRM", "workflow"]

    def test_preserves_casing_of_survivors(self) -> None:
        # HN acronyms must survive intact -- MCP, CLI, RAG, LLM matter.
        assert decompose_keyword("MCP server") == ["MCP", "server"]
        assert decompose_keyword("CLI scheduling") == ["CLI", "scheduling"]
        assert decompose_keyword("billing CRDT") == ["billing", "CRDT"]

    def test_keeps_first_two_surviving_tokens_only(self) -> None:
        # Distinctive token in position 3 is silently dropped -- the
        # very failure mode the §8 prompt warns against.
        assert decompose_keyword("vector database Rust") == ["vector", "database"]
        assert decompose_keyword("privacy preserving data collection library") == [
            "privacy",
            "preserving",
        ]

    def test_stopwords_do_not_count_against_cap(self) -> None:
        # "in" is a stopword; "Rust" survives because "in" was filtered first.
        assert decompose_keyword("MCP in Rust") == ["MCP", "Rust"]

    def test_empty_input(self) -> None:
        assert decompose_keyword("") == []

    def test_all_stopwords(self) -> None:
        assert decompose_keyword("the a an for") == []

    def test_whitespace_only_input(self) -> None:
        assert decompose_keyword("   \t  ") == []

    def test_extra_whitespace_collapses(self) -> None:
        # str.split() with no argument collapses any run of whitespace.
        assert decompose_keyword("  CLI   scheduling  ") == ["CLI", "scheduling"]

    def test_max_tokens_constant_is_two(self) -> None:
        assert MAX_TOKENS == 2
