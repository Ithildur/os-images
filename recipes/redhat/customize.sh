#!/bin/sh
set -eu

dnf install --assumeyes --setopt=install_weak_deps=False \
  cloud-init cloud-utils-growpart qemu-guest-agent openssh-server grubby

config=/var/lib/vps-manager-image/linux
# Preserve the distribution's BLS settings while applying the shared console configuration.
sed -i '/^GRUB_CMDLINE_LINUX_DEFAULT=/d; /^GRUB_CMDLINE_LINUX=/d; /^GRUB_TERMINAL=/d; /^GRUB_TERMINAL_OUTPUT=/d; /^GRUB_SERIAL_COMMAND=/d' /etc/default/grub
cat "${config}/grub.cfg" >>/etc/default/grub
# shellcheck source=recipes/linux/grub.cfg
. "${config}/grub.cfg"
grubby --update-kernel=ALL --args="${GRUB_CMDLINE_LINUX}"
grub2-mkconfig -o /boot/grub2/grub.cfg

systemctl enable cloud-init-local.service cloud-init.service cloud-config.service cloud-final.service
systemctl enable sshd.service
rpm -qa --queryformat '%{NAME}.%{ARCH}\t%{VERSION}-%{RELEASE}\n' >/var/lib/vps-manager-image/packages.tsv
sort -o /var/lib/vps-manager-image/packages.tsv /var/lib/vps-manager-image/packages.tsv

sh "${config}/configure.sh"
restorecon -RF /etc/ssh /etc/cloud /etc/default /boot
dnf clean all
