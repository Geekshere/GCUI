#!/bin/bash

# Navigate to the project root
cd ~/mission_control

# Add everything (Code, HTML, and the new Subfolders in IMAGE/)
git add .

# Commit with a timestamp
git commit -m "Station Update: $(date +'%Y-%m-%d %H:%M:%S')"

# Final Push
git push origin main

echo "🚀 Mission Control synced to GitHub successfully."
