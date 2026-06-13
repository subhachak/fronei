import re

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base
from app.db.models import Conversation


def test_conversation_public_id_uses_96_bit_hex_token():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    try:
        conv = Conversation(user_id="u1")
        db.add(conv)
        db.flush()

        assert re.fullmatch(r"[0-9a-f]{24}", conv.public_id)
    finally:
        db.close()
