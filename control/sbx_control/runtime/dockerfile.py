"""A Dockerfile replayed in a shell: for a runtime that builds a template by
running its steps in a sandbox (no Docker there). Its instructions become
TemplateSpec steps; an ENV or ARG value becomes the shell word that expands
it as Docker would (ShellWord), written to the env with export_line."""
from __future__ import annotations

import json
import shlex

from .base import Failed


class ShellWord(str):
    """A value written as the shell word that makes it: a Dockerfile's ENV or
    ARG, whose $VAR and ${VAR} expand from the env before it (earlier ENV and
    ARG, the base image's) as Docker expands them. Never a command
    substitution: shell_word escapes those."""


UNQUOTED_LITERAL = set(";&|<>()*?[]~!{}")   # shell syntax outside quotes, plain text in a Dockerfile's value


def shell_word(raw: str, *, quoted: bool = False) -> ShellWord:
    """raw as a ShellWord: as written (its quotes kept, $VAR expanding), or,
    quoted, the whole of it in double quotes (ENV's old KEY value form).
    Backticks and $( are escaped outside single quotes, and outside any
    quotes the characters that are shell syntax but plain text to Docker
    (UNQUOTED_LITERAL; ${...} stays an expansion)."""
    out, single, double, param = [], False, quoted, 0
    text = raw.replace("\\", "\\\\").replace('"', '\\"') if quoted else raw
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and not single and i + 1 < len(text):
            out.append(text[i:i + 2])
            i += 2
            continue
        if not single and text.startswith("${", i):
            out.append("${")
            param, i = param + 1, i + 2
            continue
        if c == "}" and param:
            out.append(c)
            param, i = param - 1, i + 1
            continue
        if c == "'" and not double:
            single = not single
        elif c == '"' and not single:
            double = not double
        elif not single and (c == "`" or text.startswith("$(", i)):
            out.append("\\")
        elif not single and not double and c in UNQUOTED_LITERAL:   # Docker takes them as they are
            out.append("\\")
        out.append(c)
        i += 1
    word = "".join(out)
    return ShellWord(f'"{word}"' if quoted else (word or "''"))


def export_line(key: str, value) -> str:
    """export KEY=VALUE for a shell: a ShellWord as written, anything else quoted."""
    return f"export {key}={value if isinstance(value, ShellWord) else shlex.quote(str(value))}\n"



def dockerfile_steps(text: str) -> list:
    """A Dockerfile's instructions as TemplateSpec steps: RUN, ENV, WORKDIR,
    USER and ARG (its default, as ENV for the steps after it); FROM and what
    only describes an image (EXPOSE, LABEL, CMD, ENTRYPOINT, ...) are
    skipped. COPY and ADD are refused: the sandbox that replays them has no build
    context."""
    lines, current = [], ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):   # also inside a continuation, as Docker drops them
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        lines.append((current + stripped).strip())
        current = ""
    if current.strip():
        lines.append(current.strip())
    steps = []
    for line in lines:
        word, _, rest = line.partition(" ")
        word, rest = word.upper(), rest.strip()
        if word == "RUN":
            if rest.startswith("["):
                try:
                    rest = shlex.join(json.loads(rest))
                except ValueError:
                    pass
            steps.append(("run", rest))
        elif word == "ENV":
            steps.append(("env", _pairs(rest)))
        elif word == "ARG":
            name, sep, value = rest.partition("=")
            if sep:
                steps.append(("env", {name.strip(): shell_word(value.strip())}))
        elif word == "WORKDIR":
            steps.append(("workdir", shell_word(rest, quoted=True)))   # $VAR expands, as Docker does
        elif word == "USER":
            steps.append(("user", rest.split(":")[0]))
        elif word in ("COPY", "ADD"):
            raise Failed(f"a Dockerfile's {word} can't be replayed ({line[:80]}): the template is built in a "
                         "sandbox with no build context. Fetch the file in a RUN step (curl, git) instead.")
    return steps


def _words(text: str) -> list:
    """text split at blanks outside quotes, each word as written (quotes and
    backslashes kept)."""
    words, current, quote, escaped = [], "", "", False
    for c in text:
        if escaped:
            current, escaped = current + c, False
        elif c == "\\" and quote != "'":
            current, escaped = current + c, True
        elif quote:
            current += c
            quote = "" if c == quote else quote
        elif c in "'\"":
            current, quote = current + c, c
        elif c.isspace():
            if current:
                words.append(current)
            current = ""
        else:
            current += c
    return words + ([current] if current else [])


def _pairs(text: str) -> dict:
    """ENV's KEY=VALUE pairs (or the old KEY VALUE form), each value the
    ShellWord that expands it as Docker would."""
    if "=" not in text.split(" ", 1)[0]:
        key, _, value = text.partition(" ")
        return {key: shell_word(value.strip(), quoted=True)}
    found = {}
    for token in _words(text):
        key, _, value = token.partition("=")
        found[key] = shell_word(value)
    return found
