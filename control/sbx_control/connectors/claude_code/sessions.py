"""Which sandbox each Claude Code cloud session has (claude_sessions), and
the ports its processes use: what the connector's other modules share."""
from ...state import db

KIND = "claude"
RUNNER_HEALTH_PORT = 8089        # `claude self-hosted-runner` in a session's sandbox
ORCHESTRATOR_HEALTH_PORT = 8090  # the orchestrator on the host (orchestrator.sh)


def session_of(name: str) -> str | None:
    with db() as conn:
        row = conn.execute("select session_id from claude_sessions where sandbox = ?", (name,)).fetchone()
    return row[0] if row else None


def sandbox_of(session_id: str) -> str | None:
    with db() as conn:
        row = conn.execute("select sandbox from claude_sessions where session_id = ?", (session_id,)).fetchone()
    return row[0] if row else None


def bind(session_id: str, name: str | None):
    with db() as conn:
        conn.execute("delete from claude_sessions where session_id = ?", (session_id,))
        if name:
            conn.execute("insert or replace into claude_sessions (session_id, sandbox) values (?, ?)", (session_id, name))
