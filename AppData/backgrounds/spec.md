# 背景文件生成规范（Background Spec）

本目录存放所有界面背景。每个背景是一个**子文件夹**，内含 `manifest.json` 描述元数据，加上实际的背景资源文件。前端设置页会自动扫描本目录并列出所有背景供切换。

## 1. 目录结构

```
AppData/backgrounds/
├── spec.md                  # 本规范文件
├── starfield/               # 内置：星空动画
│   ├── manifest.json
│   └── background.html
├── meteor-shower/           # 内置：流星雨动画
│   ├── manifest.json
│   └── background.html
├── aurora/                  # 内置：极光动画
│   ├── manifest.json
│   └── background.html
└── <自定义背景>/            # 用户或智能体创建的背景
    ├── manifest.json
    └── ...资源文件
```

**规则**：每个子文件夹就是一个背景，文件夹名即背景 id（manifest.json 中的 `id` 应与文件夹名一致）。`spec.md` 会被扫描器跳过。

## 2. manifest.json 字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 背景唯一标识，须与文件夹名一致 |
| `name` | string | 是 | 显示名称 |
| `type` | string | 是 | 背景类型：`animation` / `video` / `image` |
| `entry` | string | 是 | 入口文件名（相对背景文件夹）<br>· animation → 一个自包含 HTML 文件<br>· video → 视频文件（mp4/webm）<br>· image → 图片文件（jpg/png/webp/gif） |
| `description` | string | 否 | 背景描述 |
| `thumbnail` | string | 否 | 缩略图文件名（相对背景文件夹）。有则优先用于设置页预览 |
| `preview` | string | 否 | 缩略图 CSS 背景值（如 `radial-gradient(...)`），无 `thumbnail` 时使用 |
| `opacity` | number | 否 | 默认不透明度 0~1，默认 1.0 |
| `blur` | number | 否 | 默认模糊度（px），默认 0 |
| `builtin` | bool | 否 | 是否内置背景（内置不可删除） |
| `interactive` | bool | 否 | animation 是否可交互（如鼠标视差），默认 false |

### manifest.json 示例（动画）

```json
{
  "id": "starfield",
  "name": "星空",
  "type": "animation",
  "entry": "background.html",
  "description": "深邃星空，繁星闪烁，偶有流星划过",
  "preview": "radial-gradient(circle at 50% 40%, #1a2340 0%, #050816 70%)",
  "opacity": 1.0,
  "builtin": true
}
```

### manifest.json 示例（视频）

```json
{
  "id": "my-video",
  "name": "海浪",
  "type": "video",
  "entry": "waves.mp4",
  "description": "海浪拍岸",
  "thumbnail": "waves-thumb.jpg",
  "opacity": 0.7
}
```

## 3. animation 类型 · HTML 规范

动画背景是一个**自包含的 HTML 文件**，前端用 `<iframe>` 加载。规范如下：

### 必须遵守
1. **自包含**：所有 CSS / JS 内联在 HTML 中，不引用外部文件（无网络依赖、无跨文件夹引用）。
2. **全屏自适应**：`html, body { width:100%; height:100%; margin:0; overflow:hidden; }`，画布随窗口尺寸变化（监听 `resize`）。
3. **透明背景**：`body` 不要设不透明纯色背景遮挡上层；用半透明深色或让动画元素自身提供色彩。推荐 `body { background: transparent; }`，由前端背景层容器提供底色。如需自带底色，用 `rgba` 半透明。
4. **性能优先**：用 `requestAnimationFrame`；粒子数控制在合理范围（星空 ≤300，流星 ≤20）；离开页面时清理动画（iframe 被移除即自动销毁，无需特别处理）。
5. **不操作父页面**：不得访问 `parent` / `top` / `window.parent`，保持沙箱隔离。

### 推荐
- 使用 Canvas 2D 实现粒子效果（兼容性最好、性能佳）。
- 动画节奏舒缓，避免高频闪烁（影响阅读和前庭敏感用户）。
- 色彩与界面主题协调（深色背景用冷色调粒子）。
- 可选：监听 `window.matchMedia('(prefers-reduced-motion: reduce)')` 降低动画强度。

### HTML 模板

```html
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<style>
  html, body { width:100%; height:100%; margin:0; overflow:hidden; background:transparent; }
  canvas { display:block; width:100%; height:100%; }
</style>
</head>
<body>
<canvas id="c"></canvas>
<script>
  const cv = document.getElementById('c');
  const ctx = cv.getContext('2d');
  let W, H, stars = [];
  function resize() {
    W = cv.width = window.innerWidth;
    H = cv.height = window.innerHeight;
  }
  function init() {
    stars = [];
    for (let i = 0; i < 250; i++) {
      stars.push({
        x: Math.random() * W, y: Math.random() * H,
        r: Math.random() * 1.4 + 0.2,
        a: Math.random(), s: Math.random() * 0.02 + 0.005
      });
    }
  }
  function frame() {
    ctx.clearRect(0, 0, W, H);
    for (const st of stars) {
      st.a += st.s; const tw = 0.5 + 0.5 * Math.sin(st.a);
      ctx.globalAlpha = tw;
      ctx.fillStyle = '#ffffff';
      ctx.beginPath(); ctx.arc(st.x, st.y, st.r, 0, Math.PI * 2); ctx.fill();
    }
    ctx.globalAlpha = 1;
    requestAnimationFrame(frame);
  }
  window.addEventListener('resize', () => { resize(); init(); });
  resize(); init(); frame();
</script>
</body>
</html>
```

## 4. video 类型规范
- 格式：`mp4`（H.264）或 `webm`（VP9），优先 mp4（兼容性最佳）。
- 前端会自动循环播放、静音、不显示控件。
- 建议分辨率 1080p，码率适中（≤5Mbps），时长 10~30s 循环无明显接缝。
- `opacity` 建议 0.5~0.8，避免视频过亮影响界面可读性。

## 5. image 类型规范
- 格式：jpg / png / webp / gif（gif 可做简单循环动图）。
- 建议分辨率 ≥1920×1080，文件大小 ≤5MB（前端加载流畅）。
- `opacity` 建议 0.5~0.8。

## 6. 让智能体生成背景

智能体（或用户）可以自由创建背景，步骤：

1. 在 `AppData/backgrounds/` 下新建一个子文件夹（id）。
2. 编写 `manifest.json`（参考第 2 节）。
3. 根据类型放入资源：
   - `animation`：写一个符合第 3 节规范的自包含 HTML。
   - `video` / `image`：放入媒体文件。
4. 刷新设置页即可看到新背景，点击启用。

**对智能体的提示**：生成动画背景时，优先用 Canvas 2D + `requestAnimationFrame`，粒子数适中，色彩与深色界面协调，动画舒缓不抢眼。生成完毕后告知用户背景 id 与名称，用户可在「设置 → 外观设置 → 背景」中切换。

## 7. 删除背景
在设置页点击背景卡片上的删除按钮即可（内置背景不可删除）。也可直接删除 `AppData/backgrounds/<id>/` 文件夹。