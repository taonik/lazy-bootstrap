# system-deps

One file per (distro family, component). Plain text: one package per line,
`#` comments, blank lines ignored.

This directory is the **single source of truth** for the packages a build
environment needs. It is consumed by:

* `lazy-bootstrap` at run time (`--system-deps target|host|off`), and
* the `ci/images/*/Containerfile` flavours, which `ADD` these very files.

So a missing dependency is fixed once, here - never as a workaround in the
driver code (see docs/SPECS.md D-24).

| component | when it is used |
|---|---|
| `common`  | always: the distro's own build machinery |
| `gcc`     | toolchain kind `gcc` |
| `llvm`    | toolchain kind `llvm` (distro or upstream tarball) |
| `filc`    | toolchain kind `filc` |
