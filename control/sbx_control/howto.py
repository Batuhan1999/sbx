"""The owner-facing how-to pages in docs/howto/, which a check links to as
its help_url when what failed is something the owner sets up outside sbx (DNS
records, a provider's API key, the GitHub App, an Amp provider). The links go
to the pages on GitHub, so they work before the installation has an address.
No imports: runtime backends use it in their class bodies."""
from __future__ import annotations

REPOSITORY = "Batuhan1999/sbx"   # sbx's own repository, as settings.Updates names it by default
BASE = f"https://github.com/{REPOSITORY}/blob/main/docs/howto/"

# Each page and its section anchors (GitHub's slugs of the headings); the
# tests check that every one still exists.
PAGES = {
    "cloudflare": ("dns-records", "api-token", "why-sbx-needs-the-token", "delegated-dns", "check-the-records"),
    "e2b": ("api-key", "plan", "add-it-to-sbx"),
    "boat": ("api-key", "plan", "add-it-to-sbx"),
    "github": ("create-the-app", "select-repositories", "check-it"),
    "amp": ("create-the-provider", "from-the-amp-cli", "give-the-token-to-sbx", "check-the-connection"),
}


def url(page: str, anchor: str = "") -> str:
    if page not in PAGES or (anchor and anchor not in PAGES[page]):
        raise KeyError(f"no how-to {page}#{anchor}")
    return f"{BASE}{page}.md" + (f"#{anchor}" if anchor else "")


DNS_RECORDS = url("cloudflare", "dns-records")
CLOUDFLARE_TOKEN = url("cloudflare", "api-token")
DELEGATED_DNS = url("cloudflare", "delegated-dns")
E2B_KEY = url("e2b", "api-key")
BOAT_KEY = url("boat", "api-key")
BOAT_PLAN = url("boat", "plan")
GITHUB_APP = url("github", "create-the-app")
GITHUB_REPOS = url("github", "select-repositories")
AMP_PROVIDER = url("amp", "create-the-provider")
AMP_CONNECTION = url("amp", "check-the-connection")
