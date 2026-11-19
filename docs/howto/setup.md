# Your sbx server's first-run setup

The installer's last line is your onboarding link:

```text
==> Open the onboarding: https://203.0.113.10/app/setup#t=...
```

Open it in your browser. The link holds a one-time token: keep it out of
shared chats and logs. Lost it? On the server, `sudo sbx-admin setup-link`
prints it again (`--new` replaces it, and the old one stops working); running
the installer again during the setup prints the same link.

The setup page has four steps: **Domain**, **Sandboxes**, **Account** and
**GitHub**.

## Domain

Type the domain sbx should answer under, usually a subdomain such as
`sbx.example.com`. The control plane runs at `app.<domain>` and every preview
at `<name>.<domain>`. The page then lists three records to add at your DNS
provider, each with a copy button:

| Type | Name | Value |
| --- | --- | --- |
| `A` | `D` | the server's IP |
| `A` | `*.D` | the server's IP |
| `NS` | `_acme-challenge.D` | `D.` |

The `NS` record lets the server answer the certificate's DNS check itself, so
sbx needs no token from your DNS provider and no email. For Cloudflare, step
by step: [Cloudflare DNS for sbx](cloudflare.md#dns-records).

Choose **Check records**. A failed check names the record and the fix; new
records can take a few minutes to show up, so fix it and choose **Check
again**. Once the records are found, the certificate comes in the background
and you can continue.

The certificate needs inbound **TCP and UDP port 53**. If your provider's
firewall blocks it, the page says **Port 53 is blocked**: allow both in the
firewall, then choose **Try again**. If your provider can't open port 53 at
all, use a [DNS API token](cloudflare.md#api-token) instead; that is set from
the command line, not on this page.

## Sandboxes

Where sandboxes run:

- **On this server.** A server with `/dev/kvm` already has E2B Embed, shown
  as chosen. Nothing to add; **Also add a provider for more room** adds a
  hosted one next to it.
- **At a provider.** A server without `/dev/kvm` runs sandboxes at E2B Cloud
  or Boat. Pick one, paste its API key and choose **Check key**. That is all
  it asks: sbx reads the plan from the key. Boat refuses a key still on its
  free trial, because the trial saves no templates
  ([why](boat.md#plan)). Where to get each key: [E2B](e2b.md#api-key),
  [Boat](boat.md#api-key).

Once a runtime is chosen, the templates start building and a progress line
shows where they are ("step 3 of 12, about 6 min left"). The first build takes
about 10 minutes. You don't have to wait: keep going.

## Account

Enter your email and a password of your own. You become the first admin; you
can add people and single sign-on later, under **Settings**.

The form waits until the domain's certificate is ready (usually under a
minute), then the page moves itself to `https://app.<domain>`, with what you
typed kept. **Create account and finish** creates the account, signs you in
and finishes the setup. The onboarding link stops working after that.

## GitHub

sbx clones your repositories and opens pull requests through a GitHub App of
its own. Enter the GitHub organization that owns the repositories (or leave
it empty for your own account) and choose **Create the app on GitHub**;
GitHub then asks which repositories it may use ([GitHub for sbx](github.md)).
**Skip for now** leaves it for later, under **Extensions**; public
repositories clone without it.

## Your server is ready

The page opens the control plane, with a one-time **Your server is ready**
summary: the address, where sandboxes run, GitHub, and the template builds.
Under **Run agent sessions here** (optional) you can set up
[Amp](amp.md)'s self-hosted orb provider and Claude Code's
[self-hosted environment](../claude-code.md) under **Extensions**. Codex needs
nothing: each developer uses their own login.

## Connect a repository

Choose **Connect a repository**, pick the repository, and copy the prompt
(**Copy prompt**). Paste it into a coding agent (Claude Code, Codex, Amp or
another one that runs shell commands) working in a checkout of that
repository. The agent:

1. needs the sbx CLI signed in as you: if it isn't installed yet,
   `curl -fsSL https://app.<domain>/install | sh` installs it and ends with
   `sbx login`, one click in your browser, where you are signed in;
2. works out how the app is built and started and writes its `.sbx/` recipe;
3. asks you for each value only you have (an API key the app needs), which
   you enter yourself with `sbx env set NAME` or on the repository's
   **Environment** page, never in the chat;
4. starts one sandbox (`sbx up check-a`) and gives you its preview link.
