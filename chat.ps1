# Chat with the concierge in one command: for an SSH session from the phone (over Tailscale), or any window.
# The servers must already be running on this PC (.\start-concierge.ps1).
# From the phone's SSH app, type:
#   powershell -ExecutionPolicy Bypass -File "D:\Gaurav Balyan\AI Agents\Watch Concierge\chat.ps1"
Set-Location $PSScriptRoot
conda activate watch-concierge
python -m common.login --status
python -m concierge.graph --chat
