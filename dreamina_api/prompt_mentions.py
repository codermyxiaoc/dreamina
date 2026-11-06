"""Parse API reference mentions and verify their structured upstream bindings.

Image and video numbering is independent, starts at one, and follows the
validated reference order. Filenames and caller-supplied titles are not IDs.
"""
from __future__ import annotations

import re
from .prompt_text import canonical_prompt_text


_PREFIX = re.compile(r'@(图片|视频)')
_INDEX = re.compile(r'[1-9][0-9]*')
_KINDS = {'图片': 'image', '视频': 'video'}


def has_prompt_mentions(prompt: str) -> bool:
    return isinstance(prompt, str) and _PREFIX.search(prompt) is not None


def parse_prompt_mentions(prompt: str, references: list[dict]) -> list[dict]:
    """Return literal text and bound mention segments, or reject bad indices.

Text segments have ``type`` and ``text``. Mention segments have ``type``
(``image``/``video``), ``index`` (one-based within that kind),
``material_index`` (zero-based in references), ``asset_id`` and ``token``.
No caller-maintained binding map is accepted. This function has no side effects.
"""
    if not isinstance(prompt, str):
        raise ValueError('prompt must be a string.')
    if not isinstance(references, list):
        raise ValueError('references must be an array.')
    if not has_prompt_mentions(prompt):
        return [{'type': 'text', 'text': prompt}] if prompt else []
    by_kind = {'image': [], 'video': []}
    for position, reference in enumerate(references):
        if not isinstance(reference, dict):
            raise ValueError('Reference metadata is invalid.')
        kind = reference.get('kind', reference.get('type'))
        if kind not in by_kind:
            raise ValueError('Reference metadata type is invalid.')
        by_kind[kind].append((position, reference))
    segments = []
    cursor = 0
    for match in _PREFIX.finditer(prompt):
        index_match = _INDEX.match(prompt, match.end())
        if index_match is None:
            raise ValueError(f'{match.group()} requires a positive integer index, for example {match.group()}1.')
        raw_index = index_match.group()
        tail = prompt[index_match.end():]
        # Fullwidth digits, fractions and decimal indices are not aliases for an
        # integer reference. Bound length also prevents enormous integer parsing.
        if len(raw_index) > 2 or (tail and tail[0].isdecimal()) or re.match(r'[.．][0-9０-９]', tail):
            raise ValueError(f'{match.group()} requires a valid integer reference index.')
        index = int(raw_index)
        kind = _KINDS[match[1]]
        if index > len(by_kind[kind]):
            raise ValueError(f'{match.group()}{index} has no corresponding uploaded {kind} reference.')
        position, reference = by_kind[kind][index - 1]
        if match.start() > cursor:
            segments.append({'type': 'text', 'text': prompt[cursor:match.start()]})
        asset_id = reference.get('asset_id')
        if asset_id is not None and (not isinstance(asset_id, str) or not asset_id):
            raise ValueError('Reference asset identity is invalid.')
        segments.append({'type': kind, 'index': index, 'material_index': position,
                         'asset_id': asset_id, 'token': prompt[match.start():index_match.end()]})
        cursor = index_match.end()
    if cursor < len(prompt):
        segments.append({'type': 'text', 'text': prompt[cursor:]})
    return segments


def _append_text(sequence: list, text: str) -> None:
    if not text:
        return
    if sequence and sequence[-1][0] == 'text':
        sequence[-1] = ('text', sequence[-1][1] + text)
    else:
        sequence.append(('text', text))


def verify_prompt_mentions(prompt: str, references: list[dict], unified: dict, bindings: dict | None = None) -> None:
    """Require the exact ordered text/reference sequence for mention prompts.

    Native material arrays may reorder uploads. A known ``asset_id`` must resolve
    to exactly one same-kind title or trusted upload receipt in ``bindings``
    (asset_id -> {kind, resource_id}); its array index alone is insufficient.
Missing/ambiguous/extra references are rejected rather than guessed.
"""
    segments = parse_prompt_mentions(prompt, references)
    if not any(segment['type'] != 'text' for segment in segments):
        return
    if not isinstance(unified, dict):
        raise ValueError('Structured mention input is missing.')
    materials, metas = unified.get('material_list'), unified.get('meta_list')
    if not isinstance(materials, list) or len(materials) != len(references) or not isinstance(metas, list):
        raise ValueError('Structured mention materials or metadata are invalid.')
    if bindings is not None and not isinstance(bindings, dict):
        raise ValueError('Prompt mention upload bindings are invalid.')
    actual_to_expected = {}
    for expected_index, reference in enumerate(references):
        kind = reference.get('kind', reference.get('type'))
        asset_id = reference.get('asset_id')
        binding = bindings.get(asset_id) if bindings is not None and asset_id is not None else None
        if binding is not None and (not isinstance(binding, dict) or binding.get('kind') != kind
                or not isinstance(binding.get('resource_id'), str) or not binding['resource_id']):
            raise ValueError('Prompt mention upload binding identity is invalid.')
        matches = []
        for actual_index, material in enumerate(materials):
            if not isinstance(material, dict) or material.get('material_type') != kind:
                continue
            info = material.get(kind + '_info')
            if not isinstance(info, dict):
                continue
            if binding is not None:
                resource_id = info.get('vid') if kind == 'video' else info.get('image_uri', info.get('uri'))
                if resource_id != binding['resource_id']:
                    continue
                # A conflicting explicit title is not silently ignored even if
                # a malformed response recycles another upload's resource ID.
                if info.get('title') not in (None, '', asset_id):
                    continue
                if kind == 'image' and info.get('uri') not in (None, '', resource_id):
                    continue
            elif asset_id is not None:
                if info.get('title') != asset_id:
                    continue
            elif actual_index != expected_index:
                continue
            matches.append(actual_index)
        if len(matches) != 1 or matches[0] in actual_to_expected:
            raise ValueError('The upstream material identity for a prompt mention is missing or ambiguous.')
        actual_to_expected[matches[0]] = expected_index
    expected = []
    for segment in segments:
        if segment['type'] == 'text':
            _append_text(expected, segment['text'])
        else:
            expected.append((segment['type'], segment['material_index']))
    actual = []
    for meta in metas:
        if not isinstance(meta, dict):
            raise ValueError('The upstream prompt mention metadata is invalid.')
        kind = meta.get('meta_type')
        if kind == 'text':
            value = meta.get('text')
            if not isinstance(value, str):
                raise ValueError('The upstream prompt text is invalid.')
            _append_text(actual, value)
        elif kind in ('image', 'video'):
            ref = meta.get('material_ref')
            index = ref.get('material_idx') if isinstance(ref, dict) else None
            if type(index) is not int or index not in actual_to_expected:
                raise ValueError('The upstream mention does not reference a known material.')
            if materials[index]['material_type'] != kind:
                raise ValueError('The upstream mention type does not match its material.')
            actual.append((kind, actual_to_expected[index]))
        else:
            raise ValueError('The upstream prompt has an unsupported mention type.')
    # Browser editors serialize CRLF/CR as LF. Normalize merged text runs so
    # CRLF split across metadata chunks still represents one newline; retain
    # all internal whitespace and reference identities/order.
    expected=[(kind,canonical_prompt_text(value)) if kind=='text' else (kind,value) for kind,value in expected]
    actual=[(kind,canonical_prompt_text(value)) if kind=='text' else (kind,value) for kind,value in actual]
    if actual != expected:
        raise ValueError('The upstream prompt text or reference bindings do not match this job.')
