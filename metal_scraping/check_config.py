#!/usr/bin/env python3
"""
MCP Tools Availability Checker

This script checks if the required MCP tools are available in the environment
and provides guidance on how to configure them for the Metal documentation scraper.
"""

import subprocess
import sys
import os

def check_command_availability(command):
    """Check if a command is available in the system"""
    try:
        result = subprocess.run(['which', command], capture_output=True, text=True)
        return result.returncode == 0
    except Exception:
        return False

def check_mcp_tools():
    """Check for availability of MCP tools"""
    print("Checking for MCP tools availability...")
    print("="*50)
    
    tools_to_check = [
        ('cupertino', 'Open-source Cupertino tool for Apple documentation access'),
        ('apple-deep-docs', 'Open-source Apple Deep Docs search tool')
    ]
    
    available_tools = []
    missing_tools = []
    
    for tool, description in tools_to_check:
        is_available = check_command_availability(tool)
        status = "✓ Available" if is_available else "✗ Missing"
        print(f"{tool:<25} - {description:<40} [{status}]")
        
        if is_available:
            available_tools.append(tool)
        else:
            missing_tools.append(tool)
    
    print("\n" + "="*50)
    print(f"Summary: {len(available_tools)} tools available, {len(missing_tools)} missing")
    
    if available_tools:
        print("\nAvailable tools can be used directly in the scraper scripts.")
    
    if missing_tools:
        print(f"\nMissing tools: {', '.join(missing_tools)}")
        print("\nTo install these open-source tools:")
        print("  - Cupertino: https://github.com/mihaelamj/cupertino")
        print("  - Apple Deep Docs: https://github.com/Ahrentlov/appledeepdoc-mcp")
        print("  Follow the installation instructions in each repository.")

    return available_tools, missing_tools

def check_python_dependencies():
    """Check for required Python dependencies"""
    print("\nChecking Python dependencies...")
    print("-" * 30)
    
    required_packages = ['requests', 'beautifulsoup4']
    missing_packages = []
    
    for package in required_packages:
        try:
            __import__(package)
            print(f"{package:<20} - ✓ Available")
        except ImportError:
            print(f"{package:<20} - ✗ Missing")
            missing_packages.append(package)
    
    if missing_packages:
        print(f"\nInstall missing packages with: pip install {' '.join(missing_packages)}")
    
    return len(missing_packages) == 0

def main():
    print("APPLE METAL DOCUMENTATION SCRAPER - CONFIGURATION CHECKER")
    print("="*60)
    print("This tool checks if your environment is properly configured")
    print("to run the Metal documentation scraper with MCP tools.\n")
    
    available_tools, missing_tools = check_mcp_tools()
    deps_ok = check_python_dependencies()
    
    print("\n" + "="*60)
    print("RECOMMENDATIONS:")
    
    if not missing_tools and deps_ok:
        print("✓ Your environment appears to be properly configured!")
        print("  You should be able to run the Metal documentation scraper.")
    else:
        print("✗ Some configuration is needed before running the scraper:")
        
        if missing_tools:
            print(f"  - Install open-source tools: {', '.join(missing_tools)}")
            print("  - See README.md for installation instructions")

        if not deps_ok:
            print("  - Install required Python packages with: pip install -r requirements.txt")

        print("\nSee README.md for detailed setup instructions.")

if __name__ == "__main__":
    main()