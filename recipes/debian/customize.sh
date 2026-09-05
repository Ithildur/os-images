#!/bin/sh
set -eu

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install --yes --no-install-recommends cloud-init cloud-guest-utils qemu-guest-agent openssh-server

config=/var/lib/vps-manager-image/linux
install -D -m 0644 "${config}/grub.cfg" /etc/default/grub.d/90-vps-manager.cfg
update-grub

install -d -m 0755 /var/lib/vps-manager-image
dpkg-query -W -f='${binary:Package}\t${Version}\n' >/var/lib/vps-manager-image/packages.tsv

rm -f /etc/network/interfaces.d/50-cloud-init /etc/netplan/50-cloud-init.yaml
sh "${config}/configure.sh"
apt-get clean
rm -rf /var/lib/apt/lists/*
