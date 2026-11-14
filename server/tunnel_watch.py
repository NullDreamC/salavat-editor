"""Keep the approved localhost.run test URL synchronized as its domain rotates."""
import json
import re
import time

from .db import Database


def synchronize(directory):
    try:
        output = (directory / 'ssh-tunnel.log').read_text(encoding='utf-8')
        urls = re.findall(r'https://[a-z0-9]+\.(?:app\.)?lhr\.life\b', output)
        if not urls:
            return
        target = directory / 'webapp.json'
        content = json.dumps({'url': urls[-1]})
        if target.exists() and target.read_text(encoding='utf-8') == content:
            return
        pending = directory / 'webapp.json.tmp'
        pending.write_text(content, encoding='utf-8')
        pending.replace(target)
    except (OSError, ValueError):
        # A concurrent log rotation or antivirus lock is retried on the next tick.
        return


if __name__ == '__main__':
    directory = Database().path.parent
    while True:
        synchronize(directory)
        time.sleep(2)
