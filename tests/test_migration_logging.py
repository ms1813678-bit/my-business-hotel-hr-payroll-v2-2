"""In-process migrations preserve existing application logger availability."""
import logging
from pathlib import Path
from uuid import uuid4
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from app.db import engine
from app import ai_local_config as cfg


def test_inprocess_migration_does_not_disable_existing_application_warning():
    schema='logging_'+uuid4().hex[:12]
    previous=cfg._log.disabled
    try:
        cfg._log.disabled=False
        with engine.begin() as db:
            db.execute(text(f'CREATE SCHEMA "{schema}"'))
            db.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config=Config(str(Path(__file__).resolve().parents[1]/'alembic.ini'))
            config.attributes['connection']=db
            command.upgrade(config,'head')
            assert cfg._log.disabled is False
            # Capture after migration, whose fileConfig can change root handlers.
            from unittest import TestCase
            with TestCase().assertLogs(cfg._log,level=logging.WARNING) as output:
                assert cfg.resolve_model({'LOCAL_AI_MODEL':'unsafe model'},'/nonexistent/.env')==cfg.DEFAULT_MODEL
            assert output.records and all('unsafe model' not in item.getMessage() for item in output.records)
    finally:
        cfg._log.disabled=previous
        with engine.begin() as db:db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
