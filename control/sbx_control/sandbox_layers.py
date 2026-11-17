"""Sandbox layers: platform features that take part in every sandbox's
runtime write and lifecycle (endpoints is the first) without core or the
keeper knowing them. A layer sees each runtime write as a Write, keeps its
own state there between the steps, and is called at fixed points:

- outputs, values: a write's services' outputs and env, each layer's in turn;
- contribution: its files among the runtime layers (core.runtime_layer);
- after_write: once the runtime is written (sandbox_runtime.write_runtime);
- on_resume: when sbx resumes a paused sandbox (core.connect);
- tick: the keeper's look at one running sandbox; round: once per round;
- ports: a sandbox's ports it owns, never offered as previews."""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class Write:
    """One runtime write of one sandbox. sbx: the running VM, when there is
    one before the write (not on create); state: each layer's own, by name."""
    name: str
    repo: str | None
    kind: str | None
    sbx: object = None
    state: dict = dataclasses.field(default_factory=dict)

    def of(self, layer: "SandboxLayer") -> dict:
        return self.state.setdefault(layer.name, {})


class SandboxLayer:
    """What a layer may do; each method's default does nothing."""
    name = ""
    ports: frozenset = frozenset()

    def outputs(self, write: Write, created: dict) -> dict:
        return created

    def values(self, write: Write, envs: dict) -> dict:
        return envs

    def contribution(self, write: Write):
        return None

    def after_write(self, write: Write, sbx):
        pass

    def on_resume(self, sbx, info):
        pass

    def tick(self, info):
        pass

    def round(self, infos: list, running: set):
        pass


def layers() -> list[SandboxLayer]:
    from . import endpoints
    return [endpoints.LAYER]


def outputs(write: Write, created: dict) -> dict:
    for layer in layers():
        created = layer.outputs(write, created)
    return created


def values(write: Write, envs: dict) -> dict:
    for layer in layers():
        envs = layer.values(write, envs)
    return envs


def contributions(write: Write) -> list:
    return [c for c in (layer.contribution(write) for layer in layers()) if c is not None]


def after_write(write: Write, sbx):
    for layer in layers():
        layer.after_write(write, sbx)


def on_resume(sbx, info):
    """Each layer's; one that fails says so as an event, the resume goes on."""
    from . import state
    for layer in layers():
        try:
            layer.on_resume(sbx, info)
        except Exception as error:
            state.event((info.metadata or {}).get("sbx", ""), f"{layer.name} not brought on resume",
                        type(error).__name__, internal=True)


def tick(info):
    """Each layer's look at a running sandbox; NotRunning goes to the keeper, anything else is tried next round."""
    from . import runtime
    for layer in layers():
        try:
            layer.tick(info)
        except runtime.NotRunning:
            raise
        except Exception as error:
            print(f"keeper: {layer.name} of {(info.metadata or {}).get('sbx')}: {error!r}", flush=True)


def round(infos: list, running: set):
    for layer in layers():
        try:
            layer.round(infos, running)
        except Exception as error:   # the keeper's round goes on; tried again next round
            print(f"keeper: {layer.name}: {error!r}", flush=True)


def reserved_ports() -> set:
    return {port for layer in layers() for port in layer.ports}
