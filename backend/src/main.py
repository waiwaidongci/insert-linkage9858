import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from sqlalchemy import inspect, text
from scripts.seed_demo import seed
from src.config.database import Base, engine
from src.config.settings import SEED_DEMO
from src.domain.spec import SPEC
from src.middlewares import errors, headers, rate
from src.routes import distribution, identity, registry, runs
from src.utils.wakeup import broker

log = logging.getLogger(__name__)


def ensure_columns():
    """仅补建新增列，不改动既有数据；JSON 列按数据库方言选择类型。"""
    inspector = inspect(engine)
    if 'runs' not in inspector.get_table_names():
        return
    existing = {column['name'] for column in inspector.get_columns('runs')}
    if 'progress' in existing:
        return
    ddl = 'ALTER TABLE runs ADD COLUMN progress JSON NULL'
    if engine.dialect.name == 'sqlite':
        ddl = 'ALTER TABLE runs ADD COLUMN progress JSON'
    with engine.begin() as connection:
        connection.execute(text(ddl))
    log.info('Added runs.progress checkpoint column')


@asynccontextmanager
async def lifespan(_app):
    Base.metadata.create_all(engine)
    ensure_columns()
    if SEED_DEMO:
        try:
            log.info('Seeded %s', seed())
        except Exception:
            log.exception('Demo seed failed')
    yield
    engine.dispose()


app = FastAPI(title=SPEC['title'], lifespan=lifespan)
errors.install(app)
headers.install(app)
rate.install(app)
for module in (identity, registry, runs):
    app.include_router(module.router)
if SPEC.get('scheduled'):
    app.include_router(distribution.router)


@app.get('/healthz', include_in_schema=False)
def health():
    with engine.connect() as connection:
        connection.execute(text('SELECT 1'))
    broker.ping()
    return {'status': 'ok'}
