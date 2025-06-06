#!/bin/bash
# Copyright (c) 2020-2025, NVIDIA CORPORATION.

set -euo pipefail

. /opt/conda/etc/profile.d/conda.sh

rapids-logger "Downloading artifacts from previous jobs"
CPP_CHANNEL=$(rapids-download-conda-from-github cpp)
PYTHON_CHANNEL=$(rapids-download-conda-from-github python)

rapids-logger "Generate notebook testing dependencies"

ENV_YAML_DIR="$(mktemp -d)"

rapids-dependency-file-generator \
  --output conda \
  --file-key test_notebooks \
  --prepend-channel "${CPP_CHANNEL}" \
  --prepend-channel "${PYTHON_CHANNEL}" \
  --matrix "cuda=${RAPIDS_CUDA_VERSION%.*};arch=$(arch);py=${RAPIDS_PY_VERSION}" | tee "${ENV_YAML_DIR}/env.yaml"

rapids-mamba-retry env create --yes -f "${ENV_YAML_DIR}/env.yaml" -n test

# Temporarily allow unbound variables for conda activation.
set +u
conda activate test
set -u

rapids-print-env

NBTEST="$(realpath "$(dirname "$0")/utils/nbtest.sh")"
pushd notebooks

# Add notebooks that should be skipped here
# (space-separated list of filenames without paths)
SKIPNBS="performance-comparisons.ipynb"

EXITCODE=0
trap "EXITCODE=1" ERR
set +e
# Loops over `find` are fragile but this seems to be working
# shellcheck disable=SC2044
for nb in $(find . -name "*.ipynb"); do
    nbBasename=$(basename "${nb}")
    # Skip all notebooks that use dask (in the code or even in their name)
    if (echo "${nb}" | grep -qi dask) || \
        (grep -q dask "${nb}"); then
        echo "--------------------------------------------------------------------------------"
        echo "SKIPPING: ${nb} (suspected Dask usage, not currently automatable)"
        echo "--------------------------------------------------------------------------------"
    elif (echo " ${SKIPNBS} " | grep -q " ${nbBasename} "); then
        echo "--------------------------------------------------------------------------------"
        echo "SKIPPING: ${nb} (listed in skip list)"
        echo "--------------------------------------------------------------------------------"
    else
        nvidia-smi
        ${NBTEST} "${nbBasename}"
    fi
done

rapids-logger "Test script exiting with value: $EXITCODE"
exit ${EXITCODE}
