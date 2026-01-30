#!/usr/bin/env python3
import time
import subprocess
import os
import csv
from datetime import datetime

# Configuration
LOG_FILE = "/home/keith-merrill/Dev/qwen-server/server_stats.csv"
DB_PATH = "/home/keith-merrill/Dev/qwen-server/qwen_memory_db"
POWERCAP_PATH = "/sys/class/powercap/intel-rapl:0/energy_uj"
INTERVAL = 5.0

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
        # du -sb returns size in bytes
        result = subprocess.check_output(["du", "-sb", DB_PATH], encoding="utf-8")
        return int(result.split()[0])
    except Exception:
        return 0

def init_log():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "gpu_watts", "cpu_watts", "rag_db_bytes"])

def main():
    init_log()
    print(f"Monitoring started. Logging to {LOG_FILE}")
    
    last_energy = get_cpu_energy()
    last_time = time.time()
    
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
        
        # Sleep for the remaining time of the interval to keep logging regular
        elapsed = time.time() - current_time
        sleep_time = max(0, INTERVAL - elapsed)
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
