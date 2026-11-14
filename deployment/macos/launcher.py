import os
from pathlib import Path
import pwd
import sys

ROOT = Path('/Library/TelegramReadOnly')
if len(sys.argv) != 1 or os.getuid() != pwd.getpwnam('_telegram_read').pw_uid:
    raise SystemExit('Use the fixed Telegram read-only service launcher.')
sys.path.insert(0, str(ROOT / 'egress'))
from telegram_egress import run_sandboxed_child

environment = {
    'HOME': str(ROOT / 'private'), 'USER': '_telegram_read', 'LOGNAME': '_telegram_read',
    'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8', 'OTEL_SDK_DISABLED': 'true',
}
command = [str(ROOT / 'python/bin/python3.12'), '-I', '-B', str(ROOT / 'server_entry.py')]
raise SystemExit(run_sandboxed_child(command, environment, str(ROOT)))
