/*
 * Copyright (c) 2019-2025, NVIDIA CORPORATION.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include <benchmarks/join/join_common.hpp>

#include <cudf/join/join.hpp>

template <typename Key, bool Nullable>
void nvbench_inner_join(nvbench::state& state,
                        nvbench::type_list<Key, nvbench::enum_type<Nullable>>)
{
  auto join = [](cudf::table_view const& left_input,
                 cudf::table_view const& right_input,
                 cudf::null_equality compare_nulls) {
    return cudf::inner_join(left_input, right_input, compare_nulls);
  };
  BM_join<Key, Nullable>(state, join);
}

template <typename Key, bool Nullable>
void nvbench_left_join(nvbench::state& state, nvbench::type_list<Key, nvbench::enum_type<Nullable>>)
{
  auto join = [](cudf::table_view const& left_input,
                 cudf::table_view const& right_input,
                 cudf::null_equality compare_nulls) {
    return cudf::left_join(left_input, right_input, compare_nulls);
  };
  BM_join<Key, Nullable>(state, join);
}

template <typename Key, bool Nullable>
void nvbench_full_join(nvbench::state& state, nvbench::type_list<Key, nvbench::enum_type<Nullable>>)
{
  auto join = [](cudf::table_view const& left_input,
                 cudf::table_view const& right_input,
                 cudf::null_equality compare_nulls) {
    return cudf::full_join(left_input, right_input, compare_nulls);
  };
  BM_join<Key, Nullable>(state, join);
}

NVBENCH_BENCH_TYPES(nvbench_inner_join, NVBENCH_TYPE_AXES(JOIN_KEY_TYPE_RANGE, JOIN_NULLABLE_RANGE))
  .set_name("inner_join")
  .set_type_axes_names({"Key", "Nullable"})
  .add_int64_axis("left_size", JOIN_SIZE_RANGE)
  .add_int64_axis("right_size", JOIN_SIZE_RANGE);

NVBENCH_BENCH_TYPES(nvbench_left_join, NVBENCH_TYPE_AXES(JOIN_KEY_TYPE_RANGE, JOIN_NULLABLE_RANGE))
  .set_name("left_join")
  .set_type_axes_names({"Key", "Nullable"})
  .add_int64_axis("left_size", JOIN_SIZE_RANGE)
  .add_int64_axis("right_size", JOIN_SIZE_RANGE);

NVBENCH_BENCH_TYPES(nvbench_full_join, NVBENCH_TYPE_AXES(JOIN_KEY_TYPE_RANGE, JOIN_NULLABLE_RANGE))
  .set_name("full_join")
  .set_type_axes_names({"Key", "Nullable"})
  .add_int64_axis("left_size", JOIN_SIZE_RANGE)
  .add_int64_axis("right_size", JOIN_SIZE_RANGE);
