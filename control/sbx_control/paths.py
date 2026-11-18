"""Where the installation keeps its files on the host. Read from the
environment on every call, so a test (or a second installation in one
process) points them elsewhere without re-importing anything.

  SBX_ETC        /etc/sbx         the operator's files and the recovery key
  SBX_STATE_DIR  /var/lib/sbx     sbx.db, locks, mirrors, imported recipes
  SBX_STATE      <state>/sbx.db   the database itself
  SBX_RUN_DIR    /run/sbx         requests for host effects (settings)
"""
import os
from pathlib import Path


def etc() -> Path:
    return Path(os.environ.get("SBX_ETC", "/etc/sbx"))


def state_dir() -> Path:
    return Path(os.environ.get("SBX_STATE_DIR", "/var/lib/sbx"))


def database() -> Path:
    return Path(os.environ.get("SBX_STATE") or state_dir() / "sbx.db")


def cache() -> Path:
    """What sbx can make again but keeps to skip the work (build artifacts)."""
    return state_dir() / "cache"


def run() -> Path:
    return Path(os.environ.get("SBX_RUN_DIR", "/run/sbx"))


def tls() -> Path:
    """Where control/tls.sh keeps nginx's config and the certificates."""
    return Path("/opt/sbx-tls")


def apps() -> Path:
    """The operator's recipes: <owner>/<repo>/.sbx (sbx-admin recipe apply)."""
    return etc() / "apps"


def packages() -> Path:
    """The operator's catalog packages, over the shipped ones."""
    return etc() / "catalog"


def runtime_env() -> Path:
    """This host's sandbox runtime credentials, written by install.sh."""
    return etc() / "runtime.env"


def secret_key() -> Path:
    """The key the local secret store seals with; kept apart from backups."""
    return etc() / "secret.key"


_RELEASE = Path(__file__).resolve().parents[1]


def shipped(name: str) -> Path:
    """A directory release.sh ships beside sbx_control (catalog, docs): the
    release's, else the source checkout's."""
    return next((d for d in (_RELEASE / name, _RELEASE.parent / name) if d.is_dir()), _RELEASE / name)
