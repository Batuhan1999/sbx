"""Verify PostgreSQL clones share physical blocks and retain independent state."""
from pathlib import Path
import secrets

from . import cow_storage


def verify(server, storage, docker):
    """server: a hosted_services.Server (its container, data path and admin role)."""
    def sql(database, text, check=True):
        return docker('exec', '-i', server.container, 'psql', '-U', server.admin, '-X', '-qAt',
                      '-v', 'ON_ERROR_STOP=1', '-d', database, input=text, check=check)

    settings = sql('postgres', "SELECT current_setting('server_version_num')::int >= 180000 "
                   "AND current_setting('file_copy_method', true) = 'clone';").stdout.strip()
    if settings != 't':
        raise RuntimeError('Postgres must be version 18 or later with file_copy_method=clone')
    prefix = 'cow_probe_' + secrets.token_hex(8)
    original, clone = prefix + '_source', prefix + '_clone'
    try:
        sql('postgres', f'CREATE DATABASE {original} TEMPLATE template0;')
        sql(original, 'CREATE TABLE probe (id integer PRIMARY KEY, payload text); '
            'ALTER TABLE probe ALTER COLUMN payload SET STORAGE PLAIN; '
            "INSERT INTO probe SELECT g, repeat(md5(g::text), 32) FROM generate_series(1,8192) g; CHECKPOINT;")
        sql('postgres', f'CREATE DATABASE {clone} TEMPLATE {original} STRATEGY FILE_COPY;')
        data = Path(sql('postgres', 'SHOW data_directory;').stdout.strip())
        relative = data.relative_to(server.data_path)
        source_file = sql(original, "SELECT pg_relation_filepath('probe');").stdout.strip()
        clone_file = sql(clone, "SELECT pg_relation_filepath('probe');").stdout.strip()
        root = Path(storage['path']) / relative
        first, second = root / source_file, root / clone_file
        if not first.resolve().is_relative_to(root.resolve()) or not second.resolve().is_relative_to(root.resolve()):
            raise RuntimeError('Postgres reported a relation outside managed storage')
        size = first.stat().st_size
        shared = cow_storage.shared_bytes(first, second)
        if size < 8 * 1024 * 1024 or shared < size * 0.9:
            raise RuntimeError('Postgres copied the populated template instead of sharing its data blocks')
        sql(clone, "ALTER TABLE probe ADD COLUMN migrated boolean DEFAULT true; "
            "UPDATE probe SET payload='changed' WHERE id=1; DELETE FROM probe WHERE id=2;")
        independent = sql(original, "SELECT count(*) = 8192 AND min(length(payload)) = 1024 "
                          "AND NOT EXISTS (SELECT FROM information_schema.columns "
                          "WHERE table_name='probe' AND column_name='migrated') FROM probe;").stdout.strip()
        if independent != 't':
            raise RuntimeError('the clone changed the source database')
        return {'verified': True, 'template_bytes': size, 'shared_bytes': shared,
                'independent_migrations': True, 'file_copy_method': 'clone', 'filesystem': 'xfs'}
    finally:
        failures = []
        for database in (clone, original):
            result = sql('postgres', f'DROP DATABASE IF EXISTS {database} WITH (FORCE);', check=False)
            if result.returncode:
                failures.append(database)
        if failures:
            raise RuntimeError('could not clean up disposable verification databases: ' + ', '.join(failures))
