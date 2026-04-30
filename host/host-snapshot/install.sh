#!/bin/bash
# Install host-snapshot timer on the gateway EC2.
#
# Idempotent. Run with sudo. Safe to re-run (overwrites files, restarts timer).
# Pulls the unit + script from the same directory this install.sh lives in.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

install -m 0755 "$HERE/host-snapshot.sh" /usr/local/bin/host-snapshot.sh
install -m 0644 "$HERE/host-snapshot.service" /etc/systemd/system/host-snapshot.service
install -m 0644 "$HERE/host-snapshot.timer" /etc/systemd/system/host-snapshot.timer

systemctl daemon-reload
systemctl enable --now host-snapshot.timer

# Run once immediately to confirm the script works.
/usr/local/bin/host-snapshot.sh

echo
echo "=== timer status ==="
systemctl status host-snapshot.timer --no-pager | head -12
echo
echo "=== first snapshot ==="
tail -15 "/var/log/host-snapshot/$(date -u +%Y-%m-%d).log"
