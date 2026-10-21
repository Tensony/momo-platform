#!/usr/bin/env python3
"""Cross-platform build script for Lambda artifacts and dependency layer.

Works on both Windows and Linux/macOS.
Packages:
  - build/deps.zip (Lambda layer with manylinux dependencies)
  - build/saga-steps.zip
  - build/fraud-engine.zip
  - build/reconciliation.zip
  - build/db-init.zip
"""

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"
LAYER_DIR = BUILD_DIR / "layer" / "python"
PKG_DIR = BUILD_DIR / "pkg"
SRC_MOMO = ROOT / "src" / "momo"
SCHEMA_SQL = ROOT / "db" / "schema.sql"
REQS_TXT = ROOT / "requirements.txt"


def clean():
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    LAYER_DIR.mkdir(parents=True, exist_ok=True)
    PKG_DIR.mkdir(parents=True, exist_ok=True)


def build_deps_layer():
    print("Building dependencies layer...")
    # Attempt manylinux wheel download for Lambda x86_64 cp312
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--quiet",
        "--no-cache-dir",
        "-r",
        str(REQS_TXT),
        "--target",
        str(LAYER_DIR),
        "--platform",
        "manylinux2014_x86_64",
        "--implementation",
        "cp",
        "--python-version",
        "3.12",
        "--only-binary=:all:",
    ]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        print("Notice: binary manylinux wheels installation fallback to current env wheels...")
        cmd_fallback = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--quiet",
            "-r",
            str(REQS_TXT),
            "--target",
            str(LAYER_DIR),
        ]
        subprocess.run(cmd_fallback, check=True)

    # Exclude boto3 and botocore (already present in AWS Lambda runtime)
    for p in LAYER_DIR.glob("boto3*"):
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    for p in LAYER_DIR.glob("botocore*"):
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    for p in LAYER_DIR.glob("s3transfer*"):
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    for p in LAYER_DIR.rglob("__pycache__"):
        if p.is_dir():
            shutil.rmtree(p)

    deps_zip = BUILD_DIR / "deps.zip"
    with zipfile.ZipFile(deps_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(BUILD_DIR / "layer"):
            for file in files:
                full_path = Path(root) / file
                arcname = full_path.relative_to(BUILD_DIR / "layer")
                zf.write(full_path, arcname)
    print(f"Created: {deps_zip} ({deps_zip.stat().st_size / 1024:.1f} KB)")


def build_service_zips():
    services = ["saga-steps", "fraud-engine", "reconciliation", "db-init"]
    for fn in services:
        service_pkg = PKG_DIR / fn
        if service_pkg.exists():
            shutil.rmtree(service_pkg)
        service_pkg.mkdir(parents=True, exist_ok=True)

        # Copy src/momo -> service_pkg/momo
        shutil.copytree(SRC_MOMO, service_pkg / "momo", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

        if fn == "db-init" and SCHEMA_SQL.exists():
            shutil.copy(SCHEMA_SQL, service_pkg / "schema.sql")

        zip_path = BUILD_DIR / f"{fn}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(service_pkg):
                for file in files:
                    full_path = Path(root) / file
                    arcname = full_path.relative_to(service_pkg)
                    zf.write(full_path, arcname)
        print(f"Created: {zip_path} ({zip_path.stat().st_size / 1024:.1f} KB)")


def main():
    clean()
    build_deps_layer()
    build_service_zips()
    print("\nAll Lambda artifacts successfully built in build/ directory.")


if __name__ == "__main__":
    main()
