# SPDX-FileCopyrightText: Copyright (c) 2025, NVIDIA CORPORATION & AFFILIATES.
# SPDX-License-Identifier: Apache-2.0

"""Utilities for rewriting aggregations."""

from __future__ import annotations

import itertools
from functools import partial
from typing import TYPE_CHECKING

import pyarrow as pa

import pylibcudf as plc

from cudf_polars.dsl import expr, ir

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Sequence

    from cudf_polars.typing import Schema

__all__ = [
    "apply_pre_evaluation",
    "decompose_aggs",
    "decompose_single_agg",
]


def decompose_single_agg(
    named_expr: expr.NamedExpr,
    name_generator: Generator[str, None, None],
    *,
    is_top: bool,
) -> tuple[list[expr.NamedExpr], expr.NamedExpr, bool]:
    """
    Decompose a single named aggregation.

    Parameters
    ----------
    named_expr
        The named aggregation to decompose
    name_generator
        Generator of unique names for temporaries introduced during decomposition.
    is_top
        Is this the top of an aggregation expression?

    Returns
    -------
    aggregations
        Expressions to apply as grouped aggregations (whose children
        may be evaluated pointwise).
    post_aggregate
        Single expression to apply to post-process the grouped
        aggregations.
    is_nested
        Flag indicating whether processing in the inner expression
        itself requires aggregations.

    Raises
    ------
    NotImplementedError
        If the expression contains nested aggregations or unsupported
        operations in a grouped aggregation context.
    """
    agg = named_expr.value
    name = named_expr.name
    if isinstance(agg, expr.Col):
        return [named_expr], named_expr, False
    if isinstance(agg, expr.Len):
        return [named_expr], named_expr.reconstruct(expr.Col(agg.dtype, name)), True
    if isinstance(agg, (expr.Literal, expr.LiteralColumn)):
        return [], named_expr, False
    if isinstance(agg, expr.Agg):
        if agg.name == "quantile":
            # Second child the requested quantile (which is asserted
            # to be a literal on construction)
            child = agg.children[0]
        else:
            (child,) = agg.children
        needs_masking = agg.name in {"min", "max"} and plc.traits.is_floating_point(
            child.dtype
        )
        if needs_masking and agg.options:
            # pl.col("a").nan_max or nan_min
            raise NotImplementedError("Nan propagation in groupby for min/max")
        _, _, has_agg = decompose_single_agg(
            expr.NamedExpr(next(name_generator), child), name_generator, is_top=False
        )
        if has_agg:
            raise NotImplementedError("Nested aggs in groupby not supported")
        if needs_masking:
            child = expr.UnaryFunction(child.dtype, "mask_nans", (), child)
            # The aggregation is just reconstructed with the new
            # (potentially masked) child. This is safe because we recursed
            # to ensure there are no nested aggregations.
            return (
                [named_expr.reconstruct(agg.reconstruct([child]))],
                named_expr.reconstruct(expr.Col(agg.dtype, name)),
                True,
            )
        elif agg.name == "sum":
            col = (
                expr.Cast(agg.dtype, expr.Col(plc.DataType(plc.TypeId.INT64), name))
                if (
                    plc.traits.is_integral(agg.dtype)
                    and agg.dtype.id() != plc.TypeId.INT64
                )
                else expr.Col(agg.dtype, name)
            )
            if is_top:
                # In polars sum(empty_group) => 0, but in libcudf sum(empty_group) => null
                # So must post-process by replacing nulls, but only if we're a "top-level" agg.
                rep = expr.Literal(
                    agg.dtype, pa.scalar(0, type=plc.interop.to_arrow(agg.dtype))
                )
                return (
                    [named_expr],
                    named_expr.reconstruct(
                        expr.UnaryFunction(agg.dtype, "fill_null", (), col, rep)
                    ),
                    True,
                )
            else:
                return [named_expr], expr.NamedExpr(name, col), True
        else:
            return [named_expr], named_expr.reconstruct(expr.Col(agg.dtype, name)), True
    if isinstance(agg, expr.Ternary):
        raise NotImplementedError("Ternary inside groupby")
    if agg.is_pointwise:
        aggs, posts, has_aggs = _decompose_aggs(
            (expr.NamedExpr(next(name_generator), child) for child in agg.children),
            name_generator,
            is_top=False,
        )
        if any(has_aggs):
            # Any pointwise expression can be handled either by
            # post-evaluation (if outside an aggregation).
            return (
                aggs,
                named_expr.reconstruct(agg.reconstruct([p.value for p in posts])),
                True,
            )
        else:
            # Or pre-evaluation if inside an aggregation.
            return (
                [named_expr],
                named_expr.reconstruct(expr.Col(agg.dtype, name)),
                False,
            )
    raise NotImplementedError(f"No support for {type(agg)} in groupby")


def _decompose_aggs(
    aggs: Iterable[expr.NamedExpr],
    name_generator: Generator[str, None, None],
    *,
    is_top: bool,
) -> tuple[list[expr.NamedExpr], Sequence[expr.NamedExpr], Sequence[bool]]:
    new_aggs, post, has_aggs = zip(
        *(decompose_single_agg(agg, name_generator, is_top=is_top) for agg in aggs),
        strict=True,
    )
    return (
        list(itertools.chain.from_iterable(new_aggs)),
        post,
        has_aggs,
    )


def decompose_aggs(
    aggs: Iterable[expr.NamedExpr], name_generator: Generator[str, None, None]
) -> tuple[list[expr.NamedExpr], Sequence[expr.NamedExpr]]:
    """
    Process arbitrary aggregations into a form we can handle in grouped aggregations.

    Parameters
    ----------
    aggs
        List of aggregation expressions
    name_generator
        Generator of unique names for temporaries introduced during decomposition.

    Returns
    -------
    aggregations
        Aggregations to apply in the groupby node.
    post_aggregations
        Expressions to apply after aggregating (as a ``Select``).

    Notes
    -----
    The aggregation expressions are guaranteed to either be
    expressions that can be pointwise evaluated before the groupby
    operation, or aggregations of such expressions.

    Raises
    ------
    NotImplementedError
        For unsupported aggregation combinations.
    """
    new_aggs, post, _ = _decompose_aggs(aggs, name_generator, is_top=True)
    return new_aggs, post


def apply_pre_evaluation(
    output_schema: Schema,
    inp: ir.IR,
    keys: Sequence[expr.NamedExpr],
    original_aggs: Sequence[expr.NamedExpr],
    name_generator: Generator[str, None, None],
    *extra_columns: expr.NamedExpr,
) -> tuple[ir.IR, Sequence[expr.NamedExpr], Schema, Callable[[ir.IR], ir.IR]]:
    """
    Apply pre-evaluation to aggregations in a grouped or rolling context.

    Parameters
    ----------
    output_schema
        Schema of the plan node we're rewriting.
    inp
        The input to the grouped/rolling aggregation.
    keys
        Grouping keys (may be empty).
    original_aggs
        Aggregation expressions to rewrite.
    name_generator
        Generator of unique names for temporaries introduced during decomposition.
    extra_columns
        Any additional columns to be included in the output (only
        relevant for rolling aggregations). Columns will appear in the
        order `keys, extra_columns, original_aggs`.

    Returns
    -------
    new_input
        Rewritten input, suitable as input to the aggregation node
    aggregations
        The required aggregations.
    schema
        The new schema of the aggregation node
    post_process
        Function to apply to the aggregation node to apply any
        post-processing.

    Raises
    ------
    NotImplementedError
        If the aggregations are somehow unsupported.
    """
    aggs, post = decompose_aggs(original_aggs, name_generator)
    assert len(post) == len(original_aggs), (
        f"Unexpected number of post-aggs {len(post)=} {len(original_aggs)=}"
    )
    # Order-preserving unique
    aggs = list(dict.fromkeys(aggs).keys())
    if any(not isinstance(e.value, expr.Col) for e in post):
        selection = [
            *(key.reconstruct(expr.Col(key.value.dtype, key.name)) for key in keys),
            *extra_columns,
            *post,
        ]
        inter_schema = {
            e.name: e.value.dtype for e in itertools.chain(keys, extra_columns, aggs)
        }
        return (
            inp,
            aggs,
            inter_schema,
            partial(ir.Select, output_schema, selection, True),  # noqa: FBT003
        )
    else:
        return inp, aggs, output_schema, lambda inp: inp
