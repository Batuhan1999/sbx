# Cloudflare DNS for sbx

For an owner whose domain's DNS is at Cloudflare; other DNS providers take
the same records. sbx runs at `app.D` and serves previews at `<name>.D`,
where `D` is a name you choose, usually a subdomain such as
`sbx.example.com`. You type `D` on the setup page's **Domain** step, which
lists the three records with the server's public IP. Cloudflare's dashboard changes from time to time, so the labels below
may differ a little from what you see.

## DNS records

In Cloudflare, adding the records is what creates the subdomain. Nothing else
needs to be set up first. In the zone's DNS records (for `sbx.example.com`,
the zone is `example.com`), add three records:

| Type | Name | Content | Proxy status |
| --- | --- | --- | --- |
| `A` | `sbx` (the domain `D` itself) | the server's public IP | DNS only |
| `A` | `*.sbx` (the wildcard `*.D`) | the server's public IP | DNS only |
| `NS` | `_acme-challenge.sbx` | `sbx.example.com` (the domain `D`) | |

Turn the proxy off (the grey cloud, "DNS only") on both `A` records:

- Cloudflare's free proxy certificate covers `example.com` and
  `*.example.com`, not a wildcard one level further down such as
  `*.sbx.example.com`, so proxied previews would fail with a TLS error.
- sbx issues its own certificate for `D` and `*.D` and serves it itself; the
  proxy would sit in front of it.

Leave TTL on Auto. Keep no other record at `_acme-challenge.sbx` (an older
`NS` to somewhere else, a `CNAME`): it sends the certificate's challenge
away from the server, and sbx refuses the domain until it is gone. The
server must accept inbound TCP and UDP on port 53, in any cloud firewall
too ([delegated DNS](#delegated-dns)).

## Delegated DNS

The `NS` record hands `_acme-challenge.D` to the server: sbx answers Let's
Encrypt's challenge with its own small DNS server, so it needs no
credential for your DNS provider. This is what the setup page uses.

Before it asks Let's Encrypt, sbx checks the path end to end: its DNS
server answers a random test record, and sbx asks a public resolver
(1.1.1.1, else 8.8.8.8) for it. When none hears back, the setup shows
`port 53: no public resolver ... reached this server on port 53` with the
fix: allow TCP and UDP port 53 in your provider's firewall, then choose
**Try again** (the page says **Port 53 is blocked**). The challenge is answered only while that port is reachable, so
a firewall change later breaks renewals; `sbx-admin doctor` runs the same
check.

If your provider can't open port 53, use an [API token](#api-token) instead.

## API token

The fallback when port 53 can't be opened: sbx answers the challenge through
Cloudflare's API, so it needs a token that may edit the zone's DNS. To create
one (labels may differ):

1. **Profile → API Tokens → Create Token.**
2. Use the **Edit zone DNS** template.
3. **Zone Resources:** Include → Specific zone → your zone (`example.com`).
4. Optional: **Client IP Address Filtering** to the server's public IP, so
   the token works only from the server.
5. Leave **TTL** (the token's expiry) empty. sbx renews the certificate on
   its own every couple of months, and an expired token makes that renewal
   fail.
6. Create the token and copy it once; Cloudflare does not show it again.

If sbx reports that it can't find the zone, edit the token and add the
permission **Zone → Zone → Read** next to the template's DNS edit permission.

With a token, remove the `NS` record at `_acme-challenge.sbx`: sbx writes
the challenge's `TXT` record in the zone itself, and a delegation would send
Let's Encrypt elsewhere. Port 53 can stay closed.

The setup page offers only the `NS` way, so the token is set from the
command line, with the onboarding link: save the token in a file only you
can read, then run (or have your agent run)
`sbx setup address --link-file LINK-FILE --domain D --dns cloudflare --secret cloudflare_api_token < TOKEN-FILE`
([without a browser](../agent-install.md#2-the-address)). Never paste the
token into a chat or onto a command line. The setup page then shows the
domain done; go on from **Sandboxes**. After the setup, replace the token in
the web app's **Settings → Domain & certificate**. sbx keeps the token in its
secret store.

## Why sbx needs the token

Each preview has its own name under `D`, so sbx needs a wildcard certificate
for `*.D`. Let's Encrypt issues wildcard certificates only through the DNS-01
challenge: a TXT record at `_acme-challenge.D`, which Let's Encrypt reads.
With delegated DNS the server answers it itself; with a token sbx publishes
it through Cloudflare's API and removes it again. Certificates last 90 days
and sbx renews them about every 60 days, each time with a new TXT record, so
the token, or port 53, has to keep working for as long as the server runs.
The certificate needs no email: Let's Encrypt no longer sends expiry
notices.

## Check the records

The setup page's **Check records** checks them. To check them yourself from
outside:

```sh
dig +short app.D @1.1.1.1; dig +short anything.D @1.1.1.1   # both: the public IP
dig +short NS _acme-challenge.D @1.1.1.1                      # delegated: D. (with a token: nothing)
dig +short CNAME _acme-challenge.D @1.1.1.1                   # nothing
```

New records usually answer within minutes. On the server, `sbx-admin doctor`
and the Test button in **Settings → Domain & certificate** run the same
checks.
