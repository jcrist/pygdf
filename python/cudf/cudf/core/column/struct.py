# Copyright (c) 2020-2025, NVIDIA CORPORATION.
from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING, Any

import pandas as pd
import pyarrow as pa

import cudf
from cudf.core.column.column import ColumnBase
from cudf.core.column.methods import ColumnMethods
from cudf.core.dtypes import StructDtype
from cudf.core.scalar import pa_scalar_to_plc_scalar
from cudf.utils.utils import _is_null_host_scalar

if TYPE_CHECKING:
    from typing_extensions import Self

    import pylibcudf as plc

    from cudf._typing import Dtype
    from cudf.core.buffer import Buffer
    from cudf.core.column.string import StringColumn


def _maybe_na_to_none(value: Any) -> Any:
    """
    Convert NA-like values to None for pyarrow.
    """
    if _is_null_host_scalar(value):
        return None
    else:
        return value


class StructColumn(ColumnBase):
    """
    Column that stores fields of values.

    Every column has n children, where n is
    the number of fields in the Struct Dtype.
    """

    def __init__(
        self,
        data: None,
        size: int,
        dtype: StructDtype,
        mask: Buffer | None = None,
        offset: int = 0,
        null_count: int | None = None,
        children: tuple[ColumnBase, ...] = (),
    ):
        if data is not None:
            raise ValueError("data must be None.")
        dtype = self._validate_dtype_instance(dtype)
        super().__init__(
            data=data,
            size=size,
            dtype=dtype,
            mask=mask,
            offset=offset,
            null_count=null_count,
            children=children,
        )

    def _prep_pandas_compat_repr(self) -> StringColumn | Self:
        """
        Preprocess Column to be compatible with pandas repr, namely handling nulls.

        * null (datetime/timedelta) = str(pd.NaT)
        * null (other types)= str(pd.NA)
        """
        # TODO: handle if self.has_nulls(): case
        return self

    @staticmethod
    def _validate_dtype_instance(dtype: StructDtype) -> StructDtype:
        # IntervalDtype is a subclass of StructDtype, so compare types exactly
        if type(dtype) is not StructDtype:
            raise ValueError(
                f"{type(dtype).__name__} must be a StructDtype exactly."
            )
        return dtype

    @property
    def base_size(self):
        if self.base_children:
            return len(self.base_children[0])
        else:
            return self.size + self.offset

    def to_arrow(self) -> pa.Array:
        children = [child.to_arrow() for child in self.children]

        pa_type = pa.struct(
            {
                field: child.type
                for field, child in zip(self.dtype.fields, children)
            }
        )

        if self.mask is not None:
            buffers = (pa.py_buffer(self.mask.memoryview()),)
        else:
            buffers = (None,)

        return pa.StructArray.from_buffers(
            pa_type, len(self), buffers, children=children
        )

    def to_pandas(
        self,
        *,
        nullable: bool = False,
        arrow_type: bool = False,
    ) -> pd.Index:
        # We cannot go via Arrow's `to_pandas` because of the following issue:
        # https://issues.apache.org/jira/browse/ARROW-12680
        if arrow_type or nullable:
            return super().to_pandas(nullable=nullable, arrow_type=arrow_type)
        else:
            return pd.Index(self.to_arrow().tolist(), dtype="object")

    @cached_property
    def memory_usage(self) -> int:
        n = super().memory_usage
        for child in self.children:
            n += child.memory_usage

        return n

    def element_indexing(self, index: int) -> dict:
        result = super().element_indexing(index)
        return self.dtype._recursively_replace_fields(result)

    def _cast_setitem_value(self, value: Any) -> plc.Scalar:
        if isinstance(value, dict):
            new_value = {
                field: _maybe_na_to_none(value.get(field, None))
                for field in self.dtype.fields
            }
            return pa_scalar_to_plc_scalar(
                pa.scalar(new_value, type=self.dtype.to_arrow())
            )
        elif value is None or value is cudf.NA:
            return pa_scalar_to_plc_scalar(
                pa.scalar(None, type=self.dtype.to_arrow())
            )
        else:
            raise ValueError(
                f"Can not set {type(value).__name__} into StructColumn"
            )

    def copy(self, deep: bool = True) -> Self:
        # Since struct columns are immutable, both deep and
        # shallow copies share the underlying device data and mask.
        result = super().copy(deep=False)
        if deep:
            result = result._rename_fields(self.dtype.fields.keys())
        return result

    def _rename_fields(self, names) -> Self:
        """
        Return a StructColumn with the same field values as this StructColumn,
        but with the field names equal to `names`.
        """
        dtype = StructDtype(
            {name: col.dtype for name, col in zip(names, self.children)}
        )
        return StructColumn(  # type: ignore[return-value]
            data=None,
            size=self.size,
            dtype=dtype,
            mask=self.base_mask,
            offset=self.offset,
            null_count=self.null_count,
            children=self.base_children,
        )

    @property
    def __cuda_array_interface__(self):
        raise NotImplementedError(
            "Structs are not yet supported via `__cuda_array_interface__`"
        )

    def _with_type_metadata(self: StructColumn, dtype: Dtype) -> StructColumn:
        from cudf.core.column import IntervalColumn
        from cudf.core.dtypes import IntervalDtype

        # Check IntervalDtype first because it's a subclass of StructDtype
        if isinstance(dtype, IntervalDtype):
            return IntervalColumn.from_struct_column(self, closed=dtype.closed)
        elif isinstance(dtype, StructDtype):
            return StructColumn(
                data=None,
                dtype=dtype,
                children=tuple(
                    self.base_children[i]._with_type_metadata(dtype.fields[f])
                    for i, f in enumerate(dtype.fields.keys())
                ),
                mask=self.base_mask,
                size=self.size,
                offset=self.offset,
                null_count=self.null_count,
            )

        return self


class StructMethods(ColumnMethods):
    """
    Struct methods for Series
    """

    _column: StructColumn

    def __init__(self, parent=None):
        if not isinstance(parent.dtype, StructDtype):
            raise AttributeError(
                "Can only use .struct accessor with a 'struct' dtype"
            )
        super().__init__(parent=parent)

    def field(self, key):
        """
        Extract children of the specified struct column
        in the Series

        Parameters
        ----------
        key: int or str
            index/position or field name of the respective
            struct column

        Returns
        -------
        Series

        Examples
        --------
        >>> s = cudf.Series([{'a': 1, 'b': 2}, {'a': 3, 'b': 4}])
        >>> s.struct.field(0)
        0    1
        1    3
        dtype: int64
        >>> s.struct.field('a')
        0    1
        1    3
        dtype: int64
        """
        fields = list(self._column.dtype.fields.keys())
        if key in fields:
            pos = fields.index(key)
            return self._return_or_inplace(self._column.children[pos])
        else:
            if isinstance(key, int):
                try:
                    return self._return_or_inplace(self._column.children[key])
                except IndexError:
                    raise IndexError(f"Index {key} out of range")
            else:
                raise KeyError(
                    f"Field '{key}' is not found in the set of existing keys."
                )

    def explode(self):
        """
        Return a DataFrame whose columns are the fields of this struct Series.

        Notes
        -----
        Note that a copy of the columns is made.

        Examples
        --------
        >>> s
        0    {'a': 1, 'b': 'x'}
        1    {'a': 2, 'b': 'y'}
        2    {'a': 3, 'b': 'z'}
        3    {'a': 4, 'b': 'a'}
        dtype: struct

        >>> s.struct.explode()
           a  b
        0  1  x
        1  2  y
        2  3  z
        3  4  a
        """
        data = {
            name: col.copy(deep=True)
            for name, col in zip(
                self._column.dtype.fields, self._column.children
            )
        }
        rangeindex = len(data) == 0
        return cudf.DataFrame._from_data(
            cudf.core.column_accessor.ColumnAccessor(
                data, rangeindex=rangeindex
            )
        )
