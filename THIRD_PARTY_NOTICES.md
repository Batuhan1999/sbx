# Third-party notices

The root MIT license covers sbx's own code. These components keep their licenses:

| Component | Source in this repository | License |
| --- | --- | --- |
| E2B Embed (runtime) | `vendor/e2b-embed/`, pinned in `vendor/e2b-embed/UPSTREAM` | [Apache 2.0](vendor/e2b-embed/LICENSE) |
| Geist / Geist Mono | `web/src/*.woff2` | [SIL Open Font License 1.1](web/FONT-LICENSE.txt) |
| shadcn/ui components | `web/src/components/ui/` | [MIT](web/src/components/ui/LICENSE.md) |

The files under `vendor/e2b-embed/` are unmodified copies from the upstream
commit recorded in `UPSTREAM`; `vendor/e2b-embed/compose/.env` there is also
where the installer reads the runtime image pins. sbx's own runtime
configuration is in `embed/compose.override.yaml`.

The Amp connector's optional filesystem-checkpoint build
(`control/sbx_control/connectors/amp/checkpoints/build.sh`) downloads a second,
different upstream runtime commit, verifies its checksum, applies
`filesystem-checkpoints-env.patch` beside it, and builds a
modified API image and orchestrator binary from it. Those artifacts
are derived works of the upstream runtime under Apache 2.0, and the patch is the
change made. The pinned commit is in the build script.

Control-service releases include these notices under `licenses/`. Python and npm
dependencies, container images and downloaded tools keep their own licenses.
