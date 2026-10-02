---
name: html-demo
label: 先进 HTML 演示
description: 通过高级 CSS & JavaScript 技巧生成一个美观、响应式、交互性强的 HTML 页面。该技能在用户请求 "演示网页" 或 "高级 HTML" 时激活。
version: "1.0.0"
author: AgentCluster
tags: [web, html, css, javascript, demo]
---

## 何时使用
- 当用户想要展示一个美观、现代的网页示例时。
- 当用户想快速生成一个包含动态交互、动画或响应式设计的完整 HTML，CSS，JS 代码时。

## 触发关键字
- "演示网页" 或 "演示网站"
- "高级 HTML"、"前端演示"
- "交互效果"、"动画效果"
- 任何包含 "html"、"demo" 的请求。

## 输出格式
1. **index.html**：完整的 HTML 代码，包含 meta、link、script。
2. **style.css**：现代 CSS，使用 Flexbox / Grid，渐变、阴影、动画等。
3. **script.js**：交互作用，使用原生 JS 或简易库（如 Vanilla‑JS），实现滚动导航、悬停提示、图片轮播等。
4. **assets/** 目录（可选）：存放图片、图标、字体，若需要请在输出中列出。

### 代码示例
```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>高级演示页面</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <nav class="navbar">
    <ul>
      <li><a href="#hero">首页</a></li>
      <li><a href="#features">功能</a></li>
      <li><a href="#contact">联系方式</a></li>
    </ul>
  </nav>
  <section id="hero" class="hero">
    <h1>欢迎体验前端动画</h1>
    <p>简单、优雅、响应式</p>
    <button id="start-btn">开始体验</button>
  </section>
  <section id="features" class="features">
    <div class="feature-box">
      <h3>功能一</h3>
      <p>描述功能一的亮点。</p>
    </div>
    <div class="feature-box">
      <h3>功能二</h3>
      <p>描述功能二的亮点。</p>
    </div>
  </section>
  <footer class="footer">
    <p>&copy; 2026 未来科技有限公司</p>
  </footer>
  <script src="script.js"></script>
</body>
</html>
```
```css
/* style.css */
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;line-height:1.5;background:#f5f5f5}
.navbar{position:fixed;top:0;width:100%;background:rgba(0,0,0,0.7);color:#fff;z-index:100}
.navbar ul{display:flex;justify-content:center;list-style:none;margin:0;padding:1rem 0}
.navbar li{margin:0 1rem}
.navbar a{color:#fff;text-decoration:none;font-weight:600}
.hero{height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;background:url('assets/hero-bg.jpg') no-repeat center/cover;text-align:center;color:#fff}
.hero h1{font-size:3rem;margin-bottom:1rem}
.hero p{font-size:1.25rem;margin-bottom:2rem}
.hero button{padding:.75rem 1.5rem;background:#ff4081;color:#fff;border:none;border-radius:.25rem;cursor:pointer;font-size:1rem;transition:background .3s}
.hero button:hover{background:#e91e63}
.features{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:2rem;padding:4rem 1rem;background:#fff}
.feature-box{background:#fafafa;padding:2rem;border-radius:.5rem;box-shadow:0 2px 8px rgba(0,0,0,.1);transition:transform .3s}
.feature-box:hover{transform:translateY(-5px)}
.footer{background:#333;color:#eee;text-align:center;padding:1.5rem 0;font-size:.875rem}
```
```js
// script.js
document.getElementById('start-btn').addEventListener('click',()=>{
  document.querySelector('.hero').scrollIntoView({behavior:'smooth'});
});
// 简易滚动导航高亮
const sections = document.querySelectorAll('section');
const navLinks = document.querySelectorAll('.navbar a');
const observer = new IntersectionObserver((entries)=>{
  entries.forEach(entry=>{
    const id = entry.target.id;
    const navLink = document.querySelector(`.navbar a[href='#${id}']`);
    if(entry.isIntersecting) navLink.classList.add('active');
    else navLink.classList.remove('active');
  });
},{rootMargin:'-50% 0px -50% 0px'});
sections.forEach(sec=>observer.observe(sec));
```

> **可选扩展**：
> - 通过 `postCSS` 或 `Tailwind` 等框架加速开发。
> - 引入动画库 `GSAP` 或 `Anime.js` 让页面更炫。
> - 若需图片/视频素材，建议使用 `assets/` 文件夹并在 `img` 标签中指向对应路径。

## 安全与边界
- 内容保持中立，避免敏感主题。
- 若用户要求生成可执行代码，须保证不含恶意脚本。
- 大型动画可选用 CSS 或轻量 JS。

本技能仅作演示用途，若需部署，请自行检查代码安全与版权问题。