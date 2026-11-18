#!/bin/bash
# Keeps a sandbox answering when its work needs more memory than the VM has:
# the overflow is swapped into zswap, compressed in RAM, so the work slows
# down instead of the VM thrashing or the kernel OOM-killing it. The swap
# file only backs zswap; a swap file on its own (the sandbox disk is served
# over NBD) and memory.max both measured worse: the VM hung or a process died.
set -e

cat > /usr/local/sbin/sbx-swap <<'SH'
#!/bin/sh
set -e
for c in lz4 lzo; do echo $c > /sys/module/zswap/parameters/compressor 2>/dev/null && break; done
echo 40 > /sys/module/zswap/parameters/max_pool_percent
echo 1 > /sys/module/zswap/parameters/enabled
# fallocate takes no disk until a page is written out of the zswap pool;
# a recipe's resources.swap_mb resizes it (sbx_guest.py swap).
[ -f /swapfile ] || { fallocate -l 8G /swapfile; chmod 600 /swapfile; mkswap -q /swapfile; }
swapon --show=NAME --noheadings | grep -qx /swapfile || swapon /swapfile
sysctl -q vm.swappiness=100
SH

# SSH sessions (the agents) join envd's /user cgroup, next to the app envd
# starts, so all the work is one group apart from envd and sshd.
cat > /usr/local/sbin/sbx-session-cgroup <<'SH'
#!/bin/sh
echo "$PPID" > /sys/fs/cgroup/user/cgroup.procs
SH
chmod 755 /usr/local/sbin/sbx-swap /usr/local/sbin/sbx-session-cgroup

grep -q sbx-session-cgroup /etc/pam.d/sshd \
  || echo 'session optional pam_exec.so quiet /usr/local/sbin/sbx-session-cgroup' >> /etc/pam.d/sshd

cat > /etc/systemd/system/sbx-swap.service <<'UNIT'
[Unit]
Description=zswap and its backing swap file

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/sbx-swap

[Install]
WantedBy=multi-user.target
UNIT

# envd and sshd keep their memory and get the CPU first while the work
# saturates it.
mkdir -p /etc/systemd/system/system.slice.d /etc/systemd/system/ssh.service.d
printf '[Slice]\nMemoryMin=384M\nCPUWeight=1000\n' > /etc/systemd/system/system.slice.d/protect.conf
printf '[Service]\nCPUWeight=1000\n' > /etc/systemd/system/ssh.service.d/cpu.conf

# A runtime's own image may bring a smaller /swapfile, which sbx-swap would
# keep: replace it, so every runtime starts with the same 8 GiB.
if [ -f /swapfile ] && [ "$(wc -c < /swapfile)" -lt 8589934592 ]; then
  swapoff /swapfile 2>/dev/null || true
  rm -f /swapfile
fi

systemctl daemon-reload
systemctl enable -q --now sbx-swap.service
