#!/usr/bin/env python3
"""
Run once to establish a persistent RealGeeks session.
A browser window will open — complete 2FA there if prompted.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).parent / ".env")
from realgeeks_client import pre_login

pre_login(
    os.environ["REALGEEKS_LOGIN_URL"],
    os.environ["REALGEEKS_USER"],
    os.environ["REALGEEKS_PASS"],
)
