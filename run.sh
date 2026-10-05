#!/usr/bin/env bash
set -e

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q -r requirements.txt

if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

python app.py