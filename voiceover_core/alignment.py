"""Language-agnostic phrase alignment and boundary selection."""

import re
from difflib import SequenceMatcher

MIN_CURSOR_SCORE = 0.35
MIN_LOOKAHEAD_SCORE = 0.55
SEARCH_RADIUS = 0.9
MIN_SNAPPED_DURATION = 0.15
NUMBER_MAP = {
    "ноль": "0", "один": "1", "два": "2", "три": "3", "четыре": "4", "пять": "5",
    "шесть": "6", "семь": "7", "восемь": "8", "девять": "9", "десять": "10",
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "null": "0", "eins": "1", "ein": "1", "eine": "1", "zwei": "2", "drei": "3",
    "vier": "4", "fünf": "5", "funf": "5", "sechs": "6", "sieben": "7", "acht": "8",
    "neun": "9", "zehn": "10", "cero": "0", "uno": "1", "una": "1", "dos": "2",
    "tres": "3", "cuatro": "4", "cinco": "5", "seis": "6", "siete": "7", "ocho": "8",
    "nueve": "9", "diez": "10", "zéro": "0", "un": "1", "une": "1", "deux": "2",
    "trois": "3", "quatre": "4", "cinq": "5", "sept": "7", "huit": "8", "neuf": "9",
    "dix": "10", "um": "1", "uma": "1", "dois": "2", "duas": "2", "três": "3",
    "sete": "7", "oito": "8", "nove": "9", "dez": "10", "零": "0", "一": "1",
    "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8",
    "九": "9", "十": "10",
}


def normalize_word(value):
    value = re.sub(r"[^\w+]", "", str(value).lower().replace("ё", "е"), flags=re.UNICODE)
    return NUMBER_MAP.get(value, value)


def tokenize(text):
    result, buffer = [], []
    def flush():
        if buffer:
            word = normalize_word("".join(buffer))
            if word:
                result.append(word)
            buffer.clear()
    for character in str(text).lower():
        if re.fullmatch(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]", character):
            flush()
            result.append(normalize_word(character))
        elif character.isalnum() or character in "_+":
            buffer.append(character)
        else:
            flush()
    flush()
    return result


def _is_numeric(value):
    compact = re.sub(r"[\s.,_'’+\-]", "", str(value).strip())
    return bool(compact) and all(character.isdecimal() for character in compact)


def _expand_numbers(entries, words):
    targets = [token for entry in entries for token in entry.get("tokens", [])]
    if not targets or not words:
        return words
    asr = [normalize_word(word.get("word", "")) for word in words]
    expanded = []
    for tag, t0, t1, a0, a1 in SequenceMatcher(None, targets, asr, autojunk=False).get_opcodes():
        numeric = tag == "replace" and t1 > t0 and a1 > a0 and all(_is_numeric(words[i].get("word", "")) for i in range(a0, a1))
        if not numeric:
            expanded.extend(words[a0:a1])
            continue
        replacement = targets[t0:t1]
        source = words[a0:a1]
        if len(replacement) == len(source):
            expanded.extend({
                "word": token,
                "start": float(word["start"]),
                "end": float(word["end"]),
                "numeric_expansion": True,
            } for token, word in zip(replacement, source))
            continue
        start, end = float(words[a0]["start"]), float(words[a1 - 1]["end"])
        step = max(0, end - start) / len(replacement)
        expanded.extend({"word": token, "start": start + step * i, "end": start + step * (i + 1), "numeric_expansion": True} for i, token in enumerate(replacement))
    return expanded


def _expand_words(words):
    expanded = []
    for word in words:
        tokens = tokenize(word.get("word", ""))
        if not tokens:
            continue
        start, end = float(word["start"]), float(word["end"])
        for index, token in enumerate(tokens):
            item = dict(word)
            item.update(word=token, start=start + (end - start) * index / len(tokens), end=start + (end - start) * (index + 1) / len(tokens))
            expanded.append(item)
    return expanded


def _score(target, tokens, start, end):
    return SequenceMatcher(None, target, tokens[start:end], autojunk=False).ratio()


def _is_cjk(target):
    count = sum(bool(re.fullmatch(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]", token)) for token in target)
    return bool(target) and count >= 2 and count >= len(target) * .25


def find_best_match(target, tokens, cursor):
    search_end = min(len(tokens), cursor + max(250, len(target) * 4))
    minimum = max(1, int(len(target) * .65))
    maximum = min(search_end - cursor, max(len(target) * 2, len(target) + 30))
    if maximum < minimum:
        return None
    starts = range(cursor, search_end)
    if _is_cjk(target):
        matcher = SequenceMatcher(None, target, tokens[cursor:search_end], autojunk=False)
        candidates = {cursor}
        for block in sorted((b for b in matcher.get_matching_blocks() if b.size), key=lambda b: b.size, reverse=True)[:16]:
            candidates.update(cursor + block.b - block.a + offset for offset in range(-2, 3))
        starts = sorted(value for value in candidates if cursor <= value < search_end)
    best = None
    for start in starts:
        for length in range(minimum, maximum + 1):
            end = start + length
            if end > search_end:
                break
            score = _score(target, tokens, start, end)
            if best is None or score > best[0]:
                best = (score, start, end)
    return best


def find_all_matches(target, words, minimum_score=.45):
    """Return non-overlapping occurrences of one scripted phrase in ASR words.

    This intentionally keeps more candidates than the normal one-pass aligner.  A
    takes workflow needs alternatives, and ambiguous candidates are reviewed by
    the user instead of being silently discarded.
    """
    expanded = _expand_words(words)
    tokens = [normalize_word(word.get("word", "")) for word in expanded]
    target = list(target)
    if not target or not tokens:
        return []
    minimum = max(1, int(len(target) * .65))
    maximum = max(minimum, min(len(tokens), max(len(target) * 2, len(target) + 20)))
    anchors = set(target[:min(4, len(target))])
    raw = []
    for start, token in enumerate(tokens):
        if token not in anchors:
            continue
        best = None
        for length in range(minimum, maximum + 1):
            end = start + length
            if end > len(tokens):
                break
            score = _score(target, tokens, start, end)
            if best is None or score > best[0]:
                best = (score, start, end)
        if best and best[0] >= minimum_score:
            score, begin, end = best
            raw.append({
                "audio_start": float(expanded[begin]["start"]),
                "audio_end": float(expanded[end - 1]["end"]),
                "score": round(score, 3),
            })
    # Keep the strongest candidate in each overlapping cluster.
    selected = []
    for candidate in sorted(raw, key=lambda item: (-item["score"], item["audio_start"])):
        if any(
            candidate["audio_start"] < kept["audio_end"]
            and kept["audio_start"] < candidate["audio_end"]
            for kept in selected
        ):
            continue
        selected.append(candidate)
    return sorted(selected, key=lambda item: item["audio_start"])


def find_take_candidates(entries, words, minimum_score=.45):
    """Find every plausible take for every script entry in one recording."""
    prepared_words = _expand_numbers(entries, words)
    return {
        entry.get("id", index + 1): find_all_matches(
            entry.get("tokens") or tokenize(entry.get("text", "")),
            prepared_words,
            minimum_score=minimum_score,
        )
        for index, entry in enumerate(entries)
    }


def align_entries(entries, words, overrides=None, fixed_entries=None):
    words = _expand_words(_expand_numbers(entries, words))
    tokens = [normalize_word(word.get("word", "")) for word in words]
    cursor, overrides, fixed_entries = 0, overrides or {}, fixed_entries or {}
    for index, entry in enumerate(entries):
        identifier = entry.get("id", index + 1)
        fixed = fixed_entries.get(identifier)
        if fixed:
            for key in ("status", "audio_start", "audio_end", "score", "boundary_status"):
                if key in fixed:
                    entry[key] = fixed[key]
            entry["_locked"] = True
            if entry.get("audio_end") is not None:
                cursor = next((i for i, word in enumerate(words) if float(word["start"]) >= float(entry["audio_end"]) - .02), len(words))
            continue
        override = overrides.get(identifier)
        if override:
            if override.skipped:
                entry.update(status="skipped", audio_start=None, audio_end=None, score=None, boundary_status="manual")
                continue
            entry.update(status="manual", audio_start=float(override.audio_start), audio_end=float(override.audio_end), score=1.0, boundary_status="manual")
            cursor = next((i for i, word in enumerate(words) if float(word["start"]) >= entry["audio_end"] - .02), len(words))
            continue
        target = entry["tokens"]
        best = find_best_match(target, tokens, cursor)
        if not best:
            entry.update(status="not_found", audio_start=None, audio_end=None, score=0)
            continue
        score, start, end = best
        if index + 1 < len(entries):
            following = find_best_match(entries[index + 1]["tokens"], tokens, cursor)
            if following and following[0] >= MIN_LOOKAHEAD_SCORE and following[2] <= start:
                entry.update(status="not_found", audio_start=None, audio_end=None, score=round(score, 3))
                continue
        if score < MIN_CURSOR_SCORE:
            entry.update(status="not_found", audio_start=None, audio_end=None, score=round(score, 3))
            continue
        while start < end and tokens[start] not in target[:min(8, len(target))]:
            start += 1
        while end > start and tokens[end - 1] not in target[-min(8, len(target)):]:
            end -= 1
        if end <= start:
            entry.update(status="not_found", audio_start=None, audio_end=None, score=round(score, 3))
            continue
        entry.update(audio_start=float(words[start]["start"]), audio_end=float(words[end - 1]["end"]), score=round(score, 3), status="ok" if score >= .45 else "low_confidence")
        cursor = end
    return entries


def rank_silences(approximate, silences):
    candidates = []
    for start, end, duration in silences:
        midpoint = (start + end) / 2
        distance = 0 if start <= approximate <= end else min(abs(approximate - start), abs(approximate - end))
        if distance <= SEARCH_RADIUS:
            candidates.append((distance + .2 * abs(midpoint - approximate) - .12 * min(duration, .8), start, end))
    return [(start, end) for _, start, end in sorted(candidates)]


def snap_boundaries(entries, silences):
    aligned = [entry for entry in entries if entry.get("audio_start") is not None]
    originals = {id(entry): (entry["audio_start"], entry["audio_end"]) for entry in aligned}
    for previous, following in zip(aligned, aligned[1:]):
        if previous.get("_locked") or following.get("_locked") or previous.get("status") == "manual" or following.get("status") == "manual":
            previous.setdefault("boundary_status", "word")
            following.setdefault("boundary_status", "word")
            continue
        approximate = (previous["audio_end"] + following["audio_start"]) / 2
        for start, end in rank_silences(approximate, silences):
            if start >= previous["audio_start"] and end <= following["audio_end"] and abs(start - previous["audio_end"]) <= SEARCH_RADIUS and abs(end - following["audio_start"]) <= SEARCH_RADIUS:
                previous["audio_end"], following["audio_start"] = start, end
                previous["boundary_status"] = following["boundary_status"] = "silence"
                break
        else:
            previous.setdefault("boundary_status", "word")
            following.setdefault("boundary_status", "word")
    for index, entry in enumerate(aligned):
        if entry.get("_locked") or entry.get("status") == "manual":
            continue
        original_start, original_end = originals[id(entry)]
        previous = aligned[index - 1] if index else None
        following = aligned[index + 1] if index + 1 < len(aligned) else None
        starts = [s for s in silences if (not previous or (previous.get("status") != "manual" and not previous.get("_locked"))) and s[0] <= entry["audio_start"] <= s[1]]
        ends = [s for s in silences if (not following or (following.get("status") != "manual" and not following.get("_locked"))) and s[0] <= entry["audio_end"] <= s[1]]
        changed = False
        if starts:
            entry["audio_start"] = max(starts, key=lambda silence: silence[1])[1]
            changed = True
        if ends:
            entry["audio_end"] = min(ends, key=lambda silence: silence[0])[0]
            changed = True
        if changed and entry["audio_end"] - entry["audio_start"] >= MIN_SNAPPED_DURATION:
            entry["boundary_status"] = "silence"
        elif changed:
            entry["audio_start"], entry["audio_end"] = original_start, original_end
            entry["boundary_status"] = "word"
    return entries
