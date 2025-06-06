# Copyright (c) 2025, NVIDIA CORPORATION.

from cython.operator import dereference

from libcpp.memory cimport unique_ptr, make_unique
from libcpp.utility cimport move
from pylibcudf.column cimport Column
from pylibcudf.libcudf.column.column cimport column
from pylibcudf.libcudf.nvtext.deduplicate cimport (
    substring_duplicates as cpp_substring_duplicates,
    build_suffix_array as cpp_build_suffix_array,
    suffix_array_type as cpp_suffix_array_type,
)
from pylibcudf.libcudf.types cimport size_type

from rmm.librmm.device_buffer cimport device_buffer

__all__ = [
    "substring_duplicates",
    "build_suffix_array",
]

cdef Column _column_from_suffix_array(cpp_suffix_array_type suffix_array):
    # helper to convert a suffix array to a Column
    return Column.from_libcudf(
        move(
            make_unique[column](
                move(dereference(suffix_array.get())),
                device_buffer(),
                0
            )
        )
    )

cpdef Column substring_duplicates(Column input, size_type min_width):
    """
    Returns duplicate strings found anywhere in the input column
    with min_width minimum number of bytes.

    For details, see :cpp:func:`substring_duplicates`

    Parameters
    ----------
    input : Column
        Strings column of text
    min_width : size_type
        Minimum width of bytes to detect duplicates

    Returns
    -------
    Column
        New column of duplicate strings
    """
    cdef unique_ptr[column] c_result

    with nogil:
        c_result = cpp_substring_duplicates(input.view(), min_width)

    return Column.from_libcudf(move(c_result))


cpdef Column build_suffix_array(Column input, size_type min_width):
    """
    Builds a suffix array for the input strings column.
    A suffix array is the indices of the sorted set of substrings
    of the input column as: [ input[0:], input[1:], ... input[bytes-1:] ]
    where bytes is the total number of bytes in input.
    The returned array represent the sorted strings such that
    result[i] = input[result[i]:]

    For details, see :cpp:func:`build_suffix_array`

    Parameters
    ----------
    input : Column
        Strings column of text

    Returns
    -------
    Column
        New column of suffix array
    """
    cdef cpp_suffix_array_type c_result

    with nogil:
        c_result = cpp_build_suffix_array(input.view(), min_width)

    return _column_from_suffix_array(move(c_result))
