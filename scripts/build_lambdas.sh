#!/usr/bin/env bash
# Builds the Lambda artifacts Terraform deploys: one shared dependency layer + one zip per service.
# Deps are fetched as manylinux wheels so they run on Lambda regardless of the build machine.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf build && mkdir -p build/layer/python build/pkg

pip install --quiet --no-cache-dir -r requirements.txt \
  --target build/layer/python \
  --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 --only-binary=:all:
# boto3/botocore are provided by the Lambda runtime: leave them out (smaller layer, faster cold start)
rm -rf build/layer/python/boto3* build/layer/python/botocore* build/layer/python/s3transfer* build/layer/python/__pycache__
(cd build/layer && python3 -m zipfile -c ../deps.zip python)

for fn in saga-steps fraud-engine reconciliation db-init; do
  rm -rf "build/pkg/$fn" && mkdir -p "build/pkg/$fn"
  cp -r src/momo "build/pkg/$fn/momo"
  find "build/pkg/$fn" -name __pycache__ -prune -exec rm -rf {} +
  [ "$fn" = "db-init" ] && cp db/schema.sql "build/pkg/$fn/schema.sql"
  (cd "build/pkg/$fn" && python3 -m zipfile -c "../../$fn.zip" .)
done
ls -la build/*.zip
