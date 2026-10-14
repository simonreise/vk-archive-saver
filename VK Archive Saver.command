#!/bin/sh
# macOS: двойной щелчок в Finder открывает окно программы.
# Если Finder не даёт запустить: в Терминале  chmod +x "VK Archive Saver.command"
cd "$(dirname "$0")" || exit 1
exec python3 -m vksaver
