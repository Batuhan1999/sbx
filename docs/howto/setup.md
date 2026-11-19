# Your sbx server's onboarding

The installer's last line is the onboarding page's address:

```text
==> Open the onboarding: https://203.0.113.10/onboarding
```

Open it in your browser. It is an address, not a credential. Lost it?
`https://` and the server's IP address lead to it until the onboarding is
finished.

The onboarding page has four steps: **Account**, **Domain**, **Sandboxes**
and **GitHub**. A summary in the control plane comes last.

## Account

Enter your email and a password of your own. The form works at once, at the
address the installer printed. You become the first admin and stay signed in
for the steps that follow; you can add people and single sign-on later, under
**Settings**.

If the page says the server already has an admin and that is not you, run
`sudo sbx-admin setup-reset --sign-in` on the server: it removes every
account and prints the address again.

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
again**. Once the records are found, the step waits for the certificate
(usually under a minute). Then the page moves itself to
`https://app.<domain>/onboarding`, where you are signed in already, and goes
on. If that address has not answered your browser after ten seconds, the step
also offers an **Open app.\<domain\>** button. A certificate that fails
shows its error and **Try again**. Let's Encrypt issues at most five
certificates a week for the same domain; past that the page says when it
allows the next and waits until then (another subdomain has its own limit).

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
  or Boat. Pick one, paste its API key and choose **Check key**; the field
  links to the provider's dashboard, where the key is. That is all it asks:
  sbx reads the plan from the key. Boat refuses a key still on its
  free trial, because the trial saves no templates
  ([why](boat.md#plan)). Where to get each key: [E2B](e2b.md#api-key),
  [Boat](boat.md#api-key).

Adding a provider restarts the server for a few seconds: **Check key** stays
busy until the provider runs, then it shows as chosen with **Continue**. Once a
runtime is chosen, the templates start building and a progress line
shows where they are ("step 3 of 12, about 6 min left"). The first build takes
about 10 minutes. You don't have to wait: keep going. A build that fails says
why in words where sbx knows the error (a package mirror mid-update, the
network, [Boat's saved snapshots](boat.md#saved-snapshots) used up) and is
built again by itself 5 and then 30 minutes later.

## GitHub

sbx clones your repositories and opens pull requests through a GitHub App of
its own. Choose **Connect GitHub** to create it under your account (**For an
organization?** first, for an organization's repositories); GitHub then asks
which repositories it may use ([GitHub for sbx](github.md)) and brings you to
the control plane. **Skip for now** leaves it for later,
under **Extensions** (public repositories clone without it), and goes to
the control plane too.

## Your server is ready

The control plane opens on **Your server is ready**, a summary: the address,
where sandboxes run, GitHub, and the template builds. Under **Run agent
sessions here** (optional) you can go on to set up [Amp](amp.md)'s
self-hosted orb provider and Claude Code's
[self-hosted environment](../claude-code.md) under **Extensions**. Codex
needs nothing: each developer uses their own login.

**Connect a repository**, **Look around**, a set-up link or closing it
finishes the onboarding; until then the summary comes back. From then on,
`/onboarding` redirects to the control plane, and the repositories page shows
the three steps to a first sandbox until one is connected.

## Connect a repository

Choose **Connect a repository**. One field searches the repositories the
GitHub App can see and, as you type, public ones on GitHub; it also takes a
pasted `owner/repo` or GitHub URL. Choose **Connect**, then copy the prompt
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
