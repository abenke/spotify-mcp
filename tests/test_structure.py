"""Tests for chorus/repeated-section detection from synced lyrics."""

from spotify_mcp.structure import analyze_structure, parse_lrc


def _lrc_line(seconds: float, text: str) -> str:
    return f"[{int(seconds // 60):02d}:{seconds % 60:05.2f}]{text}"


def _build_song() -> str:
    """A typical pop/dance layout with three 14-second choruses.

    0:10 verse 1 (8 lines) -> 0:38 chorus -> 0:52 post-chorus hook ->
    1:00 verse 2 -> 1:28 chorus -> 1:42 post-chorus hook -> 1:50 bridge ->
    2:04 chorus (no hook after) -> 2:18 outro. Track is 150 s long.
    """
    verse1 = [
        "Woke up with the city lights still burning",
        "Every street was calling out my name",
        "I could feel the world beneath me turning",
        "Nothing here will ever look the same",
        "All the doors I never dared to open",
        "All the words I never dared to say",
        "Every promise that I left unspoken",
        "Disappearing with the light of day",
    ]
    verse2 = [
        "Shadows falling but I keep on moving",
        "Every heartbeat drumming like a war",
        "There is nothing left for me to lose here",
        "I'm not waiting on the ground no more",
        "All the maps I drew when I was younger",
        "All the roads that never led me home",
        "I can feel it now, a kind of hunger",
        "Telling me I'll never walk alone",
    ]
    chorus = [
        "Feel the fire in the night",
        "We are never coming down",
        "Hold on to the light",
        "Louder now, louder now",
    ]
    chorus_variant = [
        "Feel the fire in the night",
        "We are never coming down",
        "Hold on to the light (oh)",  # small ad-lib: still the same chorus
        "Louder now, louder now",
    ]
    hook = [
        "Na na na, na na na",
        "Never coming down, down",
    ]
    bridge = [
        "When the morning finds us",
        "We won't be the same",
        "Let the rhythm blind us",
        "Calling out your name",
    ]

    def block(start: float, lines: list[str], spacing: float = 3.5) -> list[str]:
        return [_lrc_line(start + k * spacing, text) for k, text in enumerate(lines)]

    out: list[str] = ["[ar:Test Artist]", "[ti:Test Song]"]
    out += block(10.0, verse1)  # 0:10 - 0:38
    out += block(38.0, chorus)  # 0:38 - 0:52 (next block starts at 52 -> 14 s)
    out += block(52.0, hook, spacing=4.0)  # 0:52 - 1:00 (8 s)
    out += block(60.0, verse2)  # 1:00 - 1:28
    out += block(88.0, chorus)  # 1:28 - 1:42 (14 s)
    out += block(102.0, hook, spacing=4.0)  # 1:42 - 1:50 (8 s)
    out += block(110.0, bridge)  # 1:50 - 2:04
    out += block(124.0, chorus_variant)  # 2:04 - 2:18 (14 s)
    out += [_lrc_line(138.0, "City lights still burning bright")]  # outro
    return "\n".join(out)


def test_parse_lrc_basics():
    lines = parse_lrc(
        "[ar:Artist]\n"
        "[00:10.00]First line\n"
        "[00:20.50][01:05.00]Repeated line\n"
        "[00:30.00]\n"
        "no timestamp here\n"
        "[00:40.00]<00:40.10>Word <00:41.00>tags stripped\n"
    )
    assert lines == [
        (10.0, "First line"),
        (20.5, "Repeated line"),
        (30.0, ""),
        (40.0, "Word tags stripped"),
        (65.0, "Repeated line"),
    ]


def test_finds_three_choruses_of_14_seconds():
    lines = parse_lrc(_build_song())
    result = analyze_structure(lines, track_duration_s=150.0)

    assert result["synced"] is True
    chorus = result["sections"][0]
    assert chorus["label"] == "chorus"
    assert chorus["occurrence_count"] == 3
    assert chorus["durations_seconds"] == [14, 14, 14]
    assert chorus["occurrences"][0]["start"] == "0:38"
    assert chorus["occurrences"][1]["start"] == "1:28"
    assert chorus["occurrences"][2]["start"] == "2:04"
    assert "Feel the fire in the night" in chorus["sample_lines"]
    # Gap from end of chorus 1 (0:52) to start of chorus 2 (1:28) is 36 s,
    # and from end of chorus 2 (1:42) to chorus 3 (2:04) is 22 s.
    assert chorus["gaps_between_seconds"] == [36, 22]


def test_post_chorus_hook_is_a_separate_sub_chorus():
    lines = parse_lrc(_build_song())
    result = analyze_structure(lines, track_duration_s=150.0)

    labels = [s["label"] for s in result["sections"]]
    assert labels[0] == "chorus"
    assert "sub-chorus 1" in labels
    hook = next(s for s in result["sections"] if s["label"] == "sub-chorus 1")
    assert hook["occurrence_count"] == 2
    assert hook["durations_seconds"] == [8, 8]
    assert hook["occurrences"][0]["start"] == "0:52"


def test_timeline_covers_song_in_order():
    lines = parse_lrc(_build_song())
    result = analyze_structure(lines, track_duration_s=150.0)

    timeline = result["timeline"]
    assert timeline[0]["label"] == "other (intro/verse)"
    assert timeline[0]["start"] == "0:00"
    labels = [entry["label"] for entry in timeline]
    assert labels.count("chorus") == 3
    assert labels[-1] == "other (outro)"
    starts = [entry["start"] for entry in timeline]
    assert starts == sorted(starts, key=lambda s: int(s.split(":")[0]) * 60 + int(s.split(":")[1]))


def test_final_chorus_uses_track_duration_when_song_ends_on_it():
    chorus = ["Feel the fire", "Never coming down", "Hold the light", "Louder now"]
    verse = ["First verse line one", "First verse line two", "Verses do not repeat", "So they are not sections"]
    parts = []
    for k, text in enumerate(verse):
        parts.append(_lrc_line(5 + k * 3.5, text))
    for k, text in enumerate(chorus):
        parts.append(_lrc_line(20 + k * 3.5, text))
    for k, text in enumerate(chorus):
        parts.append(_lrc_line(34 + k * 3.5, text))  # song ends on this chorus
    lines = parse_lrc("\n".join(parts))

    result = analyze_structure(lines, track_duration_s=48.0)
    chorus_section = result["sections"][0]
    assert chorus_section["durations_seconds"] == [14, 14]
    assert chorus_section["occurrences"][-1]["end"] == "0:48"


def test_back_to_back_double_chorus_counts_twice():
    chorus = ["Feel the fire", "Never coming down", "Hold the light", "Louder now"]
    verse = ["Something about the morning", "Something about the rain", "Something about the evening", "Something about the pain"]
    parts = []
    for k, text in enumerate(verse):
        parts.append(_lrc_line(5 + k * 3.5, text))
    for k, text in enumerate(chorus):
        parts.append(_lrc_line(20 + k * 3.5, text))
    for k, text in enumerate(chorus):  # double chorus at the end
        parts.append(_lrc_line(34 + k * 3.5, text))
    for k, text in enumerate(chorus):
        parts.append(_lrc_line(48 + k * 3.5, text))
    lines = parse_lrc("\n".join(parts))

    result = analyze_structure(lines, track_duration_s=62.0)
    chorus_section = result["sections"][0]
    assert chorus_section["occurrence_count"] == 3
    assert chorus_section["durations_seconds"] == [14, 14, 14]


def test_plain_lyrics_count_sections_without_timing():
    text_lines = (
        ["Verse one line %d" % k for k in range(4)]
        + ["Chorus line one", "Chorus line two", "Chorus line three"]
        + ["Verse two other words %d" % k for k in range(4)]
        + ["Chorus line one", "Chorus line two", "Chorus line three"]
    )
    lines = [(None, t) for t in text_lines]
    result = analyze_structure(lines)

    assert result["synced"] is False
    assert "timeline" not in result
    chorus = result["sections"][0]
    assert chorus["occurrence_count"] == 2
    assert "durations_seconds" not in chorus
    assert "no timing available" in chorus["summary"]


def test_no_repeats_yields_no_sections():
    texts = [
        "Golden rivers in December",
        "Nobody asked the lonely captain",
        "Fifteen sparrows on a wire",
        "The kitchen smells of cinnamon",
        "Trains departing after midnight",
        "Her umbrella turned to seaweed",
        "Counting backwards from a thousand",
        "Static on the old transistor",
        "A photograph of someone's father",
        "Wolves are quiet in the summer",
        "Paper boats along the gutter",
        "Everything dissolves eventually",
    ]
    lines = [(float(k * 4), text) for k, text in enumerate(texts)]
    result = analyze_structure(lines)
    assert result["sections"] == []
