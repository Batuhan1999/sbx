# Your sbx server's first-run setup

Your agent installs sbx on your server and then sends you two links: the
**setup link**, where you create your account, and a **keys page**, where you
paste the keys only you have. This page shows what each one looks like. Your
agent does the rest of the setup from its side.

## Create your account

The setup link opens the first-run setup at the **Your account** step. Enter
your email and a password of your own; that signs you in. You are the first
admin. Your agent never sees the password: it signs in through your browser
with `sbx login` afterwards.

![The setup's Your account step: admin email, password and repeat password](images/setup-account.png)

If you set up a domain yourself, the **Server address** step comes first: it
asks for the domain and lists its three DNS records, and port 53 must be open;
see [Cloudflare DNS for sbx](cloudflare.md#dns-records). It needs no email.

## Paste your keys

The keys page lists every key your agent needs in one form: your sandbox
runtime's API key, an agent's token, values your app needs. Sign in, paste
each key once and choose **Save keys**. Each key goes straight to where sbx
uses it and is never shown again. **How do I get this?** under a field opens
the guide for that key ([E2B](e2b.md), [Boat](boat.md), [Amp](amp.md)).

![The keys page asking for a Boat API key and an Amp provider token](images/keys-page.png)

After you save, the page shows when the server has applied runtime keys; you
can close it then. A key sbx refuses is marked under its field: fix it and
save again. The link works for 24 hours unless your agent chose otherwise; ask
it for a new one after that.

## Sandbox runtime and coding agents

Your agent normally fills in these steps for you; you can also do them on the
setup page. **Sandbox runtime** is where sandboxes run: E2B Embed on this
server (it needs `/dev/kvm`), or a hosted one such as E2B Cloud or Boat.

![The setup's Sandbox runtime step: E2B Embed, E2B Cloud and Boat](images/setup-runtime.png)

**Coding agents** are the agents your team starts sandboxes from. Every
sandbox template has each of them, chosen or not; choosing one decides which
key sbx asks for, and adding it later rebuilds nothing. Choosing none is fine.

![The setup's Coding agents step with Amp chosen](images/setup-agents.png)

The first template builds start once the address and the sandbox runtime are
chosen and take about 10 minutes; you can finish the setup while they run.
