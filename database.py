import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# =====================
# DB PATH
# =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "opost.db")

# =====================
# ENGINE
# =====================
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    echo=False  # 👈 مهم: يمنع spam في التيرمنال (خليه True للتصحيح فقط)
)

# =====================
# SESSION
# =====================
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

# =====================
# BASE MODEL
# =====================
Base = declarative_base()