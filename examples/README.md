# Examples

Worked recipes for open-source applications, each run from source and checked
with the acceptance checklist (`docs/agent-install.md`). Pick the one closest to
the repository you are setting up and read the "How this recipe was made"
section of its README: what was found in the repository, the choices made, the
pitfalls and how it was verified.

| Example | Stack | Patterns it shows |
| --- | --- | --- |
| `examples/memos/` | Go server with an embedded React UI | SQLite and uploads on the VM disk; no services; admin login created from a generated value; toolchain versions read from `go.mod` and `package.json`; limiting build parallelism to fit 2 vCPU / 4 GiB. |
| `examples/umami/` | Next.js with Prisma | Per-sandbox Postgres database (`postgres-clone`); migrations run by the normal build; replacing a shipped default admin password; `lifecycle: {resume: restart}` for stale connection pools; telemetry off. |
| `examples/maple-full/` | Bun, Vite, Rust and Go services | Multi-process stack with several health checks; per-sandbox Postgres and ClickHouse databases; a repository-specific catalog package (Electric) per sandbox; Docker inside the VM; same-origin Vite proxies and preview host checks; native builds under a disk and memory budget. |
