#!/bin/sh
# Lay out a release of the platform in DIR, with the docs and examples'
# READMEs the setup brief carries (repos.guides). host-install.sh in it
# installs and activates it from /opt/sbx-control/releases/<id>/ (install.sh
# stages it there).
#   control/release.sh DIR [VERSION]
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$here")
out=${1:?usage: release.sh DIR [VERSION]}
version=${2:-$(git -C "$root" rev-parse --short HEAD 2>/dev/null || echo dev)$(git -C "$root" diff --quiet 2>/dev/null || echo -dirty)}
rm -rf "$out" && mkdir -p "$out/cli" "$out/licenses" "$out/embed" "$out/docs"
cp -R "$here/sbx_control" "$root/catalog" "$root/infisical" "$out/"
find "$out/sbx_control" "$out/catalog" -name __pycache__ -prune -exec rm -rf {} +
find "$out" -type f \( -name .DS_Store -o -name '._*' \) -delete
# Every host script (control/install.sh is the CLI installer /install serves).
cp "$here"/*.sh "$here/requirements.txt" "$out/"
rm "$out/release.sh"
cp "$root"/embed/*.py "$out/embed/"
cp "$root/cli/sbx.py" "$out/cli/"
cp "$root/docs/service-selection.md" "$root/docs/agent-install.md" "$out/docs/"
for readme in "$root"/examples/README.md "$root"/examples/*/README.md; do
  [ -f "$readme" ] || continue
  dest="$out/examples/${readme#"$root/examples/"}"
  mkdir -p "$(dirname "$dest")" && cp "$readme" "$dest"
done
cp "$root/LICENSE" "$root/THIRD_PARTY_NOTICES.md" "$out/"
cp "$root/vendor/e2b-embed/LICENSE" "$out/licenses/e2b-embed.txt"
cp "$root/web/FONT-LICENSE.txt" "$out/licenses/Geist.txt"
cp "$root/web/src/components/ui/LICENSE.md" "$out/licenses/shadcn-ui.md"
echo "$version" > "$out/VERSION"
