# Agents working on sbx

These rules are for coding agents that change sbx itself. To install sbx or set
up a repository on it, use the [installation guide](docs/agent-install.md).

## What counts as done

A change is done when a real repository runs usefully through sbx's public
CLI or API. A green test run, a checkout or an HTTP 200 is not enough, and
neither is switching the app to a lighter mode than its owner uses.

## How to work

1. **Reproduce through the public interface.** Use `sbx`, the HTTP API or
   `sbx-admin`. Read internals to debug, but fix the problem as reusable
   platform behavior or in the recipe, then retry the same way.
2. **Put code where it belongs.** The core is the sandbox lifecycle. Behavior
   for one agent product or service provider goes in its
   [extension](docs/extensions.md); anything that knows E2B Embed goes in the
   [runtime backend](docs/development.md#sandbox-runtime); app-specific steps
   go in the recipe. Add a platform primitive only when several repositories
   need it.
3. **Use ordinary commands.** Recipes use the app's normal setup and start
   commands, injected env and health checks.
4. **Size from evidence.** Read the
   [resource guide](control/sbx_control/template_files/resource-guide.md)
   before choosing CPU, RAM, disk, swap or build concurrency.
5. **Verify isolation.** For lifecycle or service changes, run the
   [acceptance checklist](docs/agent-install.md#acceptance-checklist) with
   disposable, named sandboxes.
6. **Give agents every action.** Anything the web app can do, a coding agent
   must be able to do through the `sbx` CLI and the HTTP API, not only in the
   UI or with `sbx-admin` on the server. Ship the endpoint, the CLI command,
   its docs and tests with the UI: secrets on stdin, waiting for changes that
   finish later, a non-zero exit on failure, and `--json` output.
7. **Report honestly.** Say what you verified, with evidence, and what you did
   not test.

## Never

- Put secret values in recipes, templates, commits, reports or tool output.
- Treat earlier implementation choices or old design notes as requirements; the
  owner's current goal is the requirement.

Checks and releases are in [development](docs/development.md).
