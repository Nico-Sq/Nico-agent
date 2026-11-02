"""Session queries and title updates shared by Nico-agent interfaces."""

from .contracts import SessionStorePort, UseCaseError
from .export import render_session_markdown


class SessionUseCases:
    def __init__(self, store: SessionStorePort):
        self.store = store

    def list(self, kind: str, query: str = "") -> list[dict]:
        return self.store.list_sessions(kind, query)

    def require_session(self, session_id: str, kind: str) -> dict:
        session = self.store.get(session_id, kind)
        if session is None:
            raise UseCaseError("not_found", "会话不存在")
        return session

    def detail(self, session_id: str, kind: str) -> dict:
        snapshot = self.store.session_snapshot(session_id, kind)
        if snapshot is None:
            raise UseCaseError("not_found", "会话不存在")
        return snapshot

    def set_title(self, session_id: str, kind: str, title: str) -> dict:
        title = " ".join(title.split())
        session = self.store.set_session_title(session_id, kind, title)
        if session is None:
            raise UseCaseError("not_found", "会话不存在")
        return session

    def export_markdown(self, session_id: str, kind: str) -> str:
        session = self.require_session(session_id, kind)
        messages = self.store.messages(session_id, kind)
        report = self.store.interview_report(session_id) if kind == "interview" else None
        return render_session_markdown(session, messages, report)
