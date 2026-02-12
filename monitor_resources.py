#!/usr/bin/env python3
import time
import subprocess
import os
import csv
import shutil
from datetime import datetime

# Configuration
LOG_FILE = "/home/keith-merrill/Dev/qwen-server/server_stats.csv"
DB_PATH = "/home/keith-merrill/Dev/qwen-server/qwen_memory_db"
POWERCAP_PATH = "/sys/class/powercap/intel-rapl:0/energy_uj"
INTERVAL = 5.0
MAX_LOG_SIZE_MB = 10  # Rotate when log exceeds this size
MAX_ROTATED_FILES = 3  # Keep this many rotated files

def get_gpu_power():
    try:
        result = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits"],
            encoding="utf-8"
        )
        return float(result.strip())
    except Exception:
        return 0.0

def get_cpu_energy():
    try:
        with open(POWERCAP_PATH, "r") as f:
            return int(f.read().strip())
    except Exception:
        return 0

def get_db_size():
    try:
        result = subprocess.check_output(["du", "-sb", DB_PATH], encoding="utf-8")
        return int(result.split()[0])
    except Exception:
        return 0

def init_log():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "gpu_watts", "cpu_watts", "rag_db_bytes"])

def rotate_log():
    """Rotate log file when it exceeds MAX_LOG_SIZE_MB."""
    try:
        if not os.path.exists(LOG_FILE):
            return
        size_mb = os.path.getsize(LOG_FILE) / (1024 * 1024)
        if size_mb < MAX_LOG_SIZE_MB:
            return

        # Shift existing rotated files
        for i in range(MAX_ROTATED_FILES - 1, 0, -1):
            src = f"{LOG_FILE}.{i}"
            dst = f"{LOG_FILE}.{i + 1}"
            if os.path.exists(src):
                if i + 1 > MAX_ROTATED_FILES:
                    os.remove(src)
                else:
                    shutil.move(src, dst)

        # Rotate current file
        shutil.move(LOG_FILE, f"{LOG_FILE}.1")

        # Create fresh log with header
        init_log()
        print(f"Log rotated ({size_mb:.1f}MB). Kept {MAX_ROTATED_FILES} backups.")
    except Exception as e:
        print(f"Log rotation failed: {e}")

def main():
    init_log()
    print(f"Monitoring started. Logging to {LOG_FILE}")

    last_energy = get_cpu_energy()
    last_time = time.time()
    rotation_check_counter = 0

    # Initial sleep to establish delta
    time.sleep(INTERVAL)

    while True:
        current_time = time.time()
        current_energy = get_cpu_energy()

        # Calculate CPU Power (Watts = Joules / Seconds)
        # energy_uj is in microjoules, so divide by 1,000,000 for Joules
        time_delta = current_time - last_time
        energy_delta = (current_energy - last_energy) / 1_000_000
        cpu_watts = energy_delta / time_delta if time_delta > 0 else 0

        gpu_watts = get_gpu_power()
        db_size = get_db_size()
        timestamp = datetime.now().isoformat()

        with open(LOG_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([timestamp, f"{gpu_watts:.2f}", f"{cpu_watts:.2f}", db_size])

        last_energy = current_energy
        last_time = current_time

        # Check rotation every ~5 minutes (60 iterations at 5s interval)
        rotation_check_counter += 1
        if rotation_check_counter >= 60:
            rotation_check_counter = 0
            rotate_log()

        # Sleep for the remaining time of the interval to keep logging regular
        elapsed = time.time() - current_time
        sleep_time = max(0, INTERVAL - elapsed)
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
