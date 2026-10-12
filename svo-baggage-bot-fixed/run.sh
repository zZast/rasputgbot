#!/bin/zsh
set -e
cd "$(dirname "$0")"
source .venv/bin/activate
python -m pip install -r requirements.txt
python bot.py
