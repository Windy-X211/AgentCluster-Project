import json, base64, urllib.request, os

url = "http://127.0.0.1:7860/sdapi/v1/txt2img"
payload = {
    "prompt": "masterpiece, best quality, ultra-detailed, 1girl, cute loli girl, sweet smile, big expressive eyes, long curly twin tails with ribbons, wearing elegant gothic lolita dress, frills, lace, corset, bonnet headpiece, knee-high stockings, mary jane shoes, holding a small lace parasol, standing inside a grand gothic cathedral, stained glass windows with colorful light, tall stone pillars, vaulted ceiling, ornate altar, candle light, rays of divine light, volumetric lighting, soft rim lighting, cinematic atmosphere, intricate details, rich textures, 8k, photorealistic",
    "negative_prompt": "lowres, bad anatomy, bad hands, missing fingers, extra digits, watermark, signature, worst quality, blurry, deformed, ugly, poorly drawn face, poorly drawn hands, out of frame, cropped",
    "steps": 30,
    "cfg_scale": 7.5,
    "width": 512,
    "height": 768,
    "seed": -1,
    "sampler_name": "DPM++ 2M",
    "scheduler": "karras",
    "batch_size": 1,
    "n_iter": 1
}

req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
print("Sending request to SD WebUI...")
resp = urllib.request.urlopen(req, timeout=180)
data = json.loads(resp.read().decode("utf-8"))

if "images" in data and len(data["images"]) > 0:
    img_b64 = data["images"][0]
    img_data = base64.b64decode(img_b64)
    out_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(out_dir, "lolita_cathedral.png")
    with open(out_path, "wb") as f:
        f.write(img_data)
    print(f"Image saved to: {out_path}")
    print(f"Seed: {data.get('seed', 'N/A')}")
else:
    print("No images returned!")
    print(json.dumps(data, indent=2)[:500])