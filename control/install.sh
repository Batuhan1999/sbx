#!/bin/sh
# Dev sandboxes: install sbx on a Mac.
#   curl -fsSL https://<login host>/install | sh   (the server fills in @SBX_CONTROL@)
# Installs websocat (Homebrew) and the sbx CLI under ~/.local/share/sbx/<version>, puts sbx on PATH and signs in.
set -eu
control=${SBX_CONTROL:-@SBX_CONTROL@}
root="$HOME/.local/share/sbx"

[ "$(uname)" = Darwin ] || { echo "sbx supports macOS only for now"; exit 1; }
command -v brew >/dev/null || { echo "install Homebrew first: https://brew.sh"; exit 1; }
command -v python3 >/dev/null || brew install python
command -v websocat >/dev/null || brew install websocat

version=$(curl -fsSL "$control/dist/version")
tarball=$(mktemp)
trap 'rm -f "$tarball"' EXIT
curl -fsSL "$control/dist/sbx.tgz" -o "$tarball"
expected=$(curl -fsSL "$control/dist/sbx.tgz.sha256")
[ "$(shasum -a 256 "$tarball" | cut -d' ' -f1)" = "$expected" ] || { echo "sbx.tgz does not match its sha256; nothing installed"; exit 1; }
mkdir -p "$root/$version"
tar -xzf "$tarball" -C "$root/$version"
ln -sfn "$root/$version" "$root/current"
echo "$control" > "$root/control"

mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/sbx" <<WRAPPER
#!/bin/sh
exec python3 "$root/current/sbx.py" "\$@"
WRAPPER
chmod +x "$HOME/.local/bin/sbx"

rc="$HOME/.zshrc"; [ "${SHELL##*/}" = bash ] && rc="$HOME/.bashrc"
grep -q '.local/bin' "$rc" 2>/dev/null || printf '\n# sbx (dev sandboxes)\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$rc"

ls "$HOME"/.ssh/id_*.pub >/dev/null 2>&1 || ssh-keygen -q -t ed25519 -N '' -f "$HOME/.ssh/id_ed25519"

echo "sbx $version installed"
"$HOME/.local/bin/sbx" login
# doctor names each agent this server connects and the `sbx login AGENT` it takes.
"$HOME/.local/bin/sbx" doctor || true
echo "open a new terminal, then: sbx ssh-config (once), sbx up my-feature"
