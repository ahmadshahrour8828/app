import os
import time
from contextlib import asynccontextmanager
from typing import Generator

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Integer, String, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from prometheus_fastapi_instrumentator import Instrumentator

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://appuser:ChangeThis_To_A_Strong_Password@postgres:5432/appdb",
)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=1800,
    connect_args={"connect_timeout": 5},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Base(DeclarativeBase):
    pass


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)


class CourseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class CourseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str | None = None


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Wait briefly for PostgreSQL to become reachable when the API starts.
    last_error = None
    for attempt in range(1, 16):
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            Base.metadata.create_all(bind=engine)
            last_error = None
            break
        except SQLAlchemyError as exc:
            last_error = exc
            print(f"PostgreSQL not ready (attempt {attempt}/15): {exc}", flush=True)
            time.sleep(2)

    if last_error is not None:
        # Keep the app running so /health remains useful; /ready will report the DB issue.
        print("PostgreSQL is still unavailable; readiness checks will fail until it returns.", flush=True)

    yield
    engine.dispose()


app = FastAPI(
    title="Education API",
    description="FastAPI service backed by PostgreSQL",
    version="1.0.0",
    lifespan=lifespan,
)

# Prometheus HTTP request metrics
Instrumentator().instrument(app).expose(
    app,
    endpoint="/metrics",
    include_in_schema=False,
)

@app.get("/")
def root():
    return {"message": "Education API", "docs": "/docs", "health": "/health", "ready": "/ready"}


@app.get("/health")
def health():
    # Liveness only: confirms the API process is responding.
    return {"status": "ok"}


@app.get("/ready")
def ready():
    # Readiness: confirms PostgreSQL can answer a query.
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ready", "database": "connected"}
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is unavailable",
        ) from exc


@app.get("/courses", response_model=list[CourseRead])
def list_courses(db: Session = Depends(get_db)):
    return db.query(Course).order_by(Course.id).all()


@app.post("/courses", response_model=CourseRead, status_code=status.HTTP_201_CREATED)
def create_course(payload: CourseCreate, db: Session = Depends(get_db)):
    course = Course(title=payload.title.strip(), description=payload.description)
    if not course.title:
        raise HTTPException(status_code=422, detail="title must not be blank")
    db.add(course)
    try:
        db.commit()
        db.refresh(course)
        return course
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Could not save course") from exc
