import os, sys
from PIL import Image

CLOUD_DIR = os.path.expanduser("~/iCloud_View")
print("Starting the hunt... If this freezes, the last file on screen is the bad one!\n")

for root, dirs, files in os.walk(CLOUD_DIR):
    for f in files:
        if f.lower().endswith(('.png', '.jpg', '.jpeg')):
            path = os.path.join(root, f)
            print(f"Checking: {f}", end=" ... ")
            sys.stdout.flush() # Forces the name to print BEFORE it opens the file
            
            try:
                with Image.open(path) as img:
                    img.load() # Forces Python to read the actual pixels
                print("OK")
            except Exception as e:
                print("CORRUPTED!")
