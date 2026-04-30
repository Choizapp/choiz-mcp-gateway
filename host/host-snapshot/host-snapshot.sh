#!/bin/bash
# Capture host state every 60s via systemd timer. Logs persist across reboots
# so we can post-mortem the recurring zombie pattern (tunnel/SSM hangs) once
# the auto-reboot has already wiped ephemeral state.
#
# Logs to /var/log/host-snapshot/<UTC-date>.log, rotated 7 days.
# At ~250 bytes/line × 1440 ticks/day ≈ 360 KB/day. Trivial disk impact.

set -u

LOGDIR=/var/log/host-snapshot
LOG="$LOGDIR/$(date -u +%Y-%m-%d).log"
mkdir -p "$LOGDIR"

{
  echo "=== $(date -u +%H:%M:%SZ) ==="
  echo "uptime: $(uptime)"
  echo "memory: $(free -m | awk '/Mem:/ {printf "total=%s used=%s free=%s avail=%s buff=%s\n", $2,$3,$4,$7,$6}')"
  echo "swap: $(free -m | awk '/Swap:/ {printf "total=%s used=%s\n", $2,$3}')"
  echo "tcp_est: $(ss -tn state established 2>/dev/null | tail -n +2 | wc -l)"
  echo "tunnel_http: $(curl -s -o /dev/null -w '%{http_code}' -m 5 https://tunnel.choiz.com.mx/healthz 2>/dev/null || echo 'curl_failed')"
  echo "cloudflared: $(systemctl is-active cloudflared 2>/dev/null)"
  echo "docker: $(systemctl is-active docker 2>/dev/null)"
  echo "ssm_agent: $(systemctl is-active amazon-ssm-agent 2>/dev/null)"
  echo "containers_running: $(docker ps -q 2>/dev/null | wc -l)"
  echo "top_rss:"
  ps -eo rss,comm --sort=-rss --no-headers 2>/dev/null | head -8 | awk '{printf "  %s KB %s\n", $1, $2}'
  echo
} >> "$LOG" 2>&1

# Rotate: keep last 7 days.
find "$LOGDIR" -name "*.log" -mtime +7 -delete 2>/dev/null
