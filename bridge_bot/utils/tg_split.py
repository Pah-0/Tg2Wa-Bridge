"""Splits bridged text that's too long for one Telegram message."""

import copy

from telethon.helpers import add_surrogate, del_surrogate, strip_text

from .parse_md import parse as custom_parse

# Telegram's limits, counted in UTF-16 code units after formatting is parsed.
TG_TEXT_MAX = 4096
TG_CAPTION_MAX = 1024


def find_cut(text, start, limit):
    """Where to end the part starting at start: at the last paragraph break,
    line break or space that fits, else at the limit."""
    end = start + limit
    if end >= len(text):
        return len(text)
    # Don't cut a part shorter than half the limit just to keep words whole.
    floor = start + limit // 2
    for sep in ("\n\n", "\n", " "):
        cut = text.rfind(sep, floor, end)
        if cut != -1:
            return cut
    # Never split a surrogate pair.
    if "\udc00" <= text[end] <= "\udfff":
        end -= 1
    return end


def part_entities(entities, start, end):
    """The entities overlapping text[start:end], clipped and moved to it."""
    part = []
    for entity in entities:
        e_start = max(entity.offset, start)
        e_end = min(entity.offset + entity.length, end)
        if e_end <= e_start:
            continue
        entity = copy.copy(entity)
        entity.offset, entity.length = e_start - start, e_end - e_start
        part.append(entity)
    return part


def split_tg_text(raw, first_limit=TG_TEXT_MAX, limit=TG_TEXT_MAX):
    """Parses bridge markdown into [(text, entities)] parts for Telegram.

    The first part fits first_limit (a caption's limit for media), the rest
    fit limit. Formatting that spans a cut is carried over to both parts.
    """
    text, entities = custom_parse(raw)
    text = add_surrogate(text)
    parts = []
    start = 0
    while start < len(text):
        end = find_cut(text, start, first_limit if not parts else limit)
        ents = part_entities(entities, start, end)
        # Telegram trims each message, which would shift its entities.
        chunk = strip_text(text[start:end], ents)
        if chunk:
            parts.append((del_surrogate(chunk), ents))
        start = end
    return parts or [("", [])]
