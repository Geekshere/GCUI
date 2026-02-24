import os
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

def make_jpeg_safe(img):
    if img.mode in ('I', 'I;16', 'I;16B', 'I;16L', 'F'):
        img = img.point(lambda i: i * (1./256)).convert('L')
    if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
        bg = Image.new("RGB", img.size, (0, 0, 0))
        bg.paste(img, mask=img.convert('RGBA').split()[-1])
        return bg
    if img.mode != 'RGB':
        return img.convert('RGB')
    return img

CLOUD_DIR = os.path.expanduser("~/iCloud_View")
THUMB_DIR = os.path.expanduser("~/mission_control/thumbs")

print("Hunting for the missing thumbnail...")

for root, dirs, files in os.walk(CLOUD_DIR):
    for f in files:
        if f.lower().endswith(('.png', '.jpg', '.jpeg')):
            full_path = os.path.join(root, f)
            safe_name = os.path.relpath(full_path, CLOUD_DIR).replace("/", "_")
            thumb_path = os.path.join(THUMB_DIR, safe_name)
            
            if not os.path.exists(thumb_path):
                print(f"\nFound: {f}")
                try:
                    with Image.open(full_path) as img:
                        # THE FIX: Translate FIRST, then shrink!
                        img = make_jpeg_safe(img)
                        img.thumbnail((1200, 1200))
                        img.save("test_thumb.jpg", "JPEG", quality=85)
                    print("SUCCESS! The translation worked.")
                except Exception as e:
                    print(f"\nNEW ERROR REVEALED:\n{e}")
                exit()
