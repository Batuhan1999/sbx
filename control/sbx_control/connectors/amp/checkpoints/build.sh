#!/bin/sh
# For the Amp connector's project caches only: Amp snapshots a thread's
# sandbox filesystem-only (Sandbox.create_snapshot(mode="filesystem"),
# connectors/amp/snapshots.py), which Embed's published images keep behind a
# feature flag. This builds Embed's API image and orchestrator binary from the
# runtime source with filesystem-checkpoints-env.patch, which lets
# FILESYSTEM_ONLY_CHECKPOINT_ENABLED turn the flag on, without changing the
# vendored runtime. Run on the Linux/amd64 host; the compilers run in Docker,
# capped apart from the sandboxes' hugepage RAM.
#
#   sh /opt/sbx-control/current/sbx_control/connectors/amp/checkpoints/build.sh OUTPUT_DIRECTORY
#
# Then build the API image from OUTPUT_DIRECTORY/Dockerfile on E2B_API_IMAGE
# (docker build --build-arg BASE_IMAGE=... -t TAG OUTPUT_DIRECTORY), name both
# in the amp settings, and apply them (it recreates the runtime):
#   sbx-admin config set amp '{"checkpoint_api_image": "TAG", "orchestrator_binary": "OUTPUT_DIRECTORY/orchestrator"}'
#   sbx-admin network apply
#
# The source and versions match the vendored Embed release
# (vendor/e2b-embed/compose/.env, its 202610051633 images): change them with it.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
out=${1:?usage: build.sh OUTPUT_DIRECTORY}
commit=2ae80b7a95ae58d855d2a34f7f5beb3be0fa9816
archive_sha256=62725b01e555ea83916e1e233329a5f3e8a0866afb08267731e888c055921aca
release=202610051633
migration=20261005085240
go=golang:1.26.8-bookworm
mkdir -p "$out"
out=$(CDPATH= cd -- "$out" && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/sbx-amp-checkpoints.XXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM
curl -fsSL "https://codeload.github.com/e2b-dev/runtime/tar.gz/$commit" -o "$work/source.tar.gz"
[ "$(sha256sum "$work/source.tar.gz" | cut -d ' ' -f 1)" = "$archive_sha256" ] \
  || { echo 'runtime source archive checksum differs' >&2; exit 1; }
mkdir "$work/source"
tar -xzf "$work/source.tar.gz" --strip-components=1 -C "$work/source"
patch -s -p1 -d "$work/source" < "$here/filesystem-checkpoints-env.patch"
build() {  # package, output name, CGO_ENABLED, ldflags
  docker run --rm --cpus 2 --memory 2g --memory-swap 3g -e GOMAXPROCS=2 -e CGO_ENABLED="$3" \
    -v "$work/source:/src" -v "$out:/out" -v sbx-checkpoint-go-mod:/go/pkg/mod \
    -v sbx-checkpoint-go-build:/root/.cache/go-build -w "/src/packages/$1" \
    "$go" go build -p 2 -trimpath -ldflags="$4" -o "/out/$2" .
  (cd "$out" && sha256sum "$2" > "$2.sha256")
}
build api api 0 "-s -w -X=main.expectedMigrationTimestamp=$migration -X=main.serviceVersion=0.14.$release-sbx-checkpoints"
build orchestrator orchestrator 1 "-s -w -X=main.commitSHA=$commit -X=github.com/e2b-dev/infra/packages/orchestrator/pkg/version.Version=0.16.$release-sbx-checkpoints"
cp "$here/api.Dockerfile" "$out/Dockerfile"
printf '%s\n' "$commit" > "$out/RUNTIME_COMMIT"
echo "Built in $out; the runtime keeps its own until the amp settings name these (see the top of this script)."
