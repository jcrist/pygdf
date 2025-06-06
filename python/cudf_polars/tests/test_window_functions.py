# SPDX-FileCopyrightText: Copyright (c) 2025, NVIDIA CORPORATION & AFFILIATES.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import polars as pl

from cudf_polars.testing.asserts import assert_gpu_result_equal


@pytest.fixture
def df():
    start_date = datetime(2023, 1, 1)
    dates = [start_date + timedelta(days=i) for i in range(6)]

    return pl.LazyFrame(
        {
            "date": dates,
            "a": ["a", "a", "b", "b", "b", "c"],
            "b": [1, 2, 3, 1, 3, 2],
            "c": [7, 4, 3, 2, 3, 2],
            "d": [1.0, 2, 3, 4, 5, 6],
        }
    )


@pytest.fixture(
    params=[
        pl.col("a"),
        pl.col("b"),
        [pl.col("a"), pl.col("b")],
        pl.col("b") + pl.col("c"),
    ],
    ids=lambda key: str(key),
)
def partition_by(request):
    return request.param


@pytest.fixture(
    params=[
        pl.col("b").max(),
        pl.col("b").min(),
        pl.col("b").sum(),
        pl.col("b") + pl.col("c").sum(),
        pl.col("b").cum_sum(),
        pl.col("b").rank(),
        pl.col("b").rank(method="dense"),
        pl.col("b").count(),
        pl.col("b").n_unique(),
        pl.col("b").first(),
        pl.col("b").last(),
        pl.col("b").sum(),
        pl.col("b").mean(),
        pl.col("b").median(),
        pl.col("b").std(),
        pl.col("b").var(),
        pl.col("b").quantile(0.5),
    ],
    ids=lambda agg: str(agg),
)
def agg_expr(request):
    return request.param


@pytest.mark.xfail(reason="Window functions are not implemented in cudf-polars")
def test_over(df: pl.LazyFrame, partition_by, agg_expr):
    """Test window functions over partitions."""

    window_expr = agg_expr.over(partition_by)

    result_name = f"{agg_expr!s}_over_{partition_by!s}"
    window_expr = window_expr.alias(result_name)

    query = df.with_columns(window_expr)

    assert_gpu_result_equal(query)


@pytest.mark.xfail(reason="Window functions are not implemented in cudf-polars")
def test_over_with_sort(df: pl.LazyFrame):
    """Test window functions with sorting."""
    query = df.with_columns([pl.col("c").rank().sort().over(pl.col("a"))])
    assert_gpu_result_equal(query)


@pytest.mark.parametrize(
    "mapping_strategy",
    ["group_to_rows", "explode", "join"],
    ids=lambda x: f"mapping_{x}",
)
@pytest.mark.xfail(reason="Window functions are not implemented in cudf-polars")
def test_over_mapping_strategy(df: pl.LazyFrame, mapping_strategy: str):
    """Test window functions with different mapping strategies."""
    query = df.with_columns(
        [pl.col("b").rank().over(pl.col("a"), mapping_strategy=mapping_strategy)]
    )
    assert_gpu_result_equal(query)


@pytest.mark.xfail(reason="Window functions are not implemented in cudf-polars")
@pytest.mark.parametrize("period", ["2d", "3d"])
def test_rolling(df: pl.LazyFrame, agg_expr, period: str):
    """Test rolling window functions over time series."""
    window_expr = agg_expr.rolling(period=period, index_column="date")
    result_name = f"{agg_expr!s}_rolling_{period}"
    window_expr = window_expr.alias(result_name)

    query = df.with_columns(window_expr)

    assert_gpu_result_equal(query)


@pytest.mark.xfail(reason="Window functions are not implemented in cudf-polars")
@pytest.mark.parametrize(
    "closed",
    ["left", "right", "both", "none"],
    ids=lambda x: f"closed_{x}",
)
def test_rolling_closed(df: pl.LazyFrame, closed: str):
    """Test rolling window functions with different closed parameters."""
    query = df.with_columns(
        [pl.col("b").sum().rolling(period="2d", index_column="date", closed=closed)]
    )
    assert_gpu_result_equal(query)
