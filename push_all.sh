#!/bin/bash

# Navigate to the project root
cd ~/mission_control

# Add everything (Code, HTML, and the new Subfolders in IMAGE/)
git add .

# Commit with a timestamp
git commit -m "Station Update: $(date +'%Y-%m-%d %H:%M:%S')"

# Set the authenticated URL using your new PAT
git remote set-url origin https://Geekshere:ghp_2X1k70MiЕZdtntR7qymQDmkpFQ7K0F3S1kQJ@github.com/Geekshere/SatDump-WebGUI.git

# Final Push
git push origin main

echo "🚀 Mission Control synced to GitHub successfully."
