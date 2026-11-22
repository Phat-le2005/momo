#!/usr/bin/env bash
# Thin wrapper around the Makefile for environments that prefer a script.
set -euo pipefail
cd "$(dirname "$0")/.."
exec make "${1:-check}"
