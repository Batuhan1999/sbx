"""A managed server a connection package declares (server: in its
package.yaml): the package's Compose file, run as a Compose project of its
own on the managed servers' private network.

The package says what runs; this says how it runs here, in an override file
written beside it: the service sandboxes reach gets the server's address on
the private network, every service keeps a bounded log and restarts with the
host, every volume is a directory on the server's own disk of fixed size
(cow_storage.volume_prepare), and every container and volume carries the
server's label. With the memory and CPU limits the package must give each
service, a server that runs away fills its own disk and its own share, not
the host's. Its secrets are made once, kept with the server's record, and
reach Compose in its environment only. Removing the server removes the
project and, with its data, its disk."""
from __future__ import annotations

import os
import subprocess

import yaml

from . import paths

OVERRIDE = "sbx.override.yaml"
# Docker keeps a container's log forever otherwise (hosted_services.LOG_LIMIT).
LOGGING = {"driver": "json-file", "options": {"max-size": "50m", "max-file": "5"}}
PULL_SECONDS, UP_SECONDS = 1800, 900


def project(name: str) -> str:
    return f"sbx-server-{name}"


def folder(name: str):
    return paths.state_dir() / "servers" / name


def override(name: str, spec: dict, compose: dict, address: str, network: str, label: str, volumes: dict) -> dict:
    """What this host adds to the package's Compose file. volumes: each of
    the file's volumes -> its directory on the server's disk."""
    services = {}
    for key, service in compose["services"].items():
        services[key] = {"labels": {label: name}, "logging": LOGGING}
        if "restart" not in service:
            services[key]["restart"] = "unless-stopped"
    services[spec["service"]]["networks"] = {"default": {}, network: {"ipv4_address": address}}
    return {"services": services, "networks": {network: {"external": True}},
            "volumes": {volume: {"labels": {label: name}, "driver": "local",
                                 "driver_opts": {"type": "none", "o": "bind", "device": volumes[volume]}}
                        for volume in compose.get("volumes") or {}}}


def write(name: str, package, address: str, network: str, label: str, volumes: dict):
    """The package's Compose file and this host's override, in the server's folder."""
    spec = package.server
    text = package.files[spec["compose"]]
    target = folder(name)
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o700)
    (target / "compose.yaml").write_text(text)
    (target / OVERRIDE).write_text(yaml.safe_dump(override(name, spec, yaml.safe_load(text), address, network, label,
                                                           volumes)))


def compose(name: str, *args, secrets: dict | None = None, address: str = "", timeout: int = UP_SECONDS, check=True):
    """docker compose for the server's project. Its output may hold what the
    services print at start, so a failure says only which step failed."""
    target = folder(name)
    files = ["-f", "compose.yaml", "-f", OVERRIDE] if (target / OVERRIDE).exists() else []
    env = {**os.environ, "SBX_ADDRESS": address, "SBX_NAME": name, **(secrets or {})}
    result = subprocess.run(["docker", "compose", "-p", project(name), *files, *args], env=env, capture_output=True,
                            text=True, timeout=timeout, cwd=target if files else None)
    if check and result.returncode:
        raise RuntimeError(f"docker compose {args[0]} failed (exit {result.returncode}) for the server {name}; "
                           f"its services' logs say why: docker compose -p {project(name)} logs")
    return result


def exists(name: str) -> bool:
    """Whether Docker has containers of a project of this server's name."""
    found = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project(name)}"],
                           capture_output=True, text=True, timeout=60)
    return bool(found.stdout.strip())


def up(name: str, package, values: dict, network: str, label: str, volumes: dict):
    write(name, package, values["address"], network, label, volumes)
    args = {"secrets": values["secrets"], "address": values["address"]}
    compose(name, "pull", "--quiet", timeout=PULL_SECONDS, **args)
    compose(name, "up", "-d", "--wait", "--remove-orphans", **args)


def down(name: str, values: dict, *, delete_data: bool):
    args = ["down", "--remove-orphans", *(["--volumes"] if delete_data else [])]
    compose(name, *args, secrets=values.get("secrets"), address=values.get("address", ""))
    if delete_data:
        target = folder(name)
        for file in ("compose.yaml", OVERRIDE):
            (target / file).unlink(missing_ok=True)
        if target.is_dir():
            target.rmdir()
