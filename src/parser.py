from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class FollowEvent:
    kol: str
    project_handle: str
    bio: str = ""
    followers: int | None = None
    follower_handle: str | None = None


HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{1,15})")
URL_HANDLE_RE = re.compile(r"https?://(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})", re.IGNORECASE)


def parse_followers(text: str) -> int | None:
    m = re.search(r"粉丝数\s*[:：]\s*([\d,]+(?:\.\d+)?)\s*([kKmM万亿]?)", text)
    if not m:
        return None
    scale = {"k": 1000, "m": 1000000, "万": 10000, "亿": 100000000}.get(m[2].lower(), 1)
    return int(float(m[1].replace(",", "")) * scale)


def _clean_handle(h: str) -> str:
    """Strip @ and whitespace; preserve original case (X handles are case-insensitive
    but we keep the displayed casing from the source message)."""
    return h.lstrip("@").strip()


def _explicit_follower(value: str) -> str | None:
    # Display names are not handles. Never guess a project's outgoing follows from a KOL name.
    url = URL_HANDLE_RE.search(value)
    if url:
        return url[1]
    handle = re.search(r'(?<![\w@])@([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_])', value)
    return handle[1] if handle else None


def parse_format_one(text: str) -> list[FollowEvent]:
    if "你关注的用户" not in text or "用户列表" not in text:
        return []
    kol_match = re.search(r"你关注的用户[:：]\s*([^\n(（]+)", text)
    if not kol_match:
        return []
    kol = kol_match.group(1).strip()
    heading = next((line for line in text.splitlines() if '你关注的用户' in line), '')
    follower_handle = _explicit_follower(heading)

    list_part = text.split("用户列表", 1)[1]
    events: list[FollowEvent] = []
    seen: set[str] = set()
    for line in list_part.splitlines():
        line = line.strip()
        if not line.startswith("•") and not line.startswith("·") and not line.startswith("-"):
            continue
        m = HANDLE_RE.search(line)
        if not m:
            url_m = URL_HANDLE_RE.search(line)
            if not url_m:
                continue
            handle = _clean_handle(url_m.group(1))
        else:
            handle = _clean_handle(m.group(1))
        if handle.lower() in seen:
            continue
        seen.add(handle.lower())
        events.append(FollowEvent(kol=kol, project_handle=handle, followers=parse_followers(line), follower_handle=follower_handle))
    return events


def parse_format_two(text: str) -> list[FollowEvent]:
    if "关注了" not in text:
        return []
    first_line = text.splitlines()[0] if text else ""
    m = re.match(r"\s*(.+?)\s+关注了\s+(.+?)\s*$", first_line)
    if not m:
        return []
    kol = m.group(1).strip()
    urls = list(URL_HANDLE_RE.finditer(text))
    if not urls:
        return []
    # The final profile link identifies the followed account; bios can contain links too.
    url_m = urls[-1]
    handle = _clean_handle(url_m.group(1))
    bio_match = re.search(r"用户简介[:：]\s*", text)
    bio = text[bio_match.end():url_m.start()].strip()[:1000] if bio_match else ""
    return [FollowEvent(kol=kol, project_handle=handle, bio=bio, followers=parse_followers(text[:bio_match.start()] if bio_match else text), follower_handle=_explicit_follower(kol))]


def parse_message(text: str) -> list[FollowEvent]:
    if not text:
        return []
    if "🌟" in text or "你关注的用户" in text:
        return parse_format_one(text)
    return parse_format_two(text)
