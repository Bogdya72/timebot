#!/bin/zsh
set -e

PROJECT_DIR="/Users/bogdanbogdanov/Desktop/TimeBot"
VENV_DIR="$PROJECT_DIR/.venv"
PYTHON_BIN=""

if [ -x "/opt/homebrew/bin/python3.11" ]; then
  PYTHON_BIN="/opt/homebrew/bin/python3.11"
elif [ -x "/opt/homebrew/bin/python3.12" ]; then
  PYTHON_BIN="/opt/homebrew/bin/python3.12"
elif [ -x "/opt/homebrew/bin/python3" ]; then
  PYTHON_BIN="/opt/homebrew/bin/python3"
fi

if [ -z "$PYTHON_BIN" ]; then
  echo "Python not found. Install Python 3.12+ and retry."
  exit 1
fi

if [ -d "$VENV_DIR" ]; then
  CURRENT_PY="$VENV_DIR/bin/python"
  if [ -x "$CURRENT_PY" ]; then
    VENV_VER=$("$CURRENT_PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
  else
    VENV_VER=""
  fi
  TARGET_VER=$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
  if [ "$VENV_VER" != "$TARGET_VER" ]; then
    echo "Recreating venv for Python $TARGET_VER (was $VENV_VER)"
    rm -rf "$VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi
else
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

pip install -r "$PROJECT_DIR/requirements.txt"

python "$PROJECT_DIR/app/main.py"
