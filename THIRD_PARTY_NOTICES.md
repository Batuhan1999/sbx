# Third-party notices

The root MIT license covers sbx's own code. These components keep their licenses:

| Component | Source in this repository | License |
| --- | --- | --- |
| E2B Embed (runtime) | `vendor/e2b-embed/`, pinned in `vendor/e2b-embed/UPSTREAM` | [Apache 2.0](vendor/e2b-embed/LICENSE) |
| Geist / Geist Mono | `web/src/*.woff2` | [SIL Open Font License 1.1](web/FONT-LICENSE.txt) |
| shadcn/ui components | `web/src/components/ui/` | [MIT](web/src/components/ui/LICENSE.md) |
| Provider logos | `catalog/*/logo.svg` | Below |

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

## Provider logos

A catalog package's `logo.svg` is its provider's mark, shown only to say which
service the package works with. The marks are trademarks of their owners, who
do not endorse sbx. Each file is the owner's published mark with only the XML
prolog removed (and, for Upstash, a `viewBox` of its own size added so it
scales). A package that shares a provider links to the same file.

| Mark | File | Source | License |
| --- | --- | --- | --- |
| ClickHouse | `catalog/clickhouse-server/logo.svg` | [ClickHouse/ClickHouse](https://github.com/ClickHouse/ClickHouse/blob/10329cbbf2da51925e5a4580a8ba9faf3315cd02/docker/server/README.src/logo.svg) | Apache 2.0 (the same text as [`vendor/e2b-embed/LICENSE`](vendor/e2b-embed/LICENSE)); [trademark policy](https://clickhouse.com/legal/trademark-policy) |
| Neon | `catalog/neon-project/logo.svg` | [neon.com/brand](https://neon.com/brand), light color logomark | [Brand guidelines](https://neon.com/brand) |
| OpenRouter | `catalog/openrouter-account/logo.svg` | [openrouter.ai/brand](https://openrouter.ai/brand), glyph in Ink | [Brand guidelines](https://openrouter.ai/brand) |
| PlanetScale | `catalog/planetscale-database/logo.svg` | [planetscale.com/brand](https://planetscale.com/brand), black logo mark | [Brand guidelines](https://planetscale.com/brand) |
| PostgreSQL | `catalog/postgres-server/logo.svg` | [wiki.postgresql.org/wiki/Logo](https://wiki.postgresql.org/wiki/Logo), three-color Slonik | [Trademark policy](https://www.postgresql.org/about/policies/trademarks/) |
| Temporal | `catalog/temporal-server/logo.svg` | [Simple Icons](https://simpleicons.org/?q=temporal) 16.38.0 | CC0 1.0 |
| Tinybird | `catalog/tinybird-workspace/logo.svg` | [tinybirdco/grafana-tinybird-datasource](https://github.com/tinybirdco/grafana-tinybird-datasource/blob/7cd0163e74fc8c06e2e0325d14c22345f1d7ce5f/src/img/logo.svg) | Apache 2.0 (as above) |
| Upstash | `catalog/upstash-account/logo.svg` | [upstash.com/brand](https://upstash.com/brand), icon for white backgrounds | [Brand page](https://upstash.com/brand) |

Postgres, PostgreSQL and the Slonik Logo are trademarks or registered
trademarks of the PostgreSQL Community Association of Canada, and used with
their permission.

The AWS packages have no logo: Amazon's
[trademark guidelines](https://aws.amazon.com/trademark-guidelines/) allow
naming AWS only in plain text without its written permission.
