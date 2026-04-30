# host-snapshot

Passive on-host diagnostic capture. A systemd timer runs every 60 s and appends
a snapshot of host state to `/var/log/host-snapshot/<UTC-date>.log`. Logs
persist across reboots (they're on the EBS root) so we can post-mortem the
recurring zombie pattern even after the auto-reboot has wiped ephemeral state.

## Why this exists

`monitor-tunnel.yml` and `monitor-ssm.yml` use SSM to capture diagnostics
**when a zombie is detected from outside**. Both are useless when the failure
mode is "SSM agent dead" — they have nothing to call. A systemd timer running
*on the host itself* doesn't depend on SSM, so it captures the minute-by-minute
state of the system **before** and **during** a zombie event up to the moment
of the kernel panic / hang.

After the next zombie event:
1. Note the time the tunnel went non-200 (from the monitor-tunnel run log).
2. SSM into the recovered host and `tail` `/var/log/host-snapshot/<that-date>.log`.
3. Look at the lines for the 5–10 minutes before the failure: memory, TCP fanout,
   load, top RSS, cloudflared status. The pattern that precedes the hang is the
   root cause.

## What's captured per tick

- `uptime` (load averages)
- memory: total, used, free, available, buff/cache
- swap usage
- TCP established connection count (canary for connection storms)
- HTTP code from `tunnel.choiz.com.mx/healthz` from the host itself
- systemctl status of cloudflared, docker, amazon-ssm-agent
- count of running containers
- top 8 processes by RSS

~250 bytes/tick × 1440 ticks/day ≈ 360 KB/day. Rotated weekly.

## Install / re-install

Run with sudo on the EC2:

```sh
cd /home/ssm-user/choiz-mcp-gateway/host/host-snapshot
sudo ./install.sh
```

Or via SSM from the run-ssm.yml workflow:

```sh
gh workflow run "Ad-hoc SSM command" -F command='curl -fsSL https://raw.githubusercontent.com/Choizapp/choiz-mcp-gateway/master/host/host-snapshot/install.sh -o /tmp/install.sh && curl -fsSL https://raw.githubusercontent.com/Choizapp/choiz-mcp-gateway/master/host/host-snapshot/host-snapshot.sh -o /tmp/host-snapshot.sh && curl -fsSL https://raw.githubusercontent.com/Choizapp/choiz-mcp-gateway/master/host/host-snapshot/host-snapshot.service -o /tmp/host-snapshot.service && curl -fsSL https://raw.githubusercontent.com/Choizapp/choiz-mcp-gateway/master/host/host-snapshot/host-snapshot.timer -o /tmp/host-snapshot.timer && cd /tmp && bash install.sh'
```

(easier: clone the repo on the EC2 and run the local copy — the deploy already
keeps the repo at `/home/ssm-user/choiz-mcp-gateway`.)

## Disable

```sh
sudo systemctl disable --now host-snapshot.timer
```

Logs in `/var/log/host-snapshot/` remain — delete manually if not wanted.
