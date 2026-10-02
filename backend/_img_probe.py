# -*- coding: utf-8 -*-
from PIL import Image
import sys

path = r'F:\图片\Saved Pictures\20200416222823_slpks.png'
img = Image.open(path)
print('Format:', img.format)
print('Size:', img.size)
print('Mode:', img.mode)
print('Info:', img.info)
print('Extrema:', img.getextrema())

# 缩小后生成 base64 缩略图，方便后续视觉查看
import io, base64
small = img.copy()
small.thumbnail((800, 800))
buf = io.BytesIO()
small.save(buf, format='PNG')
b64 = base64.b64encode(buf.getvalue()).decode('ascii')
with open(r'F:\PythonProjects\TraeProjects\智能体集群\backend\_img_thumb.txt', 'w') as f:
    f.write(b64)
print('thumb bytes:', len(b64))