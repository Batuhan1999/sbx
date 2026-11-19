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
server, and two hosted ones for a server without KVM: E2B Cloud and
[Boat](https://docs.boat.dev).

## Quick start

1. **Install** on an Ubuntu 24.04 server ([what it needs](#host-requirements)):
   download a release and run `sudo sh install.sh` ([Install](#install)). Its
   last line is your onboarding link:
   `==> Open the onboarding: https://IP/app/setup#t=...`
2. **Open the link** and follow the setup page's four steps
   ([the owner's guide](docs/howto/setup.md)):
   - **Domain**: type a domain and add three DNS records;
   - **Sandboxes**: E2B Embed on this server when it has `/dev/kvm`, else
     E2B Cloud or Boat with their API key;
   - **Account**: your email and password; this finishes the setup;
   - **GitHub**: create sbx's GitHub App, or skip it for now.
3. **Connect a repository**: pick it, copy the prompt and paste it into your
   coding agent (Claude Code, Codex, Amp or any agent that runs shell
   commands) in a checkout of that repository. The agent writes the `.sbx/`
   recipe, starts one sandbox and gives you its preview link.

A coding agent can do step 1 for you, and the whole setup without a browser:
point it at the [installation guide](docs/agent-install.md).

What you and your agent read along the way, if you want the details:

- [Your first-run setup](docs/howto/setup.md): the setup page, step by step.
- [Installation guide](docs/agent-install.md): installing as an agent, the
  setup without a browser, and the
  [acceptance checklist](docs/agent-install.md#acceptance-checklist) for
  changes to sbx itself.
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
- How-to pages for what you set up outside sbx: [DNS
  records](docs/howto/cloudflare.md) (a Cloudflare token where port 53 can't open), [E2B Cloud](docs/howto/e2b.md) and
  [Boat](docs/howto/boat.md) API keys, the [GitHub App](docs/howto/github.md)
  and an [Amp orb provider](docs/howto/amp.md).

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
  on this server where it has `/dev/kvm` and the resources, and a provider's
  runtime (E2B Cloud, Boat) once its API key is given in the setup or later
  (`sbx runtimes add KIND`). Each sandbox runs on one of them, chosen for the
  sandbox, for its repository or by the installation's default. Embed, E2B
  Cloud and Boat are built in; other runtimes plug in as packages.
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

Running them at E2B Cloud or Boat (any small VPS, no KVM):

- Ubuntu 24.04, about 2 GiB RAM and 10 GiB free disk: the server runs only
  sbx-control, nginx and the secret store.
- An E2B API key (E2B's dashboard) or a Boat API key (Boat's dashboard),
  added in the setup's **Sandboxes** step. Sandboxes then run within the
  plan's limits (E2B Hobby: one hour before a sandbox pauses, renewed while
  it is used; 8 vCPUs, 8 GiB, 10 GiB disk each; 10 at once). Boat's trial
  saves no snapshots, and sbx's templates are snapshots: use a paid plan.
- A domain: the provider's sandboxes call the server back at `app.D` on 443
  (repository bundles, git credentials). They reach managed servers and
  private hosts only through [endpoints](docs/reference.md#endpoints).

A server with KVM can run Embed and a provider; add one later in Settings →
Sandbox runtimes, or with `sbx runtimes add KIND --plan PLAN --secret KEY <
key-file` (KEY is the runtime's key name, such as `E2B_CLOUD_API_KEY`; `sbx
runtimes available` lists each runtime's keys and plans). For every server:

- Inbound SSH and HTTPS (443), and TCP 80 for the installer's trusted
  certificate for the server's IP (the first visit's; without it the browser
  warns once).
- A domain `D`, which the setup page asks for: A records for `D` and `*.D`
  pointing at the server, and `_acme-challenge.D NS D.` with inbound UDP and
  TCP 53, because the server answers the certificate's DNS challenge itself.
  Open the same ports in any cloud firewall. Where port 53 can't open, a
  Cloudflare or Route 53 token replaces the `NS` record; an Embed-only server
  can also stay on its IP. Both are set
  [without a browser](docs/agent-install.md#without-a-browser-automation).

Everyone signs in with their own email and password by default. Admins add
people under **Settings → People** (or `sbx-admin user add EMAIL`) and hand
them a one-time link to set their password; removing someone signs them out
everywhere and revokes their API tokens. To use your identity provider instead,
connect OIDC (`sbx-admin connect sign_in method=oidc ...
allowed_emails='*@example.com'`): it admits the admins, the people added, and
the emails `allowed_emails` names (`*` for anyone the provider admits).

## Install

On the server (a coding agent runs the same steps over ssh with the
[installation guide](docs/agent-install.md)):

1. Download a [release](https://github.com/Batuhan1999/sbx/releases), the
   latest one unless you need another, check it against its checksum and
   unpack it:

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
2. Check the server: `sudo sh install.sh --check`. It changes nothing. It
   prints the OS, CPU, memory, disk, `/dev/kvm`, the public IP, the ports in
   use, and which runtimes this server can run (Embed, or E2B Cloud and Boat
   with an API key and a domain).
3. Install: `sudo sh install.sh`. It installs the control service, the
   secret store, and Embed where the server can run it. Without Embed it
   installs with no sandbox runtime yet; the setup adds one. With inbound
   TCP 80 open and a public IP, it also gets a trusted certificate for that
   IP, so the first visit has no browser warning. Its last line is your
   onboarding link:

   ```text
   ==> Open the onboarding: https://203.0.113.10/app/setup#t=...
   ```

   The link holds a one-time token: keep it out of shared logs and chats.
   Without the IP certificate, the line before it says so: your browser warns
   once, until you set a domain.
4. Open the link and do the rest in the browser
   ([the setup page](docs/howto/setup.md)): your domain (three DNS records),
   where sandboxes run (a provider's API key, when Embed can't run here),
   your account, then GitHub. Templates build as soon as the domain and the
   runtime are set, while you go on. `sudo sbx-admin setup-link` prints the
   link again if it is lost; running the installer again during the setup
   prints the same link.

Admins see a newer release in the web app (**Settings → Updates**: what
changed, then **Update** and its progress), or use `sbx-admin upgrade` on the host
([upgrading](docs/reference.md#operating-the-host)).

## Connect a repository

In the web app, **Connect a repository**: pick it, then **Copy prompt** and
paste it into your coding agent in a checkout of the repository (the same
text as `sbx repo brief`). The agent chooses services from evidence, writes
`.sbx/`, starts one sandbox (`sbx up check-a`) and gives you its preview
link; once that sandbox is healthy, the GitHub App opens a pull request with
the recipe.

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
| Host | Ubuntu 24.04. Running sandboxes on the server (E2B Embed) needs KVM; a server without it runs them at E2B Cloud or Boat, which need a public domain and reach the host's services only through endpoints. |
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
| [First-run setup](docs/howto/setup.md) | The setup page, step by step, and connecting the first repository. |
| [Installation guide](docs/agent-install.md) | Installing as a coding agent, the setup without a browser, and the acceptance checklist for changes to sbx. |
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
