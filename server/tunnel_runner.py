"""Reconnect the explicitly authorized localhost.run test tunnel until stopped."""
from pathlib import Path
import os
import subprocess
import time

from .db import Database
from .tunnel_watch import synchronize


def main():
    directory = Database().path.parent
    config = directory / 'ssh-tunnel-config'
    executable = Path('C:/Program Files/Git/usr/bin/ssh.exe')
    if not config.is_file() or not executable.is_file():
        raise SystemExit('The configured test tunnel is unavailable.')
    # Only one supervisor may own the forwarding process for this data directory.
    import msvcrt
    with (directory / 'tunnel-runner.lock').open('a+b') as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return
        stop = directory / 'tunnel.stop'
        while not stop.exists():
            with (directory / 'ssh-tunnel.log').open('wb') as output, (directory / 'ssh-tunnel-error.log').open('wb') as errors:
                process = subprocess.Popen([str(executable), '-T', '-F', str(config), 'salavat-webapp'],
                    stdout=output, stderr=errors, stdin=subprocess.DEVNULL, close_fds=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                try:
                    while process.poll() is None and not stop.exists():
                        synchronize(directory)
                        time.sleep(2)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=15)
            synchronize(directory)
            for _ in range(5):
                if stop.exists():
                    break
                time.sleep(2)


if __name__ == '__main__':
    main()
