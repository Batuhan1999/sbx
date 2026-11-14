# sbx

**Run GitHub repositories in development sandboxes on your own server.**

sbx is a self-hosted platform that runs each branch of a repository in its own
Firecracker VM, using [E2B Embed](vendor/e2b-embed/compose/README.md) as the
runtime. You give your coding agent the repository and access to a server; the
agent installs sbx, discovers what the app needs, and writes a small `.sbx/`
recipe. After that, `sbx up NAME` starts a sandbox with the app running, its own
development data, injected environment variables, and HTTPS preview links.

## Quick start with your coding agent

Copy this into Claude Code, Codex, Amp or any coding agent that can run shell
commands, and fill in the two values in angle brackets:

```text
Set up sbx (https://github.com/SBX_REPOSITORY) for me.

Server: <ssh user@host of an Ubuntu 24.04 machine with /dev/kvm>
Repository to run in sandboxes: <owner/name on GitHub>

1. Clone the sbx repository and read docs/agent-install.md, then follow it step by step.
2. Install sbx on the server with its normal installer; don't change its code.
3. Connect my repository: find out how it is built and started and which services
   and secrets it needs, from the repository itself. Write the .sbx/ recipe the
   setup brief asks for, declare every value you can't generate as a secret, and
   ask me for those values instead of guessing.
4. Use only the sbx CLI/API and the documented sbx-admin host commands.
5. Finish with the acceptance checklist in docs/agent-install.md: two independent
   sandboxes, their own data, a restart, and cleanup of everything they created.
   Report what passed and what you could not verify.
```

## How it is built

- **Core: the sandbox lifecycle.** Create, prepare, restart, pause, resume and
  delete, driven by one explicit state machine per sandbox. The core reads the
  repository's recipe, runs its setup and processes, checks health, and routes
  previews.
- **Extensions** plug into the core through one interface and compose into each
  sandbox and its template:
  - *agent connectors* (Claude Code, Codex, Amp) build an agent's CLI into
    templates, and Claude Code's and Amp's start agent sessions in sandboxes;
  - *catalog packages* provide per-sandbox resources on shared or hosted
    services (Postgres, ClickHouse, Neon, PlanetScale, AWS, ...);
  - *GitHub* gives sandboxes repository access and `gh`, and runs PR previews.
- **Secrets** stay out of recipes and templates, in the installation's secret
  store (a private Infisical on the host by default).
- **Templates** are composed from the platform layer, each enabled extension's
  contribution, and the repository's recipe, and fingerprinted: a change makes
  the template stale and it is rebuilt.

See [Extensions](docs/extensions.md) for the interfaces and the
[reference](docs/reference.md) for the recipe format and lifecycle.

## Host requirements

- Ubuntu 24.04 with `/dev/kvm`: bare metal, or a cloud VM with nested
  virtualization.
- At least 24 GiB RAM and 200 GB disk. The default budget reserves 12 GiB for
  host services; a sandbox takes 4 GiB unless its recipe asks for another size.
  Add RAM for more sandboxes and shared databases.
- Inbound SSH and HTTPS (443). Validating a certificate for a bare IP also needs
  inbound TCP 80. A domain is optional.
- With a domain `D`: A records for `D` and `*.D` pointing at the server. The
  default certificate mode (`--dns delegated`) also needs the record
  `_acme-challenge.D NS D.` and inbound UDP and TCP 53, because the server
  answers the certificate's DNS challenge itself. Open the same ports in any
  cloud firewall.

Sign-in is one shared password for the admins by default: whoever holds it can
sign in as any admin (or any email `sign_in.allowed_emails` lists). For a
team, connect your OIDC provider instead (`sbx-admin connect sign_in
method=oidc ... allowed_emails='*@example.com'`); OIDC admits only the emails
`allowed_emails` names (`*` for anyone the provider admits).

## Install

1. Clone this repository onto the server.
2. Run `sh install.sh` from the checkout as root. It installs the runtime, the
   control service, Infisical and the base templates, then prints a setup link.
3. Open the link, accept the temporary certificate, choose IP access or a
   domain, and create the admin login. `sbx-admin setup-link` makes a new link
   if it expires.

## Connect a repository

The intended path is agent-first: give your coding agent the prompt under
[Quick start](#quick-start-with-your-coding-agent). The agent follows the [installation guide](docs/agent-install.md): it connects
GitHub, chooses services from evidence, writes `.sbx/`, and finishes with the
[acceptance checklist](docs/agent-install.md#acceptance-checklist).

From a repository checkout on your own machine, install the desktop CLI from the
server's **CLI & access** page, then:

```sh
sbx login
sbx ssh-config           # once: ssh, VS Code, Cursor and Zed reach sbx-NAME
sbx repo connect --agent # starts your agent with the server's setup brief
sbx up my-feature        # prints the preview links when the app is healthy
sbx logs my-feature
sbx down my-feature      # deletes the sandbox and the resources it owns
```

Claude Desktop and the Codex app need `sbx ssh-config --apps` instead, run
again after each `sbx up` or `sbx down`.

A recipe can be as small as:

```yaml
# .sbx/sbx.yaml
previews:
  "": 3000
processes:
  web: npm run dev -- --host 0.0.0.0
health:
  - http://127.0.0.1:3000/
env:
  SESSION_SECRET: ${random:32}
```

## Scope and limits

sbx is early software.

| Area | Status |
| --- | --- |
| Host | Ubuntu 24.04 with KVM. A cloud VM without nested virtualization does not work. |
| Desktop CLI | macOS only ([details](docs/reference.md#limits)). Host administration uses `sbx-admin` as root. |
| Previews | With a domain, named HTTPS hosts. IP-only, HTTPS on assigned ports (21000–21255 by default). |
| Development data | Per-sandbox databases, branches or namespaces through catalog packages; you verify isolation per repository. |
| Claude Code | Optional; needs a Claude environment that supports self-hosted runners. |
| Amp | Optional self-hosted orb provider; see the [Amp guide](docs/amp-orbs.md). |
| Other agents | Work over SSH and the CLI; no native "new session" integration. |
| PR previews | Through the GitHub App's `preview` label; verify updates, links and cleanup per repository. |
| Runtime changes | Recreating the runtime with existing sandboxes is not supported; apply service networks before creating sandboxes. |
| Backups | Configuration and secrets only; not VMs, templates or hosted databases. |

The [examples](examples/) (Memos, Umami, Maple) show complete recipes. They are
examples, not a compatibility guarantee.

## Documentation

| Guide | Use it for |
| --- | --- |
| [Installation guide](docs/agent-install.md) | Fresh server to a working first repository, and the acceptance checklist. |
| [Service selection](docs/service-selection.md) | Matching what a repository uses to catalog packages. |
| [Reference](docs/reference.md) | Recipe format, env and secrets, services, lifecycle, templates, previews, limits. |
| [Extensions](docs/extensions.md) | Writing an agent connector or a catalog package. |
| [Amp](docs/amp-orbs.md) | Running Amp threads in sandboxes. |
| [Resource allocation](control/sbx_control/template_files/resource-guide.md) | CPU, RAM, disk and swap: requested, allocated and observed. |
| [Development](docs/development.md) | Working on sbx itself: checks, builds, releases. |
| [Contributing](CONTRIBUTING.md) | Reports and pull requests. |

## License

sbx's own code is [MIT licensed](LICENSE). Vendored E2B Embed remains under
[Apache 2.0](vendor/e2b-embed/LICENSE); see [third-party notices](THIRD_PARTY_NOTICES.md).
