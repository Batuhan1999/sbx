"""Owned XFS storage and mount ordering for managed servers: PostgreSQL's
copy-on-write volume, and the disk of fixed size a package's server keeps
all its volumes on."""
import fcntl
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile

from . import paths

UNITS = Path('/etc/systemd/system')
GIB = 1024 ** 3


def run(*args):
    result = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip()[-1000:]}")
    return result.stdout.strip()


def plan(server, data_root=None, size_gb=32):
    if data_root:
        root = Path(data_root)
        if not root.is_absolute() or not root.is_dir():
            raise ValueError('--data-root must be an existing absolute XFS directory')
        if run('findmnt', '-n', '-o', 'FSTYPE', '--target', root) != 'xfs':
            raise ValueError('--data-root must already be on XFS; its filesystem will not be changed')
        path = root.resolve() / f'sbx-{server.name}'
        storage = {'mode': 'directory', 'path': str(path)}
    else:
        if not 1 <= size_gb <= 4096:
            raise ValueError('--storage-gb must be between 1 and 4096')
        root = paths.state_dir() / 'volumes' / f'postgres-{server.name}'
        if root.exists() and any(root.iterdir()):
            raise ValueError('an unregistered storage directory already exists; it will not be adopted or formatted')
        storage = {'mode': 'xfs-image', 'path': str(root / 'data'),
                   'image': str(root / 'volume.xfs'), 'size_gb': size_gb}
    if any(not re.fullmatch(r'/[A-Za-z0-9_./-]+', value) for key, value in storage.items() if key in ('path', 'image')):
        raise ValueError('storage paths may contain letters, numbers, slashes, periods, underscores and hyphens')
    if Path(storage['path']).exists() and any(Path(storage['path']).iterdir()):
        raise ValueError('refusing to adopt an existing data directory; choose a fresh location')
    return storage


def extents(path):
    """Return synchronized FIEMAP extents, including the kernel's shared flag."""
    found, offset = [], 0
    with Path(path).open('rb') as file:
        while True:
            buffer = bytearray(32 + 56 * 128)
            struct.pack_into('=QQIIII', buffer, 0, offset, (1 << 64) - 1 - offset, 1, 0, 128, 0)
            fcntl.ioctl(file.fileno(), 0xC020660B, buffer, True)
            count = struct.unpack_from('=I', buffer, 20)[0]
            if not count:
                break
            for i in range(count):
                logical, physical, length, _, _, flags, *_ = struct.unpack_from('=QQQQQIIII', buffer, 32 + 56 * i)
                found.append((physical, length, flags))
                offset = logical + length
            if flags & 1:
                break
    return found


def shared_bytes(first, second):
    if Path(first).stat().st_dev != Path(second).stat().st_dev:
        return 0
    a = [(start, start + size) for start, size, flags in extents(first) if flags & 0x2000]
    b = [(start, start + size) for start, size, flags in extents(second) if flags & 0x2000]
    return sum(max(0, min(a_end, b_end) - max(a_start, b_start))
               for a_start, a_end in a for b_start, b_end in b)


def probe(path):
    """Require actual shared blocks from the syscall PostgreSQL uses."""
    with tempfile.TemporaryDirectory(prefix='.sbx-cow-', dir=path) as directory:
        first, second = Path(directory) / 'source', Path(directory) / 'clone'
        first.write_bytes(os.urandom(1024 * 1024))
        with first.open('rb') as source, second.open('wb') as target:
            remaining = first.stat().st_size
            while remaining:
                copied = os.copy_file_range(source.fileno(), target.fileno(), remaining)
                if not copied:
                    raise RuntimeError('copy_file_range stopped before copying the probe')
                remaining -= copied
        shared = shared_bytes(first, second)
        if shared < first.stat().st_size:
            raise RuntimeError('storage copied bytes instead of sharing blocks; CoW is unavailable')
        return shared


def volume_plan(name, size_gb):
    """Where a package server's disk goes: an image of size_gb, all of it
    reserved, mounted where its volumes are directories. The server can fill
    its own disk and nothing else."""
    if isinstance(size_gb, bool) or not isinstance(size_gb, int) or not 1 <= size_gb <= 4096:
        raise ValueError('--storage-gb must be between 1 and 4096')
    root = paths.state_dir() / 'volumes' / f'server-{name}'
    if root.exists() and any(root.iterdir()):
        raise ValueError('an unregistered storage directory already exists; it will not be adopted or formatted')
    storage = {'mode': 'xfs-image', 'path': str(root / 'data'), 'image': str(root / 'volume.xfs'), 'size_gb': size_gb}
    if any(not re.fullmatch(r'/[A-Za-z0-9_./-]+', storage[key]) for key in ('path', 'image')):
        raise ValueError('storage paths may contain letters, numbers, slashes, periods, underscores and hyphens')
    return storage


def volume_prepare(name, storage, volumes):
    """The server's disk made (once) and mounted, with a directory for each
    of its volumes; returns {volume: directory}. Its mount comes before
    Docker at boot: a container started without it finds no directory to
    mount and stays down, instead of writing to the host's own disk."""
    path, image = Path(storage['path']), Path(storage['image'])
    if path.is_symlink() or image.is_symlink():
        raise ValueError('managed storage paths must not be symlinks')
    image.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not image.exists():
        _create_image(image, storage['size_gb'], 'the server')
    if run('blkid', '-p', '-s', 'TYPE', '-o', 'value', image) != 'xfs':
        raise ValueError('the existing managed image is not XFS; it will not be reformatted')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    mount_unit = run('systemd-escape', '--path', '--suffix=mount', path)
    (UNITS / mount_unit).write_text(
        f'[Unit]\nDescription=sbx managed server {name} storage\nRequiresMountsFor={image.parent}\n'
        'Before=docker.service\n'
        f'[Mount]\nWhat={image}\nWhere={path}\nType=xfs\nOptions=loop,nodev,nosuid\n'
        '[Install]\nWantedBy=multi-user.target\n')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', mount_unit)
    if not os.path.ismount(path):
        raise RuntimeError("the server's storage is not mounted; nothing was started")
    found = {}
    for volume in volumes:
        (path / volume).mkdir(mode=0o755, exist_ok=True)
        found[volume] = str(path / volume)
    return found


def volume_remove(storage):
    """Unmount and delete a package server's disk, after its containers are gone."""
    path, image = Path(storage['path']), Path(storage['image'])
    mount_unit = run('systemd-escape', '--path', '--suffix=mount', path)
    if (UNITS / mount_unit).exists():
        run('systemctl', 'disable', '--now', mount_unit)
    if os.path.ismount(path):
        raise RuntimeError("the server's storage is still mounted; no data file was removed")
    (UNITS / mount_unit).unlink(missing_ok=True)
    run('systemctl', 'daemon-reload')
    image.unlink(missing_ok=True)
    if path.exists():
        path.rmdir()
    if image.parent.exists():
        image.parent.rmdir()


def _create_image(image, size_gb, what):
    """A file of size_gb, every block reserved, formatted XFS; removed again if either fails."""
    size = size_gb * GIB
    if shutil.disk_usage(image.parent).free < size + 2 * GIB:
        raise ValueError(f'not enough disk for the requested volume of {what} plus 2 GiB headroom')
    created = False
    try:
        with image.open('xb') as file:
            created = True
            image.chmod(0o600)
            os.posix_fallocate(file.fileno(), 0, size)
        run('mkfs.xfs', '-m', 'reflink=1', image)
    except BaseException:
        if created:
            image.unlink(missing_ok=True)
        raise


def prepare(storage):
    path = Path(storage['path'])
    if path.is_symlink() or (storage.get('image') and Path(storage['image']).is_symlink()):
        raise ValueError('managed storage paths must not be symlinks')
    mount_unit = ''
    if storage['mode'] == 'xfs-image':
        image = Path(storage['image'])
        image.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not image.exists():
            _create_image(image, storage['size_gb'], 'Postgres')
        if run('blkid', '-p', '-s', 'TYPE', '-o', 'value', image) != 'xfs':
            raise ValueError('the existing managed image is not XFS; it will not be reformatted')
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        mount_unit = run('systemd-escape', '--path', '--suffix=mount', path)
        unit = (f'[Unit]\nDescription=sbx Postgres CoW storage\nRequiresMountsFor={image.parent}\n'
                f'[Mount]\nWhat={image}\nWhere={path}\nType=xfs\nOptions=loop,nodev,nosuid\n'
                '[Install]\nWantedBy=multi-user.target\n')
        (UNITS / mount_unit).write_text(unit)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', '--now', mount_unit)
    else:
        path.mkdir(mode=0o755, parents=True, exist_ok=True)
        # The image drops to postgres; its private PGDATA is below this bind root.
        path.chmod(0o755)
    if run('findmnt', '-n', '-o', 'FSTYPE', '--target', path) != 'xfs':
        raise ValueError('managed Postgres requires XFS with reflinks; the existing filesystem is unchanged')
    try:
        probe(path)
    except OSError as error:
        raise RuntimeError(f'could not verify shared blocks on Postgres storage: {error}') from None
    return mount_unit


def remove(server, storage):
    """Remove only the recorded volume after its container has been removed."""
    service = UNITS / server.unit
    path = Path(storage['path'])
    if storage['mode'] == 'xfs-image':
        mount_unit = run('systemd-escape', '--path', '--suffix=mount', path)
        if (UNITS / mount_unit).exists():
            run('systemctl', 'disable', '--now', mount_unit)
        if os.path.ismount(path):
            raise RuntimeError('Postgres storage is still mounted; no data file was removed')
        (UNITS / mount_unit).unlink(missing_ok=True)
        Path(storage['image']).unlink(missing_ok=True)
        if path.exists():
            path.rmdir()
        if Path(storage['image']).parent.exists():
            Path(storage['image']).parent.rmdir()
    elif path.exists():
        shutil.rmtree(path)
    service.unlink(missing_ok=True)
    run('systemctl', 'daemon-reload')


def start(server, storage, mount_unit):
    container = server.container
    path = storage['path']
    binds = f'BindsTo={mount_unit}\nAfter={mount_unit}\n' if mount_unit else ''
    unit = (f'[Unit]\nDescription=sbx managed Postgres {server.name}\nRequires=docker.service\nAfter=docker.service\n'
            f'RequiresMountsFor={path}\n{binds}'
            f'[Service]\nExecStartPre=/usr/bin/findmnt -n -t xfs --target {path}\n'
            f'ExecStart=/usr/bin/docker start --attach {container}\n'
            f'ExecStop=/usr/bin/docker stop --time 30 {container}\nRestart=always\nRestartSec=5\nTimeoutStopSec=45\n'
            '[Install]\nWantedBy=multi-user.target\n')
    service = server.unit
    (UNITS / service).write_text(unit)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', service)
    return service
