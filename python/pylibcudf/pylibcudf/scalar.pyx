# Copyright (c) 2023-2025, NVIDIA CORPORATION.

from cpython cimport bool as py_bool, datetime
from cython cimport no_gc_clear
from libc.stdint cimport (
    int8_t,
    int16_t,
    int32_t,
    int64_t,
    uint8_t,
    uint16_t,
    uint32_t,
    uint64_t,
)
from libcpp.limits cimport numeric_limits
from libcpp cimport bool as cbool
from libcpp.memory cimport unique_ptr
from libcpp.utility cimport move
from pylibcudf.libcudf.scalar.scalar cimport (
    scalar,
    duration_scalar,
    numeric_scalar,
    timestamp_scalar,
)
from pylibcudf.libcudf.scalar.scalar_factories cimport (
    make_default_constructed_scalar,
    make_duration_scalar,
    make_empty_scalar_like,
    make_string_scalar,
    make_numeric_scalar,
    make_timestamp_scalar,
)
from pylibcudf.libcudf.types cimport type_id
from pylibcudf.libcudf.wrappers.durations cimport (
    duration_ms,
    duration_ns,
    duration_us,
    duration_s,
)
from pylibcudf.libcudf.wrappers.timestamps cimport (
    timestamp_s,
    timestamp_ms,
    timestamp_us,
    timestamp_ns,
)

from rmm.pylibrmm.memory_resource cimport get_current_device_resource

from .column cimport Column
from .traits cimport is_floating_point
from .types cimport DataType
from functools import singledispatch

try:
    import numpy as np
    np_error = None
except ImportError as err:
    np = None
    np_error = err

__all__ = ["Scalar"]


# The DeviceMemoryResource attribute could be released prematurely
# by the gc if the Scalar is in a reference cycle. Removing the tp_clear
# function with the no_gc_clear decoration prevents that. See
# https://github.com/rapidsai/rmm/pull/931 for details.
@no_gc_clear
cdef class Scalar:
    """A scalar value in device memory.

    This is the Cython representation of :cpp:class:`cudf::scalar`.
    """
    # Unlike for columns, libcudf does not support scalar views. All APIs that
    # accept scalar values accept references to the owning object rather than a
    # special view type. As a result, pylibcudf.Scalar has a simpler structure
    # than pylibcudf.Column because it can be a true wrapper around a libcudf
    # column

    def __cinit__(self, *args, **kwargs):
        self.mr = get_current_device_resource()

    def __init__(self, *args, **kwargs):
        # TODO: This case is not something we really want to
        # support, but it here for now to ease the transition of
        # DeviceScalar.
        raise ValueError("Scalar should be constructed with a factory")

    __hash__ = None

    cdef const scalar* get(self) noexcept nogil:
        return self.c_obj.get()

    cpdef DataType type(self):
        """The type of data in the column."""
        return self._data_type

    cpdef bool is_valid(self):
        """True if the scalar is valid, false if not"""
        return self.get().is_valid()

    @staticmethod
    cdef Scalar empty_like(Column column):
        """Construct a null scalar with the same type as column.

        Parameters
        ----------
        column
            Column to take type from

        Returns
        -------
        New empty (null) scalar of the given type.
        """
        return Scalar.from_libcudf(move(make_empty_scalar_like(column.view())))

    @staticmethod
    cdef Scalar from_libcudf(unique_ptr[scalar] libcudf_scalar, dtype=None):
        """Construct a Scalar object from a libcudf scalar.

        This method is for pylibcudf's functions to use to ingest outputs of
        calling libcudf algorithms, and should generally not be needed by users
        (even direct pylibcudf Cython users).
        """
        cdef Scalar s = Scalar.__new__(Scalar)
        s.c_obj.swap(libcudf_scalar)
        s._data_type = DataType.from_libcudf(s.get().type())
        return s

    @classmethod
    def from_py(cls, py_val, dtype: DataType | None = None):
        """
        Convert a Python standard library object to a Scalar.

        Parameters
        ----------
        py_val: None, bool, int, float, str, datetime, timedelta, list, dict
            Value to convert to a pylibcudf.Scalar
        dtype: DataType | None
            The datatype to cast the value to. If None,
            the type is inferred from `py_val`.

        Returns
        -------
        Scalar
            New pylibcudf.Scalar
        """
        return _from_py(py_val, dtype)

    @classmethod
    def from_numpy(cls, np_val):
        """
        Convert a NumPy scalar to a Scalar.

        Parameters
        ----------
        np_val: numpy.generic
            Value to convert to a pylibcudf.Scalar

        Returns
        -------
        Scalar
            New pylibcudf.Scalar
        """
        return _from_numpy(np_val)


cdef Scalar _new_scalar(unique_ptr[scalar] c_obj, DataType dtype):
    cdef Scalar s = Scalar.__new__(Scalar)
    s.c_obj.swap(c_obj)
    s._data_type = dtype
    return s


@singledispatch
def _from_py(py_val, dtype: DataType | None):
    raise TypeError(f"{type(py_val).__name__} cannot be converted to pylibcudf.Scalar")


@_from_py.register(type(None))
def _(py_val, dtype: DataType | None):
    cdef DataType c_dtype
    if dtype is None:
        raise ValueError("Must specify a dtype for a None value.")
    else:
        c_dtype = <DataType>dtype
    cdef unique_ptr[scalar] c_obj = make_default_constructed_scalar(c_dtype.c_obj)
    return _new_scalar(move(c_obj), dtype)


@_from_py.register(dict)
@_from_py.register(list)
def _(py_val, dtype: DataType | None):
    raise NotImplementedError(
        f"Conversion from {type(py_val).__name__} is currently not supported."
    )


@_from_py.register(float)
def _(py_val: float, dtype: DataType | None):
    cdef unique_ptr[scalar] c_obj
    cdef DataType c_dtype
    if dtype is None:
        c_dtype = DataType(type_id.FLOAT64)
    else:
        c_dtype = <DataType>dtype

    cdef type_id tid = c_dtype.id()

    if tid == type_id.FLOAT32:
        if abs(py_val) > numeric_limits[float].max():
            raise OverflowError(f"{py_val} out of range for FLOAT32 scalar")
        c_obj = make_numeric_scalar(c_dtype.c_obj)
        (<numeric_scalar[float]*>c_obj.get()).set_value(py_val)
    elif tid == type_id.FLOAT64:
        c_obj = make_numeric_scalar(c_dtype.c_obj)
        (<numeric_scalar[double]*>c_obj.get()).set_value(py_val)
    else:
        typ = c_dtype.id()
        raise TypeError(f"Cannot convert float to Scalar with dtype {typ.name}")

    return _new_scalar(move(c_obj), dtype)


@_from_py.register(int)
def _(py_val: int, dtype: DataType | None):
    cdef unique_ptr[scalar] c_obj
    cdef DataType c_dtype
    if dtype is None:
        c_dtype = DataType(type_id.INT64)
    elif is_floating_point(dtype):
        return _from_py(float(py_val), dtype)
    else:
        c_dtype = <DataType>dtype
    cdef type_id tid = c_dtype.id()

    if tid == type_id.INT8:
        if not (
            numeric_limits[int8_t].min() <= py_val <= numeric_limits[int8_t].max()
        ):
            raise OverflowError(f"{py_val} out of range for INT8 scalar")
        c_obj = make_numeric_scalar(c_dtype.c_obj)
        (<numeric_scalar[int8_t]*>c_obj.get()).set_value(py_val)

    elif tid == type_id.INT16:
        if not (
            numeric_limits[int16_t].min() <= py_val <= numeric_limits[int16_t].max()
        ):
            raise OverflowError(f"{py_val} out of range for INT16 scalar")
        c_obj = make_numeric_scalar(c_dtype.c_obj)
        (<numeric_scalar[int16_t]*>c_obj.get()).set_value(py_val)

    elif tid == type_id.INT32:
        if not (
            numeric_limits[int32_t].min() <= py_val <= numeric_limits[int32_t].max()
        ):
            raise OverflowError(f"{py_val} out of range for INT32 scalar")
        c_obj = make_numeric_scalar(c_dtype.c_obj)
        (<numeric_scalar[int32_t]*>c_obj.get()).set_value(py_val)

    elif tid == type_id.INT64:
        if not (
            numeric_limits[int64_t].min() <= py_val <= numeric_limits[int64_t].max()
        ):
            raise OverflowError(f"{py_val} out of range for INT64 scalar")
        c_obj = make_numeric_scalar(c_dtype.c_obj)
        (<numeric_scalar[int64_t]*>c_obj.get()).set_value(py_val)

    elif tid == type_id.UINT8:
        if py_val < 0:
            raise ValueError("Cannot assign negative value to UINT8 scalar")
        if py_val > numeric_limits[uint8_t].max():
            raise OverflowError(f"{py_val} out of range for UINT8 scalar")
        c_obj = make_numeric_scalar(c_dtype.c_obj)
        (<numeric_scalar[uint8_t]*>c_obj.get()).set_value(py_val)

    elif tid == type_id.UINT16:
        if py_val < 0:
            raise ValueError("Cannot assign negative value to UINT16 scalar")
        if py_val > numeric_limits[uint16_t].max():
            raise OverflowError(f"{py_val} out of range for UINT16 scalar")
        c_obj = make_numeric_scalar(c_dtype.c_obj)
        (<numeric_scalar[uint16_t]*>c_obj.get()).set_value(py_val)

    elif tid == type_id.UINT32:
        if py_val < 0:
            raise ValueError("Cannot assign negative value to UINT32 scalar")
        if py_val > numeric_limits[uint32_t].max():
            raise OverflowError(f"{py_val} out of range for UINT32 scalar")
        c_obj = make_numeric_scalar(c_dtype.c_obj)
        (<numeric_scalar[uint32_t]*>c_obj.get()).set_value(py_val)

    elif tid == type_id.UINT64:
        if py_val < 0:
            raise ValueError("Cannot assign negative value to UINT64 scalar")
        if py_val > numeric_limits[uint64_t].max():
            raise OverflowError(f"{py_val} out of range for UINT64 scalar")
        c_obj = make_numeric_scalar(c_dtype.c_obj)
        (<numeric_scalar[uint64_t]*>c_obj.get()).set_value(py_val)

    else:
        typ = c_dtype.id()
        raise TypeError(f"Cannot convert int to Scalar with dtype {typ.name}")

    return _new_scalar(move(c_obj), dtype)


@_from_py.register(py_bool)
def _(py_val: py_bool, dtype: DataType | None):
    if dtype is None:
        dtype = DataType(type_id.BOOL8)
    elif dtype.id() != type_id.BOOL8:
        tid = (<DataType>dtype).id()
        raise TypeError(
            f"Cannot convert bool to Scalar with dtype {tid.name}"
        )

    cdef unique_ptr[scalar] c_obj = make_numeric_scalar((<DataType>dtype).c_obj)
    (<numeric_scalar[cbool]*>c_obj.get()).set_value(py_val)
    return _new_scalar(move(c_obj), dtype)


@_from_py.register(str)
def _(py_val: str, dtype: DataType | None):
    if dtype is None:
        dtype = DataType(type_id.STRING)
    elif dtype.id() != type_id.STRING:
        tid = (<DataType>dtype).id()
        raise TypeError(
            f"Cannot convert str to Scalar with dtype {tid.name}"
        )
    cdef unique_ptr[scalar] c_obj = make_string_scalar(py_val.encode())
    return _new_scalar(move(c_obj), dtype)


@_from_py.register(datetime.timedelta)
def _(py_val: datetime.timedelta, dtype: DataType | None):
    cdef unique_ptr[scalar] c_obj
    cdef DataType c_dtype
    cdef duration_us c_duration_us
    cdef duration_ns c_duration_ns
    cdef duration_ms c_duration_ms
    cdef duration_s c_duration_s
    if dtype is None:
        c_dtype = DataType(type_id.DURATION_MICROSECONDS)
    else:
        c_dtype = <DataType>dtype
    cdef type_id tid = c_dtype.id()
    total_seconds = py_val.total_seconds()
    if tid == type_id.DURATION_NANOSECONDS:
        total_nanoseconds = int(total_seconds * 1_000_000_000)
        if total_nanoseconds > numeric_limits[int64_t].max():
            raise OverflowError(
                f"{total_nanoseconds} nanoseconds out of range for INT64 limit."
            )
        c_obj = make_duration_scalar(c_dtype.c_obj)
        c_duration_ns = duration_ns(<int64_t>total_nanoseconds)
        (<duration_scalar[duration_ns]*>c_obj.get()).set_value(c_duration_ns)
    elif tid == type_id.DURATION_MICROSECONDS:
        total_microseconds = int(total_seconds * 1_000_000)
        if total_microseconds > numeric_limits[int64_t].max():
            raise OverflowError(
                f"{total_microseconds} microseconds out of range for INT64 limit."
            )
        c_obj = make_duration_scalar(c_dtype.c_obj)
        c_duration_us = duration_us(<int64_t>total_microseconds)
        (<duration_scalar[duration_us]*>c_obj.get()).set_value(c_duration_us)
    elif tid == type_id.DURATION_MILLISECONDS:
        total_milliseconds = int(total_seconds * 1_000)
        if total_milliseconds > numeric_limits[int64_t].max():
            raise OverflowError(
                f"{total_milliseconds} milliseconds out of range for INT64 limit."
            )
        c_obj = make_duration_scalar(c_dtype.c_obj)
        c_duration_ms = duration_ms(<int64_t>total_milliseconds)
        (<duration_scalar[duration_ms]*>c_obj.get()).set_value(c_duration_ms)
    elif tid == type_id.DURATION_SECONDS:
        total_seconds = int(total_seconds)
        if total_seconds > numeric_limits[int64_t].max():
            raise OverflowError(
                f"{total_seconds} seconds out of range for INT64 limit."
            )
        c_obj = make_duration_scalar(c_dtype.c_obj)
        c_duration_s = duration_s(<int64_t>total_seconds)
        (<duration_scalar[duration_s]*>c_obj.get()).set_value(c_duration_s)
    else:
        typ = c_dtype.id()
        raise TypeError(f"Cannot convert timedelta to Scalar with dtype {typ.name}")
    return _new_scalar(move(c_obj), dtype)


@_from_py.register(datetime.datetime)
def _(py_val: datetime.datetime, dtype: DataType | None):
    cdef unique_ptr[scalar] c_obj
    cdef DataType c_dtype
    cdef duration_us c_duration_us
    cdef duration_ns c_duration_ns
    cdef duration_ms c_duration_ms
    cdef duration_s c_duration_s
    cdef timestamp_s c_timestamp_s
    cdef timestamp_ms c_timestamp_ms
    cdef timestamp_us c_timestamp_us
    cdef timestamp_ns c_timestamp_ns
    if dtype is None:
        c_dtype = DataType(type_id.TIMESTAMP_MICROSECONDS)
    else:
        c_dtype = <DataType>dtype
    cdef type_id tid = c_dtype.id()
    epoch_seconds = py_val.timestamp()
    if tid == type_id.TIMESTAMP_NANOSECONDS:
        epoch_nanoseconds = int(epoch_seconds * 1_000_000_000)
        if epoch_nanoseconds > numeric_limits[int64_t].max():
            raise OverflowError(
                f"{epoch_nanoseconds} nanoseconds out of range for INT64 limit."
            )
        c_obj = make_timestamp_scalar(c_dtype.c_obj)
        c_duration_ns = duration_ns(<int64_t>epoch_nanoseconds)
        c_timestamp_ns = timestamp_ns(c_duration_ns)
        (<timestamp_scalar[timestamp_ns]*>c_obj.get()).set_value(c_timestamp_ns)
    elif tid == type_id.TIMESTAMP_MICROSECONDS:
        epoch_microseconds = int(epoch_seconds * 1_000_000)
        if epoch_microseconds > numeric_limits[int64_t].max():
            raise OverflowError(
                f"{epoch_microseconds} microseconds out of range for INT64 limit."
            )
        c_obj = make_timestamp_scalar(c_dtype.c_obj)
        c_duration_us = duration_us(<int64_t>epoch_microseconds)
        c_timestamp_us = timestamp_us(c_duration_us)
        (<timestamp_scalar[timestamp_us]*>c_obj.get()).set_value(c_timestamp_us)
    elif tid == type_id.TIMESTAMP_MILLISECONDS:
        epoch_milliseconds = int(epoch_seconds * 1_000)
        if epoch_milliseconds > numeric_limits[int64_t].max():
            raise OverflowError(
                f"{epoch_milliseconds} milliseconds out of range for INT64 limit."
            )
        c_obj = make_timestamp_scalar(c_dtype.c_obj)
        c_duration_ms = duration_ms(<int64_t>epoch_milliseconds)
        c_timestamp_ms = timestamp_ms(c_duration_ms)
        (<timestamp_scalar[timestamp_ms]*>c_obj.get()).set_value(c_timestamp_ms)
    elif tid == type_id.TIMESTAMP_SECONDS:
        epoch_seconds = int(epoch_seconds)
        if epoch_seconds > numeric_limits[int64_t].max():
            raise OverflowError(
                f"{epoch_seconds} seconds out of range for INT64 limit."
            )
        c_obj = make_timestamp_scalar(c_dtype.c_obj)
        c_duration_s = duration_s(<int64_t>epoch_seconds)
        c_timestamp_s = timestamp_s(c_duration_s)
        (<timestamp_scalar[timestamp_s]*>c_obj.get()).set_value(c_timestamp_s)
    else:
        typ = c_dtype.id()
        raise TypeError(f"Cannot convert datetime to Scalar with dtype {typ.name}")
    return _new_scalar(move(c_obj), dtype)


@singledispatch
def _from_numpy(np_val):
    if np_error is not None:
        raise np_error
    raise TypeError(f"{type(np_val).__name__} cannot be converted to pylibcudf.Scalar")


if np is not None:
    @_from_numpy.register(np.datetime64)
    @_from_numpy.register(np.timedelta64)
    def _(np_val):
        raise NotImplementedError(
            f"{type(np_val).__name__} is currently not supported."
        )

    @_from_numpy.register(np.bool_)
    def _(np_val):
        cdef DataType dtype = DataType(type_id.BOOL8)
        cdef unique_ptr[scalar] c_obj = make_numeric_scalar(dtype.c_obj)
        cdef cbool c_val = np_val
        (<numeric_scalar[cbool]*>c_obj.get()).set_value(c_val)
        cdef Scalar slr = _new_scalar(move(c_obj), dtype)
        return slr

    @_from_numpy.register(np.str_)
    def _(np_val):
        cdef DataType dtype = DataType(type_id.STRING)
        cdef unique_ptr[scalar] c_obj = make_string_scalar(np_val.item().encode())
        cdef Scalar slr = _new_scalar(move(c_obj), dtype)
        return slr

    @_from_numpy.register(np.int8)
    def _(np_val):
        dtype = DataType(type_id.INT8)
        cdef unique_ptr[scalar] c_obj = make_numeric_scalar(dtype.c_obj)
        (<numeric_scalar[int8_t]*>c_obj.get()).set_value(np_val)
        cdef Scalar slr = _new_scalar(move(c_obj), dtype)
        return slr

    @_from_numpy.register(np.int16)
    def _(np_val):
        dtype = DataType(type_id.INT16)
        cdef unique_ptr[scalar] c_obj = make_numeric_scalar(dtype.c_obj)
        (<numeric_scalar[int16_t]*>c_obj.get()).set_value(np_val)
        cdef Scalar slr = _new_scalar(move(c_obj), dtype)
        return slr

    @_from_numpy.register(np.int32)
    def _(np_val):
        dtype = DataType(type_id.INT32)
        cdef unique_ptr[scalar] c_obj = make_numeric_scalar(dtype.c_obj)
        (<numeric_scalar[int32_t]*>c_obj.get()).set_value(np_val)
        cdef Scalar slr = _new_scalar(move(c_obj), dtype)
        return slr

    @_from_numpy.register(np.int64)
    def _(np_val):
        dtype = DataType(type_id.INT64)
        cdef unique_ptr[scalar] c_obj = make_numeric_scalar(dtype.c_obj)
        (<numeric_scalar[int64_t]*>c_obj.get()).set_value(np_val)
        cdef Scalar slr = _new_scalar(move(c_obj), dtype)
        return slr

    @_from_numpy.register(np.uint8)
    def _(np_val):
        dtype = DataType(type_id.UINT8)
        cdef unique_ptr[scalar] c_obj = make_numeric_scalar(dtype.c_obj)
        (<numeric_scalar[uint8_t]*>c_obj.get()).set_value(np_val)
        cdef Scalar slr = _new_scalar(move(c_obj), dtype)
        return slr

    @_from_numpy.register(np.uint16)
    def _(np_val):
        dtype = DataType(type_id.UINT16)
        cdef unique_ptr[scalar] c_obj = make_numeric_scalar(dtype.c_obj)
        (<numeric_scalar[uint16_t]*>c_obj.get()).set_value(np_val)
        cdef Scalar slr = _new_scalar(move(c_obj), dtype)
        return slr

    @_from_numpy.register(np.uint32)
    def _(np_val):
        dtype = DataType(type_id.UINT32)
        cdef unique_ptr[scalar] c_obj = make_numeric_scalar(dtype.c_obj)
        (<numeric_scalar[uint32_t]*>c_obj.get()).set_value(np_val)
        cdef Scalar slr = _new_scalar(move(c_obj), dtype)
        return slr

    @_from_numpy.register(np.uint64)
    def _(np_val):
        dtype = DataType(type_id.UINT64)
        cdef unique_ptr[scalar] c_obj = make_numeric_scalar(dtype.c_obj)
        (<numeric_scalar[uint64_t]*>c_obj.get()).set_value(np_val)
        cdef Scalar slr = _new_scalar(move(c_obj), dtype)
        return slr

    @_from_numpy.register(np.float32)
    def _(np_val):
        dtype = DataType(type_id.FLOAT32)
        cdef unique_ptr[scalar] c_obj = make_numeric_scalar(dtype.c_obj)
        (<numeric_scalar[float]*>c_obj.get()).set_value(np_val)
        cdef Scalar slr = _new_scalar(move(c_obj), dtype)
        return slr

    @_from_numpy.register(np.float64)
    def _(np_val):
        dtype = DataType(type_id.FLOAT64)
        cdef unique_ptr[scalar] c_obj = make_numeric_scalar(dtype.c_obj)
        (<numeric_scalar[double]*>c_obj.get()).set_value(np_val)
        cdef Scalar slr = _new_scalar(move(c_obj), dtype)
        return slr
