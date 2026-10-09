"""Tests for the pure plate helpers."""
import re

import pytest

from custom_components.electrifix_plate_gate import plates as p


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("XO520", "XO520"),
        ("XO ·520", "XO520"),
        ("xo-520", "XO520"),
        ("X.O.5.2.0", "XO520"),
        ("XO•520", "XO520"),
        (" 1abc 123 ", "1ABC123"),
        ("", ""),
        ("··", ""),
    ],
)
def test_normalise(raw, expected):
    assert p.normalise(raw) == expected


def test_variant_pattern_covers_confusables_and_separators():
    pat = p.variant_pattern("XO520")
    assert pat == "X[ .·•\\-]*[O0][ .·•\\-]*[5S][ .·•\\-]*[2Z][ .·•\\-]*[0O]"


def test_variant_pattern_rejects_empty():
    with pytest.raises(ValueError):
        p.variant_pattern(" · ")


@pytest.mark.parametrize("raw", ["XO520", "XO ·520", "X0520", "XO52O", "X0-52O", "X0 5 2 0"])
def test_pattern_matches_frigate_style_raw(raw):
    assert re.match(f"^{p.variant_pattern('XO520')}$", raw)


@pytest.mark.parametrize("raw", ["LO120", "XO540", "MO520", "XO5200", "O860"])
def test_pattern_rejects_near_misses(raw):
    assert not re.match(f"^{p.variant_pattern('XO520')}$", raw)


def test_parse_people_basic_and_messy():
    people = p.parse_people("Alex: XO520, 1ABC123\n\n alex two : xo 520 \n")
    assert people == [
        p.Person("Alex", ("XO520", "1ABC123")),
        p.Person("alex two", ("XO520",)),
    ]


@pytest.mark.parametrize("text", ["Alex", "Alex:", ": XO520", "Alex: ,"])
def test_parse_people_errors_carry_line(text):
    with pytest.raises(p.PlateParseError) as e:
        p.parse_people("ok: AAA111\n" + text)
    assert e.value.line_no == 2


def test_levenshtein():
    assert p.levenshtein("XO520", "XO540") == 1
    assert p.levenshtein("XO520", "LO120") == 2
    assert p.levenshtein("", "ab") == 2


def test_plate_matches_how():
    pat = p.variant_pattern("XO520")
    assert p.plate_matches(pat, "XO520", "XO ·520") == "regex"
    assert p.plate_matches(pat, "XO520", "XO540") is None
    assert p.plate_matches(pat, "XO520", "XO540", distance=1) == "distance"
    assert p.plate_matches(pat, "XO520", "LO120", distance=1) is None


def test_plate_matches_normalised_when_raw_has_odd_separator():
    pat = p.variant_pattern("XO520")
    assert p.plate_matches(pat, "XO520", "XO_520") == "normalised"


def test_match_person_first_hit_wins_and_reports():
    people = p.parse_people("Alex: XO520\nSam: LO120")
    m = p.match_person(people, "L0 120")
    assert m == p.Match("Sam", "LO120", p.variant_pattern("LO120"), "regex")
    assert p.match_person(people, "XYZ999") is None


def test_near_miss_report_nicks_history():
    people = p.parse_people("Alex: XO520")
    cands = p.parse_candidates("LO120, XO540, MO520")
    assert p.near_miss_report(people, cands, 0) == []
    hits = p.near_miss_report(people, cands, 1)
    assert [(h.candidate, h.how) for h in hits] == [("XO540", "distance"), ("MO520", "distance")]


def test_parse_candidates_dedupes_and_normalises():
    assert p.parse_candidates("lo 120,\nXO-540, LO120, ,") == ["LO120", "XO540"]


def test_render_verdicts_mentions_each_candidate():
    people = p.parse_people("Alex: XO520")
    text = p.render_verdicts(people, ["LO120", "XO540"], 1)
    assert "XO520" in text and "LO120" in text and "XO540" in text
    assert "would ALSO" in text and "safe" in text.lower()


def test_frigate_yaml_round_trips():
    import yaml

    people = p.parse_people("Alex: XO520\nSam: 1ABC123, LO120")
    doc = yaml.safe_load(p.frigate_lpr_yaml(people, debug_save_plates=True))
    assert doc["lpr"]["enabled"] is True
    assert doc["lpr"]["match_distance"] == 0
    assert doc["lpr"]["debug_save_plates"] is True
    assert doc["lpr"]["known_plates"]["Alex"] == [p.variant_pattern("XO520")]
    assert len(doc["lpr"]["known_plates"]["Sam"]) == 2


def test_frigate_yaml_omits_debug_by_default():
    import yaml

    doc = yaml.safe_load(p.frigate_lpr_yaml(p.parse_people("Alex: XO520")))
    assert "debug_save_plates" not in doc["lpr"]
