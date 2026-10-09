#!/bin/bash
# First-boot setup for the Lightsail instance (Ubuntu 24.04). Runs once as root.
set -eux
export DEBIAN_FRONTEND=noninteractive

# swap as a safety net: nightly scan + Java IB Gateway on 4 GB RAM
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

apt-get update
apt-get install -y docker.io docker-compose-v2 rsync unattended-upgrades
systemctl enable --now docker
usermod -aG docker ubuntu

# automatic security updates
dpkg-reconfigure -f noninteractive unattended-upgrades || true

mkdir -p /opt/sepa/app /opt/sepa/data /opt/sepa/site /opt/sepa/secrets /opt/sepa/ib_settings \
         /opt/sepa/caddy_data /opt/sepa/caddy_config
chown -R ubuntu:ubuntu /opt/sepa
chmod 700 /opt/sepa/secrets

touch /opt/sepa/.cloud-init-done
