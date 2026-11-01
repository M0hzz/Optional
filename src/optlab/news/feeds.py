"""Safe parsing of RSS 2.0, Atom and iCalendar feeds (defusedxml: no entity expansion, no external DTDs)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from defusedxml import ElementTree as ET  # type: ignore[import-untyped]

ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass
class FeedItem:
    title: str
    link: str | None
    guid: str | None
    summary: str | None
    published: datetime | None
    categories: list[str] = field(default_factory=list)
    extra: dict[str, str] = field(default_factory=dict)


def parse_time(text: str | None) -> datetime | None:
    """RFC 822 (RSS) or ISO 8601 (Atom) to aware UTC. None if absent or unparseable (never 'now')."""
    if not text:
        return None
    t = text.strip()
    try:
        dt = parsedate_to_datetime(t)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)(\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?$", t)
        if not m:
            return None
        if not m.group(4):
            return None                               # no time zone: can't be placed, so it's unknown
        tz = m.group(4)
        tz = "+00:00" if tz == "Z" else (tz if ":" in tz else tz[:3] + ":" + tz[3:])
        try:
            dt = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}{tz}")
        except ValueError:
            return None
    if dt.tzinfo is None:
        return None                                   # a time without a zone can't be placed: treat as unknown
    return dt.astimezone(timezone.utc)


def _text(el) -> str | None:
    if el is None:
        return None
    s = "".join(el.itertext()).strip()
    return s or None


def parse(data: bytes) -> list[FeedItem]:
    """Items of an RSS 2.0 or Atom document."""
    root = ET.fromstring(data)
    items: list[FeedItem] = []
    if root.tag == f"{ATOM}feed":
        for e in root.findall(f"{ATOM}entry"):
            link = None
            for ln in e.findall(f"{ATOM}link"):
                if ln.get("rel", "alternate") == "alternate":
                    link = ln.get("href")
                    break
            items.append(FeedItem(
                title=_text(e.find(f"{ATOM}title")) or "", link=link, guid=_text(e.find(f"{ATOM}id")),
                summary=_text(e.find(f"{ATOM}summary")) or _text(e.find(f"{ATOM}content")),
                published=parse_time(_text(e.find(f"{ATOM}published")) or _text(e.find(f"{ATOM}updated"))),
                categories=[c.get("term") or c.get("label") or "" for c in e.findall(f"{ATOM}category")]))
        return items
    channel = root.find("channel")
    if channel is None:
        raise ValueError(f"not an RSS or Atom feed (root element {root.tag!r})")
    for it in channel.findall("item"):
        extra = {}
        for child in it:
            if child.tag not in ("title", "link", "guid", "description", "pubDate", "category") and len(child) == 0:
                v = _text(child)
                if v:
                    extra[child.tag] = v
        items.append(FeedItem(
            title=_text(it.find("title")) or "", link=_text(it.find("link")), guid=_text(it.find("guid")),
            summary=_text(it.find("description")), published=parse_time(_text(it.find("pubDate"))),
            categories=[c for c in (_text(x) for x in it.findall("category")) if c], extra=extra))
    return items


# ------------------------------------------------------------------ iCalendar (release schedules)
@dataclass
class CalendarEvent:
    uid: str
    summary: str
    start: datetime


_TZ_ALIASES = {"US-Eastern": "America/New_York", "US/Eastern": "America/New_York"}


def parse_ics(data: bytes) -> list[CalendarEvent]:
    """VEVENTs with a start time (unfolds continuation lines; TZID-qualified, UTC or all-day)."""
    text = data.decode("utf-8", "replace").replace("\r\n", "\n")
    text = re.sub(r"\n[ \t]", "", text)                       # unfold
    out: list[CalendarEvent] = []
    for block in re.findall(r"BEGIN:VEVENT\n(.*?)\nEND:VEVENT", text, flags=re.S):
        props: dict[str, tuple[dict[str, str], str]] = {}
        for line in block.split("\n"):
            if ":" not in line:
                continue
            head, value = line.split(":", 1)
            name, *params = head.split(";")
            props[name.upper()] = ({p.split("=", 1)[0].upper(): p.split("=", 1)[1] for p in params if "=" in p}, value)
        if "DTSTART" not in props or "SUMMARY" not in props:
            continue
        params, v = props["DTSTART"]
        try:
            if v.endswith("Z"):
                start = datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            elif "T" in v:
                tz = ZoneInfo(_TZ_ALIASES.get(params.get("TZID", ""), params.get("TZID") or "America/New_York"))
                start = datetime.strptime(v, "%Y%m%dT%H%M%S").replace(tzinfo=tz)
            else:
                start = datetime.strptime(v, "%Y%m%d").replace(tzinfo=ZoneInfo("America/New_York"))
        except (ValueError, KeyError):
            continue
        summary = props["SUMMARY"][1].replace("\\,", ",").replace("\\;", ";").strip()
        out.append(CalendarEvent(props.get("UID", ({}, summary + v))[1], summary, start.astimezone(timezone.utc)))
    return out


__all__ = ["FeedItem", "CalendarEvent", "parse", "parse_ics", "parse_time", "timedelta"]
