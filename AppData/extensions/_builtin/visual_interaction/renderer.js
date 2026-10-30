/*!
 * visual_interaction renderer
 * 可视化交互拓展 —— 注册 7 类渲染器：choice / fill / image / flowchart / mindmap / html / layout
 *
 * 运行环境：浏览器主世界。可访问：
 *   window.Vue               —— Vue 3 运行时（h, defineComponent, ref, reactive, computed, onMounted, onUnmounted）
 *   window.__registerVizRenderer__(type, component)
 *   window.__extAPI__        —— { sendUserInput, insertInput, imageURL, fetchAPI, openLightbox, getSettings }
 *
 * 每个渲染器是 Vue 组件，props: { data, raw, extAPI }
 * choice / fill 统一「提交按钮」模式：先作答 → 点提交发送 → 锁定
 */
(function () {
    const Vue = window.Vue;
    if (!Vue || !Vue.h || !Vue.defineComponent) {
        console.error('[visual_interaction] window.Vue 未就绪，渲染器未注册');
        return;
    }
    const { h, defineComponent, ref, computed, onMounted, onUnmounted } = Vue;

    // 本地渲染器注册表（layout 递归派发用）
    const REGISTRY = {};
    function register(type, comp) {
        REGISTRY[type] = comp;
        if (typeof window.__registerVizRenderer__ === 'function') {
            window.__registerVizRenderer__(type, comp);
        }
    }

    // —— 全局样式注入（仅一次）——
    const STYLE_ID = 'viz-interaction-style';
    if (!document.getElementById(STYLE_ID)) {
        const css = `
.viz-root { font-size: 14px; color: rgb(var(--text-rgb, 230,235,245)); }
.viz-title { font-weight: 600; margin-bottom: 8px; opacity: .9; }
.viz-fallback { padding: 8px 12px; background: rgba(255,180,80,.12); border: 1px dashed rgba(255,180,80,.5); border-radius: 6px; font-size: 12px; }
.viz-fallback pre { margin: 4px 0 0; white-space: pre-wrap; word-break: break-all; font-size: 11px; opacity: .8; }

/* choice */
.viz-choice { padding: 4px 0; }
.viz-choice .viz-q { font-weight: 600; margin-bottom: 10px; line-height: 1.5; }
.viz-choice .viz-opts { display: flex; flex-direction: column; gap: 8px; }
.viz-choice .viz-opt { text-align: left; padding: 10px 14px; border-radius: 8px; border: 1px solid rgba(var(--accent-rgb,120,140,255),.35); background: rgba(var(--accent-rgb,120,140,255),.08); color: inherit; cursor: pointer; transition: all .15s; font-size: 13px; }
.viz-choice .viz-opt:hover { background: rgba(var(--accent-rgb,120,140,255),.2); border-color: rgba(var(--accent-rgb,120,140,255),.6); }
.viz-choice .viz-opt.selected { background: rgba(var(--accent-rgb,120,140,255),.3); border-color: var(--accent-light,#9db4ff); }
.viz-choice .viz-opt:disabled { opacity: .55; cursor: default; }

/* fill */
.viz-fill { padding: 4px 0; }
.viz-fill .viz-q { font-weight: 600; margin-bottom: 10px; line-height: 1.5; }
.viz-fill .viz-blanks { display: flex; flex-direction: column; gap: 8px; }
.viz-fill .viz-blank { display: flex; align-items: center; gap: 8px; }
.viz-fill .viz-blank-label { flex-shrink: 0; min-width: 56px; font-size: 13px; opacity: .85; }
.viz-fill .viz-input { flex: 1; min-width: 0; padding: 8px 12px; border-radius: 8px; border: 1px solid rgba(255,255,255,.2); background: rgba(255,255,255,.06); color: inherit; font-size: 13px; outline: none; transition: all .15s; }
.viz-fill .viz-input:focus { border-color: rgba(var(--accent-rgb,120,140,255),.6); background: rgba(255,255,255,.09); }
.viz-fill .viz-input:disabled { opacity: .55; }

/* 提交按钮（choice / fill 通用）*/
.viz-submit { margin-top: 10px; padding: 8px 18px; border-radius: 8px; border: none; background: var(--accent-light,#9db4ff); color: #0a0e1a; font-weight: 600; cursor: pointer; transition: all .15s; }
.viz-submit:hover:not(:disabled) { filter: brightness(1.08); }
.viz-submit:disabled { opacity: .5; cursor: not-allowed; }

/* image */
.viz-image { display: flex; flex-direction: column; gap: 6px; }
.viz-image img { max-width: 100%; border-radius: 8px; cursor: zoom-in; border: 1px solid rgba(255,255,255,.08); }
.viz-image .viz-cap { font-size: 12px; opacity: .65; text-align: center; }

/* flowchart */
.viz-flow { overflow: auto; padding: 4px; }
.viz-flow svg { display: block; max-width: 100%; }
.viz-flow .viz-err { color: #ff8080; font-size: 12px; }

/* mindmap */
.viz-mind { overflow: auto; padding: 4px; }
.viz-mind svg { display: block; max-width: 100%; }
.viz-mind .mm-node rect { fill: rgba(var(--accent-rgb,120,140,255),.15); stroke: rgba(var(--accent-rgb,120,140,255),.5); }
.viz-mind .mm-node text { fill: rgb(var(--text-rgb,230,235,245)); font-size: 12px; }

/* html */
.viz-html { position: relative; border: 1px solid rgba(255,255,255,.1); border-radius: 8px; overflow: hidden; }
.viz-html iframe { width: 100%; border: none; background: #fff; }
.viz-html .viz-html-bar { display: flex; justify-content: space-between; align-items: center; padding: 4px 8px; background: rgba(0,0,0,.3); font-size: 11px; }
.viz-html .viz-html-zoom { cursor: pointer; padding: 2px 8px; border-radius: 4px; border: 1px solid rgba(255,255,255,.2); background: transparent; color: inherit; }
.viz-html-full { position: fixed; inset: 0; z-index: 9999; background: rgba(0,0,0,.85); display: flex; flex-direction: column; }
.viz-html-full iframe { flex: 1; width: 100%; border: none; background: #fff; }
.viz-html-full .viz-html-full-bar { display: flex; justify-content: space-between; align-items: center; padding: 8px 16px; color: #fff; }
.viz-html-full .viz-html-close { cursor: pointer; padding: 4px 12px; border-radius: 4px; border: 1px solid rgba(255,255,255,.3); background: transparent; color: #fff; }

/* layout */
.viz-layout { display: grid; gap: 12px; }
.viz-layout .viz-cell { min-width: 0; }
.viz-layout .viz-cell-title { font-size: 12px; font-weight: 600; opacity: .7; margin-bottom: 4px; }
`;
        const style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = css;
        document.head.appendChild(style);
    }

    // —— 工具 ——
    function fallback(msg, raw) {
        return h('div', { class: 'viz-fallback' }, [
            h('div', null, '⚠️ ' + msg),
            raw ? h('pre', null, raw) : null
        ]);
    }
    function safeStr(v, d) { return (typeof v === 'string') ? v : (v == null ? d : String(v)); }

    // ============ choice ============
    // 选择题：单选/多选统一「先选择 → 点提交按钮发送」；提交后锁定
    const Choice = defineComponent({
        props: { data: Object, raw: String, extAPI: Object },
        setup(props) {
            const selected = ref(new Set());
            const submitted = ref(false);
            const data = props.data || {};
            const multiple = !!data.multiple;
            const options = Array.isArray(data.options) ? data.options : [];
            function optLabel(o) { return typeof o === 'string' ? o : safeStr(o && o.label, ''); }
            function optValue(o) { return typeof o === 'string' ? o : (o && o.value != null ? o.value : optLabel(o)); }
            function toggle(i) {
                if (submitted.value) return;
                if (multiple) {
                    const s = new Set(selected.value);
                    s.has(i) ? s.delete(i) : s.add(i);
                    selected.value = s;
                } else {
                    selected.value = new Set([i]);
                }
            }
            function submit() {
                if (submitted.value || selected.value.size === 0) return;
                const vals = [...selected.value].map(i => optValue(options[i]));
                const text = vals.map(v => typeof v === 'object' ? JSON.stringify(v) : String(v)).join(', ');
                submitted.value = true;
                if (props.extAPI && props.extAPI.sendUserInput) props.extAPI.sendUserInput(text);
            }
            return () => h('div', { class: 'viz-choice viz-root' }, [
                h('div', { class: 'viz-q' }, safeStr(data.question, '')),
                h('div', { class: 'viz-opts' },
                    options.map((o, i) => h('button', {
                        class: 'viz-opt' + (selected.value.has(i) ? ' selected' : ''),
                        disabled: submitted.value,
                        onClick: () => toggle(i)
                    }, optLabel(o))
                    )),
                h('button', {
                    class: 'viz-submit',
                    disabled: submitted.value || selected.value.size === 0,
                    onClick: submit
                }, submitted.value ? '已提交 ✓' : (multiple ? `提交选择（已选 ${selected.value.size} 项）` : '提交'))
            ]);
        }
    });

    // ============ fill ============
    // 填空题：blanks 支持 [{label, placeholder, type}] 与 ["label"] 两种形式；
    // 单空可用顶层 {question, label?, placeholder?} 简写。
    // 最后有提交按钮：拼接 "label: value" 发送，提交后锁定
    const Fill = defineComponent({
        props: { data: Object, raw: String, extAPI: Object },
        setup(props) {
            const data = props.data || {};
            const submitted = ref(false);
            const blanks = (Array.isArray(data.blanks) ? data.blanks : []).map(b =>
                typeof b === 'string' ? { label: b } : (b || {})
            );
            // 简写：没有 blanks 数组但有 placeholder/label → 单空
            if (!blanks.length && (data.placeholder != null || data.label != null)) {
                blanks.push({ label: data.label || '', placeholder: data.placeholder || '' });
            }
            const values = ref(blanks.map(() => ''));
            const canSubmit = computed(() => values.value.some(v => (v || '').trim()));
            function submit() {
                if (submitted.value || !canSubmit.value) return;
                const parts = blanks.map((b, i) => {
                    const v = (values.value[i] || '').trim();
                    if (!v) return '';
                    return b.label ? (b.label + ': ' + v) : v;
                }).filter(Boolean);
                if (!parts.length) return;
                submitted.value = true;
                if (props.extAPI && props.extAPI.sendUserInput) props.extAPI.sendUserInput(parts.join('\n'));
            }
            return () => h('div', { class: 'viz-fill viz-root' }, [
                h('div', { class: 'viz-q' }, safeStr(data.question, '')),
                h('div', { class: 'viz-blanks' },
                    blanks.map((b, i) => h('div', { class: 'viz-blank' }, [
                        b.label ? h('label', { class: 'viz-blank-label' }, b.label) : null,
                        h('input', {
                            class: 'viz-input',
                            type: b.type === 'number' ? 'number' : 'text',
                            placeholder: safeStr(b.placeholder, ''),
                            value: values.value[i],
                            disabled: submitted.value,
                            onInput: (e) => { values.value[i] = e.target.value; },
                            onKeydown: (e) => { if (e.key === 'Enter') submit(); }
                        })
                    ]))
                ),
                h('button', {
                    class: 'viz-submit',
                    disabled: submitted.value || !canSubmit.value,
                    onClick: submit
                }, submitted.value ? '已提交 ✓' : '提交')
            ]);
        }
    });

    // ============ image ============
    const Image_ = defineComponent({
        props: { data: Object, raw: String, extAPI: Object },
        setup(props) {
            const data = props.data || {};
            function resolveUrl(u) {
                if (!u) return '';
                // 远程 http(s) / data: / blob: 直接用
                if (/^(https?:|data:|blob:|\/)/i.test(u)) return u;
                // 否则视为本地路径，走 extAPI.imageURL
                return (props.extAPI && props.extAPI.imageURL) ? props.extAPI.imageURL(u) : u;
            }
            function onZoom() {
                const url = resolveUrl(data.url);
                if (props.extAPI && props.extAPI.openLightbox) props.extAPI.openLightbox(url);
            }
            return () => {
                if (!data.url) return fallback('image 缺少 url 字段', props.raw);
                return h('div', { class: 'viz-image viz-root' }, [
                    h('img', { src: resolveUrl(data.url), alt: safeStr(data.caption, ''), style: data.width ? { width: data.width + 'px' } : null, onClick: onZoom, loading: 'lazy' }),
                    data.caption ? h('div', { class: 'viz-cap' }, safeStr(data.caption, '')) : null
                ]);
            };
        }
    });

    // ============ flowchart ============
    // 简易分层布局：按 edge 拓扑分层，从上到下排列
    const Flowchart = defineComponent({
        props: { data: Object, raw: String, extAPI: Object },
        setup(props) {
            return () => {
                const d = props.data || {};
                const nodes = Array.isArray(d.nodes) ? d.nodes : [];
                const edges = Array.isArray(d.edges) ? d.edges : [];
                if (!nodes.length) return fallback('flowchart 缺少 nodes', props.raw);
                // 拓扑分层
                const ids = nodes.map(n => n.id);
                const indeg = {}; ids.forEach(id => indeg[id] = 0);
                const adj = {}; ids.forEach(id => adj[id] = []);
                edges.forEach(e => {
                    if (indeg[e.to] != null) indeg[e.to]++;
                    if (adj[e.from] != null) adj[e.from].push(e.to);
                });
                const layers = [];
                const placed = new Set();
                let remaining = ids.slice();
                while (remaining.length) {
                    const layer = remaining.filter(id => indeg[id] === 0);
                    if (!layer.length) { // 有环，剩余放一层
                        layers.push(remaining.slice()); break;
                    }
                    layers.push(layer);
                    layer.forEach(id => { placed.add(id); adj[id].forEach(t => indeg[t]--); });
                    remaining = remaining.filter(id => !placed.has(id));
                }
                // 坐标
                const NW = 120, NH = 40, GX = 40, GY = 60;
                const pos = {};
                layers.forEach((layer, li) => {
                    const totalW = layer.length * NW + (layer.length - 1) * GX;
                    layer.forEach((id, ci) => {
                        pos[id] = { x: ci * (NW + GX), y: li * (NH + GY) };
                    });
                });
                const width = Math.max(...layers.map(l => l.length)) * (NW + GX);
                const height = layers.length * (NH + GY);
                const labelOf = id => { const n = nodes.find(x => x.id === id); return n ? safeStr(n.label, id) : id; };
                return h('div', { class: 'viz-flow viz-root' }, [
                    h('svg', { width: Math.max(width, 100), height: Math.max(height, NH + GY) }, [
                        ...edges.map(e => {
                            const a = pos[e.from], b = pos[e.to];
                            if (!a || !b) return null;
                            const x1 = a.x + NW / 2, y1 = a.y + NH, x2 = b.x + NW / 2, y2 = b.y;
                            return h('g', null, [
                                h('path', { d: `M${x1},${y1} C${x1},${(y1 + y2) / 2} ${x2},${(y1 + y2) / 2} ${x2},${y2}`, fill: 'none', stroke: 'rgba(var(--accent-rgb,120,140,255),.5)', 'stroke-width': 1.5 }),
                                h('polygon', { points: `${x2 - 4},${y2 - 7} ${x2 + 4},${y2 - 7} ${x2},${y2}`, fill: 'rgba(var(--accent-rgb,120,140,255),.7)' }),
                                e.label ? h('text', { x: (x1 + x2) / 2, y: (y1 + y2) / 2 - 4, 'text-anchor': 'middle', 'font-size': 10, fill: 'rgba(255,255,255,.6)' }, safeStr(e.label, '')) : null
                            ]);
                        }),
                        ...nodes.map(n => {
                            const p = pos[n.id]; if (!p) return null;
                            return h('g', { transform: `translate(${p.x},${p.y})` }, [
                                h('rect', { width: NW, height: NH, rx: 6, fill: 'rgba(var(--accent-rgb,120,140,255),.15)', stroke: 'rgba(var(--accent-rgb,120,140,255),.5)' }),
                                h('text', { x: NW / 2, y: NH / 2 + 4, 'text-anchor': 'middle', 'font-size': 12, fill: 'rgb(var(--text-rgb,230,235,245))' }, labelOf(n.id))
                            ]);
                        })
                    ])
                ]);
            };
        }
    });

    // ============ mindmap ============
    // 横向树布局：根在左，子树向右展开
    const Mindmap = defineComponent({
        props: { data: Object, raw: String, extAPI: Object },
        setup(props) {
            return () => {
                const d = props.data || {};
                const root = d.root;
                if (!root) return fallback('mindmap 缺少 root', props.raw);
                // 布局：计算每节点 (x, y, width, height)
                const NH = 28, DY = 8, DX = 80, CW = 90;
                // 先递归求每个子树高度（行数）
                function rows(node) {
                    const ch = node.children && node.children.length ? node.children : null;
                    if (!ch) return 1;
                    return ch.reduce((s, c) => s + rows(c), 0) + (ch.length - 1) * 0; // 紧凑
                }
                // 递归布局，返回 { x, y, w, label, children }，cursor 为当前 y
                function layout(node, depth, cursor) {
                    const ch = node.children && node.children.length ? node.children : [];
                    const x = depth * (CW + DX);
                    if (!ch.length) {
                        return { x, y: cursor, w: CW, label: safeStr(node.label, ''), children: [], h: 1 };
                    }
                    const kids = [];
                    let y = cursor;
                    ch.forEach(c => {
                        const sub = layout(c, depth + 1, y);
                        kids.push(sub);
                        y += sub.h * (NH + DY);
                    });
                    const firstY = kids[0].y, lastY = kids[kids.length - 1].y;
                    const myY = (firstY + lastY) / 2;
                    return { x, y: myY, w: CW, label: safeStr(node.label, ''), children: kids, h: (y - cursor) / (NH + DY) };
                }
                const tree = layout(root, 0, 0);
                // 求最大 x + w
                let maxR = 0, maxB = 0;
                function walk(n) {
                    maxR = Math.max(maxR, n.x + n.w);
                    maxB = Math.max(maxB, n.y + NH);
                    n.children.forEach(walk);
                }
                walk(tree);
                function renderNode(n) {
                    return [
                        h('g', { class: 'mm-node', transform: `translate(${n.x},${n.y})` }, [
                            h('rect', { width: n.w, height: NH, rx: 6 }),
                            h('text', { x: n.w / 2, y: NH / 2 + 4, 'text-anchor': 'middle' }, n.label)
                        ]),
                        ...n.children.map(c => {
                            const x1 = n.x + n.w, y1 = n.y + NH / 2, x2 = c.x, y2 = c.y + NH / 2;
                            return h('path', { d: `M${x1},${y1} C${x1 + 30},${y1} ${x2 - 30},${y2} ${x2},${y2}`, fill: 'none', stroke: 'rgba(var(--accent-rgb,120,140,255),.4)', 'stroke-width': 1.5 });
                        }),
                        ...n.children.flatMap(renderNode)
                    ];
                }
                return h('div', { class: 'viz-mind viz-root' }, [
                    h('svg', { width: Math.max(maxR, 100), height: Math.max(maxB, NH) }, renderNode(tree))
                ]);
            };
        }
    });

    // ============ html ============
    const Html = defineComponent({
        props: { data: Object, raw: String, extAPI: Object },
        setup(props) {
            const full = ref(false);
            const data = props.data || {};
            const srcdoc = safeStr(data.html, '');
            const height = data.height || 200;
            return () => h('div', { class: 'viz-html viz-root' }, [
                h('div', { class: 'viz-html-bar' }, [
                    h('span', null, '🌐 HTML 交互'),
                    h('button', { class: 'viz-html-zoom', onClick: () => { full.value = true; } }, '放大')
                ]),
                h('iframe', { srcdoc, style: { height: height + 'px' }, sandbox: data.sandbox || 'allow-scripts allow-forms allow-popups' }),
                full.value ? h('div', { class: 'viz-html-full', onClick: (e) => { if (e.target.classList.contains('viz-html-full')) full.value = false; } }, [
                    h('div', { class: 'viz-html-full-bar' }, [
                        h('span', null, '🌐 HTML 全屏交互'),
                        h('button', { class: 'viz-html-close', onClick: () => { full.value = false; } }, '关闭 ✕')
                    ]),
                    h('iframe', { srcdoc, sandbox: data.sandbox || 'allow-scripts allow-forms allow-popups' })
                ]) : null
            ]);
        }
    });

    // ============ layout ============
    // 优化排布：多列网格，每项递归派发对应渲染器
    const Layout = defineComponent({
        props: { data: Object, raw: String, extAPI: Object },
        setup(props) {
            return () => {
                const d = props.data || {};
                const cols = Math.max(1, Math.min(6, Number(d.columns) || 2));
                const items = Array.isArray(d.items) ? d.items : [];
                return h('div', { class: 'viz-layout viz-root', style: { gridTemplateColumns: `repeat(${cols}, 1fr)` } },
                    items.map(it => {
                        const type = safeStr(it.type, '');
                        const comp = REGISTRY[type];
                        const child = comp
                            ? h(comp, { data: it.data || {}, raw: JSON.stringify(it.data || {}), extAPI: props.extAPI })
                            : fallback('未知子类型: ' + type, JSON.stringify(it.data || {}));
                        return h('div', { class: 'viz-cell' }, [
                            it.title ? h('div', { class: 'viz-cell-title' }, safeStr(it.title, '')) : null,
                            child
                        ]);
                    })
                );
            };
        }
    });

    // —— 注册 ——
    register('choice', Choice);
    register('fill', Fill);
    register('image', Image_);
    register('flowchart', Flowchart);
    register('mindmap', Mindmap);
    register('html', Html);
    register('layout', Layout);

    console.info('[visual_interaction] 已注册渲染器: choice, fill, image, flowchart, mindmap, html, layout');
})();