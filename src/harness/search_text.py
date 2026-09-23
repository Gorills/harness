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
        "before",
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
_LOCATION_QUERY_MARKERS = frozenset(
    {"how", "test", "testing", "tests", "where", "who", "где", "как"}
)
_LOCATION_INTENT_TERMS = frozenset(
    {
        "enforced",
        "implemented",
        "verifies",
        "реализован",
        "реализована",
        "реализовано",
        "реализованы",
        "находится",
        "находятся",
    }
)
_DOC_EXTENSIONS = {".md", ".mdx", ".rst", ".txt", ".adoc"}
_QUERY_TERM_LIMIT = 24
_ENGLISH_QUERY_SUFFIXES = (
    "ingly",
    "edly",
    "ness",
    "ment",
    "ation",
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
    "ией",
    "ием",
    "иях",
    "иям",
    "ями",
    "ами",
    "ности",
    "ость",
    "ения",
    "ение",
    "ания",
    "ание",
    "ого",
    "его",
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
    "ей",
    "ах",
    "ях",
    "ам",
    "ям",
    "ов",
    "ев",
    "ом",
    "ем",
    "ию",
    "ия",
    "ии",
    "ью",
    "а",
    "я",
    "ы",
    "и",
    "у",
    "ю",
    "е",
    "о",
    "ь",
)
# Phrase ranking uses case endings only. The broader retrieval suffixes above also contain
# derivations such as -ение/-ить; those must not equate different words at the phrase tier.
_RUSSIAN_CASE_SUFFIXES = (
    "иями",
    "ией",
    "ием",
    "иях",
    "иям",
    "ями",
    "ами",
    "ого",
    "его",
    "ему",
    "ому",
    "ыми",
    "ими",
    "ий",
    "ый",
    "ой",
    "ая",
    "яя",
    "ое",
    "ее",
    "ые",
    "ие",
    "ей",
    "ах",
    "ях",
    "ам",
    "ям",
    "ов",
    "ев",
    "ом",
    "ем",
    "ию",
    "ия",
    "ии",
    "ью",
    "а",
    "я",
    "ы",
    "и",
    "у",
    "ю",
    "е",
    "о",
    "ь",
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
    ignored = _SEARCH_STOP_WORDS
    if any(term in _LOCATION_QUERY_MARKERS for term in all_terms):
        ignored = ignored | _LOCATION_INTENT_TERMS
    meaningful = tuple(term for term in all_terms if term not in ignored)
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


def matching_term_count(terms: tuple[str, ...], *values: str) -> int:
    """Count query terms represented by exact or FTS-equivalent prefix tokens."""
    return len(matching_terms(terms, *values))


def matching_terms(terms: tuple[str, ...], *values: str) -> tuple[str, ...]:
    """Match all query terms against one tokenization of the supplied text."""
    candidates = frozenset(token for value in values for token in identifier_tokens(value))
    matched: list[str] = []
    for term in terms:
        prefixes = _query_prefixes(term)
        if any(
            candidate == prefix or (len(prefix) >= 3 and candidate.startswith(prefix))
            for candidate in candidates
            for prefix in prefixes
        ):
            matched.append(term)
    return tuple(matched)


def contains_term_phrase(terms: tuple[str, ...], value: str) -> bool:
    """Return whether significant query terms occur consecutively in normalized text."""
    if not terms:
        return False
    tokens = identifier_tokens(value)
    width = len(terms)
    return any(tokens[index : index + width] == terms for index in range(len(tokens) - width + 1))


def contains_russian_case_phrase(terms: tuple[str, ...], value: str) -> bool:
    """Recognize consecutive Russian case forms without promoting arbitrary prefix matches."""
    if not terms or not any(_is_russian_word(term) for term in terms):
        return False
    tokens = identifier_tokens(value)
    alternatives = tuple(_russian_case_forms(term) for term in terms)
    candidate_forms = {token: _russian_case_forms(token) for token in set(tokens)}
    width = len(terms)
    return any(
        all(
            forms.intersection(candidate_forms[token])
            for forms, token in zip(alternatives, tokens[index : index + width], strict=True)
        )
        for index in range(len(tokens) - width + 1)
    )


def _russian_case_forms(term: str) -> frozenset[str]:
    if _is_russian_word(term):
        for suffix in _RUSSIAN_CASE_SUFFIXES:
            if term.endswith(suffix) and len(term) - len(suffix) >= 5:
                return frozenset((term, term[: -len(suffix)]))
    return frozenset((term,))


def _is_russian_word(value: str) -> bool:
    return bool(value) and all("а" <= character <= "я" or character == "ё" for character in value)


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
