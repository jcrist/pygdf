# Copyright (c) 2025, NVIDIA CORPORATION.

from pylibcudf.column import Column

def substring_duplicates(input: Column, min_width: int) -> Column: ...
def build_suffix_array(input: Column, min_width: int) -> Column: ...
