#!/bin/sh
# Запуск на Linux и macOS.
#   ./vksaver.sh                 — окно программы
#   ./vksaver.sh scan all        — командная строка (см. ./vksaver.sh --help)
# Если файл не запускается: sh vksaver.sh   (или chmod +x vksaver.sh)
cd "$(dirname "$0")" || exit 1
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python
exec "$PY" -m vksaver "$@"
