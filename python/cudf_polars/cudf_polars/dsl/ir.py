# SPDX-FileCopyrightText: Copyright (c) 2024-2025, NVIDIA CORPORATION & AFFILIATES.
# SPDX-License-Identifier: Apache-2.0
"""
DSL nodes for the LogicalPlan of polars.

An IR node is either a source, normal, or a sink. Respectively they
can be considered as functions:

- source: `IO () -> DataFrame`
- normal: `DataFrame -> DataFrame`
- sink: `DataFrame -> IO ()`
"""

from __future__ import annotations

import itertools
import json
import random
import time
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import pyarrow as pa
from typing_extensions import assert_never

import polars as pl

import pylibcudf as plc

import cudf_polars.dsl.expr as expr
from cudf_polars.containers import Column, DataFrame
from cudf_polars.dsl.nodebase import Node
from cudf_polars.dsl.to_ast import to_ast, to_parquet_filter
from cudf_polars.utils import dtypes
from cudf_polars.utils.versions import POLARS_VERSION_LT_128

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable, Iterable, Sequence
    from typing import Literal

    from typing_extensions import Self

    from polars.polars import _expr_nodes as pl_expr

    from cudf_polars.typing import CSECache, Schema, Slice as Zlice
    from cudf_polars.utils.config import ConfigOptions
    from cudf_polars.utils.timer import Timer


__all__ = [
    "IR",
    "Cache",
    "ConditionalJoin",
    "DataFrameScan",
    "Distinct",
    "Empty",
    "ErrorNode",
    "Filter",
    "GroupBy",
    "HConcat",
    "HStack",
    "Join",
    "MapFunction",
    "Projection",
    "PythonScan",
    "Scan",
    "Select",
    "Slice",
    "Sort",
    "Union",
]


def broadcast(*columns: Column, target_length: int | None = None) -> list[Column]:
    """
    Broadcast a sequence of columns to a common length.

    Parameters
    ----------
    columns
        Columns to broadcast.
    target_length
        Optional length to broadcast to. If not provided, uses the
        non-unit length of existing columns.

    Returns
    -------
    List of broadcasted columns all of the same length.

    Raises
    ------
    RuntimeError
        If broadcasting is not possible.

    Notes
    -----
    In evaluation of a set of expressions, polars type-puns length-1
    columns with scalars. When we insert these into a DataFrame
    object, we need to ensure they are of equal length. This function
    takes some columns, some of which may be length-1 and ensures that
    all length-1 columns are broadcast to the length of the others.

    Broadcasting is only possible if the set of lengths of the input
    columns is a subset of ``{1, n}`` for some (fixed) ``n``. If
    ``target_length`` is provided and not all columns are length-1
    (i.e. ``n != 1``), then ``target_length`` must be equal to ``n``.
    """
    if len(columns) == 0:
        return []
    lengths: set[int] = {column.size for column in columns}
    if lengths == {1}:
        if target_length is None:
            return list(columns)
        nrows = target_length
    else:
        try:
            (nrows,) = lengths.difference([1])
        except ValueError as e:
            raise RuntimeError("Mismatching column lengths") from e
        if target_length is not None and nrows != target_length:
            raise RuntimeError(
                f"Cannot broadcast columns of length {nrows=} to {target_length=}"
            )
    return [
        column
        if column.size != 1
        else Column(
            plc.Column.from_scalar(column.obj_scalar, nrows),
            is_sorted=plc.types.Sorted.YES,
            order=plc.types.Order.ASCENDING,
            null_order=plc.types.NullOrder.BEFORE,
            name=column.name,
        )
        for column in columns
    ]


class IR(Node["IR"]):
    """Abstract plan node, representing an unevaluated dataframe."""

    __slots__ = ("_non_child_args", "schema")
    # This annotation is needed because of https://github.com/python/mypy/issues/17981
    _non_child: ClassVar[tuple[str, ...]] = ("schema",)
    # Concrete classes should set this up with the arguments that will
    # be passed to do_evaluate.
    _non_child_args: tuple[Any, ...]
    schema: Schema
    """Mapping from column names to their data types."""

    def get_hashable(self) -> Hashable:
        """
        Hashable representation of node, treating schema dictionary.

        Since the schema is a dictionary, even though it is morally
        immutable, it is not hashable. We therefore convert it to
        tuples for hashing purposes.
        """
        # Schema is the first constructor argument
        args = self._ctor_arguments(self.children)[1:]
        schema_hash = tuple(self.schema.items())
        return (type(self), schema_hash, args)

    # Hacky to avoid type-checking issues, just advertise the
    # signature. Both mypy and pyright complain if we have an abstract
    # method that takes arbitrary *args, but the subclasses have
    # tighter signatures. This complaint is correct because the
    # subclass is not Liskov-substitutable for the superclass.
    # However, we know do_evaluate will only be called with the
    # correct arguments by "construction".
    do_evaluate: Callable[..., DataFrame]
    """
    Evaluate the node (given its evaluated children), and return a dataframe.

    Parameters
    ----------
    args
        Non child arguments followed by any evaluated dataframe inputs.

    Returns
    -------
    DataFrame (on device) representing the evaluation of this plan
    node.

    Raises
    ------
    NotImplementedError
        If evaluation fails. Ideally this should not occur, since the
        translation phase should fail earlier.
    """

    def evaluate(self, *, cache: CSECache, timer: Timer | None) -> DataFrame:
        """
        Evaluate the node (recursively) and return a dataframe.

        Parameters
        ----------
        cache
            Mapping from cached node ids to constructed DataFrames.
            Used to implement evaluation of the `Cache` node.
        timer
            If not None, a Timer object to record timings for the
            evaluation of the node.

        Notes
        -----
        Prefer not to override this method. Instead implement
        :meth:`do_evaluate` which doesn't encode a recursion scheme
        and just assumes already evaluated inputs.

        Returns
        -------
        DataFrame (on device) representing the evaluation of this plan
        node (and its children).

        Raises
        ------
        NotImplementedError
            If evaluation fails. Ideally this should not occur, since the
            translation phase should fail earlier.
        """
        children = [child.evaluate(cache=cache, timer=timer) for child in self.children]
        if timer is not None:
            start = time.monotonic_ns()
            result = self.do_evaluate(*self._non_child_args, *children)
            end = time.monotonic_ns()
            # TODO: Set better names on each class object.
            timer.store(start, end, type(self).__name__)
            return result
        else:
            return self.do_evaluate(*self._non_child_args, *children)


class ErrorNode(IR):
    """Represents an error translating the IR."""

    __slots__ = ("error",)
    _non_child = (
        "schema",
        "error",
    )
    error: str
    """The error."""

    def __init__(self, schema: Schema, error: str):
        self.schema = schema
        self.error = error
        self.children = ()


class PythonScan(IR):
    """Representation of input from a python function."""

    __slots__ = ("options", "predicate")
    _non_child = ("schema", "options", "predicate")
    options: Any
    """Arbitrary options."""
    predicate: expr.NamedExpr | None
    """Filter to apply to the constructed dataframe before returning it."""

    def __init__(self, schema: Schema, options: Any, predicate: expr.NamedExpr | None):
        self.schema = schema
        self.options = options
        self.predicate = predicate
        self._non_child_args = (schema, options, predicate)
        self.children = ()
        raise NotImplementedError("PythonScan not implemented")


class Scan(IR):
    """Input from files."""

    __slots__ = (
        "cloud_options",
        "config_options",
        "include_file_paths",
        "n_rows",
        "paths",
        "predicate",
        "reader_options",
        "row_index",
        "skip_rows",
        "typ",
        "with_columns",
    )
    _non_child = (
        "schema",
        "typ",
        "reader_options",
        "cloud_options",
        "config_options",
        "paths",
        "with_columns",
        "skip_rows",
        "n_rows",
        "row_index",
        "include_file_paths",
        "predicate",
    )
    typ: str
    """What type of file are we reading? Parquet, CSV, etc..."""
    reader_options: dict[str, Any]
    """Reader-specific options, as dictionary."""
    cloud_options: dict[str, Any] | None
    """Cloud-related authentication options, currently ignored."""
    config_options: ConfigOptions
    """GPU-specific configuration options"""
    paths: list[str]
    """List of paths to read from."""
    with_columns: list[str] | None
    """Projected columns to return."""
    skip_rows: int
    """Rows to skip at the start when reading."""
    n_rows: int
    """Number of rows to read after skipping."""
    row_index: tuple[str, int] | None
    """If not None add an integer index column of the given name."""
    include_file_paths: str | None
    """Include the path of the source file(s) as a column with this name."""
    predicate: expr.NamedExpr | None
    """Mask to apply to the read dataframe."""

    PARQUET_DEFAULT_CHUNK_SIZE: int = 0  # unlimited
    PARQUET_DEFAULT_PASS_LIMIT: int = 16 * 1024**3  # 16GiB

    def __init__(
        self,
        schema: Schema,
        typ: str,
        reader_options: dict[str, Any],
        cloud_options: dict[str, Any] | None,
        config_options: ConfigOptions,
        paths: list[str],
        with_columns: list[str] | None,
        skip_rows: int,
        n_rows: int,
        row_index: tuple[str, int] | None,
        include_file_paths: str | None,
        predicate: expr.NamedExpr | None,
    ):
        self.schema = schema
        self.typ = typ
        self.reader_options = reader_options
        self.cloud_options = cloud_options
        self.config_options = config_options
        self.paths = paths
        self.with_columns = with_columns
        self.skip_rows = skip_rows
        self.n_rows = n_rows
        self.row_index = row_index
        self.include_file_paths = include_file_paths
        self.predicate = predicate
        self._non_child_args = (
            schema,
            typ,
            reader_options,
            config_options,
            paths,
            with_columns,
            skip_rows,
            n_rows,
            row_index,
            include_file_paths,
            predicate,
        )
        self.children = ()
        if self.typ not in ("csv", "parquet", "ndjson"):  # pragma: no cover
            # This line is unhittable ATM since IPC/Anonymous scan raise
            # on the polars side
            raise NotImplementedError(f"Unhandled scan type: {self.typ}")
        if self.typ == "ndjson" and (self.n_rows != -1 or self.skip_rows != 0):
            raise NotImplementedError("row limit in scan for json reader")
        if self.skip_rows < 0:
            # TODO: polars has this implemented for parquet,
            # maybe we can do this too?
            raise NotImplementedError("slice pushdown for negative slices")
        if (
            POLARS_VERSION_LT_128 and self.typ in {"csv"} and self.skip_rows != 0
        ):  # pragma: no cover
            # This comes from slice pushdown, but that
            # optimization doesn't happen right now
            raise NotImplementedError("skipping rows in CSV reader")
        if self.cloud_options is not None and any(
            self.cloud_options.get(k) is not None for k in ("aws", "azure", "gcp")
        ):
            raise NotImplementedError(
                "Read from cloud storage"
            )  # pragma: no cover; no test yet
        if any(str(p).startswith("https:/") for p in self.paths):
            raise NotImplementedError("Read from https")
        if self.typ == "csv":
            if self.reader_options["skip_rows_after_header"] != 0:
                raise NotImplementedError("Skipping rows after header in CSV reader")
            parse_options = self.reader_options["parse_options"]
            if (
                null_values := parse_options["null_values"]
            ) is not None and "Named" in null_values:
                raise NotImplementedError(
                    "Per column null value specification not supported for CSV reader"
                )
            if (
                comment := parse_options["comment_prefix"]
            ) is not None and "Multi" in comment:
                raise NotImplementedError(
                    "Multi-character comment prefix not supported for CSV reader"
                )
            if not self.reader_options["has_header"]:
                # Need to do some file introspection to get the number
                # of columns so that column projection works right.
                raise NotImplementedError("Reading CSV without header")
        elif self.typ == "ndjson":
            # TODO: consider handling the low memory option here
            # (maybe use chunked JSON reader)
            if self.reader_options["ignore_errors"]:
                raise NotImplementedError(
                    "ignore_errors is not supported in the JSON reader"
                )
            if include_file_paths is not None:
                # TODO: Need to populate num_rows_per_source in read_json in libcudf
                raise NotImplementedError("Including file paths in a json scan.")
        elif (
            self.typ == "parquet"
            and self.row_index is not None
            and self.with_columns is not None
            and len(self.with_columns) == 0
        ):
            raise NotImplementedError(
                "Reading only parquet metadata to produce row index."
            )

    def get_hashable(self) -> Hashable:
        """
        Hashable representation of the node.

        The options dictionaries are serialised for hashing purposes
        as json strings.
        """
        schema_hash = tuple(self.schema.items())
        return (
            type(self),
            schema_hash,
            self.typ,
            json.dumps(self.reader_options),
            json.dumps(self.cloud_options),
            self.config_options,
            tuple(self.paths),
            tuple(self.with_columns) if self.with_columns is not None else None,
            self.skip_rows,
            self.n_rows,
            self.row_index,
            self.include_file_paths,
            self.predicate,
        )

    @staticmethod
    def add_file_paths(
        name: str, paths: list[str], rows_per_path: list[int], df: DataFrame
    ) -> DataFrame:
        """
        Add a Column of file paths to the DataFrame.

        Each path is repeated according to the number of rows read from it.
        """
        (filepaths,) = plc.filling.repeat(
            # TODO: Remove call from_arrow when we support python list to Column
            plc.Table([plc.interop.from_arrow(pa.array(map(str, paths)))]),
            plc.interop.from_arrow(pa.array(rows_per_path, type=pa.int32())),
        ).columns()
        return df.with_columns([Column(filepaths, name=name)])

    @classmethod
    def do_evaluate(
        cls,
        schema: Schema,
        typ: str,
        reader_options: dict[str, Any],
        config_options: ConfigOptions,
        paths: list[str],
        with_columns: list[str] | None,
        skip_rows: int,
        n_rows: int,
        row_index: tuple[str, int] | None,
        include_file_paths: str | None,
        predicate: expr.NamedExpr | None,
    ) -> DataFrame:
        """Evaluate and return a dataframe."""
        if typ == "csv":

            def read_csv_header(
                path: Path | str, sep: str
            ) -> list[str]:  # pragma: no cover
                with Path(path).open() as f:
                    for line in f:
                        stripped = line.strip()
                        if stripped:
                            return stripped.split(sep)
                return []

            parse_options = reader_options["parse_options"]
            sep = chr(parse_options["separator"])
            quote = chr(parse_options["quote_char"])
            eol = chr(parse_options["eol_char"])
            if reader_options["schema"] is not None:
                # Reader schema provides names
                column_names = list(reader_options["schema"]["fields"].keys())
            else:
                # file provides column names
                column_names = None
            usecols = with_columns
            # TODO: support has_header=False
            header = 0

            # polars defaults to no null recognition
            null_values = [""]
            if parse_options["null_values"] is not None:
                ((typ, nulls),) = parse_options["null_values"].items()
                if typ == "AllColumnsSingle":
                    # Single value
                    null_values.append(nulls)
                else:
                    # List of values
                    null_values.extend(nulls)
            if parse_options["comment_prefix"] is not None:
                comment = chr(parse_options["comment_prefix"]["Single"])
            else:
                comment = None
            decimal = "," if parse_options["decimal_comma"] else "."

            # polars skips blank lines at the beginning of the file
            pieces = []
            seen_paths = []
            read_partial = n_rows != -1
            for p in paths:
                skiprows = reader_options["skip_rows"]
                path = Path(p)
                with path.open() as f:
                    while f.readline() == "\n":
                        skiprows += 1
                options = (
                    plc.io.csv.CsvReaderOptions.builder(plc.io.SourceInfo([path]))
                    .nrows(n_rows)
                    .skiprows(
                        skiprows if POLARS_VERSION_LT_128 else skiprows + skip_rows
                    )  # pragma: no cover
                    .lineterminator(str(eol))
                    .quotechar(str(quote))
                    .decimal(decimal)
                    .keep_default_na(keep_default_na=False)
                    .na_filter(na_filter=True)
                    .build()
                )
                options.set_delimiter(str(sep))
                if column_names is not None:
                    options.set_names([str(name) for name in column_names])
                else:
                    if (
                        not POLARS_VERSION_LT_128 and skip_rows > header
                    ):  # pragma: no cover
                        # We need to read the header otherwise we would skip it
                        column_names = read_csv_header(path, str(sep))
                        options.set_names(column_names)
                options.set_header(header)
                options.set_dtypes(schema)
                if usecols is not None:
                    options.set_use_cols_names([str(name) for name in usecols])
                options.set_na_values(null_values)
                if comment is not None:
                    options.set_comment(comment)
                tbl_w_meta = plc.io.csv.read_csv(options)
                pieces.append(tbl_w_meta)
                if include_file_paths is not None:
                    seen_paths.append(p)
                if read_partial:
                    n_rows -= tbl_w_meta.tbl.num_rows()
                    if n_rows <= 0:
                        break
            tables, colnames = zip(
                *(
                    (piece.tbl, piece.column_names(include_children=False))
                    for piece in pieces
                ),
                strict=True,
            )
            df = DataFrame.from_table(
                plc.concatenate.concatenate(list(tables)),
                colnames[0],
            )
            if include_file_paths is not None:
                df = Scan.add_file_paths(
                    include_file_paths,
                    seen_paths,
                    [t.num_rows() for t in tables],
                    df,
                )
        elif typ == "parquet":
            filters = None
            if predicate is not None and row_index is None:
                # Can't apply filters during read if we have a row index.
                filters = to_parquet_filter(predicate.value)
            options = plc.io.parquet.ParquetReaderOptions.builder(
                plc.io.SourceInfo(paths)
            ).build()
            if with_columns is not None:
                options.set_columns(with_columns)
            if filters is not None:
                options.set_filter(filters)
            if config_options.parquet_options.chunked:
                # We handle skip_rows != 0 by reading from the
                # up to n_rows + skip_rows and slicing off the
                # first skip_rows entries.
                # TODO: Remove this workaround once
                # https://github.com/rapidsai/cudf/issues/16186
                # is fixed
                nrows = n_rows + skip_rows
                if nrows > -1:
                    options.set_num_rows(nrows)
                reader = plc.io.parquet.ChunkedParquetReader(
                    options,
                    chunk_read_limit=config_options.parquet_options.chunk_read_limit,
                    pass_read_limit=config_options.parquet_options.pass_read_limit,
                )
                chunk = reader.read_chunk()
                rows_left_to_skip = skip_rows

                def slice_skip(tbl: plc.Table) -> plc.Table:
                    nonlocal rows_left_to_skip
                    if rows_left_to_skip > 0:
                        table_rows = tbl.num_rows()
                        chunk_skip = min(rows_left_to_skip, table_rows)
                        # TODO: Check performance impact of skipping this
                        # call and creating an empty table manually when the
                        # slice would be empty (chunk_skip == table_rows).
                        (tbl,) = plc.copying.slice(tbl, [chunk_skip, table_rows])
                        rows_left_to_skip -= chunk_skip
                    return tbl

                tbl = slice_skip(chunk.tbl)
                # TODO: Nested column names
                names = chunk.column_names(include_children=False)
                concatenated_columns = tbl.columns()
                while reader.has_next():
                    chunk = reader.read_chunk()
                    tbl = slice_skip(chunk.tbl)

                    for i in range(tbl.num_columns()):
                        concatenated_columns[i] = plc.concatenate.concatenate(
                            [concatenated_columns[i], tbl._columns[i]]
                        )
                        # Drop residual columns to save memory
                        tbl._columns[i] = None

                df = DataFrame.from_table(
                    plc.Table(concatenated_columns),
                    names=names,
                )
                if include_file_paths is not None:
                    df = Scan.add_file_paths(
                        include_file_paths, paths, chunk.num_rows_per_source, df
                    )
            else:
                if n_rows != -1:
                    options.set_num_rows(n_rows)
                if skip_rows != 0:
                    options.set_skip_rows(skip_rows)
                tbl_w_meta = plc.io.parquet.read_parquet(options)
                df = DataFrame.from_table(
                    tbl_w_meta.tbl,
                    # TODO: consider nested column names?
                    tbl_w_meta.column_names(include_children=False),
                )
                if include_file_paths is not None:
                    df = Scan.add_file_paths(
                        include_file_paths, paths, tbl_w_meta.num_rows_per_source, df
                    )
            if filters is not None:
                # Mask must have been applied.
                return df

        elif typ == "ndjson":
            json_schema: list[plc.io.json.NameAndType] = [
                (name, typ, []) for name, typ in schema.items()
            ]
            plc_tbl_w_meta = plc.io.json.read_json(
                plc.io.json._setup_json_reader_options(
                    plc.io.SourceInfo(paths),
                    lines=True,
                    dtypes=json_schema,
                    prune_columns=True,
                )
            )
            # TODO: I don't think cudf-polars supports nested types in general right now
            # (but when it does, we should pass child column names from nested columns in)
            df = DataFrame.from_table(
                plc_tbl_w_meta.tbl, plc_tbl_w_meta.column_names(include_children=False)
            )
            col_order = list(schema.keys())
            if row_index is not None:
                col_order.remove(row_index[0])
            df = df.select(col_order)
        else:
            raise NotImplementedError(
                f"Unhandled scan type: {typ}"
            )  # pragma: no cover; post init trips first
        if row_index is not None:
            name, offset = row_index
            offset += skip_rows
            dtype = schema[name]
            step = plc.Scalar.from_py(1, dtype)
            init = plc.Scalar.from_py(offset, dtype)
            index_col = Column(
                plc.filling.sequence(df.num_rows, init, step),
                is_sorted=plc.types.Sorted.YES,
                order=plc.types.Order.ASCENDING,
                null_order=plc.types.NullOrder.AFTER,
                name=name,
            )
            df = DataFrame([index_col, *df.columns])
            if next(iter(schema)) != name:
                df = df.select(schema)
        assert all(c.obj.type() == schema[name] for name, c in df.column_map.items())
        if predicate is None:
            return df
        else:
            (mask,) = broadcast(predicate.evaluate(df), target_length=df.num_rows)
            return df.filter(mask)


class Cache(IR):
    """
    Return a cached plan node.

    Used for CSE at the plan level.
    """

    __slots__ = ("key", "refcount")
    _non_child = ("schema", "key", "refcount")
    key: int
    """The cache key."""
    refcount: int
    """The number of cache hits."""

    def __init__(self, schema: Schema, key: int, refcount: int, value: IR):
        self.schema = schema
        self.key = key
        self.refcount = refcount
        self.children = (value,)
        self._non_child_args = (key, refcount)

    def get_hashable(self) -> Hashable:  # noqa: D102
        # Polars arranges that the keys are unique across all cache
        # nodes that reference the same child, so we don't need to
        # hash the child.
        return (type(self), self.key, self.refcount)

    def is_equal(self, other: Self) -> bool:  # noqa: D102
        if self.key == other.key and self.refcount == other.refcount:
            self.children = other.children
            return True
        return False

    @classmethod
    def do_evaluate(
        cls, key: int, refcount: int, df: DataFrame
    ) -> DataFrame:  # pragma: no cover; basic evaluation never calls this
        """Evaluate and return a dataframe."""
        # Our value has already been computed for us, so let's just
        # return it.
        return df

    def evaluate(self, *, cache: CSECache, timer: Timer | None) -> DataFrame:
        """Evaluate and return a dataframe."""
        # We must override the recursion scheme because we don't want
        # to recurse if we're in the cache.
        try:
            (result, hits) = cache[self.key]
        except KeyError:
            (value,) = self.children
            result = value.evaluate(cache=cache, timer=timer)
            cache[self.key] = (result, 0)
            return result
        else:
            hits += 1
            if hits == self.refcount:
                del cache[self.key]
            else:
                cache[self.key] = (result, hits)
            return result


class DataFrameScan(IR):
    """
    Input from an existing polars DataFrame.

    This typically arises from ``q.collect().lazy()``
    """

    __slots__ = ("_id_for_hash", "config_options", "df", "projection")
    _non_child = ("schema", "df", "projection", "config_options")
    df: Any
    """Polars internal PyDataFrame object."""
    projection: tuple[str, ...] | None
    """List of columns to project out."""
    config_options: ConfigOptions
    """GPU-specific configuration options"""

    def __init__(
        self,
        schema: Schema,
        df: Any,
        projection: Sequence[str] | None,
        config_options: ConfigOptions,
    ):
        self.schema = schema
        self.df = df
        self.projection = tuple(projection) if projection is not None else None
        self.config_options = config_options
        self._non_child_args = (
            schema,
            pl.DataFrame._from_pydf(df),
            self.projection,
        )
        self.children = ()
        self._id_for_hash = random.randint(0, 2**64 - 1)

    def get_hashable(self) -> Hashable:
        """
        Hashable representation of the node.

        The (heavy) dataframe object is not hashed. No two instances of
        ``DataFrameScan`` will have the same hash, even if they have the
        same schema, projection, and config options, and data.
        """
        schema_hash = tuple(self.schema.items())
        return (
            type(self),
            schema_hash,
            self._id_for_hash,
            self.projection,
            self.config_options,
        )

    @classmethod
    def do_evaluate(
        cls,
        schema: Schema,
        df: Any,
        projection: tuple[str, ...] | None,
    ) -> DataFrame:
        """Evaluate and return a dataframe."""
        if projection is not None:
            df = df.select(projection)
        df = DataFrame.from_polars(df)
        assert all(
            c.obj.type() == dtype
            for c, dtype in zip(df.columns, schema.values(), strict=True)
        )
        return df


class Select(IR):
    """Produce a new dataframe selecting given expressions from an input."""

    __slots__ = ("exprs", "should_broadcast")
    _non_child = ("schema", "exprs", "should_broadcast")
    exprs: tuple[expr.NamedExpr, ...]
    """List of expressions to evaluate to form the new dataframe."""
    should_broadcast: bool
    """Should columns be broadcast?"""

    def __init__(
        self,
        schema: Schema,
        exprs: Sequence[expr.NamedExpr],
        should_broadcast: bool,  # noqa: FBT001
        df: IR,
    ):
        self.schema = schema
        self.exprs = tuple(exprs)
        self.should_broadcast = should_broadcast
        self.children = (df,)
        self._non_child_args = (self.exprs, should_broadcast)

    @classmethod
    def do_evaluate(
        cls,
        exprs: tuple[expr.NamedExpr, ...],
        should_broadcast: bool,  # noqa: FBT001
        df: DataFrame,
    ) -> DataFrame:
        """Evaluate and return a dataframe."""
        # Handle any broadcasting
        columns = [e.evaluate(df) for e in exprs]
        if should_broadcast:
            columns = broadcast(*columns)
        return DataFrame(columns)


class Reduce(IR):
    """
    Produce a new dataframe selecting given expressions from an input.

    This is a special case of :class:`Select` where all outputs are a single row.
    """

    __slots__ = ("exprs",)
    _non_child = ("schema", "exprs")
    exprs: tuple[expr.NamedExpr, ...]
    """List of expressions to evaluate to form the new dataframe."""

    def __init__(
        self, schema: Schema, exprs: Sequence[expr.NamedExpr], df: IR
    ):  # pragma: no cover; polars doesn't emit this node yet
        self.schema = schema
        self.exprs = tuple(exprs)
        self.children = (df,)
        self._non_child_args = (self.exprs,)

    @classmethod
    def do_evaluate(
        cls,
        exprs: tuple[expr.NamedExpr, ...],
        df: DataFrame,
    ) -> DataFrame:  # pragma: no cover; not exposed by polars yet
        """Evaluate and return a dataframe."""
        columns = broadcast(*(e.evaluate(df) for e in exprs))
        assert all(column.size == 1 for column in columns)
        return DataFrame(columns)


class GroupBy(IR):
    """Perform a groupby."""

    __slots__ = (
        "agg_requests",
        "config_options",
        "keys",
        "maintain_order",
        "zlice",
    )
    _non_child = (
        "schema",
        "keys",
        "agg_requests",
        "maintain_order",
        "zlice",
        "config_options",
    )
    keys: tuple[expr.NamedExpr, ...]
    """Grouping keys."""
    agg_requests: tuple[expr.NamedExpr, ...]
    """Aggregation expressions."""
    maintain_order: bool
    """Preserve order in groupby."""
    zlice: Zlice | None
    """Optional slice to apply after grouping."""
    config_options: ConfigOptions
    """GPU-specific configuration options"""

    def __init__(
        self,
        schema: Schema,
        keys: Sequence[expr.NamedExpr],
        agg_requests: Sequence[expr.NamedExpr],
        maintain_order: bool,  # noqa: FBT001
        zlice: Zlice | None,
        config_options: ConfigOptions,
        df: IR,
    ):
        self.schema = schema
        self.keys = tuple(keys)
        self.agg_requests = tuple(agg_requests)
        self.maintain_order = maintain_order
        self.zlice = zlice
        self.config_options = config_options
        self.children = (df,)
        self._non_child_args = (
            self.keys,
            self.agg_requests,
            maintain_order,
            self.zlice,
        )

    @classmethod
    def do_evaluate(
        cls,
        keys_in: Sequence[expr.NamedExpr],
        agg_requests: Sequence[expr.NamedExpr],
        maintain_order: bool,  # noqa: FBT001
        zlice: Zlice | None,
        df: DataFrame,
    ) -> DataFrame:
        """Evaluate and return a dataframe."""
        keys = broadcast(*(k.evaluate(df) for k in keys_in), target_length=df.num_rows)
        sorted = (
            plc.types.Sorted.YES
            if all(k.is_sorted for k in keys)
            else plc.types.Sorted.NO
        )
        grouper = plc.groupby.GroupBy(
            plc.Table([k.obj for k in keys]),
            null_handling=plc.types.NullPolicy.INCLUDE,
            keys_are_sorted=sorted,
            column_order=[k.order for k in keys],
            null_precedence=[k.null_order for k in keys],
        )
        requests = []
        names = []
        for request in agg_requests:
            name = request.name
            value = request.value
            if isinstance(value, expr.Len):
                # A count aggregation, we need a column so use a key column
                col = keys[0].obj
            elif isinstance(value, expr.Agg):
                if value.name == "quantile":
                    child = value.children[0]
                else:
                    (child,) = value.children
                col = child.evaluate(df).obj
            else:
                # Anything else, we pre-evaluate
                col = value.evaluate(df).obj
            requests.append(plc.groupby.GroupByRequest(col, [value.agg_request]))
            names.append(name)
        group_keys, raw_tables = grouper.aggregate(requests)
        results = [
            Column(column, name=name)
            for name, column in zip(
                names,
                itertools.chain.from_iterable(t.columns() for t in raw_tables),
                strict=True,
            )
        ]
        result_keys = [
            Column(grouped_key, name=key.name)
            for key, grouped_key in zip(keys, group_keys.columns(), strict=True)
        ]
        broadcasted = broadcast(*result_keys, *results)
        # Handle order preservation of groups
        if maintain_order and not sorted:
            # The order we want
            want = plc.stream_compaction.stable_distinct(
                plc.Table([k.obj for k in keys]),
                list(range(group_keys.num_columns())),
                plc.stream_compaction.DuplicateKeepOption.KEEP_FIRST,
                plc.types.NullEquality.EQUAL,
                plc.types.NanEquality.ALL_EQUAL,
            )
            # The order we have
            have = plc.Table([key.obj for key in broadcasted[: len(keys)]])

            # We know an inner join is OK because by construction
            # want and have are permutations of each other.
            left_order, right_order = plc.join.inner_join(
                want, have, plc.types.NullEquality.EQUAL
            )
            # Now left_order is an arbitrary permutation of the ordering we
            # want, and right_order is a matching permutation of the ordering
            # we have. To get to the original ordering, we need
            # left_order == iota(nrows), with right_order permuted
            # appropriately. This can be obtained by sorting
            # right_order by left_order.
            (right_order,) = plc.sorting.sort_by_key(
                plc.Table([right_order]),
                plc.Table([left_order]),
                [plc.types.Order.ASCENDING],
                [plc.types.NullOrder.AFTER],
            ).columns()
            ordered_table = plc.copying.gather(
                plc.Table([col.obj for col in broadcasted]),
                right_order,
                plc.copying.OutOfBoundsPolicy.DONT_CHECK,
            )
            broadcasted = [
                Column(reordered, name=old.name)
                for reordered, old in zip(
                    ordered_table.columns(), broadcasted, strict=True
                )
            ]
        return DataFrame(broadcasted).slice(zlice)


class ConditionalJoin(IR):
    """A conditional inner join of two dataframes on a predicate."""

    class Predicate:
        """Serializable wrapper for a predicate expression."""

        predicate: expr.Expr
        ast: plc.expressions.Expression

        def __init__(self, predicate: expr.Expr):
            self.predicate = predicate
            self.ast = to_ast(predicate)

        def __reduce__(self) -> tuple[Any, ...]:
            """Pickle a Predicate object."""
            return (type(self), (self.predicate,))

    __slots__ = ("ast_predicate", "options", "predicate")
    _non_child = ("schema", "predicate", "options")
    predicate: expr.Expr
    """Expression predicate to join on"""
    options: tuple[
        tuple[
            str,
            pl_expr.Operator | Iterable[pl_expr.Operator],
        ],
        bool,
        Zlice | None,
        str,
        bool,
        Literal["none", "left", "right", "left_right", "right_left"],
    ]
    """
    tuple of options:
    - predicates: tuple of ir join type (eg. ie_join) and (In)Equality conditions
    - nulls_equal: do nulls compare equal?
    - slice: optional slice to perform after joining.
    - suffix: string suffix for right columns if names match
    - coalesce: should key columns be coalesced (only makes sense for outer joins)
    - maintain_order: which DataFrame row order to preserve, if any
    """

    def __init__(
        self, schema: Schema, predicate: expr.Expr, options: tuple, left: IR, right: IR
    ) -> None:
        self.schema = schema
        self.predicate = predicate
        self.options = options
        self.children = (left, right)
        predicate_wrapper = self.Predicate(predicate)
        _, nulls_equal, zlice, suffix, coalesce, maintain_order = self.options
        # Preconditions from polars
        assert not nulls_equal
        assert not coalesce
        assert maintain_order == "none"
        if predicate_wrapper.ast is None:
            raise NotImplementedError(
                f"Conditional join with predicate {predicate}"
            )  # pragma: no cover; polars never delivers expressions we can't handle
        self._non_child_args = (predicate_wrapper, zlice, suffix, maintain_order)

    @classmethod
    def do_evaluate(
        cls,
        predicate_wrapper: Predicate,
        zlice: Zlice | None,
        suffix: str,
        maintain_order: Literal["none", "left", "right", "left_right", "right_left"],
        left: DataFrame,
        right: DataFrame,
    ) -> DataFrame:
        """Evaluate and return a dataframe."""
        lg, rg = plc.join.conditional_inner_join(
            left.table,
            right.table,
            predicate_wrapper.ast,
        )
        left = DataFrame.from_table(
            plc.copying.gather(
                left.table, lg, plc.copying.OutOfBoundsPolicy.DONT_CHECK
            ),
            left.column_names,
        )
        right = DataFrame.from_table(
            plc.copying.gather(
                right.table, rg, plc.copying.OutOfBoundsPolicy.DONT_CHECK
            ),
            right.column_names,
        )
        right = right.rename_columns(
            {
                name: f"{name}{suffix}"
                for name in right.column_names
                if name in left.column_names_set
            }
        )
        result = left.with_columns(right.columns)
        return result.slice(zlice)


class Join(IR):
    """A join of two dataframes."""

    __slots__ = ("config_options", "left_on", "options", "right_on")
    _non_child = ("schema", "left_on", "right_on", "options", "config_options")
    left_on: tuple[expr.NamedExpr, ...]
    """List of expressions used as keys in the left frame."""
    right_on: tuple[expr.NamedExpr, ...]
    """List of expressions used as keys in the right frame."""
    options: tuple[
        Literal["Inner", "Left", "Right", "Full", "Semi", "Anti", "Cross"],
        bool,
        Zlice | None,
        str,
        bool,
        Literal["none", "left", "right", "left_right", "right_left"],
    ]
    """
    tuple of options:
    - how: join type
    - nulls_equal: do nulls compare equal?
    - slice: optional slice to perform after joining.
    - suffix: string suffix for right columns if names match
    - coalesce: should key columns be coalesced (only makes sense for outer joins)
    - maintain_order: which DataFrame row order to preserve, if any
    """
    config_options: ConfigOptions
    """GPU-specific configuration options"""

    def __init__(
        self,
        schema: Schema,
        left_on: Sequence[expr.NamedExpr],
        right_on: Sequence[expr.NamedExpr],
        options: Any,
        config_options: ConfigOptions,
        left: IR,
        right: IR,
    ):
        self.schema = schema
        self.left_on = tuple(left_on)
        self.right_on = tuple(right_on)
        self.options = options
        self.config_options = config_options
        self.children = (left, right)
        self._non_child_args = (self.left_on, self.right_on, self.options)
        # TODO: Implement maintain_order
        if options[5] != "none":
            raise NotImplementedError("maintain_order not implemented yet")

    @staticmethod
    @cache
    def _joiners(
        how: Literal["Inner", "Left", "Right", "Full", "Semi", "Anti"],
    ) -> tuple[
        Callable, plc.copying.OutOfBoundsPolicy, plc.copying.OutOfBoundsPolicy | None
    ]:
        if how == "Inner":
            return (
                plc.join.inner_join,
                plc.copying.OutOfBoundsPolicy.DONT_CHECK,
                plc.copying.OutOfBoundsPolicy.DONT_CHECK,
            )
        elif how == "Left" or how == "Right":
            return (
                plc.join.left_join,
                plc.copying.OutOfBoundsPolicy.DONT_CHECK,
                plc.copying.OutOfBoundsPolicy.NULLIFY,
            )
        elif how == "Full":
            return (
                plc.join.full_join,
                plc.copying.OutOfBoundsPolicy.NULLIFY,
                plc.copying.OutOfBoundsPolicy.NULLIFY,
            )
        elif how == "Semi":
            return (
                plc.join.left_semi_join,
                plc.copying.OutOfBoundsPolicy.DONT_CHECK,
                None,
            )
        elif how == "Anti":
            return (
                plc.join.left_anti_join,
                plc.copying.OutOfBoundsPolicy.DONT_CHECK,
                None,
            )
        assert_never(how)  # pragma: no cover

    @staticmethod
    def _reorder_maps(
        left_rows: int,
        lg: plc.Column,
        left_policy: plc.copying.OutOfBoundsPolicy,
        right_rows: int,
        rg: plc.Column,
        right_policy: plc.copying.OutOfBoundsPolicy,
    ) -> list[plc.Column]:
        """
        Reorder gather maps to satisfy polars join order restrictions.

        Parameters
        ----------
        left_rows
            Number of rows in left table
        lg
            Left gather map
        left_policy
            Nullify policy for left map
        right_rows
            Number of rows in right table
        rg
            Right gather map
        right_policy
            Nullify policy for right map

        Returns
        -------
        list of reordered left and right gather maps.

        Notes
        -----
        For a left join, the polars result preserves the order of the
        left keys, and is stable wrt the right keys. For all other
        joins, there is no order obligation.
        """
        init = plc.Scalar.from_py(0, plc.types.SIZE_TYPE)
        step = plc.Scalar.from_py(1, plc.types.SIZE_TYPE)
        left_order = plc.copying.gather(
            plc.Table([plc.filling.sequence(left_rows, init, step)]), lg, left_policy
        )
        right_order = plc.copying.gather(
            plc.Table([plc.filling.sequence(right_rows, init, step)]), rg, right_policy
        )
        return plc.sorting.stable_sort_by_key(
            plc.Table([lg, rg]),
            plc.Table([*left_order.columns(), *right_order.columns()]),
            [plc.types.Order.ASCENDING, plc.types.Order.ASCENDING],
            [plc.types.NullOrder.AFTER, plc.types.NullOrder.AFTER],
        ).columns()

    @classmethod
    def do_evaluate(
        cls,
        left_on_exprs: Sequence[expr.NamedExpr],
        right_on_exprs: Sequence[expr.NamedExpr],
        options: tuple[
            Literal["Inner", "Left", "Right", "Full", "Semi", "Anti", "Cross"],
            bool,
            Zlice | None,
            str,
            bool,
            Literal["none", "left", "right", "left_right", "right_left"],
        ],
        left: DataFrame,
        right: DataFrame,
    ) -> DataFrame:
        """Evaluate and return a dataframe."""
        how, nulls_equal, zlice, suffix, coalesce, _ = options
        if how == "Cross":
            # Separate implementation, since cross_join returns the
            # result, not the gather maps
            columns = plc.join.cross_join(left.table, right.table).columns()
            left_cols = [
                Column(new, name=old.name).sorted_like(old)
                for new, old in zip(
                    columns[: left.num_columns], left.columns, strict=True
                )
            ]
            right_cols = [
                Column(
                    new,
                    name=name
                    if name not in left.column_names_set
                    else f"{name}{suffix}",
                )
                for new, name in zip(
                    columns[left.num_columns :], right.column_names, strict=True
                )
            ]
            return DataFrame([*left_cols, *right_cols]).slice(zlice)
        # TODO: Waiting on clarity based on https://github.com/pola-rs/polars/issues/17184
        left_on = DataFrame(broadcast(*(e.evaluate(left) for e in left_on_exprs)))
        right_on = DataFrame(broadcast(*(e.evaluate(right) for e in right_on_exprs)))
        null_equality = (
            plc.types.NullEquality.EQUAL
            if nulls_equal
            else plc.types.NullEquality.UNEQUAL
        )
        join_fn, left_policy, right_policy = cls._joiners(how)
        if right_policy is None:
            # Semi join
            lg = join_fn(left_on.table, right_on.table, null_equality)
            table = plc.copying.gather(left.table, lg, left_policy)
            result = DataFrame.from_table(table, left.column_names)
        else:
            if how == "Right":
                # Right join is a left join with the tables swapped
                left, right = right, left
                left_on, right_on = right_on, left_on
            lg, rg = join_fn(left_on.table, right_on.table, null_equality)
            if how == "Left" or how == "Right":
                # Order of left table is preserved
                lg, rg = cls._reorder_maps(
                    left.num_rows, lg, left_policy, right.num_rows, rg, right_policy
                )
            if coalesce:
                if how == "Full":
                    # In this case, keys must be column references,
                    # possibly with dtype casting. We should use them in
                    # preference to the columns from the original tables.
                    left = left.with_columns(left_on.columns, replace_only=True)
                    right = right.with_columns(right_on.columns, replace_only=True)
                else:
                    right = right.discard_columns(right_on.column_names_set)
            left = DataFrame.from_table(
                plc.copying.gather(left.table, lg, left_policy), left.column_names
            )
            right = DataFrame.from_table(
                plc.copying.gather(right.table, rg, right_policy), right.column_names
            )
            if coalesce and how == "Full":
                left = left.with_columns(
                    (
                        Column(
                            plc.replace.replace_nulls(left_col.obj, right_col.obj),
                            name=left_col.name,
                        )
                        for left_col, right_col in zip(
                            left.select_columns(left_on.column_names_set),
                            right.select_columns(right_on.column_names_set),
                            strict=True,
                        )
                    ),
                    replace_only=True,
                )
                right = right.discard_columns(right_on.column_names_set)
            if how == "Right":
                # Undo the swap for right join before gluing together.
                left, right = right, left
            right = right.rename_columns(
                {
                    name: f"{name}{suffix}"
                    for name in right.column_names
                    if name in left.column_names_set
                }
            )
            result = left.with_columns(right.columns)
        return result.slice(zlice)


class HStack(IR):
    """Add new columns to a dataframe."""

    __slots__ = ("columns", "should_broadcast")
    _non_child = ("schema", "columns", "should_broadcast")
    should_broadcast: bool
    """Should the resulting evaluated columns be broadcast to the same length."""

    def __init__(
        self,
        schema: Schema,
        columns: Sequence[expr.NamedExpr],
        should_broadcast: bool,  # noqa: FBT001
        df: IR,
    ):
        self.schema = schema
        self.columns = tuple(columns)
        self.should_broadcast = should_broadcast
        self._non_child_args = (self.columns, self.should_broadcast)
        self.children = (df,)

    @classmethod
    def do_evaluate(
        cls,
        exprs: Sequence[expr.NamedExpr],
        should_broadcast: bool,  # noqa: FBT001
        df: DataFrame,
    ) -> DataFrame:
        """Evaluate and return a dataframe."""
        columns = [c.evaluate(df) for c in exprs]
        if should_broadcast:
            columns = broadcast(
                *columns, target_length=df.num_rows if df.num_columns != 0 else None
            )
        else:
            # Polars ensures this is true, but let's make sure nothing
            # went wrong. In this case, the parent node is a
            # guaranteed to be a Select which will take care of making
            # sure that everything is the same length. The result
            # table that might have mismatching column lengths will
            # never be turned into a pylibcudf Table with all columns
            # by the Select, which is why this is safe.
            assert all(e.name.startswith("__POLARS_CSER_0x") for e in exprs)
        return df.with_columns(columns)


class Distinct(IR):
    """Produce a new dataframe with distinct rows."""

    __slots__ = ("keep", "stable", "subset", "zlice")
    _non_child = ("schema", "keep", "subset", "zlice", "stable")
    keep: plc.stream_compaction.DuplicateKeepOption
    """Which distinct value to keep."""
    subset: frozenset[str] | None
    """Which columns should be used to define distinctness. If None,
    then all columns are used."""
    zlice: Zlice | None
    """Optional slice to apply to the result."""
    stable: bool
    """Should the result maintain ordering."""

    def __init__(
        self,
        schema: Schema,
        keep: plc.stream_compaction.DuplicateKeepOption,
        subset: frozenset[str] | None,
        zlice: Zlice | None,
        stable: bool,  # noqa: FBT001
        df: IR,
    ):
        self.schema = schema
        self.keep = keep
        self.subset = subset
        self.zlice = zlice
        self.stable = stable
        self._non_child_args = (keep, subset, zlice, stable)
        self.children = (df,)

    _KEEP_MAP: ClassVar[dict[str, plc.stream_compaction.DuplicateKeepOption]] = {
        "first": plc.stream_compaction.DuplicateKeepOption.KEEP_FIRST,
        "last": plc.stream_compaction.DuplicateKeepOption.KEEP_LAST,
        "none": plc.stream_compaction.DuplicateKeepOption.KEEP_NONE,
        "any": plc.stream_compaction.DuplicateKeepOption.KEEP_ANY,
    }

    @classmethod
    def do_evaluate(
        cls,
        keep: plc.stream_compaction.DuplicateKeepOption,
        subset: frozenset[str] | None,
        zlice: Zlice | None,
        stable: bool,  # noqa: FBT001
        df: DataFrame,
    ) -> DataFrame:
        """Evaluate and return a dataframe."""
        if subset is None:
            indices = list(range(df.num_columns))
            keys_sorted = all(c.is_sorted for c in df.column_map.values())
        else:
            indices = [i for i, k in enumerate(df.column_names) if k in subset]
            keys_sorted = all(df.column_map[name].is_sorted for name in subset)
        if keys_sorted:
            table = plc.stream_compaction.unique(
                df.table,
                indices,
                keep,
                plc.types.NullEquality.EQUAL,
            )
        else:
            distinct = (
                plc.stream_compaction.stable_distinct
                if stable
                else plc.stream_compaction.distinct
            )
            table = distinct(
                df.table,
                indices,
                keep,
                plc.types.NullEquality.EQUAL,
                plc.types.NanEquality.ALL_EQUAL,
            )
        # TODO: Is this sortedness setting correct
        result = DataFrame(
            [
                Column(new, name=old.name).sorted_like(old)
                for new, old in zip(table.columns(), df.columns, strict=True)
            ]
        )
        if keys_sorted or stable:
            result = result.sorted_like(df)
        return result.slice(zlice)


class Sort(IR):
    """Sort a dataframe."""

    __slots__ = ("by", "null_order", "order", "stable", "zlice")
    _non_child = ("schema", "by", "order", "null_order", "stable", "zlice")
    by: tuple[expr.NamedExpr, ...]
    """Sort keys."""
    order: tuple[plc.types.Order, ...]
    """Sort order for each sort key."""
    null_order: tuple[plc.types.NullOrder, ...]
    """Null sorting location for each sort key."""
    stable: bool
    """Should the sort be stable?"""
    zlice: Zlice | None
    """Optional slice to apply to the result."""

    def __init__(
        self,
        schema: Schema,
        by: Sequence[expr.NamedExpr],
        order: Sequence[plc.types.Order],
        null_order: Sequence[plc.types.NullOrder],
        stable: bool,  # noqa: FBT001
        zlice: Zlice | None,
        df: IR,
    ):
        self.schema = schema
        self.by = tuple(by)
        self.order = tuple(order)
        self.null_order = tuple(null_order)
        self.stable = stable
        self.zlice = zlice
        self._non_child_args = (
            self.by,
            self.order,
            self.null_order,
            self.stable,
            self.zlice,
        )
        self.children = (df,)

    @classmethod
    def do_evaluate(
        cls,
        by: Sequence[expr.NamedExpr],
        order: Sequence[plc.types.Order],
        null_order: Sequence[plc.types.NullOrder],
        stable: bool,  # noqa: FBT001
        zlice: Zlice | None,
        df: DataFrame,
    ) -> DataFrame:
        """Evaluate and return a dataframe."""
        sort_keys = broadcast(*(k.evaluate(df) for k in by), target_length=df.num_rows)
        # TODO: More robust identification here.
        keys_in_result = {
            k.name: i
            for i, k in enumerate(sort_keys)
            if k.name in df.column_map and k.obj is df.column_map[k.name].obj
        }
        do_sort = plc.sorting.stable_sort_by_key if stable else plc.sorting.sort_by_key
        table = do_sort(
            df.table,
            plc.Table([k.obj for k in sort_keys]),
            list(order),
            list(null_order),
        )
        columns: list[Column] = []
        for name, c in zip(df.column_map, table.columns(), strict=True):
            column = Column(c, name=name)
            # If a sort key is in the result table, set the sortedness property
            if name in keys_in_result:
                i = keys_in_result[name]
                column = column.set_sorted(
                    is_sorted=plc.types.Sorted.YES,
                    order=order[i],
                    null_order=null_order[i],
                )
            columns.append(column)
        return DataFrame(columns).slice(zlice)


class Slice(IR):
    """Slice a dataframe."""

    __slots__ = ("length", "offset")
    _non_child = ("schema", "offset", "length")
    offset: int
    """Start of the slice."""
    length: int
    """Length of the slice."""

    def __init__(self, schema: Schema, offset: int, length: int, df: IR):
        self.schema = schema
        self.offset = offset
        self.length = length
        self._non_child_args = (offset, length)
        self.children = (df,)

    @classmethod
    def do_evaluate(cls, offset: int, length: int, df: DataFrame) -> DataFrame:
        """Evaluate and return a dataframe."""
        return df.slice((offset, length))


class Filter(IR):
    """Filter a dataframe with a boolean mask."""

    __slots__ = ("mask",)
    _non_child = ("schema", "mask")
    mask: expr.NamedExpr
    """Expression to produce the filter mask."""

    def __init__(self, schema: Schema, mask: expr.NamedExpr, df: IR):
        self.schema = schema
        self.mask = mask
        self._non_child_args = (mask,)
        self.children = (df,)

    @classmethod
    def do_evaluate(cls, mask_expr: expr.NamedExpr, df: DataFrame) -> DataFrame:
        """Evaluate and return a dataframe."""
        (mask,) = broadcast(mask_expr.evaluate(df), target_length=df.num_rows)
        return df.filter(mask)


class Projection(IR):
    """Select a subset of columns from a dataframe."""

    __slots__ = ()
    _non_child = ("schema",)

    def __init__(self, schema: Schema, df: IR):
        self.schema = schema
        self._non_child_args = (schema,)
        self.children = (df,)

    @classmethod
    def do_evaluate(cls, schema: Schema, df: DataFrame) -> DataFrame:
        """Evaluate and return a dataframe."""
        # This can reorder things.
        columns = broadcast(
            *(df.column_map[name] for name in schema), target_length=df.num_rows
        )
        return DataFrame(columns)


class MergeSorted(IR):
    """Merge sorted operation."""

    __slots__ = ("key",)
    _non_child = ("schema", "key")
    key: str
    """Key that is sorted."""

    def __init__(self, schema: Schema, key: str, left: IR, right: IR):
        assert isinstance(left, Sort)
        assert isinstance(right, Sort)
        assert left.order == right.order
        assert len(left.schema.keys()) <= len(right.schema.keys())
        self.schema = schema
        self.key = key
        self.children = (left, right)
        self._non_child_args = (key,)

    @classmethod
    def do_evaluate(cls, key: str, *dfs: DataFrame) -> DataFrame:
        left, right = dfs
        right = right.discard_columns(right.column_names_set - left.column_names_set)
        on_col_left = left.select_columns({key})[0]
        on_col_right = right.select_columns({key})[0]
        return DataFrame.from_table(
            plc.merge.merge(
                [right.table, left.table],
                [left.column_names.index(key), right.column_names.index(key)],
                [on_col_left.order, on_col_right.order],
                [on_col_left.null_order, on_col_right.null_order],
            ),
            left.column_names,
        )


class MapFunction(IR):
    """Apply some function to a dataframe."""

    __slots__ = ("name", "options")
    _non_child = ("schema", "name", "options")
    name: str
    """Name of the function to apply"""
    options: Any
    """Arbitrary name-specific options"""

    _NAMES: ClassVar[frozenset[str]] = frozenset(
        [
            "rechunk",
            "rename",
            "explode",
            "unpivot",
            "row_index",
        ]
    )

    def __init__(self, schema: Schema, name: str, options: Any, df: IR):
        self.schema = schema
        self.name = name
        self.options = options
        self.children = (df,)
        if (
            self.name not in MapFunction._NAMES
        ):  # pragma: no cover; need more polars rust functions
            raise NotImplementedError(
                f"Unhandled map function {self.name}"
            )  # pragma: no cover
        if self.name == "explode":
            (to_explode,) = self.options
            if len(to_explode) > 1:
                # TODO: straightforward, but need to error check
                # polars requires that all to-explode columns have the
                # same sub-shapes
                raise NotImplementedError("Explode with more than one column")
            self.options = (tuple(to_explode),)
        elif self.name == "rename":
            old, new, strict = self.options
            # TODO: perhaps polars should validate renaming in the IR?
            if len(new) != len(set(new)) or (
                set(new) & (set(df.schema.keys()) - set(old))
            ):
                raise NotImplementedError("Duplicate new names in rename.")
            self.options = (tuple(old), tuple(new), strict)
        elif self.name == "unpivot":
            indices, pivotees, variable_name, value_name = self.options
            value_name = "value" if value_name is None else value_name
            variable_name = "variable" if variable_name is None else variable_name
            if len(pivotees) == 0:
                index = frozenset(indices)
                pivotees = [name for name in df.schema if name not in index]
            if not all(
                dtypes.can_cast(df.schema[p], self.schema[value_name]) for p in pivotees
            ):
                raise NotImplementedError(
                    "Unpivot cannot cast all input columns to "
                    f"{self.schema[value_name].id()}"
                )  # pragma: no cover
            self.options = (
                tuple(indices),
                tuple(pivotees),
                variable_name,
                value_name,
            )
        elif self.name == "row_index":
            col_name, offset = options
            self.options = (col_name, offset)
        self._non_child_args = (schema, name, self.options)

    @classmethod
    def do_evaluate(
        cls, schema: Schema, name: str, options: Any, df: DataFrame
    ) -> DataFrame:
        """Evaluate and return a dataframe."""
        if name == "rechunk":
            # No-op in our data model
            # Don't think this appears in a plan tree from python
            return df  # pragma: no cover
        elif name == "rename":
            # final tag is "swapping" which is useful for the
            # optimiser (it blocks some pushdown operations)
            old, new, _ = options
            return df.rename_columns(dict(zip(old, new, strict=True)))
        elif name == "explode":
            ((to_explode,),) = options
            index = df.column_names.index(to_explode)
            subset = df.column_names_set - {to_explode}
            return DataFrame.from_table(
                plc.lists.explode_outer(df.table, index), df.column_names
            ).sorted_like(df, subset=subset)
        elif name == "unpivot":
            (
                indices,
                pivotees,
                variable_name,
                value_name,
            ) = options
            npiv = len(pivotees)
            index_columns = [
                Column(col, name=name)
                for col, name in zip(
                    plc.reshape.tile(df.select(indices).table, npiv).columns(),
                    indices,
                    strict=True,
                )
            ]
            (variable_column,) = plc.filling.repeat(
                plc.Table(
                    [
                        plc.interop.from_arrow(
                            pa.array(
                                pivotees,
                                type=plc.interop.to_arrow(schema[variable_name]),
                            ),
                        )
                    ]
                ),
                df.num_rows,
            ).columns()
            value_column = plc.concatenate.concatenate(
                [
                    df.column_map[pivotee].astype(schema[value_name]).obj
                    for pivotee in pivotees
                ]
            )
            return DataFrame(
                [
                    *index_columns,
                    Column(variable_column, name=variable_name),
                    Column(value_column, name=value_name),
                ]
            )
        elif name == "row_index":
            col_name, offset = options
            dtype = schema[col_name]
            step = plc.Scalar.from_py(1, dtype)
            init = plc.Scalar.from_py(offset, dtype)
            index_col = Column(
                plc.filling.sequence(df.num_rows, init, step),
                is_sorted=plc.types.Sorted.YES,
                order=plc.types.Order.ASCENDING,
                null_order=plc.types.NullOrder.AFTER,
                name=col_name,
            )
            return DataFrame([index_col, *df.columns])
        else:
            raise AssertionError("Should never be reached")  # pragma: no cover


class Union(IR):
    """Concatenate dataframes vertically."""

    __slots__ = ("zlice",)
    _non_child = ("schema", "zlice")
    zlice: Zlice | None
    """Optional slice to apply to the result."""

    def __init__(self, schema: Schema, zlice: Zlice | None, *children: IR):
        self.schema = schema
        self.zlice = zlice
        self._non_child_args = (zlice,)
        self.children = children
        schema = self.children[0].schema

    @classmethod
    def do_evaluate(cls, zlice: Zlice | None, *dfs: DataFrame) -> DataFrame:
        """Evaluate and return a dataframe."""
        # TODO: only evaluate what we need if we have a slice?
        return DataFrame.from_table(
            plc.concatenate.concatenate([df.table for df in dfs]),
            dfs[0].column_names,
        ).slice(zlice)


class HConcat(IR):
    """Concatenate dataframes horizontally."""

    __slots__ = ("should_broadcast",)
    _non_child = ("schema", "should_broadcast")

    def __init__(
        self,
        schema: Schema,
        should_broadcast: bool,  # noqa: FBT001
        *children: IR,
    ):
        self.schema = schema
        self.should_broadcast = should_broadcast
        self._non_child_args = (should_broadcast,)
        self.children = children

    @staticmethod
    def _extend_with_nulls(table: plc.Table, *, nrows: int) -> plc.Table:
        """
        Extend a table with nulls.

        Parameters
        ----------
        table
            Table to extend
        nrows
            Number of additional rows

        Returns
        -------
        New pylibcudf table.
        """
        return plc.concatenate.concatenate(
            [
                table,
                plc.Table(
                    [
                        plc.Column.all_null_like(column, nrows)
                        for column in table.columns()
                    ]
                ),
            ]
        )

    @classmethod
    def do_evaluate(
        cls,
        should_broadcast: bool,  # noqa: FBT001
        *dfs: DataFrame,
    ) -> DataFrame:
        """Evaluate and return a dataframe."""
        # Special should_broadcast case.
        # Used to recombine decomposed expressions
        if should_broadcast:
            return DataFrame(
                broadcast(*itertools.chain.from_iterable(df.columns for df in dfs))
            )

        max_rows = max(df.num_rows for df in dfs)
        # Horizontal concatenation extends shorter tables with nulls
        return DataFrame(
            itertools.chain.from_iterable(
                df.columns
                for df in (
                    df
                    if df.num_rows == max_rows
                    else DataFrame.from_table(
                        cls._extend_with_nulls(df.table, nrows=max_rows - df.num_rows),
                        df.column_names,
                    )
                    for df in dfs
                )
            )
        )


class Empty(IR):
    """Represents an empty DataFrame."""

    __slots__ = ()
    _non_child = ()

    def __init__(self) -> None:
        self.schema = {}
        self._non_child_args = ()
        self.children = ()

    @classmethod
    def do_evaluate(cls) -> DataFrame:  # pragma: no cover
        """Evaluate and return a dataframe."""
        return DataFrame([])
