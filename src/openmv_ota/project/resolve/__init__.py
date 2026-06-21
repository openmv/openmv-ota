"""Read-only resolvers that turn a firmware checkout + SDK into snapshot values.

Every resolver reads files (or, via :mod:`openmv_ota.project.gitrepo`, runs
``git`` porcelain). No toolchain binary is ever executed.
"""
