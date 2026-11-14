"""Requested configuration, actual VM allocation, and live usage are different facts."""
import json

from . import core


def snapshot(name, live=False):
    info = core.find(name)
    if not info:
        raise core.SandboxError(f"no sandbox {name}")
    actual = core.Sandbox.get_info(info.sandbox_id)
    app = core.app_of(info)
    size = app.size
    runtime_state = core.runtime_state(actual)
    result = {
        "name": name, "sandbox_id": info.sandbox_id, "state": runtime_state,
        "allocated": {"cpu": actual.cpu_count, "memory_mb": actual.memory_mb, "template_id": actual.template_id},
        "recipe": {"template": (info.metadata or {}).get("template", ""), "cpu": size["cpu"], "memory_mb": size["memory_mb"],
                   "swap_mb": app.swap_mb, "min_free_disk_mb": size["disk_mb"],
                   "allocation_source": "build_resources" if app.spec.get("build_resources")
                   else "resources" if app.spec.get("resources") else "default"},
        "usage": None, "host": core.capacity(actual.memory_mb), "issues": [],
    }
    if live and runtime_state == "running":
        sandbox = core.Sandbox.connect(info.sandbox_id, timeout=core.TIMEOUT_SECONDS)
        core.ship_guest(sandbox)
        result["usage"] = json.loads(sandbox.commands.run(core.guest("usage"), timeout=20).stdout)
    elif live:
        result["issues"].append({"code": "usage_unavailable", "message": "Live usage is unavailable while the workspace is paused."})
    for field in ("cpu", "memory_mb"):
        if result["allocated"][field] < size[field]:
            result["issues"].append({"code": "allocation_below_recipe", "resource": field,
                                     "message": "This existing VM is smaller than the current recipe. Build the template and start a new sandbox."})
    if result["usage"]:
        disk = result["usage"]["disk"]
        if disk["available_bytes"] < 1024**3:
            result["issues"].append({"code": "low_disk_space", "message": "Less than 1 GiB is writable. Inspect caches and build output before retrying setup."})
        requested = result["recipe"]["min_free_disk_mb"]
        if requested and requested * 1024**2 > disk["total_bytes"]:
            result["issues"].append({"code": "disk_request_not_applied", "message": "The requested build disk reserve exceeds this filesystem. The runtime did not apply the request; changing the recipe does not resize an existing VM."})
    result["agent_guidance"] = agent_brief(result)
    return result


def agent_brief(resources):
    allocated = resources["allocated"]
    text = f"Allocated: {allocated['cpu']} vCPU, {allocated['memory_mb'] / 1024:g} GiB RAM."
    usage = resources.get("usage")
    if usage:
        disk = usage["disk"]
        text += f" Root filesystem: {disk['total_bytes'] / 1024**3:.1f} GiB, {disk['available_bytes'] / 1024**3:.1f} GiB writable."
    text += (" These are this VM's actual values; a changed recipe size applies to sandboxes started after its "
             "template is built again. "
             "vCPUs share the host. Shared services consume the host budget separately. "
             "Check CPU, RAM, disk, and inode pressure before increasing resources; preserve application data. "
             "Limit parallel compiler/test jobs before requesting a larger VM. "
             "Verify the allocation again after starting a new sandbox.")
    for issue in resources.get("issues") or []:
        text += f" {issue['message']}"
    return text
