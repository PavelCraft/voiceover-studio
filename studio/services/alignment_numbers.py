import re
from difflib import SequenceMatcher


def is_numeric_token(value):
    """Return True for a number written with Unicode decimal digits."""
    compact = re.sub(r"[\s.,_'’+\-]", "", str(value).strip())
    return bool(compact) and all(character.isdecimal() for character in compact)


def expand_numeric_replacements(entries, words, normalize_word):
    """Expand ASR digits to the expected tokens using global text anchors.

    Speech recognizers often replace an arbitrary spoken number expression with
    one digit token. SequenceMatcher locates that replacement between unchanged
    text anchors. The digit token's acoustic interval is then divided between
    the expected tokens, so the regular phrase aligner can remain language
    agnostic.
    """
    target_tokens = [
        token
        for entry in entries
        for token in entry.get("tokens", [])
    ]
    if not target_tokens or not words:
        return words

    asr_tokens = [normalize_word(word.get("word", "")) for word in words]
    matcher = SequenceMatcher(None, target_tokens, asr_tokens, autojunk=False)
    expanded = []

    for tag, target_start, target_end, asr_start, asr_end in matcher.get_opcodes():
        numeric_replacement = (
            tag == "replace"
            and target_end > target_start
            and asr_end > asr_start
            and all(is_numeric_token(words[index].get("word", ""))
                    for index in range(asr_start, asr_end))
        )
        if not numeric_replacement:
            expanded.extend(words[asr_start:asr_end])
            continue

        replacement_tokens = target_tokens[target_start:target_end]
        source_words = words[asr_start:asr_end]
        if len(replacement_tokens) == len(source_words):
            for token, source_word in zip(replacement_tokens, source_words):
                expanded.append({
                    "word": token,
                    "start": float(source_word["start"]),
                    "end": float(source_word["end"]),
                    "numeric_expansion": True,
                })
            continue

        interval_start = float(words[asr_start]["start"])
        interval_end = float(words[asr_end - 1]["end"])
        step = max(0.0, interval_end - interval_start) / len(replacement_tokens)
        for offset, token in enumerate(replacement_tokens):
            expanded.append({
                "word": token,
                "start": interval_start + step * offset,
                "end": interval_start + step * (offset + 1),
                "numeric_expansion": True,
            })

    return expanded
