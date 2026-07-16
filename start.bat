@echo off
echo Installing dependencies and starting NovelAI Gateway...
uv sync
uv run main.py
pause
