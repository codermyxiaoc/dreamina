"""Serialize additive PostgreSQL migrations across independently starting processes."""
from contextlib import contextmanager

@contextmanager
def schema_migration_lock(engine):
    if engine.dialect.name!='postgresql':
        yield;return
    connection=engine.connect()
    try:
        connection.exec_driver_sql('SELECT pg_advisory_lock(721305419)');connection.commit()
        try:yield
        finally:
            connection.exec_driver_sql('SELECT pg_advisory_unlock(721305419)');connection.commit()
    finally:connection.close()
