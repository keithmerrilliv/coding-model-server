#!/usr/bin/env bash
# Repair an NVIDIA driver/library version mismatch without a reboot.
#
# Symptom:  nvidia-smi prints
#             Failed to initialize NVML: Driver/library version mismatch
# Cause:    a driver package upgrade replaced the USERSPACE libraries while the
#           OLD kernel module stayed resident. Processes holding a CUDA context
#           from before the upgrade keep working; anything NEW fails.
#
# On zooshly 2026-09-16: loaded NVRM 595.84, installed libnvidia-ml 595.91.07,
# from nvidia-driver-595-open installed at 01:26:54.
#
# This unloads and reloads the kernel modules. Everything using the GPU must
# stop first, which this script does for the coding-model services.
#
# Run as:   sudo bash scripts/fix_nvidia_mismatch.sh
set -euo pipefail

echo "==> before"
cat /proc/driver/nvidia/version 2>/dev/null | head -1 || echo "   module not loaded"
ls -1 /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.*.* 2>/dev/null | tail -1

echo "==> stopping GPU consumers"
systemctl stop coding-model-orchestrator coding-model-server 2>/dev/null || true
pkill -f "tools/llama-server" 2>/dev/null || true
sleep 3

echo "==> checking for a display server on the GPU"
if lsof /dev/nvidia* 2>/dev/null | grep -qE "^(Xorg|gnome-shell|Hyprland|sway|wayland)"; then
    echo
    echo "    Xorg (or another display server) holds /dev/nvidia0."
    echo "    The modules CANNOT be unloaded while it runs, so this script will not"
    echo "    help from inside a graphical session."
    echo
    echo "    Two options:"
    echo "      1. REBOOT — the reliable fix:   sudo reboot"
    echo "      2. Drop to a console first:     sudo systemctl isolate multi-user.target"
    echo "         then re-run this script, then: sudo systemctl isolate graphical.target"
    echo
    read -r -p "    Continue anyway and try the unload? [y/N] " ans
    [[ "${ans:-N}" =~ ^[Yy]$ ]] || { echo "    Aborted. Reboot is the fastest path."; exit 1; }
fi

echo "==> processes still holding the GPU (must be empty to proceed)"
if command -v fuser >/dev/null && fuser -v /dev/nvidia* 2>&1 | grep -q .; then
    fuser -v /dev/nvidia* 2>&1 || true
fi

echo "==> unloading modules"
for m in nvidia_uvm nvidia_drm nvidia_modeset nvidia; do
    modprobe -r "$m" 2>/dev/null && echo "   removed $m" || echo "   $m not loaded or busy"
done

echo "==> reloading"
modprobe nvidia
modprobe nvidia_uvm 2>/dev/null || true

echo "==> after"
if nvidia-smi --query-gpu=name,driver_version,memory.free --format=csv,noheader; then
    echo "==> FIXED. Restarting services."
    systemctl start coding-model-server coding-model-orchestrator
    echo "   give the server ~20s, then: curl -s localhost:5000/health"
else
    echo "==> STILL BROKEN — a process is likely still holding /dev/nvidia*."
    echo "    A reboot is the reliable fallback:  sudo reboot"
    exit 1
fi
