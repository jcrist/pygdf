#!/bin/bash
# Copyright (c) 2023-2025, NVIDIA CORPORATION.

set -euo pipefail

RAPIDS_VERSION="$(rapids-version)"
RAPIDS_VERSION_MAJOR_MINOR="$(rapids-version-major-minor)"
export RAPIDS_VERSION
export RAPIDS_VERSION_MAJOR_MINOR

rapids-logger "Create test conda environment"
. /opt/conda/etc/profile.d/conda.sh

ENV_YAML_DIR="$(mktemp -d)"

rapids-logger "Downloading artifacts from previous jobs"
CPP_CHANNEL=$(rapids-download-conda-from-github cpp)
PYTHON_CHANNEL=$(rapids-download-conda-from-github python)

rapids-dependency-file-generator \
  --output conda \
  --file-key docs \
  --prepend-channel "${CPP_CHANNEL}" \
  --prepend-channel "${PYTHON_CHANNEL}" \
  --matrix "cuda=${RAPIDS_CUDA_VERSION%.*};arch=$(arch);py=${RAPIDS_PY_VERSION}" | tee "${ENV_YAML_DIR}/env.yaml"

rapids-mamba-retry env create --yes -f "${ENV_YAML_DIR}/env.yaml" -n docs
conda activate docs

rapids-print-env

RAPIDS_DOCS_DIR="$(mktemp -d)"
export RAPIDS_DOCS_DIR

EXITCODE=0
trap "EXITCODE=1" ERR
set +e

rapids-logger "Build CPP docs"
pushd cpp/doxygen
aws s3 cp s3://rapidsai-docs/librmm/html/"${RAPIDS_VERSION_MAJOR_MINOR}"/rmm.tag . || echo "Failed to download rmm Doxygen tag"
doxygen Doxyfile
mkdir -p "${RAPIDS_DOCS_DIR}/libcudf/html"
mv html/* "${RAPIDS_DOCS_DIR}/libcudf/html"
popd

rapids-logger "Build Python docs"
pushd docs/cudf
make dirhtml O="-j 8"
mkdir -p "${RAPIDS_DOCS_DIR}/cudf/html"
mv build/dirhtml/* "${RAPIDS_DOCS_DIR}/cudf/html"
popd

rapids-logger "Build dask-cuDF Sphinx docs"
pushd docs/dask_cudf
make dirhtml
mkdir -p "${RAPIDS_DOCS_DIR}/dask-cudf/html"
mv build/dirhtml/* "${RAPIDS_DOCS_DIR}/dask-cudf/html"
popd

RAPIDS_VERSION_NUMBER="${RAPIDS_VERSION_MAJOR_MINOR}" rapids-upload-docs

exit ${EXITCODE}
