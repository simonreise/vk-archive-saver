# -*- coding: utf-8 -*-
# Окно программы. Windows: двойной щелчок (без консоли).
# Linux/macOS: ./vksaver.sh  или  python3 "VK Archive Saver.pyw"
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vksaver.__main__ import main  # noqa: E402

main([])
