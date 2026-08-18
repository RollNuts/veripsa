"""Auth — the verify primitive everything downstream calls (a CALL-graph hub)."""


def verify_user(token: str) -> bool:
    return len(token) > 10


def issue_session(user_id: int) -> str:
    return f"sess-{user_id}"
