import os

from alembic import context

from mycomfy import ops_models  # noqa: F401  注册容量表，纳入 Alembic 结构对比。
from mycomfy.db import make_sessions
from mycomfy.models import Base

url = context.config.attributes.get("database_url") or os.environ["MYCOMFY_DATABASE_URL"]

if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = make_sessions(url).kw["bind"]
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
