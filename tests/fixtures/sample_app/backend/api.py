"""API surface — imports + CALLS auth.verify_user (cross-file call coupling to auth.py)."""
from auth import verify_user, issue_session


def serve_request(req) -> str:
    if verify_user(req.token):
        return issue_session(req.user_id)
    return "denied"
