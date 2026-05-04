#!/bin/bash
set -euo pipefail

echo "=========================================================="
echo "🔥 Provisioning Kernell OS Crucible Node (Bare Metal/KVM)"
echo "=========================================================="

if [[ "$EUID" -ne 0 ]]; then
  echo "Please run as root (sudo)"
  exit 1
fi

echo "[1/6] Validating KVM Support..."
if [ ! -e /dev/kvm ]; then
    echo "ERROR: /dev/kvm not found! Ensure this instance is bare-metal or supports nested virtualization."
    exit 1
fi
echo "✅ KVM Support detected."

echo "[2/6] System Tuning (FDs & Limits)..."
# Increase file descriptor limits for massive socket/VM concurrency
cat <<EOF >> /etc/security/limits.conf
* soft nofile 1000000
* hard nofile 1000000
root soft nofile 1000000
root hard nofile 1000000
EOF

# Ensure systemd limits are also raised if the service is run via systemctl
mkdir -p /etc/systemd/system/kernell.service.d
cat <<EOF > /etc/systemd/system/kernell.service.d/override.conf
[Service]
LimitNOFILE=1000000
EOF
systemctl daemon-reload || true

# Increase fs inotify and virtual memory mappings
cat <<EOF > /etc/sysctl.d/99-kernell.conf
fs.inotify.max_user_instances=8192
fs.inotify.max_user_watches=524288
vm.max_map_count=262144
# Aggressive network tuning for UDS / IPC
net.core.somaxconn=65535
net.core.netdev_max_backlog=65535
EOF
sysctl --system >/dev/null
echo "✅ Kernel limits tuned."

echo "[3/6] Installing Dependencies..."
apt-get update -y
apt-get install -y acl binutils python3 python3-pip python3-venv curl wget git
echo "✅ Dependencies installed."

echo "[4/6] Setting up Firecracker..."
FC_VERSION="v1.7.0"
ARCH="$(uname -m)"

if [ ! -f "/usr/local/bin/firecracker" ]; then
    echo "Downloading Firecracker $FC_VERSION..."
    wget -q https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-${ARCH}.tgz
    tar -xzf firecracker-${FC_VERSION}-${ARCH}.tgz
    mv release-${FC_VERSION}-${ARCH}/firecracker-${FC_VERSION}-${ARCH} /usr/local/bin/firecracker
    mv release-${FC_VERSION}-${ARCH}/jailer-${FC_VERSION}-${ARCH} /usr/local/bin/jailer
    rm -rf release-${FC_VERSION}-${ARCH} firecracker-${FC_VERSION}-${ARCH}.tgz
fi

# Set permissions so the SDK can invoke them
setfacl -m u:$SUDO_USER:rw /dev/kvm || true
chmod +x /usr/local/bin/firecracker /usr/local/bin/jailer
echo "✅ Firecracker & Jailer ready."

echo "[5/6] Creating Kernell Data Directories..."
mkdir -p /var/lib/kernell
mkdir -p /var/log/kernell
mkdir -p /tmp/fcsnapshots
chown -R $SUDO_USER:$SUDO_USER /var/lib/kernell /var/log/kernell /tmp/fcsnapshots
echo "✅ Directories created."

echo "[6/6] Installing Kernell OS SDK & Server..."
# Assuming we are running this from within the repo
cd "$(dirname "$0")/../../.." || exit
su - $SUDO_USER -c "python3 -m venv .venv"
su - $SUDO_USER -c ".venv/bin/pip install -e . fastapi uvicorn httpx prometheus_client"
echo "✅ Python environment ready."

echo "=========================================================="
echo "🚀 Crucible Node Provisioning Complete!"
echo "=========================================================="
echo "Next Steps:"
echo "1. Compile kernel & rootfs: cd kernell_sdk/runtime/firecracker && sudo ./build_rootfs.sh"
echo "2. Move vmlinux & rootfs to /var/lib/kernell/"
echo "3. Start the Control Plane: .venv/bin/python3 -m kernell_sdk.runtime.firecracker.server"
echo "4. In another terminal, run the attack: .venv/bin/python3 tests/load_tester.py --scenario burst"
echo ""
