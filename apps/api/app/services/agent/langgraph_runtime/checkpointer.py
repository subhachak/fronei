from __future__ import annotations

import builtins
import sqlite3
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.sqlite import SqliteSaver

from app.config import get_settings


class LockedSqliteSaver(SqliteSaver):
    """Serialize access to the shared SQLite checkpointer singleton."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__(conn)
        self._outer_lock = threading.RLock()

    def setup(self) -> None:
        with self._outer_lock:
            return super().setup()

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        with self._outer_lock:
            return super().get_tuple(config)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ):
        with self._outer_lock:
            return iter(builtins.list(super().list(config, filter=filter, before=before, limit=limit)))

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        with self._outer_lock:
            return super().put(config, checkpoint, metadata, new_versions)

    def put_writes(
        self,
        config: RunnableConfig,
        writes,
        task_id: str,
        task_path: str = "",
    ) -> None:
        with self._outer_lock:
            return super().put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        with self._outer_lock:
            return super().delete_thread(thread_id)


@lru_cache(maxsize=1)
def get_checkpointer() -> LockedSqliteSaver:
    """Return the process-wide LangGraph SQLite checkpointer.

    The compiled graph is shared across requests, so its checkpointer must be
    shared too.  LangGraph namespaces persisted state by RunnableConfig
    `configurable.thread_id`; callers pass run_id as that thread_id.
    """
    settings = get_settings()
    db_path = Path(settings.langgraph_checkpoint_db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    saver = LockedSqliteSaver(conn)
    saver.setup()
    return saver
