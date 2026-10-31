#!/bin/bash

set -euo pipefail

# logger utility, output ANSI-colored messages to stderr; first argument is level (debug/info/warn/error), all other arguments are the message.
declare -A log_colors=(["debug"]="0;36" ["info"]="0;32" ["notice"]="1;32" ["warn"]="1;33" ["warning"]="1;33" ["error"]="1;31")
declare -A log_emoji=(["debug"]="🐛" ["info"]="🌿" ["notice"]="🌱" ["warn"]="🚸" ["warning"]="🚸" ["error"]="🚨")
function log() {
	declare level="${1}"
	shift
	[[ "${level}" == "debug" && "${DEBUG}" != "yes" ]] && return # Skip debugs unless DEBUG=yes is set in the environment
	# Normal output
	declare color="\033[${log_colors[${level}]}m"
	declare emoji="${log_emoji[${level}]}"
	declare ansi_reset="\033[0m"
	level=$(printf "%-5s" "${level}") # pad to 5 characters before printing
	echo -e "${emoji} ${ansi_reset}[${color}${level}${ansi_reset}] ${color}${*}${ansi_reset}" >&2
}


# Facts about the flavor being built, for scripts (including custom/ ones) to branch on.
declare -r CAPTAIN_FLAVOR_ID="{{ flavor.id }}"
declare -r CAPTAIN_DISTRO="{{ flavor.distro.mkosi_distribution }}"
declare -r CAPTAIN_DISTRO_RELEASE="{{ flavor.distro.release }}"
declare -r CAPTAIN_DISTRO_FAMILY="{{ flavor.distro.family }}" # deb or rpm
declare -r CAPTAIN_ARCH="{{ cfg.arch_info.arch }}" # amd64 or arm64
declare -r CAPTAIN_ROLE="{{ flavor.role.name }}" # eg. tink, k8s-capt
declare -r CAPTAIN_OUTPUT="{{ flavor.output.name }}" # mem or img
declare -r CAPTAIN_PLATFORM="{{ flavor.platform.custom_names() | join(' ') }}" # eg. "acpi", "armbian rpi"
declare -r CAPTAIN_KERNEL="{{ flavor.platform.kernel_names() | join(' ') }}" # eg. distro, captainos, armbian-edge-genio
