"""One-time transactional restore of the synthetic checkpoint into an empty DB."""
import gzip
import io
from pathlib import Path

import psycopg2


def restore(database_url, seed=Path('/app/seed/milestone-5.sql.gz')):
    with psycopg2.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(650031)')
            cursor.execute("SELECT to_regclass('public.friday_restore_checkpoint')")
            if cursor.fetchone()[0]:
                cursor.execute('SELECT checkpoint FROM public.friday_restore_checkpoint')
                if cursor.fetchall() != [('milestone-5',)]:
                    raise RuntimeError('Unexpected database checkpoint')
                return
            cursor.execute("SELECT count(*) FROM pg_tables WHERE schemaname='public'")
            if cursor.fetchone()[0]:
                raise RuntimeError('Refusing to restore over an existing database')
            with gzip.open(seed, 'rt') as stream:
                sql = []
                for line in stream:
                    if line.startswith('COPY ') and line.rstrip().endswith('FROM stdin;'):
                        if any(row.strip() and not row.lstrip().startswith('--') for row in sql):
                            cursor.execute(''.join(sql))
                            sql = []
                        data = []
                        for row in stream:
                            if row.rstrip('\n') == '\\.':
                                break
                            data.append(row)
                        else:
                            raise RuntimeError('Incomplete checkpoint COPY block')
                        cursor.copy_expert(line, io.StringIO(''.join(data)))
                    else:
                        sql.append(line)
                if any(row.strip() and not row.lstrip().startswith('--') for row in sql):
                    cursor.execute(''.join(sql))
            cursor.execute('SET search_path TO public')
            cursor.execute('SELECT count(*) FROM llm_calls')
            if cursor.fetchone()[0] != 7:
                raise RuntimeError('Checkpoint ledger count mismatch')
            cursor.execute("SELECT count(*) FROM friday_slack_events WHERE state='delivered'")
            if cursor.fetchone()[0] != 1:
                raise RuntimeError('Checkpoint event count mismatch')
            cursor.execute('CREATE TABLE friday_restore_checkpoint (checkpoint text PRIMARY KEY)')
            cursor.execute("INSERT INTO friday_restore_checkpoint VALUES ('milestone-5')")
