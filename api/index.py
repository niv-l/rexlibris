"""Vercel serverless function entry point."""

import sys
import os
import threading

# Add project root to path so we can import rexlibris
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rexlibris import WebHandler, AppConfig, _word_supply

# Initialize on cold start
_app_config = AppConfig()
WebHandler.app_config = _app_config

# Prime word supply in background (non-blocking)
threading.Thread(target=_word_supply.prime, daemon=True).start()


# Vercel expects a class named 'handler' (lowercase)
class handler(WebHandler):
    pass
