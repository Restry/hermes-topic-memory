from __future__ import annotations

import re
from dataclasses import dataclass

_CONTEXT_RADIUS = 2
_MAX_EXACT_MATCHES = 3
_PERIODS = "凌晨|早上|上午|中午|下午|傍晚|晚上"
_CHINESE_DIGITS = "〇零一二两三四五六七八九十"
_NUMERIC_TIME = re.compile(
    rf"(?<!\d)(?:(?P<period>{_PERIODS})\s*)?"
    r"(?P<hour>[01]?\d|2[0-3])\s*[:：]\s*(?P<minute>[0-5]\d)(?!\d)"
)
_CHINESE_TIME = re.compile(
    rf"(?<![0-9{_CHINESE_DIGITS}])(?:(?P<period>{_PERIODS})\s*)?"
    rf"(?P<hour>[0-9{_CHINESE_DIGITS}]{{1,3}})\s*点"
    rf"(?:\s*(?P<half>半)|\s*(?P<minute>[0-9{_CHINESE_DIGITS}]{{1,3}})\s*分)?"
)
_CHINESE_DATE = re.compile(
    r"(?:(?P<year>\d{4})\s*年\s*)?"
    r"(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*[日号]?"
)
_ISO_DATE = re.compile(
    r"(?<!\d)(?P<year>\d{4})\s*[-/.]\s*(?P<month>\d{1,2})"
    r"\s*[-/.]\s*(?P<day>\d{1,2})(?!\d)"
)
_MARKDOWN_HEADING = re.compile(r"^\s{0,3}#{1,6}(?:\s+|$)")
_DIGIT_VALUES = {
    "〇": 0,
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


@dataclass(frozen=True, slots=True)
class _DateAnchor:
    year: int | None
    month: int
    day: int


@dataclass(frozen=True, slots=True)
class _Heading:
    index: int
    text: str
    dates: tuple[_DateAnchor, ...]


@dataclass(frozen=True, slots=True)
class _ExactMatch:
    index: int
    date_heading: _Heading | None
    section_heading: _Heading | None


def build_temporal_evidence_capsule(body: str, question: str) -> str | None:
    """Select a bounded, authoritative Markdown window for one exact query time."""

    lines = body.splitlines()
    requested_time = _resolve_requested_time(question, lines)
    if requested_time is None:
        return None

    matches = [
        _match_with_headings(lines, index)
        for index, line in enumerate(lines)
        if requested_time in _line_times(line)
    ]
    if not matches:
        return None

    question_dates = _extract_dates(question)
    if question_dates:
        dated_matches = [
            match
            for match in matches
            if match.date_heading is not None
            and any(
                _dates_compatible(question_date, heading_date)
                for question_date in question_dates
                for heading_date in match.date_heading.dates
            )
        ]
        if dated_matches:
            matches = dated_matches
        elif any(match.date_heading is not None for match in matches):
            return None
        elif len(matches) != 1:
            return None

    if len(matches) > _MAX_EXACT_MATCHES:
        return None

    output = [f"NORMALIZED TIME: {requested_time}"]
    exact_indices = {match.index for match in matches}
    for position, match in enumerate(matches, start=1):
        if len(matches) > 1:
            output.append(f"MATCH {position}:")
        if match.date_heading is not None:
            output.append(f"DATE HEADING: {match.date_heading.text}")
        if match.section_heading is not None:
            output.append(f"SECTION HEADING: {match.section_heading.text}")
        start = max(0, match.index - _CONTEXT_RADIUS)
        stop = min(len(lines), match.index + _CONTEXT_RADIUS + 1)
        for line_index in range(start, stop):
            line = lines[line_index].rstrip()
            if not line:
                continue
            marker = "[EXACT]" if line_index in exact_indices else "[CONTEXT]"
            output.append(f"{marker} {line}")
    return "\n".join(output)


def _resolve_requested_time(question: str, lines: list[str]) -> str | None:
    body_times = {time for line in lines for time in _line_times(line)}
    candidate_sets = _time_candidate_sets(question, ambiguous_chinese=True)
    if not candidate_sets:
        return None

    resolved: set[str] = set()
    for candidates in candidate_sets:
        available = candidates & body_times
        if len(available) != 1:
            return None
        resolved.update(available)
    return next(iter(resolved)) if len(resolved) == 1 else None


def _line_times(line: str) -> frozenset[str]:
    times: set[str] = set()
    for candidates in _time_candidate_sets(line, ambiguous_chinese=False):
        if len(candidates) == 1:
            times.update(candidates)
    return frozenset(times)


def _time_candidate_sets(
    text: str, *, ambiguous_chinese: bool
) -> tuple[frozenset[str], ...]:
    values: list[frozenset[str]] = []
    for match in _NUMERIC_TIME.finditer(text):
        candidates = _normalized_candidates(
            int(match.group("hour")),
            int(match.group("minute")),
            match.group("period"),
            ambiguous_without_period=False,
        )
        if candidates:
            values.append(candidates)
    for match in _CHINESE_TIME.finditer(text):
        hour = _parse_chinese_number(match.group("hour"))
        minute_text = match.group("minute")
        minute = (
            30
            if match.group("half")
            else _parse_chinese_number(minute_text)
            if minute_text
            else 0
        )
        if hour is None or minute is None:
            continue
        candidates = _normalized_candidates(
            hour,
            minute,
            match.group("period"),
            ambiguous_without_period=ambiguous_chinese,
        )
        if candidates:
            values.append(candidates)
    return tuple(values)


def _normalized_candidates(
    hour: int,
    minute: int,
    period: str | None,
    *,
    ambiguous_without_period: bool,
) -> frozenset[str]:
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return frozenset()
    if period in {"凌晨", "早上", "上午"}:
        normalized = 0 if hour == 12 else hour
        return frozenset({_format_time(normalized, minute)})
    if period == "中午":
        normalized = hour + 12 if 1 <= hour <= 10 else hour
        return frozenset({_format_time(normalized, minute)})
    if period in {"下午", "傍晚", "晚上"}:
        if period == "晚上" and hour == 12:
            normalized = 0
        else:
            normalized = hour + 12 if 1 <= hour <= 11 else hour
        return frozenset({_format_time(normalized, minute)})
    if ambiguous_without_period and 1 <= hour <= 11:
        return frozenset({_format_time(hour, minute), _format_time(hour + 12, minute)})
    if ambiguous_without_period and hour == 12:
        return frozenset({_format_time(0, minute), _format_time(12, minute)})
    return frozenset({_format_time(hour, minute)})


def _format_time(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def _parse_chinese_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    if "十" in value:
        left, _, right = value.partition("十")
        if "十" in right:
            return None
        tens = 1 if not left else _DIGIT_VALUES.get(left)
        ones = 0 if not right else _DIGIT_VALUES.get(right)
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    digits = [_DIGIT_VALUES.get(character) for character in value]
    if not digits or any(digit is None for digit in digits):
        return None
    return int("".join(str(digit) for digit in digits))


def _match_with_headings(lines: list[str], index: int) -> _ExactMatch:
    nearest_heading: _Heading | None = None
    date_heading: _Heading | None = None
    for heading_index in range(index - 1, -1, -1):
        text = lines[heading_index].strip()
        if not _MARKDOWN_HEADING.match(text):
            continue
        heading = _Heading(heading_index, text, _extract_dates(text))
        if nearest_heading is None:
            nearest_heading = heading
        if heading.dates and date_heading is None:
            date_heading = heading
        if nearest_heading is not None and date_heading is not None:
            break
    section_heading = (
        nearest_heading
        if nearest_heading is not None
        and (date_heading is None or nearest_heading.index != date_heading.index)
        else None
    )
    return _ExactMatch(index, date_heading, section_heading)


def _extract_dates(text: str) -> tuple[_DateAnchor, ...]:
    dates: list[_DateAnchor] = []
    for pattern in (_CHINESE_DATE, _ISO_DATE):
        for match in pattern.finditer(text):
            year_text = match.group("year")
            anchor = _DateAnchor(
                int(year_text) if year_text else None,
                int(match.group("month")),
                int(match.group("day")),
            )
            if 1 <= anchor.month <= 12 and 1 <= anchor.day <= 31 and anchor not in dates:
                dates.append(anchor)
    return tuple(dates)


def _dates_compatible(question: _DateAnchor, heading: _DateAnchor) -> bool:
    return (
        question.month == heading.month
        and question.day == heading.day
        and (
            question.year is None
            or heading.year is None
            or question.year == heading.year
        )
    )
