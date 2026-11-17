# Install sbx and onboard a repository

This guide takes a coding agent (or a person) from a fresh server to a first
repository running in sandboxes. It ends with the
[acceptance checklist](#acceptance-checklist).

**How to work.** Check first, then decide. Each step says what to run, how to
read the output, what to decide, and what to ask the owner. Ask only when the
server can't tell you: a choice that is the owner's, an account, a key, a DNS
record. Group your questions into one message where you can, and wait for
the answers before you do anything that depends on them. Don't stop for
approval between routine steps. Keep the current step visible and resolve a
failed step before you call it done.

**Where commands run.** Every command runs on your own machine (the
owner's laptop or wherever you work). Commands for the server go through
`ssh HOST`, so each block can be pasted as it is. Each command is
self-contained: nothing depends on a variable or a `cd` from an earlier
block.

**Placeholders.** Replace `HOST` (what you pass to `ssh`, with sudo that
asks no password), `IP` (the server's public IP, from step 1), `D` (the
domain, step 3), `E` (the email for certificate notices),
`owner@example.com` (the first admin) and `KIND` (a runtime: `embed`,
`e2b-cloud` or `boat`). In step 6, `dev-pg`, `temporal`, `10.0.0.5:7233`,
`10.0.0.0/24` and `owner/app` are examples: use the real names.

**Secrets.** API keys, passwords and the setup link never go into the chat,
a command line, a recipe, a commit or a log. They live in files under
`~/.sbx-install` on your machine, which only the owner and you can read.
Commands read them from stdin or a file. Create the folder once:

```sh
mkdir -m 700 -p ~/.sbx-install
```

Every secret file is created readable only by its owner (`umask 077`, as
the commands below do), and never printed: to check a file arrived, count
its bytes (`wc -c < FILE`). Once sbx has stored a key (step 5), delete its
file. At the end, only `setup-link` and `admin-password` may remain, and
the owner keeps or deletes them.

## 1. Inspect the server

Download the latest release onto the server, check its checksum, and unpack
it to `~/sbx` there. The last line printed is the release's tag:

```sh
ssh HOST sh -s <<'EOF'
set -e
cd ~
v=$(curl -fsSL -o /dev/null -w '%{url_effective}' https://github.com/Batuhan1999/sbx/releases/latest)
v=${v##*/} url=https://github.com/Batuhan1999/sbx/releases/download
curl -fsSLO "$url/$v/sbx-$v.tar.gz"
curl -fsSL "$url/$v/sbx-$v.tar.gz.sha256" | sha256sum -c
tar -xzf "sbx-$v.tar.gz"
ln -sfn "sbx-$v" sbx
echo "$v"
EOF
```

`sha256sum -c` prints `sbx-v….tar.gz: OK`. Anything else means the download
is damaged: run the block again.

Copy that release's `sbx` CLI to your machine. It is one Python 3 file with
no dependencies, and step 5 runs it:

```sh
scp HOST:sbx/cli/sbx.py ~/.sbx-install/sbx.py
```

Then check the host:

```sh
ssh HOST 'cd ~/sbx && sudo sh install.sh --check'
```

`--check` installs nothing. It prints the facts and the runtimes this host can
run, using the same checks as the install. It exits 1 when sbx can't be
installed here.

```text
os:         Ubuntu 24.04.1 LTS
machine:    a VM (kvm), 8 CPUs
memory:     15988 MiB
disk:       142 GiB free on /
/dev/kvm:   no; a VM without nested virtualization: enable it at the provider, or use bare metal
public IP:  203.0.113.10
port 22:    in use by sshd on 0.0.0.0:22
port 53:    in use by systemd-resolve on 127.0.0.53%lo:53
port 80:    free
port 443:   free
sbx:        not installed
runtimes this host can run:
  embed      no: no /dev/kvm: use bare metal or a VM with nested virtualization enabled
  e2b-cloud  yes: needs an E2B API key and a domain pointing here
  boat       yes: needs a Boat API key (a paid plan: the trial saves no templates) and a domain pointing here
verdict:    a provider runtime (e2b-cloud or boat): it needs the owner's API key and a domain
```

How to read it:

- **os.** Only Ubuntu 24.04 is supported. On anything else, stop and ask the
  owner for an Ubuntu 24.04 server.
- **machine and /dev/kvm.** E2B Embed runs sandboxes on this server and needs
  `/dev/kvm`. If it is missing, the line explains why: a VM without nested
  virtualization (the owner can turn it on at the provider, or use bare
  metal), or a kvm module that isn't loaded.
- **memory and disk.** Compare them with the thresholds in step 2.
- **public IP.** The address DNS records point to. If it says unknown, the
  server has no outbound HTTPS, which sbx needs.
- **ports.** A listener on loopback only (`127.0.0.53`, `127.0.0.1`) doesn't
  matter. If something else holds 80 or 443 on a public address, sbx can't
  serve there: ask the owner whether to stop it. 53 matters only for
  `--dns delegated`.
- **sbx: installed.** Running `install.sh` again upgrades the installation and
  keeps its settings. Use [operating the host](reference.md#operating-the-host)
  rather than this guide.

**From outside.** The server can't see its provider's firewall, so check from
your own machine. Before the install nothing listens on 80 or 443, so the
kind of failure tells you what is there:

```sh
nc -vz -w 5 IP 443    # "refused": the port reaches the server; "timed out": a firewall drops it
```

Check each port the decisions below need. A timeout means the provider's
firewall (security group, cloud firewall) or a host firewall (`ssh HOST
sudo ufw status`) drops it: ask the owner to open it. sbx's host firewall opens only
what it needs:

| Port | Needed for |
| --- | --- |
| TCP 22, 443 | always (SSH, HTTPS: the web app, previews, endpoints) |
| TCP 80 | IP access (the IP certificate) |
| UDP and TCP 53 | a domain with `--dns delegated` |
| TCP 21000–21255 | IP access only: each preview gets its own port |

## 2. Decide the runtimes

A runtime is where sandboxes run ([runtimes](reference.md#runtimes)). An
installation can enable several, and each sandbox runs on one of them.

| Runtime | Runs sandboxes | The server needs | The owner provides |
| --- | --- | --- | --- |
| `embed` | on this server (Firecracker) | `/dev/kvm` and `/dev/net/tun`; 24 GiB of memory (16 is the minimum, with an explicit `--hugepages`); 60 GiB of free disk (200 GB recommended) | nothing |
| `e2b-cloud` | at E2B | 2 GiB of memory, 10 GiB of free disk | an E2B API key, the team's plan (Hobby or Pro), a domain |
| `boat` | at Boat | 2 GiB of memory, 10 GiB of free disk | a Boat API key, the plan (`20`, `100`, `500`, `2000`), a domain |

Decide:

- **`embed: yes`** → use Embed. You don't need to ask anything. Each sandbox
  takes 4 GiB unless its recipe asks for more, and 12 GiB stays for the host.
  The owner may also want a provider runtime for more capacity. Ask only if
  the repositories need more than the server holds.
- **`embed: no`** → use a provider runtime. Tell the owner why Embed can't run
  (the line from `--check`) and what would fix it, then ask:
  1. **E2B Cloud or Boat, and which plan.** Show the owner this comparison:

     | | E2B Cloud | Boat |
     |---|---|---|
     | New sandbox ready | about 1–2 s | about 5–6 s |
     | Pause and resume | keeps memory: processes continue | keeps the disk: a resume after ~10 minutes is a cold boot and sbx restarts the app |
     | Plans | Hobby: 1 h before an idle sandbox pauses, 8 vCPUs, 8 GiB, 10 GiB disk, 10 at once. Pro: 24 h, 20 GiB disk, 100 at once | `20`: up to 8 vCPUs, 16 GiB, 100 at once; `100` and up add 16 vCPUs, 32 GiB. The trial can't save snapshots, and sbx's templates are snapshots, so plan on `20` or above |
     | Price | per second while a sandbox runs | per second while a sandbox runs (e.g. 4 vCPUs, 8 GiB: about $0.04 an hour) |

     Both work with the same recipes; several can be enabled.
  2. **The API key.** The owner creates it in the provider's dashboard (E2B:
     [e2b.dev/dashboard](https://e2b.dev/dashboard), API keys; Boat:
     [boat.dev/dashboard](https://boat.dev/dashboard), API keys, scope
     `full-sandbox`) and saves it in a file only they and you can read, on
     your machine. It never goes into the chat or onto a command line.
     Give the owner the line for their system:

     ```sh
     # macOS, with the key copied
     (umask 077; pbpaste > ~/.sbx-install/e2b-key)
     # any system: type or paste the key, then Enter
     (umask 077; read -rs k; printf '%s\n' "$k" > ~/.sbx-install/e2b-key)
     ```

     (`boat-key` for Boat.) Check it arrived without reading it:
     `wc -c < ~/.sbx-install/e2b-key` (a few dozen bytes). Step 5 hands it to
     sbx's secret store; delete the file then (`rm ~/.sbx-install/e2b-key`).
  3. A domain (step 3). Provider runtimes need one.
- **Several runtimes** are fine. The first one enabled is the default for
  new sandboxes. Change it with `sbx runtimes default KIND`, or set it per
  repository.
- **Untrusted public repositories** (`access.public_repos`) can't be served
  while Boat is enabled (it has no per-sandbox egress rules). They can't
  share a server with managed servers or `internal_cidrs` either.

## 3. Decide the address

| | IP only | A domain `D` |
| --- | --- | --- |
| Embed alone | works: a trusted IP certificate (port 80), previews on ports 21000–21255 | previews at `<name>.D` on 443 |
| E2B Cloud, Boat | no: their sandboxes call the server back at `login.D` | required |
| Endpoints (step 6) | no | required |
| The `sbx` CLI | works | works with a trusted certificate (not `--dns none`) |

If the decision needs a domain, or the owner prefers one, ask for:

- **The domain** `D`: one the owner controls, usually a subdomain
  (`sbx.example.com`). The server will be at `login.D`, with previews at
  `<name>.D`.
- **An email address** for Let's Encrypt's expiry notices.
- **How the wildcard certificate is issued** (`--dns`):
  - `delegated` (the default, needs no credential): records `D A IP`,
    `*.D A IP` and `_acme-challenge.D NS D.`, plus inbound UDP and TCP 53.
  - `cloudflare`: the A records, plus an API token that may edit the zone's
    DNS. The owner saves it in `~/.sbx-install/cloudflare-token`.
  - `route53`: the A records. On EC2 it uses the instance role. Elsewhere,
    the owner saves an AWS access key id and its secret as two lines in
    `~/.sbx-install/aws-keys`.
  - `none`: a self-signed certificate, for a quick test only. With
    `D` set to the public IP with dashes for dots, plus `.sslip.io` (IP
    `203.0.113.10` → `203-0-113-10.sslip.io`), DNS needs no records at all.
    Browsers warn, and the `sbx` CLI refuses the certificate (only `sbx setup
    --insecure` takes it), so steps 6 to 8 can't use the CLI. In provider
    sandboxes, only sbx's own calls accept it. Use it to look at sbx, not to
    onboard a team or a repository.

Once the owner says the records are in, check them from outside before you
go on:

```sh
dig +short login.D @1.1.1.1; dig +short anything.D @1.1.1.1   # both: the public IP
dig +short NS _acme-challenge.D @1.1.1.1                      # delegated: D.
```

## 4. Install

The install command is the same in every case. Step 5 applies the runtime
and address decisions through the setup link, so a provider's key goes from
your machine to sbx's secret store over HTTPS and never passes through the
server's shell.

The install takes several minutes on a provider host and 30 minutes or more
with Embed (images, then the base templates). Start it detached on the
server, so a dropped connection or a tool timeout can't stop it:

```sh
ssh HOST 'cd ~/sbx && sudo rm -f /root/sbx-install.exit && sudo nohup sh -c "sh install.sh > /root/sbx-install.log 2>&1; echo \$? > /root/sbx-install.exit" < /dev/null > /dev/null 2>&1 &'
```

Then check it every minute or two until it prints an exit status:

```sh
ssh HOST 'sudo grep -v "#t=" /root/sbx-install.log | tail -n 5; sudo cat /root/sbx-install.exit 2>/dev/null || echo running'
```

The `grep` keeps the setup link out of your output.

- **`0` as the last line** means done. Confirm the outcome with
  `ssh HOST 'sudo grep -E "Done:|No sandbox runtime yet|FAILED" /root/sbx-install.log'`.
  It shows `==> Done: finish the setup in your browser`. On a provider host
  it also shows `No sandbox runtime yet`. That is expected: step 5 adds the
  runtime. On an Embed host, Embed is enabled and its templates are built.
- **Another number** means it failed. Read the `FAILED: ...` line, and
  `ssh HOST 'sudo grep -v "#t=" /root/sbx-install.log | tail -n 40'` for
  more. Fix the cause and start the same command again: every step is safe
  to repeat.

The installer has more options (`ssh HOST 'cd ~/sbx && sh install.sh
--help'`). You don't need them here. Private ranges are set in step 6 if
the repository needs them.

## 5. Finish the setup

`sbx setup` takes the setup wizard's steps on your machine. It authenticates
with the setup link, which it reads from a file. Fetch the link into that
file without printing it:

```sh
ssh HOST sudo sbx-admin setup-link > ~/.sbx-install/setup-link
python3 ~/.sbx-install/sbx.py setup status --link-file ~/.sbx-install/setup-link --insecure
```

`status` lists `address`, `runtime` and `admin` as `todo` or `done`. Each
`setup-link` call makes a new link, and the previous one stops working.
`--insecure` accepts the self-signed certificate the server uses until the
address is set.

**a. The address** (step 3). Run the one line that matches the decision. Each
waits until HTTPS works, for up to 15 minutes:

| Decision | Command |
| --- | --- |
| domain, `delegated` | `python3 ~/.sbx-install/sbx.py setup address --link-file ~/.sbx-install/setup-link --insecure --domain D --email E` |
| domain, `cloudflare` | `python3 ~/.sbx-install/sbx.py setup address --link-file ~/.sbx-install/setup-link --insecure --domain D --email E --dns cloudflare --secret cloudflare_api_token < ~/.sbx-install/cloudflare-token` |
| domain, `route53` on EC2 | `python3 ~/.sbx-install/sbx.py setup address --link-file ~/.sbx-install/setup-link --insecure --domain D --email E --dns route53` |
| domain, `route53` elsewhere | `python3 ~/.sbx-install/sbx.py setup address --link-file ~/.sbx-install/setup-link --insecure --domain D --email E --dns route53 --secret aws_access_key_id --secret aws_secret_access_key < ~/.sbx-install/aws-keys` |
| IP only | `python3 ~/.sbx-install/sbx.py setup address --link-file ~/.sbx-install/setup-link --insecure --ip` |
| quick test | `python3 ~/.sbx-install/sbx.py setup address --link-file ~/.sbx-install/setup-link --insecure --domain D --email E --dns none` |

- `address ready: https://login.D` (or `https://IP`) means it is done.
- `sbx-control: Check the DNS records ...` lists each DNS check. Fix the
  record it names and run the same line again.
- `waiting for the certificate: ...` that runs out usually means a closed
  port: 53 for `delegated`, 80 for IP only (step 1, "From outside").

Then fetch the link again. It now names the trusted address, so the
commands below no longer need `--insecure`. Only the quick test keeps
`--insecure` on every command below.

```sh
ssh HOST sudo sbx-admin setup-link > ~/.sbx-install/setup-link
```

**b. The runtime.** Skip this on an Embed host: Embed is already enabled.
For a provider, run the line for the owner's choice and plan (E2B Cloud:
`hobby` or `pro`; Boat: `20`, `100`, `500` or `2000`):

```sh
python3 ~/.sbx-install/sbx.py setup runtime add e2b-cloud --link-file ~/.sbx-install/setup-link --plan hobby --secret E2B_CLOUD_API_KEY < ~/.sbx-install/e2b-key
python3 ~/.sbx-install/sbx.py setup runtime add boat --link-file ~/.sbx-install/setup-link --plan 20 --secret BOAT_API_KEY < ~/.sbx-install/boat-key
```

It waits while the server applies the change and restarts. It prints that
the runtime is enabled, and exits non-zero with the reason when it fails.
If the provider refuses the key, nothing changes: ask the owner to check
the key file. The base templates then build on the new runtime, which takes
about ten minutes. For a second provider, run its line too.

**c. The first admin.** This is the owner's own account. Ask for their
email. Write a password into a file (or let the owner write their own
there), then create the account and finish:

```sh
(umask 077; openssl rand -base64 24 > ~/.sbx-install/admin-password)
python3 ~/.sbx-install/sbx.py setup admin owner@example.com --link-file ~/.sbx-install/setup-link < ~/.sbx-install/admin-password
python3 ~/.sbx-install/sbx.py setup finish --admin owner@example.com --link-file ~/.sbx-install/setup-link < ~/.sbx-install/admin-password
```

`finish` runs the server's checks, including one that starts a sandbox and
reaches this server from inside it.

- `set up: https://login.D; the setup link no longer works. Next: sbx login`
  means it is done.
- `The server needs attention before setup can finish` is followed by each
  failed check. If the check says the base template is still building, wait
  five minutes and run `finish` again. For anything else, fix what the check
  names, then run `finish` again.

Tell the owner the admin password is in `~/.sbx-install/admin-password`.
They can change it under **CLI & access**. Teammates get their own accounts
under Settings → People, or with
`ssh HOST sudo sbx-admin user add teammate@example.com`, which prints a
one-time link to send them. To sign in through an identity
provider instead, see OIDC in the [README](../README.md#host-requirements).

**If the owner prefers the browser.** They run `ssh HOST sudo sbx-admin
setup-link` in their own terminal and open the link. The wizard has the same
steps. Continue when they say it is finished.

**d. Sign in the desktop CLI** (macOS). It is needed from step 6 on, and it
doesn't work with the quick test's certificate:

```sh
curl -fsSL https://login.D/install | sh    # IP only: https://IP/install
~/.local/bin/sbx login
```

`sbx login` opens the browser. The owner signs in there with
`owner@example.com` and the password file. When it prints that you are
signed in, `~/.local/bin/sbx runtimes` lists the enabled runtimes. Below,
`sbx` means `~/.local/bin/sbx`. The desktop CLI runs on macOS only. On
Linux, use the [HTTP API](reference.md#http-api) with a token from
`ssh HOST sudo sbx-admin token create owner@example.com --label agent`
(printed once: write it to a file in `~/.sbx-install`), and the server-side
commands given below.

## 6. Optional: shared servers

Do this only if the repository (step 7) needs a service on the server: a
managed Postgres or ClickHouse whose databases each sandbox clones, or a host
on the owner's private network. Hosted catalog packages (Neon, PlanetScale,
...) need none of it. You will usually find out while reading the repository
in step 7. Come back here when you do.

```sh
ssh HOST sh -s <<'EOF'
sudo sbx-admin server up dev-pg --kind postgres                         # a managed Postgres (or --kind clickhouse)
sudo sbx-admin config set network '{"internal_cidrs": ["10.0.0.0/24"]}'  # only for a private host: its range
sudo sbx-admin network apply                                            # Embed: before creating sandboxes that use either
EOF
```

Keep only the lines you need.

- **On Embed**, sandboxes reach these directly. Nothing is exposed.
- **On E2B Cloud or Boat**, sandboxes reach them only through an
  [endpoint](reference.md#endpoints). An endpoint makes the service reachable
  from the internet at `<name>.D:443`, behind TLS with a client certificate
  per sandbox (its own CA, 24-hour certificates, only for the repositories
  allowed to use it). It needs the domain from step 3. **Ask the owner
  before you add one**, naming the service and the address it will have.
  Then:

  ```sh
  sbx endpoints                                    # the managed servers without one
  sbx endpoints add --server dev-pg                # for the repos attached to it; waits until it is on
  sbx endpoints add --name temporal --target 10.0.0.5:7233 --repo owner/app   # a private host, per repo
  ```

  `add` prints `NAME: on, at NAME.D:443` when it is done, and exits non-zero
  with the reason when it fails. On the server, `sbx-admin endpoint ls|add`
  takes the same flags.

  A repository that needs one and doesn't have it is refused on that runtime,
  and the refusal names the command. Sandboxes connect to `127.0.0.1` in
  plain text, and sbx rewrites the injected addresses. Check with a real
  query from a sandbox (`sbx sandbox exec`), not only that it starts.

## 7. Set up the repository

1. **Access.** The owner connects GitHub on the web app's **Extensions** page
   and selects the repository. A public repository URL works without a
   GitHub App.
2. **Recipe.** In the repository's checkout, `sbx repo connect --agent`
   starts Claude Code (or `--agent codex`) with the server's setup brief.
   Without that agent, it prints the brief for you to follow (the API serves
   it at `GET /api/repos/OWNER/NAME/setup-brief`). The
   [setup brief](../control/sbx_control/template_files/setup-brief.md) is the
   procedure from discovery to a verified first launch. It chooses services
   with [service selection](service-selection.md), declares every variable,
   writes a small [recipe](reference.md#recipe), sizes it with the
   [resource guide](../control/sbx_control/template_files/resource-guide.md)
   (within the runtime's limits: `sbx runtimes` lists them) and launches it.
   For preloaded data, see
   [development baselines](reference.md#development-baselines). Ask the owner
   for every secret value you can't generate, as a file, never in the chat.
3. **Coding agents.** Ask which coding agents the team uses. For Claude Code
   cloud sessions, follow [Claude Code](claude-code.md). For Amp threads,
   follow [Amp](amp-orbs.md). Each needs a key that only the owner can copy
   and paste on the **Extensions** page. Start one session to confirm that
   its sandbox comes up. Other agents reach sandboxes over SSH
   (`sbx ssh-config`) or with `sbx open NAME AGENT`.

## 8. Verify and report

```sh
ssh HOST sudo sbx-admin doctor                 # each part: ok / NO with the fix; exit 1 when one is NO
sbx runtimes test KIND                     # per enabled runtime; exit 1 on a NO
ssh HOST sudo sbx-admin runtime verify KIND   # end to end: create, command, file, pause/resume, preview, delete
curl -sS -o /dev/null -w '%{http_code}\n' https://login.D/   # from your machine: a 200 or 30x, no TLS error
```

- `doctor` and `runtimes test` print one line per check, `ok` or `NO` with
  the fix. Fix every `NO` before you go on.
- When `runtimes test` says the base template is still building, wait and
  run it again.
- `runtime verify` reports each step of a throwaway sandbox's life. Run it
  for each enabled runtime.

Then run the [acceptance checklist](#acceptance-checklist) on each runtime
the repository uses.

Report to the owner:

- what is set up: the address, the runtimes and their plans, the default,
  any endpoints and who may use them;
- working preview links, and the commands to start another sandbox
  (`sbx up`), read logs (`sbx logs`), change variables (`sbx env`) and delete
  one (`sbx down`);
- what you verified, with the evidence (the checks' output, the checklist),
  and what you did not verify;
- what is limited (for example a `--dns none` certificate, or a plan's
  limits) and which actions remain theirs (the admin password file, keys to
  rotate);
- next options: a setup PR (`sbx recipe pr`), PR previews, a prebuilt
  template.

## Acceptance checklist

Every sandbox is a development environment of its own: it must be able to
migrate, seed and reset its data without changing another sandbox's data or
schema. Shared service infrastructure keeps this cheap, but the databases,
branches or namespaces on it belong to one sandbox each. This is the one
place that rule is stated; other docs link here.

Before you start, make sure the recipe gives each sandbox:

- its own database, branch or namespace, provisioned before `.sbx/setup` runs,
  with a login that may run the repository's normal migrations (schema changes
  included);
- setup that runs the normal migrations and development seeds and is safe to
  repeat;
- SQLite files, uploads and other app state in its own filesystem or in a
  resource of its own;
- a template, if any, with tools and dependencies only: no credentials and no
  other sandbox's data.

Then check, with two disposable, named sandboxes A and B on each runtime the
repository uses (`sbx up A --runtime KIND`):

- [ ] Both launch cleanly from the recipe and pass a useful app action.
- [ ] Migrate and seed both. Change or reset A's data; B still has its original
      data and schema.
- [ ] Restart A (`sbx sandbox restart A`), then pause and resume it
      (`sbx sandbox pause|resume A`, or `POST /api/sandboxes/A/pause` and `/resume`):
      its working data is still there. (On Boat every resume is a cold boot:
      the app must start again from the recipe.)
- [ ] On a provider runtime with an endpoint: a real query from a sandbox
      reaches the shared server through it.
- [ ] Delete A (`sbx down A`): B still works, and A's owned resources (its
      database, branch, role or namespace) are gone while the shared servers stay.
- [ ] No secret value appears in the recipe, the template, commits or logs.

Record what you verified and what you did not (for example optional providers
that were never configured).
