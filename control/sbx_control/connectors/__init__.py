"""The agent connectors that ship with sbx-control (extensions.BUILTIN).
Each implements extensions.Connector in a package of its own, with what it
runs on the host beside it, and reaches sandboxes only through the public
domain API (sandboxes, core.attach, core.pause, …)."""
