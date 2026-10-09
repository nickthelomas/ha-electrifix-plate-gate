"""Pure plate helpers: normalisation, tolerant patterns, matching.

No Home Assistant imports. Everything here is unit-tested without HA.

Frigate (verified in v0.18.0) matches a known plate with
``re.match(f"^{pattern}$", raw_ocr_string)`` and does NOT normalise the OCR
string first, so the patterns generated here tolerate the separators
PaddleOCR emits (``XO ·520``) and the usual OCR confusions (O/0, I/1 ...).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SEPARATORS = " .·•-"
SEP_CLASS = "[ .·•\\-]*"
CONFUSABLE_GROUPS = ("O0", "I1", "B8", "S5", "G6", "Z2")
_CLASS_FOR = {ch: "[" + ch + grp.replace(ch, "") + "]" for grp in CONFUSABLE_GROUPS for ch in grp}
_NON_ALNUM = re.compile(r"[^A-Z0-9]")


class PlateParseError(ValueError):
    """A line of the people/plates text could not be understood."""

    def __init__(self, line_no: int, line: str, why: str) -> None:
        super().__init__(f"line {line_no}: {why}: {line!r}")
        self.line_no = line_no
        self.line = line
        self.why = why


@dataclass(frozen=True)
class Person:
    """One person and their normalised plates."""

    name: str
    plates: tuple[str, ...]


@dataclass(frozen=True)
class Match:
    """Why a raw plate read matched a person."""

    person: str
    plate: str
    pattern: str
    how: str  # "regex" | "normalised" | "distance"


def normalise(raw: str) -> str:
    """Upper-case and strip every separator / non-alphanumeric character."""
    return _NON_ALNUM.sub("", (raw or "").upper())


def variant_pattern(plate: str) -> str:
    """Regex tolerant of separators and OCR confusions, e.g. XO520 -> X[ .·•\\-]*[O0]..."""
    plain = normalise(plate)
    if not plain:
        raise ValueError("plate is empty after removing separators")
    return SEP_CLASS.join(_CLASS_FOR.get(ch, re.escape(ch)) for ch in plain)


def parse_people(text: str) -> list[Person]:
    """Parse 'Name: PLATE, PLATE' lines. Blank lines are ignored."""
    people: list[Person] = []
    for line_no, line in enumerate((text or "").splitlines(), start=1):
        if not line.strip():
            continue
        if ":" not in line:
            raise PlateParseError(line_no, line, "expected 'Name: PLATE, PLATE'")
        name, _, rest = line.partition(":")
        name = name.strip()
        plates = tuple(p for p in (normalise(x) for x in rest.split(",")) if p)
        if not name:
            raise PlateParseError(line_no, line, "missing name")
        if not plates:
            raise PlateParseError(line_no, line, "missing plate")
        people.append(Person(name, plates))
    return people


def patterns_for(person: Person) -> list[tuple[str, str]]:
    """[(plate, pattern), ...] for one person."""
    return [(plate, variant_pattern(plate)) for plate in person.plates]


def levenshtein(a: str, b: str) -> int:
    """Edit distance between two strings."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def plate_matches(pattern: str, plate: str, raw: str, distance: int = 0) -> str | None:
    """How ``raw`` matches ``pattern`` (built from ``plate``), or None."""
    if re.match(f"^{pattern}$", raw or ""):
        return "regex"
    plain = normalise(raw)
    if plain and re.match(f"^{pattern}$", plain):
        return "normalised"
    if distance > 0 and plain and levenshtein(plate, plain) <= distance:
        return "distance"
    return None


def match_person(people: list[Person], raw: str, distance: int = 0) -> Match | None:
    """First person whose plate matches the raw read, in list order."""
    for person in people:
        for plate, pattern in patterns_for(person):
            how = plate_matches(pattern, plate, raw, distance)
            if how:
                return Match(person.name, plate, pattern, how)
    return None


@dataclass(frozen=True)
class NearMiss:
    """A candidate plate that would also match a configured person."""

    candidate: str
    person: str
    plate: str
    how: str


def parse_candidates(text: str) -> list[str]:
    """Comma/newline separated plates, normalised and de-duplicated."""
    out: list[str] = []
    for chunk in re.split(r"[,\n]", text or ""):
        cand = normalise(chunk)
        if cand and cand not in out:
            out.append(cand)
    return out


def near_miss_report(people: list[Person], candidates: list[str], distance: int = 0) -> list[NearMiss]:
    """Which candidates would ALSO match (hits only)."""
    hits: list[NearMiss] = []
    for cand in candidates:
        m = match_person(people, cand, distance)
        if m:
            hits.append(NearMiss(cand, m.person, m.plate, m.how))
    return hits


def render_verdicts(people: list[Person], candidates: list[str], distance: int = 0) -> str:
    """Plain-text summary of the patterns and the near-miss verdicts for the setup screen."""
    lines: list[str] = []
    for person in people:
        for plate, pattern in patterns_for(person):
            lines.append(f"{person.name}: {plate} → pattern `{pattern}`")
    if candidates:
        hits = {h.candidate: h for h in near_miss_report(people, candidates, distance)}
        for cand in candidates:
            if cand in hits:
                h = hits[cand]
                lines.append(f"⚠️ {cand} would ALSO match {h.person} ({h.plate}, by {h.how})")
            else:
                lines.append(f"✅ {cand} does not match (safe)")
    if distance > 0:
        lines.append(
            f"match distance {distance}: plates within {distance} character change(s) also match"
        )
    return "\n".join(lines)


def frigate_lpr_yaml(people: list[Person], *, debug_save_plates: bool = False) -> str:
    """The Frigate ``lpr:`` block for these people.

    ``match_distance`` is 0 on purpose: Frigate's fallback compares the OCR
    string against the regex *text*, so it is useless with patterns, and Plate
    Gate does its own tolerant matching on the raw read anyway.
    """
    import yaml  # HA ships PyYAML; imported lazily so plates.py stays light

    known = {person.name: [pat for _, pat in patterns_for(person)] for person in people}
    lpr: dict = {"enabled": True, "match_distance": 0, "known_plates": known}
    if debug_save_plates:
        lpr["debug_save_plates"] = True
    return yaml.safe_dump({"lpr": lpr}, sort_keys=False, allow_unicode=True, default_flow_style=False)
