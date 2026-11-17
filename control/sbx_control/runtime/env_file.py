"""/etc/sbx/runtime.env, the one way to it: the enabled runtimes
(SBX_RUNTIMES, SBX_RUNTIME_DEFAULT) and their keys, root-only and never
backed up. Lines are NAME=value (shell-quoted); comments and every line a
change does not name stay as they are."""
from __future__ import annotations

import shlex


def path():
    from .. import paths
    return paths.runtime_env()


def text() -> str | None:
    """The file as it is; None when there is none."""
    file = path()
    return file.read_text() if file.exists() else None


def parse(content: str) -> dict:
    """Every NAME=value line of content, by name (spaces around it allowed):
    the value unquoted ("" when empty); the last line of a name wins."""
    found = {}
    for line in content.splitlines():
        name, sep, value = line.partition("=")
        if not sep or line.lstrip().startswith("#"):
            continue
        try:
            found[name.strip()] = (shlex.split(value) or [""])[0]
        except ValueError:   # not a value sbx wrote
            continue
    return found


def read(keys=None) -> dict:
    """The file's values (of keys, when given)."""
    found = parse(text() or "")
    return found if keys is None else {k: v for k, v in found.items() if k in keys}


def restore(content: str | None) -> None:
    """The file as content was (None: no file), atomically and root-only."""
    file = path()
    if content is None:
        file.unlink(missing_ok=True)
        return
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_suffix(".new")
    temporary.touch(mode=0o600)
    temporary.chmod(0o600)
    temporary.write_text(content)
    temporary.replace(file)


def edit(values: dict, header: str = "") -> None:
    """Set these names (None: remove its line) in place; one the file does
    not have yet is added at the end. header: a new file's first line."""
    out, done = [], set()
    current = text()
    lines = current.splitlines() if current is not None else ([header] if header else [])
    for line in lines:
        name, sep, _ = line.partition("=")
        name = name.strip()
        if sep and not line.lstrip().startswith("#") and name in values:
            if name not in done and values[name] is not None:
                out.append(f"{name}={shlex.quote(values[name])}")
            done.add(name)
            continue
        out.append(line)
    out += [f"{name}={shlex.quote(value)}" for name, value in values.items() if name not in done and value is not None]
    restore("\n".join(out) + "\n")
