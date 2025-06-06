# Copyright (c) 2024-2025, NVIDIA CORPORATION.

import pyarrow as pa
import pyarrow.compute as pc
import pytest
from utils import assert_column_eq

import pylibcudf as plc


@pytest.fixture(scope="module")
def target_col():
    pa_array = pa.array(
        ["AbC", "de", "FGHI", "j", "kLm", "nOPq", None, "RsT", None, "uVw"]
    )
    return pa_array, plc.Column(pa_array)


@pytest.fixture(
    params=[
        "A",
        "de",
        ".*",
        "^a",
        "^A",
        "[^a-z]",
        "[a-z]{3,}",
        "^[A-Z]{2,}",
        "j|u",
    ],
    scope="module",
)
def pa_target_scalar(request):
    return pa.scalar(request.param, type=pa.string())


@pytest.fixture(scope="module")
def plc_target_pat(pa_target_scalar):
    prog = plc.strings.regex_program.RegexProgram.create(
        pa_target_scalar.as_py(), plc.strings.regex_flags.RegexFlags.DEFAULT
    )
    return prog


def test_contains_re(target_col, pa_target_scalar, plc_target_pat):
    pa_target_col, plc_target_col = target_col
    got = plc.strings.contains.contains_re(plc_target_col, plc_target_pat)
    expect = pc.match_substring_regex(pa_target_col, pa_target_scalar.as_py())
    assert_column_eq(expect, got)


def test_count_re():
    pattern = "[1-9][a-z]"
    arr = pa.array(["A1a2A3a4", "A1A2A3", None])
    got = plc.strings.contains.count_re(
        plc.Column(arr),
        plc.strings.regex_program.RegexProgram.create(
            pattern, plc.strings.regex_flags.RegexFlags.DEFAULT
        ),
    )
    expect = pc.count_substring_regex(arr, pattern)
    assert_column_eq(expect, got)


def test_match_re():
    pattern = "[1-9][a-z]"
    arr = pa.array(["1a2b", "b1a2", None])
    got = plc.strings.contains.matches_re(
        plc.Column(arr),
        plc.strings.regex_program.RegexProgram.create(
            pattern, plc.strings.regex_flags.RegexFlags.DEFAULT
        ),
    )
    expect = pc.match_substring_regex(arr, f"^{pattern}")
    assert_column_eq(expect, got)


def test_like():
    pattern = "%a"
    arr = pa.array(["1a2aa3aaa"])
    got = plc.strings.contains.like(
        plc.Column(arr),
        plc.Column(pa.array([pattern])),
    )
    expect = pc.match_like(arr, pattern)
    assert_column_eq(expect, got)
