from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Turn, Workspace, Base


def test_text_columns_strip_nul_before_persistence():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    db = Session()
    try:
        workspace = Workspace(id="w1", user_id="u1", name="Client\x00deck")
        db.add(workspace)
        db.commit()
        db.refresh(workspace)

        turn = Turn(
            id="t1",
            user_id="u1",
            conversation_id="c1",
            objective="Research summary\x00 feeding document plan",
            route="quick",
            answer="ok\x00",
        )
        db.add(turn)
        db.commit()
        db.refresh(turn)

        assert "\x00" not in workspace.name
        assert turn.objective == "Research summary feeding document plan"
        assert turn.answer == "ok"
    finally:
        db.close()
