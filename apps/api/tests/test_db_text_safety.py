from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, Conversation, ConversationMessage, ResearchRun, ResearchSource


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
        conv = Conversation(user_id="u1", title="Client\x00deck")
        db.add(conv)
        db.commit()
        db.refresh(conv)

        msg = ConversationMessage(
            conversation_id=conv.id,
            role="assistant",
            content="Research summary\x00 feeding document plan",
            execution_log_json='{"answer":"ok\x00"}',
        )
        run = ResearchRun(user_id="u1", query="AI\x00 governance", mode="deep", status="completed")
        db.add_all([msg, run])
        db.commit()
        db.refresh(run)

        source = ResearchSource(
            run_id=run.id,
            title="Official\x00 docs",
            url="https://example.com/source\x00",
            provider="test",
            excerpt="Source excerpt\x00 with bad byte",
        )
        db.add(source)
        db.commit()
        db.refresh(msg)
        db.refresh(source)
        db.refresh(conv)

        assert "\x00" not in conv.title
        assert msg.content == "Research summary feeding document plan"
        assert "\x00" not in (msg.execution_log_json or "")
        assert source.title == "Official docs"
        assert source.url == "https://example.com/source"
        assert source.excerpt == "Source excerpt with bad byte"
    finally:
        db.close()

