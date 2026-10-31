"""Role traits: what the machine does once booted."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from captain.traits import Axis, Trait

if TYPE_CHECKING:
    from captain.flavor import Flavor


class Role(Trait):
    axis = Axis.ROLE
    # What the machine is, for humans: banner, motd, /etc/issue and /etc/os-release VARIANT=.
    title: ClassVar[str]
    # motd lines pointing at the role's logs and tools.
    hints: ClassVar[tuple[str, ...]] = ()


class Tink(Role):
    """Tinkerbell provisioning: tink-agent pulled and run via containerd."""

    content = "role/tink"
    title = "Tinkerbell provisioning environment"
    hints = (
        "Use `journalctl -u tink-agent` command to access the tink worker/agent logs.",
        "Use `nerdctl` commands to access workflow action containers.",
    )

    def packages(self, flavor: Flavor) -> set[str]:
        # tink-agent-setup unpacks the image with tar (-z for OCI blobs);
        # Essential on Debian, not on Rocky.
        return {"tar", "gzip"}


class Kubernetes(Role):
    """Kubernetes node: kubeadm/kubelet/kubectl from pkgs.k8s.io, containerd (CRI), cloud-init."""

    content = "role/k8s"
    title = "Kubernetes node"
    hints = (
        "Use `journalctl -u kubelet` to access the kubelet logs.",
        "Use `crictl ps` / `crictl logs` to access pod containers.",
        "On control planes, use `kubectl --kubeconfig /etc/kubernetes/admin.conf`.",
    )

    @property
    def name(self) -> str:
        return "k8s"

    def packages(self, flavor: Flavor) -> set[str]:
        return {
            "kubelet",
            "kubeadm",
            "kubectl",
            "cri-tools",
            "cloud-init",
            # cloud-init runs lsb_release on deb distros; resolute's cloud-init doesn't pull it in
            "lsb-release",
            # kubeadm preflight requirements
            "conntrack",
            "socat",
            "ebtables",
            "ethtool",
        }


class KubernetesCapt(Kubernetes):
    """Kubernetes node for Cluster API Provider Tinkerbell: cloud-init reads Tootles (EC2-style)."""

    content = "role/k8s-capt"
    title = "Kubernetes node (Cluster API Provider Tinkerbell)"
    hints = (
        "Use `cloud-init status --long` / `journalctl -u cloud-init` for the Tootles bootstrap.",
        *Kubernetes.hints,
    )

    @property
    def name(self) -> str:
        return "k8s-capt"
