#!/usr/bin/env bash
set -e
source .venv/bin/activate 2>/dev/null || true
python runners/run_single_sync.py
