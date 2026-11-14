"""Launch Amp from its writable update destination, preserving workspace state;
with --stop, stop the orb's bootstraps instead (before a pause, after a resume).
Shipped next to the platform's guest helper (core.ship_guest), which it imports."""
import os
from pathlib import Path
import shutil
import sys
import tempfile

import sbx_guest

SEED = Path('/opt/sbx-amp/bin/amp')   # where the template layer installs Amp (Amp.contribution)


def runtime_binary(home, seed):
    directory = home / '.amp/bin'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / 'amp'
    if not target.is_file() or not os.access(target, os.X_OK):
        descriptor, temporary = tempfile.mkstemp(prefix='.sbx-amp-', dir=directory)
        try:
            with os.fdopen(descriptor, 'wb') as output, seed.open('rb') as source:
                shutil.copyfileobj(source, output)
            os.chmod(temporary, 0o755)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return target


def bootstrap_process(process, executables):
    """Our own `amp orb bootstrap` running one of executables (an updated
    one's replaced inode reads as "<path> (deleted)")."""
    try:
        return (process.stat().st_uid == os.getuid()
                and str((process / 'exe').resolve()).removesuffix(' (deleted)') in {str(p) for p in executables}
                and (process / 'cmdline').read_bytes().split(b'\0')[1:3] == [b'orb', b'bootstrap'])
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False


def stop_bootstraps(executables):
    """Stop every bootstrap of executables; the headless executor and the
    app keep running. Raises when one survives SIGKILL."""
    for process in sbx_guest.PROC.iterdir():
        if process.name.isdigit() and bootstrap_process(process, executables):
            if not sbx_guest.terminate(int(process.name), lambda: bootstrap_process(process, executables)):
                raise RuntimeError('the Amp bootstrap did not stop')


def main():
    os.umask(0o077)
    if sys.argv[1:] == ['--stop']:   # before a pause, and after a resume (Amp.before_pause, Amp.on_resume)
        stop_bootstraps((SEED, Path.home() / '.amp/bin/amp'))
        return
    if sys.argv[1:]:
        raise SystemExit('usage: amp-bootstrap.py [--stop]')
    executable = runtime_binary(Path.home(), SEED)
    stop_bootstraps((SEED,))
    # Keep diagnostics private; provider output must never render bootstrap tokens.
    log = sbx_guest.RUN / 'amp-bootstrap.log'
    with log.open('a') as output:
        os.chmod(log, 0o600)
        os.dup2(output.fileno(), 1)
        os.dup2(output.fileno(), 2)
    os.execve(executable, [str(executable), 'orb', 'bootstrap'], os.environ)


if __name__ == '__main__':
    main()
