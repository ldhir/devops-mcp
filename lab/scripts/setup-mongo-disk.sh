#!/usr/bin/env bash
# Gives MongoDB its own 2 GB disk (a file mounted as a filesystem), so a
# "disk full" failure stays inside it and never touches the machine's real disk.
set -euo pipefail
if mountpoint -q /mnt/mongo-data; then echo "already mounted"; exit 0; fi
sudo mkdir -p /mnt/mongo-data /var/lib/lab
[ -f /var/lib/lab/mongo-disk.img ] || { sudo fallocate -l 2G /var/lib/lab/mongo-disk.img; sudo mkfs.ext4 -q /var/lib/lab/mongo-disk.img; }
sudo mount -o loop /var/lib/lab/mongo-disk.img /mnt/mongo-data
grep -q mongo-disk.img /etc/fstab || echo "/var/lib/lab/mongo-disk.img /mnt/mongo-data ext4 loop 0 0" | sudo tee -a /etc/fstab >/dev/null
df -h /mnt/mongo-data | tail -1
