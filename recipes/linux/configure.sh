#!/bin/sh
set -eu

config=/var/lib/vps-manager-image/linux
install -D -m 0644 "${config}/cloud.cfg" /etc/cloud/cloud.cfg.d/90-vps-manager.cfg
# OpenSSH uses the first value; this must precede cloud-image defaults.
install -D -m 0644 "${config}/sshd.conf" /etc/ssh/sshd_config.d/00-vps-manager.conf

systemctl enable qemu-guest-agent.service
systemctl enable serial-getty@ttyS0.service

rm -f /etc/cloud/cloud-init.disabled
cloud-init clean --logs
rm -rf /var/lib/cloud/*
: >/etc/machine-id
rm -f /var/lib/dbus/machine-id
rm -f /etc/ssh/ssh_host_* /var/lib/systemd/random-seed
find /tmp /var/tmp -mindepth 1 -delete
