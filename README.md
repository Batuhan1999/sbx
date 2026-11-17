# sbx

**Self-hosted orchestration for agent sandboxes.**

Sandboxes are cheap now; the hard part is everything your app talks to. sbx
handles that part. Your coding agent reads the repository, picks the services
it needs from a shared catalog and writes one small `.sbx/` recipe. From then
on, every sandbox gets its own databases and other resources on your shared or
hosted services, its secrets injected, the app running and HTTPS preview links,
and all of it is cleaned up when the sandbox goes. `sbx up`, Claude Code
sessions, Amp threads and PR previews all start from that recipe; Codex and
any other agent work over SSH.

Each sandbox is a VM on a runtime the installation enables. Built in are
[E2B Embed](vendor/e2b-embed/compose/README.md), Firecracker VMs on your own
server, and E2B Cloud, E2B's hosted sandboxes, for a server without KVM.

## Quick start with your coding agent

Copy this into Claude Code, Codex, Amp or any coding agent that can run shell
commands, and fill in the two values in angle brackets:

```text
Set up sbx (https://github.com/Batuhan1999/sbx) for me.

Server: <ssh user@host of an Ubuntu 24.04 machine>
(With /dev/kvm and 24 GiB RAM, the server runs the sandboxes itself on E2B
Embed. Without, they run at E2B Cloud: I'll give you an E2B API key, and the
server needs a domain.)
Repository to run in sandboxes: <owner/name on GitHub>

1. Clone the sbx repository, check out its latest release tag, and read
   docs/agent-install.md there, then follow it step by step.
2. Install sbx on the server with its normal installer; don't change its code.
3. Connect my repository: find out how it is built and started and which services
   and secrets it needs, from the repository itself. Choose services with
   docs/service-selection.md, write the .sbx/ recipe in the format of
   docs/reference.md (examples/ has complete ones), and size it with
   control/sbx_control/template_files/resource-guide.md. Declare every value you
   can't generate as a secret, and ask me for those values instead of guessing.
4. Use only the sbx CLI/API and the documented sbx-admin host commands.
5. Finish with the acceptance checklist in docs/agent-install.md: two independent
   sandboxes, their own data, a restart, and cleanup of everything they created.
   Report what passed and what you could not verify.
```

What the agent reads along the way, if you want the details yourself:

- [Installation guide](docs/agent-install.md): the install and onboarding
  steps, and the [acceptance checklist](docs/agent-install.md#acceptance-checklist)
  that decides when the setup is done.
- [Service selection](docs/service-selection.md): how to work out what the
  repository depends on and pick a catalog package or local service for each.
- [Reference](docs/reference.md): the [recipe](docs/reference.md#recipe),
  [env and secrets](docs/reference.md#environment-and-secrets),
  [services](docs/reference.md#services), [lifecycle](docs/reference.md#lifecycle),
  [templates](docs/reference.md#templates) and [previews](docs/reference.md#previews).
- [Resource guide](control/sbx_control/template_files/resource-guide.md): how
  much CPU, RAM, disk and swap a sandbox gets, from evidence.
- [Examples](examples/): complete recipes for Memos, Umami and Maple.
- [Claude Code](docs/claude-code.md), [Amp](docs/amp-orbs.md) and
  [Extensions](docs/extensions.md): optional agent connectors, and how to add
  your own connector or catalog package.

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
- **Runtimes**: the compute under the core, reached through one interface
  (`control/sbx_control/runtime`) and pluggable. An installation runs the
  runtimes `/etc/sbx/runtime.env` enables: a fresh install enables E2B Embed
  on this server where it has `/dev/kvm` and the resources, and E2B Cloud
  once its API key is given (`E2B_CLOUD_API_KEY`, or later `sbx-admin
  runtime add e2b-cloud`). Each sandbox runs on one of them, chosen for the
  sandbox, for its repository or by the installation's default. Embed and E2B
  Cloud are built in; other runtimes plug in as packages.
- **Secrets** stay out of recipes and templates, in the installation's secret
  store (a private Infisical on the host by default).
- **Templates** are composed from the platform layer, each enabled extension's
  contribution, and the repository's recipe, and fingerprinted: a change makes
  the template stale and it is rebuilt.

See [Extensions](docs/extensions.md) for the interfaces and the
[reference](docs/reference.md) for the recipe format and lifecycle.

## Host requirements

What the server needs depends on where sandboxes run
([runtimes](docs/reference.md#runtimes)).

Running them on the server itself (E2B Embed):

- Ubuntu 24.04 with `/dev/kvm`: bare metal, or a cloud VM with nested
  virtualization.
- At least 24 GiB RAM and 200 GB disk. The default budget reserves 12 GiB for
  host services; a sandbox takes 4 GiB unless its recipe asks for another size.
  Add RAM for more sandboxes and shared databases.

Running them at E2B Cloud (any small VPS, no KVM):

- Ubuntu 24.04, about 2 GiB RAM and 10 GiB free disk: the server runs only
  sbx-control, nginx and the secret store.
- An E2B API key (E2B's dashboard), in the installer's environment
  ([Install](#install)).
  Sandboxes then run at E2B, within your E2B plan's limits (Hobby: one hour
  before a sandbox pauses, renewed while it is used; 8 vCPUs, 8 GiB, 10 GiB
  disk each; 20 at once).
- A domain: E2B's sandboxes call the server back at `login.D` on 443
  (repository bundles, git credentials). Managed servers on the host's
  private network are not available to them.

A server with KVM can run both; add E2B Cloud later with `sbx-admin runtime
add e2b-cloud` (the key on stdin). For every server:

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

1. Download a [release](https://github.com/Batuhan1999/sbx/releases) onto
   the server, the latest one unless you need another, check it against its
   checksum and unpack it:

   ```sh
   v=$(curl -fsSL -o /dev/null -w '%{url_effective}' https://github.com/Batuhan1999/sbx/releases/latest)
   v=${v##*/} url=https://github.com/Batuhan1999/sbx/releases/download
   curl -fsSLO "$url/$v/sbx-$v.tar.gz" && curl -fsSL "$url/$v/sbx-$v.tar.gz.sha256" | sha256sum -c \
     && tar -xzf "sbx-$v.tar.gz" && cd "sbx-$v"
   ```

   The archive carries the web app built, so the server needs no Node. A git
   checkout (a release tag, or `main` for the newest, unreleased changes)
   installs too once its web app is built, with Node 22:
   `npm --prefix web ci && npm --prefix web run build`
   ([pinning a release](docs/reference.md#operating-the-host)).
2. Run `sh install.sh` from that directory as root. It installs the runtime, the
   control service, Infisical and the base templates, then prints a setup link.
   On a server without `/dev/kvm`, give it an E2B API key and the domain
   instead (the key in the environment, never on the command line of a shared
   shell history: `read -rs E2B_CLOUD_API_KEY; export E2B_CLOUD_API_KEY`, then
   `sudo --preserve-env=E2B_CLOUD_API_KEY sh install.sh --domain D --email E`).
   Without either it stops and says what to set.
3. Open the link, accept the temporary certificate, choose IP access or a
   domain, and create the admin login. `sbx-admin setup-link` makes a new link
   if it expires.

Admins see a newer release in the web app (**Settings → Updates**: what
changed, then **Update** and its progress), or use `sbx-admin upgrade` on the host
([upgrading](docs/reference.md#operating-the-host)).

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
| Host | Ubuntu 24.04. Running sandboxes on the server (E2B Embed) needs KVM; a server without it runs them at E2B Cloud, which needs a public domain and can't reach the host's private network. |
| Desktop CLI | macOS only ([details](docs/reference.md#limits)). Host administration uses `sbx-admin` as root. |
| Previews | With a domain, named HTTPS hosts. IP-only, HTTPS on assigned ports (21000–21255 by default). |
| Development data | Per-sandbox databases, branches or namespaces through catalog packages; you verify isolation per repository. |
| Claude Code | Optional; cloud sessions through self-hosted runners (Team/Enterprise); see the [Claude Code guide](docs/claude-code.md). |
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
| [Claude Code](docs/claude-code.md) | Running Claude Code cloud sessions in sandboxes. |
| [Amp](docs/amp-orbs.md) | Running Amp threads in sandboxes. |
| [Resource allocation](control/sbx_control/template_files/resource-guide.md) | CPU, RAM, disk and swap: requested, allocated and observed. |
| [Development](docs/development.md) | Working on sbx itself: checks, builds, releases. |
| [Contributing](CONTRIBUTING.md) | Reports and pull requests. |

## License

sbx's own code is [MIT licensed](LICENSE). Vendored E2B Embed remains under
[Apache 2.0](vendor/e2b-embed/LICENSE); see [third-party notices](THIRD_PARTY_NOTICES.md).
