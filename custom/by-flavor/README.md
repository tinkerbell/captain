# custom/by-flavor/

Each `<flavor-id>/` here is shaped like `custom/` (`files/`, `packages.txt`, `mkosi-postinst/`,
`mkosi-finalize/`) and applies only to that flavor, after `custom/` and the other `custom/by-*/` dirs.
See "Customization via `custom/`" in the top-level `README.md`.

To give two flavors the same customizations, symlink one to the other:

    mkdir -p custom/by-flavor/trixie-meson64/mkosi-postinst
    ln -s trixie-meson64 custom/by-flavor/trixie-genio
