"""Priority classification for community reports (bonus feature)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.db.models.enums import ReportPriority
from app.schemas.report import Report

_LIFE_THREATENING: dict[str, float] = {
    "trapped": 0.55, "buried": 0.55, "drowning": 0.6, "not breathing": 0.6,
    "unconscious": 0.55, "bleeding": 0.45, "cardiac": 0.5, "heart attack": 0.55,
    "collapsed": 0.4, "collapse": 0.35, "gas leak": 0.5, "electrocuted": 0.55,
    "missing": 0.45, "swept away": 0.55, "cannot breathe": 0.6, "suffocating": 0.6,
    "critical condition": 0.5, "life support": 0.5, "out of insulin": 0.45,
    "out of oxygen": 0.55, "severe burns": 0.5, "landslide hit": 0.4,
}

_URGENT_NEED: dict[str, float] = {
    "urgent": 0.3, "immediately": 0.25, "emergency": 0.25, "sos": 0.4,
    "medical help": 0.35, "ambulance": 0.35, "rescue": 0.3, "evacuate": 0.3,
    "evacuation": 0.25, "need water": 0.28, "no water": 0.28, "drinking water": 0.25,
    "need food": 0.25, "no food": 0.28, "baby formula": 0.25, "medicine": 0.28,
    "injured": 0.3, "stranded": 0.28, "still rising": 0.35, "turning people away": 0.25,
    "reached capacity": 0.2, "blocked": 0.2, "do not attempt": 0.25,
    "without supply": 0.15,
}

_SITUATIONAL: dict[str, float] = {
    "power": 0.12, "power out": 0.15, "no electricity": 0.15, "network": 0.08,
    "road": 0.1, "relief camp": 0.2, "shelter": 0.12, "helpline": 0.08,
    "advisory": 0.1, "alert": 0.12, "warning": 0.12, "waterlogging": 0.12,
    "water logging": 0.12,
}

_DE_ESCALATING: dict[str, float] = {
    "we are all safe": -0.3, "all safe": -0.25, "everyone is safe": -0.3,
    "restored": -0.2, "under control": -0.25, "clean-up": -0.2, "cleanup": -0.2,
    "now passable": -0.2, "dropping": -0.15, "no longer": -0.15,
    "distributing": -0.1, "volunteers": -0.08, "operational": -0.1,
}

_VULNERABLE = re.compile(
    r"\b(child|children|baby|babies|infant|toddler|elderly|grandmother|grandfather|"
    r"pregnant|disabled|wheelchair|diabetic|senior citizens?)\b",
    re.IGNORECASE,
)

_PEOPLE_COUNT = re.compile(
    r"\b(\d{1,4})\s+(people|persons|families|residents|students|patients|children)\b",
    re.IGNORECASE,
)

_DURATION = re.compile(
    r"\b(?:since|for (?:over |more than )?)\s*(\d+\s*(?:hours?|days?|hrs?)|"
    r"morning|yesterday|last night|two days|three days)\b",
    re.IGNORECASE,
)

_THRESHOLD_CRITICAL = 0.70
_THRESHOLD_HIGH = 0.42
_THRESHOLD_MEDIUM = 0.18


@dataclass(frozen=True, slots=True)
class Classification:
    priority: ReportPriority
    score: float
    signals: list[str]


def classify_text(text: str) -> Classification:
    """Score one piece of report text."""
    if not text or not text.strip():
        return Classification(ReportPriority.LOW, 0.0, [])

    lowered = text.lower()
    score = 0.0
    signals: list[str] = []
    claimed: list[tuple[int, int]] = []

    for lexicon, label in (
        (_LIFE_THREATENING, "life_threatening"),
        (_URGENT_NEED, "urgent_need"),
        (_SITUATIONAL, "situational"),
        (_DE_ESCALATING, "de_escalating"),
    ):
        for phrase, weight in lexicon.items():
            position = lowered.find(phrase)
            if position != -1:
                score += weight
                signals.append(f"{label}:{phrase.replace(' ', '_')}")
                claimed.append((position, position + len(phrase)))

    if re.search(r"\b[A-Z]{4,}\b", text):
        score += 0.12
        signals.append("emphasis:shouting")

    if "!" in text:
        score += 0.05
        signals.append("emphasis:exclamation")

    vulnerable = _VULNERABLE.search(text)
    if vulnerable and not _overlaps(vulnerable.span(), claimed):
        score += 0.18
        signals.append("vulnerable_group")

    people = _PEOPLE_COUNT.search(text)
    if people:
        count = int(people.group(1))
        score += min(0.25, 0.06 + count / 1000)
        signals.append(f"people_affected:{count}")

    if _DURATION.search(lowered):
        score += 0.1
        signals.append("duration_mentioned")

    score = max(0.0, min(1.0, round(score, 4)))
    return Classification(_bucket(score), score, signals)


def _overlaps(span: tuple[int, int], claimed: list[tuple[int, int]]) -> bool:
    """True if span intersects any already-scored phrase."""
    start, end = span
    return any(
        start < claimed_end and claimed_start < end
        for claimed_start, claimed_end in claimed
    )


def _bucket(score: float) -> ReportPriority:
    if score >= _THRESHOLD_CRITICAL:
        return ReportPriority.CRITICAL
    if score >= _THRESHOLD_HIGH:
        return ReportPriority.HIGH
    if score >= _THRESHOLD_MEDIUM:
        return ReportPriority.MEDIUM
    return ReportPriority.LOW


def classify_report(report: Report) -> Report:
    """Return a copy of report with priority fields populated."""
    result = classify_text(report.content)

    if report.source == "official" and report.priority != ReportPriority.LOW:
        return report.model_copy(
            update={
                "priority_score": result.score,
                "matched_signals": [*result.signals, "official:agency_severity_trusted"],
            }
        )

    return report.model_copy(
        update={
            "priority": result.priority,
            "priority_score": result.score,
            "matched_signals": result.signals,
        }
    )


_PRIORITY_RANK = {
    ReportPriority.CRITICAL: 0,
    ReportPriority.HIGH: 1,
    ReportPriority.MEDIUM: 2,
    ReportPriority.LOW: 3,
}


def sort_key(report: Report) -> tuple[int, float, float]:
    return (
        _PRIORITY_RANK.get(report.priority, 3),
        -report.priority_score,
        -report.created_at.timestamp(),
    )
