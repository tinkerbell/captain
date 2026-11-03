log warn "Example custom post-install script (custom/mkosi-postinst/example-postinst.sh) running in mkosi post-install phase. This runs after packages are installed."
# Runs *outside* the image chroot after packages are installed.
# The target filesystem is at /buildroot; use 'mkosi-chroot' to run commands inside the chroot environment.

declare -i ENABLE_EXAMPLE_CONTAINERD_MIRRORS=0 # flip to 1 to enable
declare -i ENABLE_EXAMPLE_CA_CERTS=0           # flip to 1 to enable

### Example: configure containerd registry mirrors for a few common registries.
# Each target registry is a directory under /etc/containerd/certs.d with a hosts.toml file.
# You could also do this with individual files and directories in custom/files/ dir.

if [[ ${ENABLE_EXAMPLE_CONTAINERD_MIRRORS} -gt 0 ]]; then

    declare -A registry_mirrors=(
        ["docker.io"]="http://harbor.app.192.168.66.171.nip.io/v2/docker.io"
        ["quay.io"]="http://harbor.app.192.168.66.171.nip.io/v2/quay.io"
        ["ghcr.io"]="http://harbor.app.192.168.66.171.nip.io/v2/ghcr.io"
        ["registry.k8s.io"]="http://harbor.app.192.168.66.171.nip.io/v2/registry.k8s.io"
    )

    log info "Configuring containerd registry mirrors..."
    mkdir -pv "${BUILDROOT}/etc/containerd/certs.d"

    for registry in "${!registry_mirrors[@]}"; do
        mirror="${registry_mirrors[$registry]}"
        log info "Configuring mirror for registry '$registry' pointing to target '$mirror'"
        mkdir -pv "${BUILDROOT}/etc/containerd/certs.d/${registry}"

        cat <<- ONE_HOSTS_TOML > "${BUILDROOT}/etc/containerd/certs.d/${registry}/hosts.toml"
server = "${mirror}"
[host."${mirror}"]
capabilities = ["pull", "resolve"]
override_path = true
ONE_HOSTS_TOML
    done

    log info "Containerd registry mirrors configuration complete."
fi

### Example: add a custom root CA certificate to the image's trust store.
# You could also do this by dropping .crt files in custom/files/usr/local/share/ca-certificates
if [[ ${ENABLE_EXAMPLE_CA_CERTS} -gt 0 ]]; then

    log info "Adding custom root CA certificate to image trust store..."

    # Debian-family and RPM-family distros keep local CAs in different places, with different tools.
    if [[ "${CAPTAIN_DISTRO_FAMILY}" == "rpm" ]]; then
        ca_dir="${BUILDROOT}/etc/pki/ca-trust/source/anchors"
    else
        ca_dir="${BUILDROOT}/usr/local/share/ca-certificates"
    fi
    mkdir -pv "${ca_dir}"

    curl https://letsencrypt.org/certs/staging/letsencrypt-stg-root-x1.pem \
        -o "${ca_dir}/letsencrypt-stg-root-x1.crt"

    curl https://letsencrypt.org/certs/staging/letsencrypt-stg-root-x2.pem \
        -o "${ca_dir}/letsencrypt-stg-root-x2.crt"

    # update the CA certificate bundle to include the new certs.
    # this is not needed when using .crt files in the custom/files/ directory, since the regular
    # (non-custom) postinst already does this.
    if [[ "${CAPTAIN_DISTRO_FAMILY}" == "rpm" ]]; then
        mkosi-chroot update-ca-trust extract
    else
        mkosi-chroot update-ca-certificates --fresh --verbose
    fi

    log info "Custom root CA certificate added and CA bundle updated."
fi
