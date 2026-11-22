"""Draws icon.ico: the FG monogram used by the app, the exe and the installer.
Run once: python make_icon.py
"""
from PIL import Image, ImageDraw, ImageFont

S = 1024  # draw large, then let Pillow downscale into each .ico size
INK = (12, 12, 11, 255)
PAPER = (247, 245, 239, 255)
ACCENT = (46, 204, 91, 255)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rectangle((0, 0, S, S), fill=INK)
d.rectangle((0, 0, S - 1, S - 1), outline=ACCENT, width=56)

font = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 480)
box = d.textbbox((0, 0), "FG", font=font)
w, h = box[2] - box[0], box[3] - box[1]
d.text(((S - w) / 2 - box[0], (S - h) / 2 - box[1]), "FG", font=font, fill=PAPER)

img.save("icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.resize((256, 256), Image.LANCZOS).save("icon_preview.png")
print("wrote icon.ico and icon_preview.png")
