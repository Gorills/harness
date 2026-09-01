from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_CAMEL_LOWER_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL_ACRONYM_BOUNDARY = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_WORDS = re.compile(r"\w+", flags=re.UNICODE)
_SEARCH_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "happen",
        "happens",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "where",
        "which",
        "with",
        "а",
        "без",
        "в",
        "во",
        "где",
        "для",
        "и",
        "из",
        "как",
        "который",
        "на",
        "о",
        "об",
        "по",
        "при",
        "происходит",
        "работает",
        "с",
        "со",
        "что",
        "это",
    }
)
_DOC_EXTENSIONS = {".md", ".mdx", ".rst", ".txt", ".adoc"}
_GENERATED_TEXT_OUTPUT_EXTENSIONS = {".log", ".out"}
_QUERY_TERM_LIMIT = 24
_ENGLISH_QUERY_SUFFIXES = (
    "ingly",
    "edly",
    "ness",
    "ment",
    "ions",
    "ion",
    "ing",
    "ies",
    "ed",
    "es",
    "s",
)
_RUSSIAN_QUERY_SUFFIXES = (
    "иями",
    "ями",
    "ами",
    "ности",
    "ость",
    "ения",
    "ение",
    "ания",
    "ание",
    "ого",
    "ему",
    "ому",
    "ыми",
    "ими",
    "ция",
    "ции",
    "ий",
    "ый",
    "ой",
    "ая",
    "яя",
    "ое",
    "ее",
    "ые",
    "ие",
    "ать",
    "ять",
    "ить",
    "а",
    "я",
    "ы",
    "и",
    "у",
    "ю",
    "е",
    "о",
)


@dataclass(frozen=True, slots=True)
class AnalyzedSearchQuery:
    """Deterministic lexical query shared by local retrieval channels."""

    normalized: str
    terms: tuple[str, ...]
    fts_expression: str
    all_fts_expression: str


def analyze_search_query(query: str) -> AnalyzedSearchQuery:
    """Remove conversational filler while retaining a safe FTS5 prefix expression."""
    all_terms = _deduplicate(identifier_tokens(query))[:_QUERY_TERM_LIMIT]
    meaningful = tuple(term for term in all_terms if term not in _SEARCH_STOP_WORDS)
    terms = meaningful or all_terms
    operands = tuple(_fts_term_operand(term) for term in terms)
    return AnalyzedSearchQuery(
        normalized=query.strip(),
        terms=terms,
        fts_expression=" OR ".join(operands),
        all_fts_expression=" AND ".join(operands),
    )


def identifier_tokens(value: str) -> tuple[str, ...]:
    """Split Unicode words plus common ASCII camel/snake identifier boundaries."""
    split_value = _CAMEL_LOWER_BOUNDARY.sub(" ", value)
    split_value = _CAMEL_ACRONYM_BOUNDARY.sub(" ", split_value)
    tokens: list[str] = []
    for word in _WORDS.findall(split_value):
        tokens.extend(part.casefold() for part in word.split("_") if part)
    return tuple(tokens)


def identifier_expansion(value: str, *, maximum_bytes: int) -> str:
    """Return only extra tokens needed to make compound identifiers lexically searchable."""
    expanded: list[str] = []
    used_bytes = 0
    for raw_word in _WORDS.findall(value):
        parts = identifier_tokens(raw_word)
        if len(parts) <= 1:
            continue
        for part in parts:
            encoded_size = len(part.encode("utf-8")) + (1 if expanded else 0)
            if used_bytes + encoded_size > maximum_bytes:
                return " ".join(expanded)
            expanded.append(part)
            used_bytes += encoded_size
    return " ".join(expanded)


def query_term_prefixes(term: str) -> tuple[str, ...]:
    """Return the exact term plus the one optional inflection prefix used by lexical matching."""
    return _query_prefixes(term)


def matching_term_count(terms: tuple[str, ...], *values: str) -> int:
    """Count query terms represented by exact or FTS-equivalent prefix tokens."""
    candidates = frozenset(token for value in values for token in identifier_tokens(value))
    return sum(
        any(
            candidate == prefix or (len(prefix) >= 3 and candidate.startswith(prefix))
            for candidate in candidates
            for prefix in _query_prefixes(term)
        )
        for term in terms
    )


def contains_term_phrase(terms: tuple[str, ...], value: str) -> bool:
    """Return whether significant query terms occur consecutively in normalized text."""
    if not terms:
        return False
    tokens = identifier_tokens(value)
    width = len(terms)
    return any(tokens[index : index + width] == terms for index in range(len(tokens) - width + 1))


def is_document_path(path: str) -> bool:
    """Return whether an indexed path belongs to the repository documentation corpus."""
    lowered = path.casefold()
    name = lowered.rsplit("/", 1)[-1]
    suffix = Path(name).suffix
    return (
        suffix in _DOC_EXTENSIONS
        or lowered.startswith("docs/")
        or "/docs/" in lowered
        or (not suffix and name.startswith(("readme", "adr")))
    )


def is_generated_text_output_path(path: str) -> bool:
    """Return whether a text path is a generated diagnostic/output artifact, not code/docs."""
    name = path.casefold().rsplit("/", 1)[-1]
    return Path(name).suffix in _GENERATED_TEXT_OUTPUT_EXTENSIONS


def _deduplicate(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


def _fts_term_operand(term: str) -> str:
    prefixes = _query_prefixes(term)
    operands = tuple(f'"{prefix}"*' if len(prefix) >= 3 else f'"{prefix}"' for prefix in prefixes)
    if len(operands) == 1:
        return operands[0]
    return f"({' OR '.join(operands)})"


def _query_prefixes(term: str) -> tuple[str, ...]:
    prefixes = [term]
    suffixes = _RUSSIAN_QUERY_SUFFIXES if _contains_cyrillic(term) else _ENGLISH_QUERY_SUFFIXES
    for suffix in suffixes:
        if term.endswith(suffix) and len(term) - len(suffix) >= 5:
            prefixes.append(term[: -len(suffix)])
            break
    return tuple(prefixes)


def _contains_cyrillic(value: str) -> bool:
    return any("а" <= character <= "я" or character == "ё" for character in value)
