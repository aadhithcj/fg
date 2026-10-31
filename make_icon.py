"""Draws icon.ico (the violet download-arrow mark from the app header). Run once: python make_icon.py"""
from PIL import Image, ImageDraw

S = 1024  # draw large, then let Pillow downscale into each .ico size
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((32, 32, S - 32, S - 32), radius=230, fill=(109, 77, 255, 255))
w, white = 92, (255, 255, 255, 255)
d.line((512, 230, 512, 640), fill=white, width=w)                        # shaft
d.line((318, 450, 512, 644, 706, 450), fill=white, width=w, joint="curve")  # arrow head
d.line((262, 800, 762, 800), fill=white, width=w)                        # tray
for x, y in ((512, 230), (318, 450), (706, 450), (262, 800), (762, 800)):  # round caps
    d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=white)
img.save("icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.resize((256, 256), Image.LANCZOS).save("icon_preview.png")
print("wrote icon.ico")
