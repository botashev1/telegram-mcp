import json
import os
from pathlib import Path
import sys

ROOT = Path('/Library/TelegramReadOnly')
sys.path.insert(0, str(ROOT / 'src'))

from telegram_mcp.server import serve

credentials_file = ROOT / 'private' / 'credentials.json'
if credentials_file.is_file():
    credentials = json.loads(credentials_file.read_text())
    os.environ['TELEGRAM_API_ID'] = str(credentials['api_id'])
    os.environ['TELEGRAM_API_HASH'] = credentials['api_hash']
    os.environ['TELEGRAM_SESSION_STRING'] = credentials['session']
    del credentials

serve(ROOT / 'config' / 'allowed_chats.json')
