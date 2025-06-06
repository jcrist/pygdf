# Copyright (c) 2025, NVIDIA CORPORATION.

from pylibcudf.io.types import SourceInfo

__all__ = [
    "ParquetColumnSchema",
    "ParquetMetadata",
    "ParquetSchema",
    "read_parquet_metadata",
]

class ParquetColumnSchema:
    def name(self) -> str: ...
    def num_children(self) -> int: ...
    def child(self, idx: int) -> ParquetColumnSchema: ...
    def children(self) -> list[ParquetColumnSchema]: ...

class ParquetSchema:
    def root(self) -> ParquetColumnSchema: ...

class ParquetMetadata:
    def schema(self) -> ParquetSchema: ...
    def num_rows(self) -> int: ...
    def num_rowgroups(self) -> int: ...
    def num_rowgroups_per_file(self) -> list[int]: ...
    def metadata(self) -> dict[str, str]: ...
    def rowgroup_metadata(self) -> list[dict[str, int]]: ...
    def columnchunk_metadata(self) -> dict[str, list[int]]: ...

def read_parquet_metadata(src_info: SourceInfo) -> ParquetMetadata: ...
