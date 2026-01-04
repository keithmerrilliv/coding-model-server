
import sys
import os
import re
import shlex
import subprocess
import threading
from collections import deque
from datetime import datetime
import uuid

# Mock the globals/imports from qwen_remote.py
COLORS = {
    "HEADER": "", "BLUE": "", "GREEN": "", "WARNING": "", "FAIL": "", "ENDC": "", "BOLD": "", "CYAN": ""
}
ALLOW_SHELL_MODE = False
COMMAND_WHITELIST = None

# Mock JobTracker
class JobTracker:
    def __init__(self):
        self.jobs = {}
        self.lock = threading.Lock()
    def create_job(self, command):
        return "job-123"
    def update_job(self, *args, **kwargs): pass
    def add_output(self, *args, **kwargs): pass

job_tracker = JobTracker()

def print_colored(text, color):
    print(text)

def parse_command_safely(command):
    if not ALLOW_SHELL_MODE:
        dangerous_chars = ['|', '&', ';', '$', '`', '\n', '>', '<', '(', ')']
        if any(char in command for char in dangerous_chars):
            raise ValueError("Dangerous char")
    return shlex.split(command)

def expand_paths_in_args(args):
    return args

def is_command_allowed(args):
    return True, "Allowed"

# Copied from qwen_remote.py (simplified)
def execute_remote_command(command, async_mode=False):
    print(f"Executing: {command}")
    try:
        if not ALLOW_SHELL_MODE:
            command_args = parse_command_safely(command)
            # allowed, msg = is_command_allowed(command_args) # skipped
    except ValueError as e:
        print(f"Validation error: {e}")
        return
    
    # Mocking user input - force yes for test
    # choice = input(...) 
    choice = 'y'
    
    if async_mode:
        print("Async mode started")
    else:
        try:
            # We don't actually run it to avoid side effects, just parse
            print("Sync mode executed")
        except Exception as e:
            print(f"Exec error: {e}")

def process_remote_commands(response_text):
    commands = [
        (r'<<<REMOTE_EXEC_ASYNC>>>\s*(.*?)\s*<<<REMOTE_EXEC_ASYNC>>>',
         lambda cmd: execute_remote_command(cmd.strip(), async_mode=True),
         True),
        (r'<<<REMOTE_EXEC>>>\s*(.*?)\s*<<<REMOTE_EXEC>>>',
         lambda cmd: execute_remote_command(cmd.strip(), async_mode=False),
         True),
    ]

    for pattern, handler, has_capture in commands:
        match = re.search(pattern, response_text, re.DOTALL)
        if match:
            arg = match.group(1) if has_capture else None
            return handler(arg)
    return None

# Test Cases
test_cases = [
    # Case 1: Standard command
    "<<<REMOTE_EXEC>>>ls -la<<<REMOTE_EXEC>>>",
    # Case 2: Newlines
    "<<<REMOTE_EXEC>>>\nls -la\n<<<REMOTE_EXEC>>>",
    # Case 3: Empty
    "<<<REMOTE_EXEC>>><<<REMOTE_EXEC>>>",
    # Case 4: Malformed quotes (should catch ValueError)
    '''<<<REMOTE_EXEC>>>echo "hello<<<REMOTE_EXEC>>>''',
    # Case 5: Shell chars (should catch ValueError)
    "<<<REMOTE_EXEC>>>ls | grep py<<<REMOTE_EXEC>>>",
    # Case 6: Multiple tags
    "<<<REMOTE_EXEC>>>ls<<<REMOTE_EXEC>>> <<<REMOTE_EXEC>>>pwd<<<REMOTE_EXEC>>>",
    # Case 7: Backslashes
    r"<<<REMOTE_EXEC>>>echo \n<<<REMOTE_EXEC>>>",
    # Case 8: Nested tags (regex is non-greedy, so inner might break it?)
    "<<<REMOTE_EXEC>>>echo '<<<REMOTE_EXEC>>>'<<<REMOTE_EXEC>>>" 
]

for i, case in enumerate(test_cases):
    print(f"\n--- Test Case {i+1} ---")
    print(f"Input: {case!r}")
    try:
        process_remote_commands(case)
    except Exception as e:
        print(f"CRASHED: {e}")
        import traceback
        traceback.print_exc()
