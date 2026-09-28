"""Tests for the pure plate helpers."""
import re

import pytest

from custom_components.electrifix_plate_gate import plates as p


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("NO860", "NO860"),
        ("NO ·860", "NO860"),
        ("no-860", "NO860"),
        ("N.O.8.6.0", "NO860"),
        ("NO•860", "NO860"),
        (" 1abc 123 ", "1ABC123"),
        ("", ""),
        ("··", ""),
    ],
)
def test_normalise(raw, expected):
    assert p.normalise(raw) == expected


def test_variant_pattern_covers_confusables_and_separators():
    pat = p.variant_pattern("NO860")
    assert pat == "N[ .·•\\-]*[O0][ .·•\\-]*[8B][ .·•\\-]*[6G][ .·•\\-]*[0O]"


def test_variant_pattern_rejects_empty():
    with pytest.raises(ValueError):
        p.variant_pattern(" · ")


@pytest.mark.parametrize("raw", ["NO860", "NO ·860", "N0860", "NO86O", "N0-86O", "N0 8 6 0"])
def test_pattern_matches_frigate_style_raw(raw):
    assert re.match(f"^{p.variant_pattern('NO860')}$", raw)


@pytest.mark.parametrize("raw", ["LO160", "NO840", "MO860", "NO8600", "O860"])
def test_pattern_rejects_near_misses(raw):
    assert not re.match(f"^{p.variant_pattern('NO860')}$", raw)


def test_parse_people_basic_and_messy():
    people = p.parse_people("Bonnie: NO860, 1ABC123\n\n bonnie two : no 860 \n")
    assert people == [
        p.Person("Bonnie", ("NO860", "1ABC123")),
        p.Person("bonnie two", ("NO860",)),
    ]


@pytest.mark.parametrize("text", ["Bonnie", "Bonnie:", ": NO860", "Bonnie: ,"])
def test_parse_people_errors_carry_line(text):
    with pytest.raises(p.PlateParseError) as e:
        p.parse_people("ok: AAA111\n" + text)
    assert e.value.line_no == 2


def test_levenshtein():
    assert p.levenshtein("NO860", "NO840") == 1
    assert p.levenshtein("NO860", "LO160") == 2
    assert p.levenshtein("", "ab") == 2


def test_plate_matches_how():
    pat = p.variant_pattern("NO860")
    assert p.plate_matches(pat, "NO860", "NO ·860") == "regex"
    assert p.plate_matches(pat, "NO860", "NO840") is None
    assert p.plate_matches(pat, "NO860", "NO840", distance=1) == "distance"
    assert p.plate_matches(pat, "NO860", "LO160", distance=1) is None


def test_plate_matches_normalised_when_raw_has_odd_separator():
    pat = p.variant_pattern("NO860")
    assert p.plate_matches(pat, "NO860", "NO_860") == "normalised"


def test_match_person_first_hit_wins_and_reports():
    people = p.parse_people("Bonnie: NO860\nSam: LO160")
    m = p.match_person(people, "L0 160")
    assert m == p.Match("Sam", "LO160", p.variant_pattern("LO160"), "regex")
    assert p.match_person(people, "XYZ999") is None


def test_near_miss_report_nicks_history():
    people = p.parse_people("Bonnie: NO860")
    cands = p.parse_candidates("LO160, NO840, MO860")
    assert p.near_miss_report(people, cands, 0) == []
    hits = p.near_miss_report(people, cands, 1)
    assert [(h.candidate, h.how) for h in hits] == [("NO840", "distance"), ("MO860", "distance")]


def test_parse_candidates_dedupes_and_normalises():
    assert p.parse_candidates("lo 160,\nNO-840, LO160, ,") == ["LO160", "NO840"]


def test_render_verdicts_mentions_each_candidate():
    people = p.parse_people("Bonnie: NO860")
    text = p.render_verdicts(people, ["LO160", "NO840"], 1)
    assert "NO860" in text and "LO160" in text and "NO840" in text
    assert "would ALSO" in text and "safe" in text.lower()


def test_frigate_yaml_round_trips():
    import yaml

    people = p.parse_people("Bonnie: NO860\nSam: 1ABC123, LO160")
    doc = yaml.safe_load(p.frigate_lpr_yaml(people, debug_save_plates=True))
    assert doc["lpr"]["enabled"] is True
    assert doc["lpr"]["match_distance"] == 0
    assert doc["lpr"]["debug_save_plates"] is True
    assert doc["lpr"]["known_plates"]["Bonnie"] == [p.variant_pattern("NO860")]
    assert len(doc["lpr"]["known_plates"]["Sam"]) == 2


def test_frigate_yaml_omits_debug_by_default():
    import yaml

    doc = yaml.safe_load(p.frigate_lpr_yaml(p.parse_people("Bonnie: NO860")))
    assert "debug_save_plates" not in doc["lpr"]
