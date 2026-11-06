"""Canonical browser newline handling, without changing prompt meaning.

HTML editors serialize Windows CRLF and standalone CR as LF. Compare that
representation only: interior spaces, blank lines, Unicode and words remain
significant. Stored/API prompts and their request fingerprints stay original.
"""


def canonical_prompt_text(value: str, *, strip_edges: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError('Prompt text must be a string.')
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    return value.strip() if strip_edges else value


def prompt_text_equal(actual, expected, *, strip_edges: bool = True) -> bool:
    """Match editor newline conventions; never collapse interior whitespace."""
    if not isinstance(actual, str) or not isinstance(expected, str):
        return False
    return canonical_prompt_text(actual, strip_edges=strip_edges) == canonical_prompt_text(expected, strip_edges=strip_edges)
