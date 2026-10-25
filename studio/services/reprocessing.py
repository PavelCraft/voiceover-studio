from __future__ import annotations

from difflib import SequenceMatcher

from django.db import transaction


_SEVERITY = {"text": 1, "timing": 2, "structure": 3, "full": 4}


def _clock(item, key):
    value = item.get(key)
    return None if value in (None, "") else round(float(value), 3)


def phrase_mapping(old_phrases, new_phrases):
    """Return an old 1-based position -> new 1-based position mapping."""
    old_text = [str(item.get("text", "")).strip() for item in old_phrases]
    new_text = [str(item.get("text", "")).strip() for item in new_phrases]
    mapping = {}
    matcher = SequenceMatcher(a=old_text, b=new_text, autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            pairs = zip(range(old_start, old_end), range(new_start, new_end))
        elif tag == "replace":
            pairs = zip(range(old_start, old_end), range(new_start, new_end))
        else:
            pairs = ()
        for old_index, new_index in pairs:
            mapping[old_index + 1] = new_index + 1
    return mapping


def build_change_plan(processed_phrases, new_phrases):
    if not processed_phrases:
        return {"kind": "full", "from_position": 1, "positions": []}

    old_text = [str(item.get("text", "")).strip() for item in processed_phrases]
    new_text = [str(item.get("text", "")).strip() for item in new_phrases]
    matcher = SequenceMatcher(a=old_text, b=new_text, autojunk=False)
    mapping = {}
    text_positions = set()
    structure_positions = set()
    structure_changed = False

    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            for old_index, new_index in zip(range(old_start, old_end), range(new_start, new_end)):
                mapping[new_index + 1] = old_index + 1
            continue
        if tag == "replace":
            paired = min(old_end - old_start, new_end - new_start)
            for offset in range(paired):
                mapping[new_start + offset + 1] = old_start + offset + 1
                text_positions.add(new_start + offset + 1)
            if old_end - old_start != new_end - new_start:
                structure_changed = True
                structure_positions.update(range(new_start + 1, new_end + 1))
                structure_positions.add(max(1, new_start + 1))
            continue
        structure_changed = True
        structure_positions.update(range(new_start + 1, new_end + 1))
        structure_positions.add(max(1, new_start + 1))

    timing_positions = set()
    for new_position, old_position in mapping.items():
        old_item = processed_phrases[old_position - 1]
        new_item = new_phrases[new_position - 1]
        if _clock(old_item, "start") != _clock(new_item, "start") or _clock(old_item, "end") != _clock(new_item, "end"):
            timing_positions.add(new_position)
            if new_position > 1:
                timing_positions.add(new_position - 1)

    changed = timing_positions | text_positions | structure_positions
    if structure_changed:
        kind = "structure"
    elif timing_positions:
        kind = "timing"
    elif text_positions:
        kind = "text"
    else:
        return {}
    first = min(changed or {1})
    return {"kind": kind, "from_position": first, "positions": sorted(changed)}


def merge_change_plans(existing, incoming):
    if not existing:
        return dict(incoming or {})
    if not incoming:
        return dict(existing)
    kind = max((existing.get("kind", "timing"), incoming.get("kind", "timing")), key=lambda value: _SEVERITY[value])
    positions = sorted(set(existing.get("positions", [])) | set(incoming.get("positions", [])))
    starts = [value for value in (existing.get("from_position"), incoming.get("from_position")) if value]
    return {
        "kind": kind,
        "from_position": min(starts) if starts else 1,
        "positions": positions,
    }


@transaction.atomic
def remap_project_annotations(project, old_phrases, new_phrases):
    """Keep manual boundaries and replacements attached across phrase insertions/deletions."""
    if not old_phrases:
        return
    mapping = phrase_mapping(old_phrases, new_phrases)
    temporary_offset = 1_000_000
    for manager in (project.manual_boundaries, project.segment_replacements):
        items = list(manager.select_for_update())
        for item in items:
            new_position = mapping.get(item.position)
            if new_position is None:
                item.delete()
                continue
            item.position = new_position + temporary_offset
            item.save(update_fields=["position"])
        for item in manager.filter(position__gte=temporary_offset):
            item.position -= temporary_offset
            item.save(update_fields=["position"])
