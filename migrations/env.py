import os

from alembic import context

from mycomfy.database import capacity_models, prompt_models  # noqa: F401  注册独立模块的业务表。
from mycomfy.database.models import Base
from mycomfy.database.session import make_sessions

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
