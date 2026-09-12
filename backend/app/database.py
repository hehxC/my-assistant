from collections.abc import Generator

from sqlalchemy import URL, create_engine, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


settings = get_settings()

# URL.create 会正确转义密码和带连字符的数据库名，避免手工拼接连接串。
database_url = URL.create(
    drivername="mysql+pymysql",
    username=settings.mysql_user,
    password=settings.mysql_password,
    host=settings.mysql_host,
    port=settings.mysql_port,
    database=settings.mysql_database,
)

# pool_pre_ping 会在取出连接前检查连接是否可用，避免使用失效连接。
engine = create_engine(database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables() -> None:
    from .models import ApplicationState

    Base.metadata.create_all(bind=engine)

    inspector = inspect(engine)
    study_columns = {column["name"] for column in inspector.get_columns("study_sessions")}

    # create_all 不会修改已有表，因此旧数据库需要幂等补上用户归属列和索引。
    if "user_id" not in study_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE study_sessions ADD COLUMN user_id INTEGER NULL"))

    study_indexes = {index["name"] for index in inspect(engine).get_indexes("study_sessions")}
    if "ix_study_sessions_user_id" not in study_indexes:
        with engine.begin() as connection:
            connection.execute(
                text("CREATE INDEX ix_study_sessions_user_id ON study_sessions (user_id)")
            )

    with SessionLocal.begin() as db:
        if db.scalar(select(ApplicationState).where(ApplicationState.id == 1)) is None:
            db.add(ApplicationState(id=1))
