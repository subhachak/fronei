from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, TwinProfile, WritingSample, get_twin_profile


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def test_get_twin_profile_none(db):
    assert get_twin_profile(db, "u1") is None


def test_get_twin_profile_returns_record(db):
    p = TwinProfile(user_id="u1", created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(p)
    db.commit()
    assert get_twin_profile(db, "u1") is not None
    assert get_twin_profile(db, "u2") is None


def test_writing_sample_char_count(db):
    s = WritingSample(
        user_id="u1",
        content="hello world",
        char_count=11,
        created_at=datetime.utcnow(),
    )
    db.add(s)
    db.commit()
    assert db.query(WritingSample).filter(WritingSample.user_id == "u1").count() == 1
