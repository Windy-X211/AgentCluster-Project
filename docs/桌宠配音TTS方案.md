# 桌宠配音（TTS）方案 v1

> 目标：给 `plugins/desktop_pet` 增加配音能力 —— 动画与配音同时播放、字幕与音频严格同步、首声延迟尽量低、显存占用尽量小，并支持「本地 Qwen3-TTS 0.6B / 在线 TTS」自由切换与背景音。
>
> 本文是**方案**，不是实现。确认后再进入 P0（测试小脚本）。

---

## 0. 结论先行

| 项目 | 方案 |
| --- | --- |
| 引擎 | 本地 `Qwen3-TTS-12Hz-0.6B`（CustomVoice 预设音色 / Base 声纹克隆二选一）＋ 在线 TTS（火山豆包 / Azure / OpenAI 兼容） |
| 进程 | TTS 独立守护进程（自带 venv，只装 torch+qwen-tts，不装 Qt），插件宿主与渲染 worker 完全不碰 torch |
| 低延迟 | 句级流水线（第 1 句合成完立刻播，同时合成第 2 句）＋ 320ms 预缓冲 ＋ 内存直传（TCP，无磁盘/无 HTTP 头）＋ 播放时钟驱动字幕 |
| 字幕 | 段边界＝真实 PCM 样本数（误差 0ms）；段内逐字时间由音节权重分配（±80ms），在线 provider 若带原生时间戳则直接用 |
| 显存 | 0.6B fp16 峰值目标 ≤ 3.2GB；空闲 120s 自动卸载 → 0；显存不足自动转在线；`tts_unload` 可强制释放 |
| 背景音 | 语音与 BGM 在 worker 内混音，支持音量/ducking/淡入淡出/循环，参数随每次 `speak` 传 |
| 与现配音关系 | 完全不动原有「模型自带 Sound」链路；TTS 走独立音频通道，二者互斥（说话时临时静音动作音） |
| 首次交付 | `tools/tts_probe.py`：生成 wav + srt + marks.json + 预览窗口 + 性能报告，供你听感/字幕验收 |

---

## 1. 现状勘查结论（已读代码，不是猜测）

| 事实 | 位置 | 对本方案的含义 |
| --- | --- | --- |
| 每个模型一个 worker 进程，`tool.py` 通过 `127.0.0.1` HTTP `/cmd`、`/cmd_sync` 下命令 | `tool.py` `_WorkerClient` | 新增 `speak/stop_speak` 走同一通道，无需改通信框架 |
| 内置配音由渲染页 Chromium `HTMLAudio` 播放，路径由 `model3.json → Sound` 决定 | `pet_window.py` `_motion_text/_probe_sound_dur`、`_make_render_page` | TTS 不能复用这条链路（无法混 BGM、时钟不可控） |
| 字幕是 Qt 侧 `QPainter` 画的，目前只有「一段文本 + 到期时间」 | `pet_window.py` `show_subtitle / _paint_subtitle` | 升级为 `SubtitleTrack`（mark 列表 + 音频时钟），保留旧接口不动 |
| 宿主 python = `F:\anaconda3\python.exe` 3.13.9，`torch 2.6.0+cu124` 可用，`PyQt5 5.15.2`（含 QtMultimedia） | 实测 | worker 可直接用 `QAudioSink`；但 **transformers 是 5.2.0** |
| `qwen-tts 0.1.1` 硬依赖 `transformers==4.57.3` | 解包 wheel 的 METADATA | **绝不能把 qwen-tts 装进宿主环境**（会把 transformers 降到 4.57.3，可能拖垮 AgentCluster 与其他插件）→ 必须独立 venv |
| 显卡 RTX 2060 6GB（CC 7.5），当前空闲 5120MB；Turing 无原生 bf16 | 实测 | 用 fp16；`attn_implementation="sdpa"`（flash-attn 在 Windows/sm75 不可用） |
| `edge-tts` 今天实测 403（微软 WSS 拒绝） | 实测 | 不做默认在线方案；在线优先火山豆包 / Azure / OpenAI 兼容 |
| 插件目录在 C 盘，C 盘只剩 16.4GB；E 盘剩 140GB | 实测 | 模型目录做成可配置，默认放 E 盘 |
| 依赖管理器只认 `pip / npm / external` | `backend/core/dependency_manager.py` | 模型权重不是现有依赖类型 → 见 §10 两个落地选项 |

---

## 2. 整体架构

```
Agent(LLM)
   │  action=speak(text, bgm=..., animation=...)
   ▼
tool.py（AgentCluster 宿主进程，F:\anaconda3\python）
   ├─ 确保 TTS 守护进程在跑（_tts_env\python tts\daemon.py）
   ├─ 端口/状态写 _tts\daemon.json
   └─ /cmd  {"type":"speak", "daemon_port":..., "job":..., "bgm":{...}}
                                   │
                                   ▼
pet_window worker（每个模型一个进程，Qt + WebEngine）
   ├─ TTSClient（socket，后台线程）：收「段头 JSON + PCM」
   ├─ AudioEngine（QAudioSink 48k 单声道）：语音 + BGM 混音、ducking、淡入淡出
   ├─ SubtitleTrack：音频时钟 → 逐字高亮（Qt 叠加层，现有 _paint_subtitle 扩展）
   └─ LipSync：语音 RMS → ParamMouthOpenY（走现有 stream 通道，25Hz）

TTS 守护进程（独立 venv：torch 2.6 + qwen-tts 0.1.1 + transformers 4.57.3）
   ├─ 懒加载 / 空闲卸载（默认 120s → 显存归零）
   ├─ 分段 → 逐段合成 → 24k PCM → 48k 重采样 → 计算段/字时间轴
   ├─ 显存守卫（free < 2200MB 拒绝本地并转在线）
   └─ 在线引擎（HTTP/WS，可插拔）
```

**为什么是这个切分**

| 备选 | 否决理由 |
| --- | --- |
| 在宿主 `tool.py` 进程里加载模型 | 首次加载 3~8s 会卡住宿主；torch 崩溃=整个 Agent 挂掉；transformers 版本冲突 |
| 在 worker 里加载模型 | 每个模型一个 worker，多开就多份 0.6B，显存直接爆 |
| 用 `QMediaPlayer` 播 TTS | 起播 100~300ms、无法与 BGM 混音、拿不到稳定播放时钟 |
| 继续用 Chromium `HTMLAudio` 播 TTS | 与内置配音同链路，但没法混 BGM、字幕时钟只能靠定时器估算，违背「字幕同步」要求 |

---

## 3. 延迟设计

### 3.1 预算（热态，RTX 2060 + 0.6B fp16，待 P0 实测校准）

| 环节 | 预估 | 手段 |
| --- | --- | --- |
| 文本分段 | < 1ms | 标点切句，最短 8 字、最长 40 字合并/切分 |
| 首段合成首包 | 150~400ms | 官方 97ms 是理想环境；2060 上按实测；开 CUDA Graph 可再降（见 §6.4） |
| 预缓冲 | 320ms（1 个 packet） | 攒够 4 token=320ms 就起播，不等整句 |
| TCP 直传 | < 1ms | JSON 行头 + 裸 PCM，不落盘 |
| 24k→48k 重采样 | 1~3ms | `scipy.signal.resample_poly`（零相位、低延迟） |
| Qt 音频输出缓冲 | 60~100ms | `QAudioSink.setBufferSize(60ms)` |
| **合计首声** | **约 0.55~0.85s** | 冷启动首次另加 4~8s（显示「准备配音…」气泡） |

### 3.2 七个降低延迟的手段

1. **句级流式流水线**：段 N 在播时，段 N+1 已在合成；播放与合成重叠。
2. **常驻守护进程 + 预热**：`speak` 前可选 `tts_prewarm`（或首次 speak 后保活），后续请求无冷启动。
3. **内存直传**：TCP `{"ev":"seg",...}\n` + PCM，零磁盘 IO、零 HTTP 分块开销。
4. **音频时钟驱动字幕**：`QAudioSink.processedUSecs()` 当播放头，字幕不存在「定时器漂移」。
5. **按标点抢跑**：第一段只取到第一个逗号/句号就发合成（≤40 字），不等人把整段话说完。
6. **失败快速回退**：本地首包超时 `first_packet_timeout_ms`（默认 1200）→ 该段改用在线引擎重合成，不卡住整句。
7. **可选流式文本输入（P2）**：上层 LLM 边吐字边 `speak_append`，句末即合成，首声可压到 ~0.3~0.5s。

### 3.3 不做的事

- 不做全句合成完再播（长句会 2~4s 才出声）。
- 不用 `speed` 改本地音速（重采样变速会变调）；本地语速靠模型自身韵律与标点控制，在线引擎才映射 `rate`。
- 不默认开 CUDA Graph（省 2~7x 时间但多占 ~0.3~0.5GB 显存，6GB 卡上先关，P2 压测后决定）。

---

## 4. 字幕同步

### 4.1 数据模型（守护进程 → worker，每段一条头）

```json
{"ev":"seg","i":0,"text":"今天天气不错，","sr":48000,"samples":87840,
 "start_ms":0,"dur_ms":1830,"aligned":"estimated",
 "chars":[{"ch":"今","t0":0,"t1":196},{"ch":"天","t0":196,"t1":392},
          {"ch":"气","t0":392,"t1":588},{"ch":"不","t0":588,"t1":784},
          {"ch":"错","t0":784,"t1":1200},{"ch":"，","t0":1200,"t1":1830}]}
```

- `start_ms` 由守护进程累计真实样本数算出，**段边界误差 = 0ms**（不是估算）。
- `chars` 是段内时间轴：中文 1.0 权重 / 字母数字 0.6 / 空格 0.2 / 标点 0 但把停顿权重记在前一个字尾部。
- 在线引擎若有原生 word boundary（Azure / 火山字幕时间戳），`aligned="native"`，直接用，误差 <10ms。

### 4.2 播放与字幕的时钟

```
playhead_ms = QAudioSink.processedUSecs()/1000        // 已真正播出的样本
当前段 = 最后一个 start_ms ≤ playhead 的段
当前字 = 该段 chars 中 t0 ≤ (playhead - start_ms) < t1 的字
```

- 暂停/换句/切模型时以 `stop_speak` 清空，不会残留上一条。
- 若设备不支持 48k 而回落到 44.1k，时间轴按 `samples/sr` 重算，不受影响。

### 4.3 渲染

- 复用现有底部半透明胶囊（`_paint_subtitle`），升级为：
  - 只显示**当前段**（2 行内，超长截断加 `…`），与内置配音的观感一致；
  - 已读字高亮（`#FFD24A`），未读字白色，可关（`subtitle_style=plain`）；
  - 换段 80ms 淡入 / 段间停顿 >400ms 时 120ms 淡出；
  - `QTextLayout` 预排版并缓存每段字形位置，每帧只算高亮下标（不重复 measure）。
- 用户右键「显示字幕」开关沿用现有 `state["subtitles"]`，关掉即整条不画。

### 4.4 和现有内置配音的关系

- 内置配音仍走原路径（`show_subtitle` + `subtitle_extend`），代码不动。
- TTS 播放期间默认 `duck_motion_sound=true`：本次动作以 `mute=true` 下发，避免两条音轨叠在一起。
- 若用户手动触发带配音的动作，TTS 自动暂停（或按 `mode=append` 排队），策略在实现时固定为「TTS 优先」。

---

## 5. 本地引擎：Qwen3-TTS 0.6B

### 5.1 选型

| 变体 | 能力 | 显存 | 用途 |
| --- | --- | --- | --- |
| `Qwen3-TTS-12Hz-0.6B-CustomVoice` | 9 个预设音色，零配置 | ~3GB | **默认**，首跑不依赖参考音频 |
| `Qwen3-TTS-12Hz-0.6B-Base` | 3 秒参考音频声纹克隆 | ~3GB | 想让桌宠说「角色自己的声音」时装 |
| `Qwen3-TTS-12Hz-1.7B-*` | 质量/情绪指令更好 | ~6GB+ | 6GB 卡不建议；`instruct` 只有 1.7B 支持 |

- 两者共用 `Qwen3-TTS-Tokenizer-12Hz`，磁盘合计约 3~4GB。
- 输出 24kHz 单声道，`generate_custom_voice(text, language, speaker)`；`instruct` 在 0.6B 上被忽略（官方 API 行为）。
- 声纹克隆只允许使用本人或已获授权的参考音频（方案内置这条约束，不做他人声纹克隆）。

### 5.2 加载与卸载

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `dtype` | `float16` | Turing 无原生 bf16 |
| `attn_implementation` | `sdpa` | flash-attn 在 Windows/sm75 不可用 |
| `device_map` | `cuda:0` | 失败回落 CPU（会明确提示慢） |
| `idle_unload_s` | 120 | 无请求 120s 后卸载模型 + `empty_cache()` → 显存归 0 |
| `daemon_exit_s` | 600 | 再空闲 10 分钟进程退出，回收 ~500MB 内存 |
| `min_free_vram_mb` | 2200 | 低于此值拒绝本地、自动转在线（错误码 `VRAM_LOW`） |
| `max_new_tokens` | 512 | 单段上限，防止 KV 无界增长 |
| `PYTORCH_CUDA_ALLOC_CONF` | `expandable_segments:True,max_split_size_mb:64` | 降低碎片化 |

### 5.3 显存预算（6GB 卡）

| 项 | 预估 |
| --- | --- |
| 0.6B 权重 fp16 | ~1.2GB |
| Tokenizer/Codec 12Hz | ~0.8~1.2GB |
| KV + 激活（单段 ≤40 字） | ~0.3~0.5GB |
| CUDA context / 碎片 | ~0.3GB |
| **TTS 小计** | **~2.7~3.2GB** |
| Live2D + Chromium（现网） | ~0.4~0.9GB |
| 6GB 余量 | ~2GB（可接受，但不再装 1.7B） |

> P0 会用 `torch.cuda.max_memory_allocated` + `nvidia-smi` 双口径记录真实峰值，写入报告。

### 5.4 加速选项（P2，不在首版开）

- `faster-qwen3-tts` / `qwen3-tts-streaming` 这类 CUDA Graph 方案，官方口径 RTF/首包可提升数倍，代价是显存 +0.3~0.5GB、依赖第三方补丁。
- 上游 `qwen-tts` 的 `generate_custom_voice` 默认返回整段音频；真流式要靠补丁或 vLLM-Omni。首版用**句级流水线**绕开这个限制，效果等价且稳。
- 可选 int8（torchao）压到 ~2GB，音质需 A/B 验收，默认不开。

---

## 6. 在线引擎与切换

### 6.1 可插拔 provider

| provider | 鉴权 | 流式 | 字幕时间戳 | 说明 |
| --- | --- | --- | --- | --- |
| `volcano`（火山/豆包 TTS） | API Key | 双向流式 WS | 有（字幕/时间戳字段） | 国内延迟最低，首选在线 |
| `azure` | API Key | 有 | 原生 WordBoundary | 时间戳最准 |
| `openai`（OpenAI 兼容 `/v1/audio/speech`） | endpoint + key | 有（PCM 流） | 无 → 用估算法 | 兼容自建/中转 |
| `edge` | 无 | 有 | 原生 WordBoundary | 今天实测 403，默认关闭，可手动开 |

- 统一接口：`synth_stream(text, voice, opts) -> (PCM 24k/48k, marks)`。
- 密钥优先读环境变量（`VOLC_TTS_KEY` / `AZURE_TTS_KEY` / `OPENAI_API_KEY`），其次 `tts_config.json`（会提示明文存储风险）。

### 6.2 `backend=auto` 选择规则

```
有本地模型目录 && CUDA 可用 && free_vram ≥ 2200MB && 未强制 online
    → local
否则 → 第一个「已配置密钥」的在线 provider
否则 → 报错 TTS_UNAVAILABLE（附可操作提示：装模型 or 配 key）
```

- `speak(backend="online")` 时默认 `release_vram=true` → 同一次调用内卸载本地模型（你说的「需要时释放显存」）。
- 本地段级失败（首包超时/OOM/解码异常）→ 该段在线重合成一次，不影响后续段。

---

## 7. 背景音（BGM）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `bgm` | 空 | 文件路径、`assets/bgm/` 下的预设名、或 `none` |
| `bgm_volume` | 0.35 | 0~1，相对语音 |
| `bgm_duck` | 0.25 | 说话时 BGM 降到的比例（-12dB）；静音段恢复 |
| `bgm_fade_ms` | 180 | 起播/结束淡入淡出，避免爆音 |
| `bgm_loop` | true | 循环；首尾 30ms 等功率交叉淡化，无接缝 |
| `bgm_mode` | `speech` | `speech`=随语音起停；`always`=常驻环境音（`bgm_only` 播） |
| `bgm_tail_ms` | 0 | 语音结束后 BGM 再留多久 |

实现：BGM 解码成 48k 单声道 `float32` 常驻内存（wav/flac/ogg 用 `soundfile`，mp3/m4a 用 `pydub+ffmpeg`，本机 ffmpeg 已存在），混音在 worker 的音频线程里按 10ms tick 完成，ducking 用 80ms attack / 250ms release 的增益包络。

预置素材放 `assets/bgm/`（首版可只放 1~2 段免版权轻音乐），用户也可传任意本地路径。

---

## 8. 插件接口变更

### 8.1 新增 action（`tool.yaml` enum 6 个）

| action | 作用 | 关键参数 | 返回 |
| --- | --- | --- | --- |
| `speak` | 合成并播放配音（可同时起动画/BGM/口型） | `text`、`voice`、`backend`、`bgm`、`bgm_volume`、`bgm_duck`、`subtitle`、`animation`/`motion_group`、`lip_sync`、`mode`、`wait` | `job_id`、`backend`、`ttfa_ms`、`segments`、`est_duration_ms`、`playing` |
| `stop_speak` | 停止当前配音（淡出） | `fade_ms` | `stopped` |
| `tts_status` | 引擎/模型/显存/队列/最近延迟 | — | `loaded`、`vram_mb`、`queue`、`last_ttfa_ms`、`last_rtf` |
| `tts_unload` | 立刻卸载本地模型释放显存 | — | `freed_mb` |
| `tts_config` | 读写默认配置（后端/音色/BGM/字幕样式） | `get`/`set` + 字段 | 生效后的配置 |
| `tts_setup` | 建 venv、装依赖、下载模型（可被能力管理调用） | `action=install\|download\|status`、`model_dir` | 进度/结果 |

### 8.2 `speak` 的语义细节

- `mode="replace"`（默认）：打断当前配音，清空 Buffer 与字幕，40ms 淡出后切新内容。
- `mode="append"`：排队，一个说完接下一个（多句连续对白）。
- `wait=false`（默认）：**首段音频已入队即返回**（带 `ttfa_ms`），不阻塞 LLM。
- `wait=true`：等全部播放完（上限 30s），适合需要「说完再做下一件事」的场景。
- 同屏多模型：全局语音锁，默认「后来者打断」；`model_path` 决定声音从哪个桌宠出、字幕画在哪个窗口。
- 超长文本（>500 字）：自动分句排队，返回 `segments` 数与预计总时长。

### 8.3 其他文件改动

| 文件 | 改动 |
| --- | --- |
| `tool.yaml` | 新增 6 个 action + 参数说明（含枚举、默认值、互斥关系） |
| `manifest.yaml` | 版本 → 0.7.0；description 增补配音；`external` 增补模型条目；**不把 qwen-tts 写进 pip** |
| `tool.py` | `_dispatch` 增加 handler；`_ensure_tts_daemon()`；`tts_setup` |
| `pet_window.py` | 命令队列增加 `speak/stop_speak/set_bgm`；AudioEngine 启动；字幕渲染升级；lip sync |
| 新增 `tts/`、`tools/` | 见 §11 |

---

## 9. 配置（`desktop_pet/tts_config.json`）

```json
{
  "backend": "auto",
  "voice": { "local": "Serena", "online": "zh-CN-XiaoxiaoNeural" },
  "language": "Chinese",
  "subtitle": { "mode": "karaoke", "style": "karaoke", "max_chars": 42 },
  "bgm": { "default": "", "volume": 0.35, "duck": 0.25, "fade_ms": 180, "loop": true, "mode": "speech" },
  "lip_sync": true,
  "local": {
    "model_dir": "E:\\AgentClusterModels\\Qwen3-TTS-12Hz-0.6B-CustomVoice",
    "variant": "custom_voice",
    "dtype": "float16", "attn": "sdpa",
    "idle_unload_s": 120, "min_free_vram_mb": 2200,
    "first_packet_timeout_ms": 1200, "max_new_tokens": 512
  },
  "online": { "provider": "volcano", "api_key_env": "VOLC_TTS_KEY" }
}
```

---

## 10. 依赖与安装（重点）

### 10.1 依赖分层

| 层 | 内容 | 装到哪 | 触发方式 |
| --- | --- | --- | --- |
| 插件本体 | PyQt5 / PyQtWebEngine | 宿主 env（现状） | 能力管理自动 |
| TTS 运行环境 | `qwen-tts==0.1.1`、`transformers==4.57.3`、`accelerate`、`librosa`、`soundfile`、`einops` | `desktop_pet/_tts_env`（`--system-site-packages` 复用宿主 torch 2.6，不重装 2.5GB） | `tts_setup` 或能力管理 |
| 模型权重 | Tokenizer-12Hz + 0.6B-CustomVoice（约 3GB） | `model_dir`（默认 E 盘） | 能力管理「下载」按钮 / `tts_setup` |

> 明确不把 `qwen-tts` 写进 `manifest.dependencies.pip`：它会把宿主的 `transformers 5.2.0` 降到 `4.57.3`，属于会伤到其他能力的高风险副作用。安装必须在私有 venv 内完成。

### 10.2 模型作为「能力管理里的依赖」——两个落地选项

**方案 B（推荐，符合你的原话，但要动宿主代码）**

- `dependency_manager.py` 新增 `models` 类型（与 pip/npm/external 并列，向后兼容）：
  ```yaml
  dependencies:
    models:
      - name: "Qwen3-TTS-12Hz-0.6B-CustomVoice"
        source: "modelscope:Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
        dest: "models/Qwen3-TTS-12Hz-0.6B-CustomVoice"
        size_gb: 3.0
        optional: true
  ```
- 依赖面板新增「🧠 模型」一栏：已安装/未安装/大小/路径，提供「下载」「删除」按钮，下载走后台任务与进度（与现有 ASR 模型下载风格一致）。
- 需要改：`backend/core/dependency_manager.py`、`backend/plugins/__init__.py`（快照）、`frontend/.../DependencyInstallPanel.vue`。约 1 人日，且对所有插件通用（以后别的插件也能声明模型）。

**方案 A（零宿主改动）**

- `manifest.dependencies.external` 增加一条：模型名 + ModelScope 链接 + 大小说明；用户点链接自行下载，或调 `tts_setup(action=download)` 一键下载到 `model_dir`。
- 缺点：能力管理面板里只是「下载链接」，没有状态/进度。

> 我的建议：**B 为主、A 兜底**。即使 B 没做，`tts_setup` 也能独立完成安装；做了 B，其他用户就是「点一下下载」。

### 10.3 安装步骤（`tts_setup` 内部）

```
1) python -m venv --system-site-packages desktop_pet/_tts_env
2) _tts_env\python -m pip install --no-deps qwen-tts==0.1.1
3) _tts_env\python -m pip install transformers==4.57.3 accelerate librosa soundfile einops
   （跳过 gradio / sox / onnxruntime：只有 25Hz 与 demo 用得到）
4) modelscope download --model Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice --local_dir <model_dir>
   modelscope download --model Qwen/Qwen3-TTS-Tokenizer-12Hz --local_dir <model_dir>\tokenizer
5) 自检：加载→合成 1 句→卸载，记录加载耗时与峰值显存，写 _tts\install_report.json
```

下载走清华/ModelScope 源；C 盘不足时默认落 E 盘，路径写进 `tts_config.json`。

---

## 11. 目录结构

```
desktop_pet/
├─ tts/
│  ├─ config.py           # tts_config.json 读写 / 默认值 / 密钥解析
│  ├─ segmenter.py        # 中英混排分段（标点、最短/最长、权重）
│  ├─ marks.py            # 段内逐字时间轴 / SRT 生成
│  ├─ protocol.py         # TCP 行头 JSON + PCM 帧协议
│  ├─ daemon.py           # 守护进程入口（venv 运行）
│  ├─ engine_qwen.py      # 本地 Qwen3-TTS（加载/卸载/显存守卫）
│  ├─ engine_online.py    # volcano / azure / openai 适配器
│  ├─ client.py           # worker 侧拉流客户端（取消/重连/统计）
│  ├─ resample.py         # 24k→48k（scipy polyphase）
│  └─ audio_engine.py     # QAudioSink + BGM 混音 + ducking + 播放时钟
├─ tools/
│  ├─ tts_probe.py        # ★ P0 测试脚本
│  ├─ tts_setup.py        # 建 venv / 装依赖 / 下模型（CLI，可被 tts_setup action 调）
│  └─ bench_latency.py    # P2：多句压测（TTFA/RTF/显存）
├─ assets/bgm/            # 预置背景音
├─ assets/voices/         # 克隆参考音频（仅授权素材）
├─ _tts_env/              # 私有 venv（不进 git）
├─ _tts/                  # daemon.json / 日志 / 安装报告
└─ tts_config.json
```

---

## 12. 分阶段与验收

| 阶段 | 内容 | 验收标准 |
| --- | --- | --- |
| **P0（你确认后立即做）** | `tools/tts_probe.py`：本地合成 + 字幕文件 + 预览窗口 + 性能报告 | 见 §13；你听/看后判定音色、语速、字幕同步、BGM 是否达标 |
| P1 | 插件集成：`speak/stop_speak`、AudioEngine、BGM、字幕逐字、动画同播、内置配音互斥 | 桌宠说话时口型/动作/字幕/背景音同时成立；`stop_speak` 立即静音；关字幕开关生效 |
| P2 | 在线 provider（火山/Azure/OpenAI 兼容）、显存守卫自动切换、`tts_unload` 验证 | 拔掉本地模型后可无缝走在线；online 调用后显存回落 0 |
| P3 | 能力管理 `models` 依赖 + 面板下载/删除、CUDA Graph、流式文本输入、多声线 | 换机器/新用户能一键装；首声再降 30%+ |

---

## 13. P0 测试脚本规格（下一个交付物）

**命令**

```bat
F:\anaconda3\python.exe tools\tts_probe.py ^
  --backend local ^
  --model-dir E:\AgentClusterModels\Qwen3-TTS-12Hz-0.6B-CustomVoice ^
  --voice Serena ^
  --text "今天天气不错，我们一起出去玩吧。不过要先把作业写完，不然会被骂的。" ^
  --bgm assets\bgm\calm_loop.wav --bgm-volume 0.35 --bgm-duck 0.25 ^
  --preview
```

**产出（默认 `workspace/tts_probe/<时间戳>/`）**

| 文件 | 内容 |
| --- | --- |
| `voice.wav` | 48kHz 单声道，含 BGM 的最终混音（另出 `voice_dry.wav` 纯配音） |
| `voice.srt` | 段级字幕，可直接对照播放器验证 |
| `marks.json` | 段 + 逐字时间轴（插件后续直接吃这个结构） |
| `report.json` | 冷加载耗时、每段 TTFA、RTF、总时长、峰值显存（torch 口径 + `nvidia-smi` 口径）、采样率、段数 |
| `preview` 窗口 | 用插件同款字幕画法：底部胶囊 + 逐字高亮 + 当前时间/播放进度，实时对照听感 |

**脚本内部结构**（刻意与插件同构，避免「测试能用、集成翻车」）

1. `segmenter` 分段 → 2. `engine_qwen`（懒加载，可 `--cold`）→ 3. `marks` 生成 → 4. `AudioEngine`（真实 `QAudioSink` 播放，不是 `sounddevice`）→ 5. 预览窗口（同 `_paint_subtitle`）→ 6. 写文件 + 打印报告。

**判定指标**

- 段级字幕误差：`|marks.start_ms - 实际播放头|` ≤ 20ms（样本级应≈0）。
- 字级高亮误差：≤ 120ms（中文按音节估算，主观可接受）。
- 热态首声 ≤ 900ms（2060）；RTF ≤ 1.0（能实时跟上）。
- 峰值显存 ≤ 3.4GB；空闲卸载后 ≤ 200MB。
- BGM：语音段 BGM 明显下压、语音结束平滑恢复，无爆音/断点。

---

## 14. 风险与取舍

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| `qwen-tts` 把 transformers 降到 4.57.3 | 宿主其他能力异常 | 私有 venv；manifest 不声明该 pip |
| 6GB 显存同时跑 Live2D + TTS | OOM / 掉帧 | 空闲卸载、显存守卫、`backend=online` 释放、不推荐 1.7B |
| 上游默认非真流式 | 长句首声偏慢 | 句级流水线；P3 接 CUDA Graph 补丁 |
| 24k→48k 重采样 | 额外 1~3ms + 潜在音质损失 | `resample_poly` 高质量；或设备直出 24k |
| Windows 共享模式音频延迟 | 首声 +60~100ms | `setBufferSize(60ms)`；P3 可试 WASAPI 独占 |
| `edge-tts` 403 | 零配置在线方案不可用 | 在线改为火山/Azure/OpenAI 兼容，key 从环境变量读 |
| C 盘 16GB | 装不下模型 + venv | `model_dir` 默认 E 盘；`tts_setup` 校验磁盘 |
| 声纹克隆滥用 | 伦理/法律风险 | 仅允许本人或已授权参考音频；文档明示 |
| 字幕过长挡脸 | 观感 | 2 行截断、最多 42 字、可关 |

---

## 15. 需要你确认的 4 件事

1. **本地模型变体**：先只装 `CustomVoice`（预设音色，零配置，建议 P0 用 `Serena`）？还是直接上 `Base`（3 秒克隆角色音，需要你提供参考音频 + 文本）？还是两个都装（磁盘 ~5GB）？
2. **模型依赖的落地**：按 §10.2 改宿主（`models` 类型 + 面板下载按钮，其他插件也受益），还是先只做 `external` + `tts_setup` 一键下载？
3. **在线 provider**：先接哪个（火山豆包 / Azure / OpenAI 兼容）？有没有现成 key？没有的话 P2 先留接口不联调。
4. **P0 试听文本/音色**：默认用 `Serena` + 上面那段两句文本够不够？想听特定语气（撒娇/冷淡/吐槽）可以给一句参考文案。

> 确认后我先交付 P0 的 `tools/tts_probe.py`（含 `tts_setup.py` 的 venv/模型安装入口），跑出 wav + 字幕 + 报告 + 预览窗口，你验收效果再进 P1。

---

## 16. P0 实测结果（2026-11-14，RTX 2060 6GB）

### 16.1 已达成

| 项 | 实测 |
| --- | --- |
| 段边界同步误差 | **0 ms**（按真实 48k 样本数推算） |
| 峰值显存 | **2213 MB**（预算 ≤3.4GB，达成） |
| 音频规格 | 48k 单声道 PCM，与插件播放层一致 |
| 字幕逐字高亮 | 已出帧验证（胶囊、金色 `#FFD24A`、42 字上限、3 行内） |
| 播放链路 | QAudioSink push 模式已验证：正常起播、时钟取自 `processedUSecs()`、正常 `finished` |
| BGM | 内置垫乐 + ducking 已混入 `voice.wav`（RMS 0.0707→0.0741，未削波） |

### 16.2 未达成（这台机器上做不到）

| 目标（§13） | 实测 | 差距 |
| --- | --- | --- |
| 热态首声 ≤ 900 ms | 冷启 31.5 s；热态约 14 s（= 首句合成时间） | 15 倍以上 |
| RTF ≤ 1.0 | **5.6 ~ 6.1** | 6 倍 |

### 16.3 定位到的三个硬约束

1. **fp16 不可用**：开启采样（top-k / multinomial）立即 `device-side assert`（`TensorCompare.cu:110`）；
   关闭采样能跑但不吐 EOS（生成到 1024 token，录出 81 s 尾音）。→ 只能 bf16。
2. **bf16 在 Turing 是模拟实现**：同卡 matmul 实测 fp16 626 / fp32 247 / **bf16 105 GFLOP/s**；
   SDPA 退化为 `_scaled_dot_product_attention_math`（mem-efficient kernel 不支持 sm75 的 bf16）。
3. **本机 WDDM 单次 kernel 调度 ~0.28 ms**（正常 0.02~0.03 ms），
   逐帧几百次小 kernel 的自回归结构被放大 5~10 倍。profiler：一次 40 token 生成里
   `scaled_dot_product_attention` 调用 3530 次、`linear` 25263 次。

### 16.4 据此修正的方案

- §13 的延迟指标在 2060 上**不成立**，改为按 provider 分档：
  | 路径 | 首声 | 说明 |
  | --- | --- | --- |
  | 在线 Edge-TTS | <1 s（网络正常时） | 实时对话主路径；当前 403 待解决 |
  | 本地 0.6B 热态 | ~10~14 s/句 | 仅作离线、兜底、预生成 |
  | 本地命中缓存 | ~0 | 预生成台词库 |
- 句级流水线仍保留：它把"整段等多长"变成"等首句多长"，但没有改变单句 RTF。
- P3 的 CUDA Graph / 流式文本提前到 P1.5 候选；若 Graph 能把 kernel 调度摊薄，本地才有实时可能。
- 在线 provider 的优先级上调：**Edge 403 必须先解决**（校时/代理），否则实时路径为空。


## 17. 本地链路联调结果（2026-11-14，插件侧实机验证）

### 17.1 已验证跑通的部分

| 环节 | 验证方式 | 结果 |
| --- | --- | --- |
| daemon 协议 | `tools/tts_daemon.py` + `_tts_env` 私有 venv，TCP 一问一答 | `READY` → `{ev:ready}` → 逐段 `{ev:seg}` + 裸 PCM → `{ev:end}` 正常 |
| 本地合成 | Qwen3-TTS 0.6B-CustomVoice / Serena / bf16 | 正常出音频，24 kHz 原样进引擎，内部重采样到 48 kHz 播放 |
| 播放 | QtMultimedia push 模式（`QAudioOutput`） | 正常起播，clock 取自 `processedUSecs()`，播完正常 `finished` |
| 段边界 | 由真实样本数推导 | **0 ms** 误差，两段连续播无空隙 |
| 逐字字幕 | 真机截图（`_workers/tts_sub_crop.png`） | 整句白字 + 已念字金色 `#FFD24A`，字位与语音对齐 |
| BGM | 内置垫乐 `synth_soft_pad` + ducking | 正常混入，不削波 |
| 连续第二句 | 同一进程连播两次 | 两句都能起播（此前只播第一句的 bug 已修） |
| 预热 | `{op:"preload"}` + 极短合成冒烟 | 插件启动即后台加载，warm 后首句不再吃 load 的十几秒 |
| 动作同播 | `speak` 前先推 `play_animation(mute=True)` | 内置配音静音、TTS 接手，避免两套声音打架 |

### 17.2 联调时踩到的三个坑（都已修，写下来免得重犯）

1. **进程凭空消失 `0xC0000409`（无 traceback）**
   根因不是音频、也不是跨线程，而是 **Python 异常逃出了 Qt 槽函数**：`char_marks()` 返回的是
   `[{ch,t0,t1}]` 字典列表，服务里却当成"毫秒整数列表"比较，`dict <= float` 抛 `TypeError`；
   PyQt 对槽内未捕获异常直接 `abort()`，进程连栈都不打。修法两条：按 `t0` 取值 + `_on_tick` 全程
   `try/except` 兜底（异常只写进 `status().error`，不再杀进程）。
2. **只播第一句，第二句没声音**
   `AudioEngine._on_tick` 播完会 `timer.stop()`，而 `enqueue()` 没有重新 `start()`，
   新一段语音堆在 `_voice` 里永远不泵。修法：`enqueue()` 里 `if not self._timer.isActive(): start()`。
3. **第二句起字幕/时钟漂移**
   `QAudioOutput.processedUSecs()` 是设备启动以来的**累计值**，`reset()` 不清零。
   修法：起播时记 `_clock_base`，`clock_ms()` 返回差值，每句都从 0 开始。

另外把字幕策略从"按段取当前段文本"改成**整句拼接 + 全局进度高亮**：段与段交界处不会出现
"上一段已消失、下一段还没上"的空档，读感更接近自带配音。

### 17.3 实测数据（本机 RTX 2060 6GB，空闲程度不同差别很大）

| 项 | 数值 |
| --- | --- |
| 冷加载模型 | 10.8 s（GPU 空闲）~ 40.5 s（GPU 被占用） |
| 预热总耗时 `warm_ms` | 16.0 s ~ 51.0 s（含一次冒烟合成） |
| 峰值显存 | **2059 MB**（warm 后稳定，`unload` 可全退） |
| 热态首声（10 字） | 8.4 ~ 8.8 s |
| 热态首声（17~24 字） | 13.6 ~ 26.2 s |
| 单句 RTF | 约 5.6 ~ 6.5 |
| 播放 / 字幕 / BGM | 全程无崩溃、无丢帧 |

结论不变：**这台卡上本地模型做不到实时**，但"动画 + 配音 + 逐字字幕 + BGM 同播"这条链路是通的，
适合离线预生成、短句应答、以及在线失败时的兜底。

### 17.4 配置项（`tts_config.json`，缺省值见 `tts/config.py`）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `local.model_dir` | `E:\AgentClusterModels\Qwen3-TTS-12Hz-0.6B-CustomVoice` | 模型目录（面板下载后改这里） |
| `local.dtype` | `bfloat16` | 该卡只能 bf16（fp16 采样即崩） |
| `local.idle_unload_s` / `daemon_exit_s` | 120 / 600 | 闲置卸载显存 / daemon 自杀 |
| `prebuffer_ms` | 320 | 起播前缓冲 |
| `audio_backend` | `qt` | `qt`（零依赖）/ `sd`（PortAudio，回调式，可选） |
| `prewarm` | `true` | 桌宠启动即后台预热本地模型 |
| `bgm` | `{volume:0.35, duck:0.25, mode:"speech"}` | 说话时自动压低 |
| `voice` | `{local:"Serena", online:"zh-CN-XiaoxiaoNeural"}` | 预设音色。**跨后端自动归一**：含 `Neural` 的名字只给在线，其余只给本地，传错了自动落到该后端默认音色（曾因此报 `Invalid voice 'Serena'`） |

### 17.5 调用方式

- 插件工具：`action=speak`，参数 `text / voice / backend(local|edge) / speed / bgm / bgm_volume / bgm_duck / subtitle / lip_sync / animation / motion_group / mode / wait`
- 本地 HTTP：`POST http://127.0.0.1:<控制端口>/tts`，`{"op":"speak|stop|status|unload|config", ...}`
- 状态：`GET /tts` 返回 `clock_ms / ttfa_ms / segments / segments_timeline / sub / warm / audio`

### 17.6 仍未完成（按优先级）

1. ~~在线 Edge-TTS 403~~ **已解决（2026-11-14）**：根因是 7.2.8 只发 v1 版 `Sec-MS-GEC`（`Sec-MS-GEC-Version=1-143.x`），
   微软已改要 v2 令牌。把 `edge-tts` 升到 **7.3.1**（插件私有 venv + 宿主 python 各装一份，因为在线引擎跑在桌宠进程里）后实测：
   `ttfa = 1045 ms`、`clock` 正常推进、字幕逐字高亮正常（真机截图 `_workers/tts_edge_1.png`）。
   在线音色默认 `zh-CN-XiaoxiaoNeural`，Edge 对中文不返回 WordBoundary，逐字时间轴走估算分配（段边界仍是真实样本、0 误差）。
2. `tts_play_motion` / `tts_lip_start` / `tts_lip_stop` 仍是 stub：动画靠 `tool.py` 先 `play_animation`，
   **口型未驱动**（下一步接 Cubism `ParamMouthOpenY` + 每 20 ms PCM RMS）。
3. 宿主 `models` 依赖类型（`dependency_manager.py` + `DependencyInstallPanel.vue`）未加，模型目前靠手工放 `E:\AgentClusterModels`。
4. `tool.yaml` 的 6 个新 action 还没走真实工具链调用验证（HTTP 层已验）。
5. `assets\bgm\*` 素材未落盘，只有内置垫乐。

### 17.7 音色怎么选（写给调用方，不是给代码猜）

两套音色名**互不通用**，必须和 `backend` 配套；`tool.yaml` 的 `voice` 参数里已写全规则并给出 15 个 enum，
智能体按描述选即可，不依赖服务端猜：

| backend | 音色名体系 | 女声 | 男声 | 默认 |
| --- | --- | --- | --- | --- |
| `local` | Qwen3-TTS 预设（9 个） | Serena（温柔知性）/ vivian（清亮）/ ono_anna（轻快日系）/ sohee（柔和韩系） | ryan（沉稳叙事）/ aiden（年轻明亮）/ dylan（松弛口语）/ eric（磁性成熟）/ uncle_fu（厚重中年） | Serena |
| `edge` | Azure 音色 id（必须 `Neural` 结尾） | zh-CN-XiaoxiaoNeural（自然亲和）/ zh-CN-XiaoyiNeural（年轻活泼）/ zh-CN-liaoning-XiaobeiNeural（东北口音） | zh-CN-YunxiNeural（清朗少年）/ zh-CN-YunjianNeural（浑厚解说）/ zh-CN-YunyangNeural（端正播报） | zh-CN-XiaoxiaoNeural |

选择规则：

1. **先定 backend，再在该后端清单里挑**；不要自造别名，也不要把本地名配给 edge。
2. 按角色气质挑：温柔陪伴 → `Serena` / `zh-CN-XiaoxiaoNeural`；活泼俏皮 → `vivian` / `zh-CN-XiaoyiNeural`；
   沉稳叙述 → `ryan` / `zh-CN-YunjianNeural`；少年感 → `aiden` / `zh-CN-YunxiNeural`。
3. **不传 `voice` = 该后端默认**（最安全）。同一个角色要跨后端保持一致，就在两边各显式指定一次。
4. 运行时想确认清单与默认值：`action=tts_status` 返回的 `voices.local` / `voices.online` / `voices.default`。
   该字典由 `tts/service.py` 的 `LOCAL_VOICES` / `ONLINE_VOICES` 提供，是唯一事实来源。

兜底行为（`_resolve_voice`）：名字带 `Neural` 只认在线，不带只认本地；传错后端不报错，**静默落到该后端默认音色**，
所以"没声音"类故障不会由音色名引起——但这层兜底只防崩溃，**不代表可以乱传**，选音色仍应由调用方按上表决定。

### 17.8 字幕与 BGM 的第二轮调整（同日）

**字幕：只显示正在说的这一句**

- 原先是"整段全文拼接 + 全局进度高亮"，句子长了会把多句挤在一个胶囊里。现改为**按 clock 找当前段，只画这一句**；
  段与段之间不留空档——上一句说完、下一句还没开始时仍显示上一句，直到下一段接上。
- 整段语音播完后字幕**停留 3 秒**再消失（`subtitle.hold_ms`，默认 3000）。口型立刻停，字幕留白。
- 实测日志（在线，三句）：
  `3.62s→第一句只显示这一行。` / `5.88s→第二句应该替换掉上一句。` / `…→第三句说完之后停三秒。`，
  `finish` 后 `+3.0s` 清屏，`+3.66s` 查 `sub` 为空。

**这轮踩到的两个坑（都值得记住）**

1. **字幕 tick 把"停留后清除"又画回去了**：`_on_audio_finished` 里用 `QTimer.singleShot(3000)` 清字幕，
   但服务侧 30ms 一次的 `_tick` 仍在跑，每次都把最后一句重新 `sub_karaoke` 上去，看起来像"永远不消失"。
   修法：播完先 `_tick.stop()`，且 `_tick_body` 增加"已播完就不再重画"的早返回。
2. **tick 只在 `open()` 启动过一次**：`open()` 在引擎已存在时直接 return，第二句不会再起搏。
   修法：`speak()` 里检测 `_tick.isActive()`，不活跃就补 `start()`。

**BGM：换成真实轻音乐素材**

| 预设名 | 文件 | 内容 | 时长 |
| --- | --- | --- | --- |
| `piano` / `light`（默认推荐） | `assets/bgm/light_piano.wav` | 轻柔钢琴分解和弦，安静不抢人声 | 30 s |
| `ambient` / `soft` | `assets/bgm/soft_ambient.wav` | 柔和弦乐氛围垫，更"空" | 30 s |
| `pad` | 内置合成（`synth_soft_pad`） | 旧版正弦垫乐，仅兜底 | 8 s |

- 之前"杂乱"的两个来源：① 合成 pad 本身由正弦叠加 + 噪声构成，有拍频与噪感；
  ② 循环时直接接缝、没有淡化，30 s 一到就"啪"一下。现在换真实素材，并在 `set_bgm` 里做
  **40 ms 等功率交叠**把首尾接起来，循环听不到断点。
- 默认音量降到 **0.30**（原 0.35），说话时下压到 0.25；`piano` 这类轻音乐建议 `bgm_volume=0.25~0.35`。
- 素材是 40 kHz 单声道 wav（各约 4.8 MB），入引擎时重采样到 48 kHz，与语音走同一路混音。

### 17.9 BGM「早进晚退、渐入渐出」（同日第三轮）

**为什么要改**：之前 BGM 与语音同起同停，开头是硬切、结尾也是硬切，听感像一个开关被按了两下；
而且短句时音乐还没铺开就没了。

**现在的时序**

```
设备起播 ──(preroll 1.2s，只有 BGM，渐入 0.5s)──▶ 人声开始 ──▶ 人声结束 ──(tail 1.6s，渐出)──▶ 收工
```

| 配置键 | 默认 | 作用 |
| --- | --- | --- |
| `bgm.preroll_ms` | 1200 | 人声前先垫的纯音乐时长（渐入段也在里面） |
| `bgm.tail_ms` | 1600 | 人声结束后继续播放的尾奏时长（渐出段） |
| `bgm.fade_ms` | 500 | 淡入/淡出时长（线性，等功率接缝单独处理） |

**实现要点（两个容易错的点）**

1. **preroll 不是"延迟起播"，而是"往语音缓冲前面垫静音"**：第一段 PCM 入队时在开头插 `preroll_ms` 的零样本，
   设备立刻起播、这段时间里只有 BGM 出声，人声按点接上。这样音频设备不需要等待，也不会有"先空转再出声"的抖动。
2. **字幕时钟必须换成"人声起点"**：设备时钟从起播那一刻算，但人声晚了 1.2s；如果字幕仍用设备时钟，
   字幕会整整提前 1.2s 出现（短句甚至会提前说完）。新增 `AudioEngine.voice_clock_ms() = clock_ms() - preroll`，
   `_tick_body` 全部改用它。
3. **尾奏不该拉长字幕**：`hold_ms`（停留 3 秒）现在按"人声结束"计，所以清屏定时 = `hold_ms - tail_ms`，
   保证体验上是"说完停 3 秒"，而不是"尾奏 1.6s + 再停 3s"。

**实测（在线，`bgm=piano`，句长 6.24s）**

| 事件 | 时刻（相对 POST） |
| --- | --- |
| 设备起播（BGM 独奏开始，voice clock 仍为 -50ms） | 6.09s |
| 人声开始（voice clock 140ms，字幕同步出现） | 6.30s |
| 人声结束（voice clock 6240ms） | ~12.3s |
| speaking 转 false（= 人声结束 + 尾奏 1.58s） | 14.34s |
| 字幕清空（= 人声结束 + 3.09s） | 15.39s |

三个数字互相咬得住：`preroll ≈ 1.2s`、`tail ≈ 1.58s`、`人声结束→字幕消失 ≈ 3.0s`。
#### 17.9.1 本轮顺带修掉的两个真 bug

用「说一半再插一句」的实测暴露出来的，都不是 BGM 本身的问题，但会直接毁掉体验：

1. **欠载后音频设备不再被唤醒 → 字幕和 speaking 一起卡死**。
   引擎在语音缓冲吃空时会 `suspend()` 设备来冻住时钟（防止 `processedUSecs` 空转）。
   但下一段语音到货时没有任何人 `resume()`，`processedUSecs` 从此停在原地：
   `speaking` 永远 true、逐字高亮永远不动。修法：`_on_tick` 每次发现「设备 suspended 且还有数据/开场静音」就 `resume()`
   （`bgm.mode=always` 时只要 BGM 在放也 resume）。
2. **`mode=replace` 时上一句的合成线程会污染新句子**。
   `stop()` 只是置位 `_stop_flag`，旧线程还在往外吐段；新句子把 flag 清掉后，
   这些旧段就被当成新句子的音频入队——实测「第二句只要 2.7s，却播了 8.1s」。
   修法：加会话代号 `_gen`，每次 replace 自增，线程带着自己的 `gen` 投递，
   `_on_seg/_on_end/_on_err` 只接受 `gen` 等于当前值的消息。

**实测（在线，打断替换）**

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| 第二句 audio.duration_ms | 8136（混进了第一句剩余段） | **2736**（只有自己） |
| 收尾时刻 | 9700ms（多播 5s 别人的音频） | **4320ms = 2736 + 尾奏 1584** |

### 17.10 BGM 素材：6 条真实下载的轻音乐（同日第四轮）

用户明确要求「不要 AI 生成，去网上下载」。6 条素材全部来自 Wikimedia Commons 的现成录音，
许可为 CC0 / Public Domain / CC BY 4.0，署名与直链写在 `assets/bgm/LICENSES.md`，
每条另有 `assets/bgm/_sources/<名字>.json` 记录下载地址与许可字段。

| 预设名（含别名） | 文件 | 曲目 / 来源 | 许可 | 时长 |
| --- | --- | --- | --- | --- |
| `piano` / `light` / `轻音乐` / `钢琴` | light_piano.wav | Chopin Nocturne Op.15 No.1（Musopen 录制） | CC0 | 48.0s |
| `ambient` / `soft` / `氛围` / `空灵` | soft_ambient.wav | Scott Buckley《Sleep》 | CC BY 4.0 | 48.0s |
| `cafe` / `lofi` / `咖啡馆` / `慵懒` | cafe_lofi.wav | Kevin MacLeod《Study And Relax》 | CC BY 4.0 | 50.0s |
| `night` / `夜晚` / `忧郁` | night_soft.wav | Chopin Nocturne No.20 in C# minor | CC0 | 50.0s |
| `nature` / `自然` / `森林` / `环境` | nature_calm.wav | ezwa《Birds singing in garden》 | Public Domain | 49.5s |
| `bright` / `活泼` / `轻快` / `节日` | bright_playful.wav | Mary Ruth Everett《Elmore》(1921) | Public Domain | 52.0s |

统一后处理：48kHz / 单声道 / 16bit PCM、峰值 ≤ -3dBFS、**直流偏移归零**、
响度对齐到 RMS ≈ 0.085（`cafe_lofi` 因峰值顶到 -3dBFS 停在 0.090，`nature_calm` 的鸟鸣瞬态也只能到 0.059，
其余四条都落在 0.083~0.085），单文件 ≤ 5MB。

**为什么必须去掉直流偏移**：`night_soft` 原始段有 +0.019 的直流分量，循环播放等于给扬声器加了个恒定偏压，
接缝处会「噗」一声、还白白吃掉 1.5% 的动态范围——这是"杂乱"听感的来源之一，不只是拍频。

**素材缺失时的退让链**（`tts/bgm.py`）：指定预设 → `light_piano` → 按固定顺序找任意存在的真实素材 → 合成垫乐。
`manifest.json` 里的「预设名 → 文件名」会在启动时并入预设表，新增素材不必改代码。

## §17.11 本地 TTS「一直没有声音」的根因与修复（2026-11-14 实测）

### 症状
`backend=local` 时状态一直 `speaking=True`、`segments=0`、`clock=-1`、`ttfa` 空、无 error，等 70s 以上也没有声音。

### 三层根因（叠加，缺一不可）

| # | 根因 | 现象 | 修复 |
|---|------|------|------|
| 1 | `_tts_env\Scripts\python.exe` 是 **launcher 型 venv 入口**：它启动后立刻再 exec 一个真解释器，自身退出 | `Popen` 句柄 `poll()` 立刻为「已退出」，`_ensure_proc` 每次说话都判 daemon 已死并重新拉起 → 同进程越攒越多 daemon（实测 4 个），每个都背一份模型抢 6GB 显存，speak 连到的那个永远在冷加载 | 存活判断改为「有端口 + `ready` 标记」；从 `READY` 里取真实解释器 `pid` 存 `_daemon_pid`，清理时按这个 pid `taskkill`（launcher 句柄杀不干净） |
| 2 | 同进程多个 `TTSService` 实例各持一套 daemon 状态 | 每个实例各起一个 daemon | 状态提到模块级 `_DAEMON_STATE` + `_DAEMON_LOCK`，进程内单例 |
| 3 | `idle_unload_s=120` | 模型 2 分钟就被卸载，用户隔一会儿再说话几乎每次都在冷加载 | 改 `idle_unload_s=900` / `daemon_exit_s=1800` |

### 附带修复

- `local_stall_s=45`：socket 超时改成**事件间隔**而不是总时长。半天不出一段就判定生成卡死，`_kill_daemon()` 重建（daemon 卡在 generate 里时断连接不会退，会把显存一直占着），并把错误落到 `state.error`，不再永远「正在说」。
- `max_new_tokens` 1024 → 512，限制不收敛的长尾生成。
- `min_chars=4 / max_chars=28` 传进 daemon，让第一段更短、首声更早。
- 修了两处被误删的 `_inbox` / `_tick` 初始化（现在统一在 `_init_qt_parts()`）。

### 真机实测（RTX 2060 6GB，桌宠渲染同时占显存）

| 项 | 数值 |
|---|---|
| 预热 `load_ms` | 22,838 ms（另一次冷加载 71,098 ms） |
| 预热 `warm_ms` | 41,843 ms |
| 模型显存 | 2,068 MB |
| 短句「你好，我是符玄。」(8 字) | `ttfa=24,654 ms`、1 段、`duration=1,583 ms`、`marks=8`、说完 3s 后字幕清除 |
| 显存占用（桌宠渲染 + 模型同时） | 5,898 / 6,144 MiB |

### 结论（重要）

本地链路**已经能出声**，但 RTF 在「桌宠渲染同时吃显存」的现实条件下会掉到 15 左右：8 个字要 24.6s 才出声，20 字以上长句会因显存挤爆而卡到看门狗超时。这与此前 §16 的硬结论一致——sm75 上 kernel 发射是硬墙，降精度（fp16/bf16）救不了。

因此**默认策略仍应是 `backend=auto`：在线 Edge 优先（实测 `ttfa≈1.0s`），本地作为无网/隐私兜底**，且本地只适合短句。

## §17.12 本地引擎换成 Piper 轻量流式（2026-11-14）

### 为什么换

§17.11 已经证明：本地 Qwen3-TTS-0.6B 链路通了，但 RTF 15（桌宠渲染同时吃显存时），8 个字要 24.6s 才出声。这是 sm75 上的硬墙，不是配置问题。用户要求"更小更轻量、能流式生成"。

### 候选对比（本机 RTX 2060 6GB + 桌宠渲染同时跑）

| 方案 | 体积 | 运行位置 | 首块 | RTF | 中文 | 结论 |
|---|---|---|---|---|---|---|
| Qwen3-TTS-0.6B | ~1.2GB 权重 | GPU，2.2GB 显存 | 31.5s（冷） | 6~15 | 9 音色，音质最好 | 保留为可选引擎，只适合短句 |
| **Piper（zh_CN-huayan-medium）** | **63MB** | **CPU，零显存** | **639ms** | **0.17** | 单音色，中等音质 | **选为默认** |
| Kokoro-82M ONNX | ~340MB | CPU | — | ~0.3 | 多音色 | 备选，中文支持要装 misaki[zh] |
| MeloTTS | ~50MB | CPU | — | ~0.3 | 中文效果好 | 备选，依赖较重 |

### 集成方式

| 层 | 改动 |
|---|---|
| `tts/engine_piper.py`（新增） | `PiperTTSEngine`：接口与 `QwenTTSEngine` 完全一致（`load/unload/status/synthesize/stats/sample_rate`），daemon 协议一行没改。内部把 `voice.synthesize(text)` 的 chunk 攒成 int16 PCM，首尾各剪 40ms 静音 + 4% 阈值的头尾空白 |
| `tools/tts_daemon.py` | 新增 `--backend qwen|piper`、`--piper-model`；`_get_engine` 按 backend 分支；`ready` 事件的 `model` 字段改为 Piper 文件名 |
| `tts/service.py` | `_ensure_proc_inner` 透传 backend / piper_model；`_resolve_voice` 在 piper 引擎下把任何名字归一成 `huayan`（qwen 预设名不会再把 Piper 弄崩）；`LOCAL_VOICES` 首项改成 huayan |
| `tts/config.py` | `local.engine="piper"`、`local.piper_model=<插件>/assets/piper/zh_CN-huayan-medium.onnx`（可用 `DESKTOP_PET_PIPER_MODEL` 覆盖） |
| `tool.yaml` | `voice` enum 增 `huayan`；`backend` / `voice` 描述改成"local 两条引擎、音色名不通用" |
| 素材 | `assets/piper/` 放 63MB onnx + json（从 hf-mirror 下载，`E:\AgentClusterModels\piper` 留了同一份） |

### 顺带修掉的第三个 bug：tick 起不来

`_on_audio_finished` 里 `_tick.stop()`，下一句 `speak()` 又在 **HTTP 线程**里调 `_tick.start()`。Qt 定时器只能在所属线程启停——跨线程调用静默失效，于是 tick 不跑、`_drain()` 不执行，段全留在队列里：状态就是 `ttfa` 有值、`segments=0`、`clock` 冻结、`speaking` 永远 True。

修法：`TTSService.wake = pyqtSignal()`，`__init__` 里 `wake.connect(self._tick.start, Qt.QueuedConnection)`，`speak()` 和 `open()` 都改成 `wake.emit()`，由主线程真正启动定时器。

### 真机实测（同一台机器，桌宠同时渲染）

| 场景 | 首声 ttfa | 段数 | 音频时长 | 端到端(含 3s 停留) |
|---|---|---|---|---|
| 短句「你好，我是符玄。」 | **171ms** | 1 | 1,376ms | 4.49s |
| 长句 36 字 | **253ms** | 4 | 7,700ms | 9.41s |
| 长句 + BGM piano | **211ms** | 2 | 4,057ms | 7.32s（停留自动扣掉 1.6s 尾奏） |
| 预热 | load 4,180ms / warm 4,881ms / vram **0MB** | | | |

前三项都是"边生成边播"：长句 4 段各自合成完就入队，段间无空档。

### 音质与许可

- 音质是"中等"（medium 模型、22,050Hz、单女声），明显不如 Qwen3-TTS 的 9 个预设音色；换来的是 0.2 秒出声、零显存。
- 许可：Piper 代码 MIT；`zh_CN-huayan-medium` 声源来自 `github.com/PlayVoice/HuaYan_TTS`，模型卡标注 **License: Unknown**。自用没问题，**商业分发前需要确认授权**。
- 想回高音质：`tts_config.json` 里把 `local.engine` 改成 `qwen`，音色名同时换回 Serena 那套。

### 现在的默认策略

`backend=auto` 仍是在线 Edge 优先（首声 ~1s、音质好）；`backend=local` 现在是 Piper（离线可用、零显存、0.2s 出声、音质中等）。两条路都不再需要等几十秒，也不再有"一直没声音"。

## §17.13 BGM 听得见了 + TTS 自动对口型（2026-11-14）

### 一、BGM 太小声：三层原因

| 层 | 原来 | 现在 | 说明 |
|---|---|---|---|
| 音量 | `bgm.volume=0.30` | **0.85** | 素材 RMS≈0.085，0.30 → RMS 0.026（约 −32dBFS），本身就偏轻 |
| 说话时压低 | `duck=0.25` | **0.60** | 0.30×0.25=7.5%，说话那几秒 BGM 实际上等于消失 |
| 净结果 | RMS≈0.006（约 −44dBFS） | **说话时 RMS≈0.05，安静段 ≈0.07** | 提升约 18dB，人耳清楚可闻 |
| 硬编码 | `tool.py` 与 HTTP 层各自带 0.35/0.25 默认，改配置不生效 | 全部改成 `None` → 读 `tts_config.json` | 一个入口，改配置就生效 |
| 素材前奏 | `light_piano` 前 **3.2s 几乎无声**（RMS 0.0004） | 起播自动跳过近静音前奏 | 否则"早进"进的是安静，听着像没开 BGM |

实测（`[ae] rms bgm=…` 每 400ms 一条，sounddevice 输出前的最终混音）：
- 垫场阶段：bgm 0.033~0.043（duck=1.00）
- 说话阶段：bgm 0.046~0.109，人声 0.09~0.34，合成 0.10~0.33（duck 0.60）
- 结论：BGM 比人声低 6~12dB，听得清又不盖字。

调音入口：`tts_config.json` → `bgm.volume`（0.85）/ `bgm.duck`（0.60）。想更响就把 volume 提到 0.95，想更弱降到 0.6。

### 二、自动对口型

| 环节 | 实现 |
|---|---|
| 振幅来源 | `AudioEngine.lip_level()`：取播放头前后各 25ms 窗口的 RMS（往后那半是"马上要发的音"，所以嘴会比声音略早开几毫秒）；`(rms-0.012)*5.6` 映射到 0~1，0.19 张到头 |
| 垫场/播完 | `_lead_left>0` 时返回 0（BGM 垫场期间必须闭嘴）；广播头越过语音末尾自然为 0 |
| 推送 | `TTSService._tick_body()` 每 30ms 调 `pet.tts_lip_level(v)`；变化 <0.04 不推，省一半 IPC |
| 写入 | `setPetCommand({type:'setParams', params:{ParamMouthOpenY:v, ParamMouthForm:-0.15+0.45v}, smoothMs:0})`，走既有通道，renderer 的 `applyParam` → `setParameterValueById`；模型没有该参数会被安全忽略 |
| 生命周期 | `tts_lip_start()` 张嘴归零并开跟随，`tts_lip_stop()`（播完/打断）在 90ms 内闭回 0 |
| 踩的坑 | 这三个方法属于 **PetWidget**；一开始把 `page` 挂在窗口外套对象上，`tts_lip_level` 拿不到 page → 静默 return False。改成 `pet._cmd_page = page` 才通 |

验证：同一句里连拍 6 帧（间隔 380ms），嘴型依次为「张→闭（眨眼）→小张→闭→张→半张」，与音频包络一致；日志 `[lip] #1 y=0.00 #3 y=0.99 #5 y=0.13 …`。

### 三、现在的完整链路

`speak` → 分句 → Piper 流式合成（首声 ~0.2s）→ 入队 → BGM 早进 1.2s（跳过近静音前奏）+ 人声 duck 到 60% → 播放 → 每 30ms 取包络驱动 `ParamMouthOpenY` → 段结束接下一段 → 播完 BGM 尾奏 1.6s 淡出 → 字幕停留 3s（扣掉尾奏）后消失。

## §17.14 本地 Piper 支持调语速（2026-11-14）

### 为什么不用 Piper 自带的 length_scale

`PiperVoice.synthesize(text, syn_config=SynthesisConfig(length_scale=…))` 确实有这参数，但实测不可用：

| 同一句话（19 字） | ls=0.7 | ls=1.0 | ls=1.3 | ls=1.6 | ls=2.0 |
|---|---|---|---|---|---|
| 原始时长 | 4.44s | 4.76s | 5.25s | 5.53s | 7.24s |

- 只有不到线性一半的响应（ls 从 1.0 到 2.0 时长只涨 1.5 倍）；
- 还带随机性：ls=1.6 有时比 1.3 还短；
- 两端饱和：ls≤0.35 卡在 3.68s，ls≥2.6 卡在 8.7s。

结论：靠模型内部参数，"用户听到的语速"没法保证。所以改成 **后处理时间伸缩**。

### 做法：WSOLA 变速（保音高）

`tts/engine_piper.py` 新增 `_stretch(pcm, sr, speed)`：
- 30ms Hann 帧、10ms 合成步长、分析步长 = 步长 × speed；
- 每帧在 ±6ms 内用滑动窗口做相关搜索，挑与「上一帧自然延续」最相似的位置再叠加，避免丢样本的咔哒声；
- 重叠加窗归一化，输出时长严格 = 原时长 / speed，音高不变；
- 只在 speed≠1 时启用，speed 钳在 0.6~1.6（超出范围音质明显劣化）。

### 全链路打通

| 位置 | 改动 |
|---|---|
| `tool.yaml` speed | 描述改成"local（Piper）保音高时间伸缩，0.6~1.6 真实生效" |
| `tool.py` → `/tts` | speed 原样透传（已有 `speed` 字段） |
| `tts/service.py::_run_local` | 请求里新增 `"speed": float(speed or 1.0)` |
| `tools/tts_daemon.py` | `engine.synthesize(..., speed=float(req.get("speed") or 1.0))` |
| `tts/engine_piper.py` | 按原速合成 → `_stretch` 变速 → 剪首尾静音 |
| `tts/engine_qwen.py` | 签名加 `speed` 占位（预设音色接口不暴露语速，收到不生效，不报错） |

### 实测

离线（`_tts_env\stretch_probe.py`，同一段 5.28s 语音）：

| speed | 输出时长 | 要求时长 | RMS | 主峰频率 |
|---|---|---|---|---|
| 0.70 | 7.55s | 7.55s | 0.157 | 206Hz |
| 0.85 | 6.22s | 6.21s | 0.158 | 201Hz |
| 1.15 | 4.60s | 4.59s | 0.157 | 222Hz |
| 1.30 | 4.07s | 4.06s | 0.156 | 206Hz |
| 1.50 | 3.52s | 3.52s | 0.155 | 223Hz |

时长误差 <10ms，RMS 稳定（没有丢帧/爆音），主峰频率基本不动 → 音高确实保住了；单段开销 67~167ms。

真机（桌宠 + Piper，同一句 23 字）：

| speed | ttfa | 音频时长 | 端到端 |
|---|---|---|---|
| 0.7 | 326ms | 6,871ms | 10.11s |
| 1.0 | 326ms | 4,964ms | 6.59s |
| 1.4 | 314ms | 3,587ms | 5.39s |

首声延迟没变（拉伸只增加几十毫秒），字幕按每段实际时长算（`dur_ms` 来自实际 PCM 长度），所以语速改变后逐字高亮仍然对得上。

---

## §17.15 本地分段重做（P0：只在标点处切，不再按字数砍中文）

### 17.15.1 问题与实测证据

用户第二次反馈"一个句子中一些词语被乱拆分，导致意思出错，停顿也不合理"。定位到 `tts/segmenter.py`：
`min_chars=4 / max_chars=28`，而 `_safe_cut()` 只处理空格和 ASCII 字母数字，
对中文等于**纯按字数硬切**。实测切点（修复前）：

| 原文 | 切成 |
|---|---|
| `符玄在这里陪你一起做实验顺便聊聊今天遇到的那些事情希望你今天过得还算顺利`（36 字） | `…希望你`(28) ｜ `今天过得还算顺利`(8) ← 切在"希望你/今天"中间 |
| `这次我们把本地引擎换成了轻量流式模型所以第一句话大概两百毫秒就能出声不用等整句合成完`（42 字） | `…大概两百`(28) ｜ `毫秒就能出声…` ← 把"两百毫秒"砍断 |

**第二个关键事实：Piper 几乎不响应中文标点停顿。** 同一句话带/不带逗号分别合成：

| 文本 | 时长 | 内部静音段（>60ms） |
|---|---|---|
| 无标点 30 字 | 6.51s | 0.18 / 0.06 / 0.07s |
| 带"，。"33 字 | 6.49s | 0.07 / 0.19 / 0.06 / 0.06s |

多两个逗号，时长只差 20ms，静音分布几乎一致 → 停顿不归标点管，
**所有停顿都来自段边界**：每段被当成独立句子念（句尾降调 + 段间静音拼接），
于是一个词被拆成两截，听起来语义就变了。

第三个事实：Piper 对整段输入**只产出 1 个 chunk**（42 字：chunk#1 at 929ms，
9.72s 音频；33 字：chunk#1 at 581ms），所以"整句一次合成"等于"首声等到整句合成完"，
不能靠 chunk 流式找回延迟。

### 17.15.2 P0 规则（`tts/segmenter.py` 已重写）

1. 先按句末标点 `。！？!?…\n` 切成句子；
2. 句子 ≤ `max_chars`（**24**）→ 整句一段；
3. 句子 > 24 且含逗号/顿号/分号/冒号 → **只在标点处切**，再把条款贪心拼回 24 字以内
   （逗号本来就是停顿，切这里不改变词义，同时把首声压回 0.3~0.5s）；
4. 句子 > 24 但**一个标点都没有** → `UNPUNCT_MAX=48` 以内整句合成（多等 0.2~0.4s 换语义完整），
   超过 48 才退化硬切，且优先切在逗号/空格之后，最后才按字数；
5. 永不切开英文单词或数字。

参数落点：`tts/config.py` `local.min_chars=6 / max_chars=24`；
`tts/service.py` `_run_local` 默认 6/24；`tools/tts_daemon.py` 收到缺省也是 6/24。
`tts_config.json` 不存在，全部走 DEFAULTS，改代码即生效。

### 17.15.3 验收（真机，桌宠 + Piper，同一组四个用例）

| 用例 | 段数 | ttfa | 音频 | 切点是否拆词 |
|---|---|---|---|---|
| 33 字带逗号 | 2 | **349ms** | 6,450ms | 否（切在"，"后） |
| 36 字无标点 | 1 | 613ms | 6,831ms | 否（整句一次合成） |
| 42 字无标点 | 1 | 843ms | 8,772ms | 否（整句一次合成） |
| 两句 16 字 | 2 | **165ms** | 3,110ms | 否（切在"。"后） |

对比修复前（28 字硬切）：33/36/42 字分别切成 2/2/2 段、ttfa 320ms 量级，
但 36、42 字两句都在词中间断开。现在**零处拆词**，标点丰富的正常文本
（LLM 输出几乎都带标点）ttfa 回到 165~349ms。

**保留的取舍**：完全没有标点的长串（37~48 字）走整句合成，ttfa 0.6~0.9s，
牺牲一点延迟换语义完整；超过 48 字才退化硬切，这一段留给 P1（jieba 词边界）。

### 17.15.4 后续（未实施，等用户确认）

- **P1**：`_tts_env` 装 jieba，硬切前按**词边界**切，并禁止切在
  `的/地/得/了/着/和/与/或/在/是/把/被/给/对/就/才/也/还` 前后 → 彻底消灭拆词；
- **P2**：段间显式 gap（逗号 120ms、句号 350ms）+ 20ms 交叉淡化，
  让"停在哪、停多久"可配置，而不是被切点决定；
- **P3**：MeloTTS 对比（中文前端自带分词与多音字处理），评估音质与 CPU RTF。

---

## §17.16 停顿可控：噪声收敛 + 文本停顿/连读符号

### 17.16.1 为什么"停顿不自然"

两个独立原因，先后被实测确认：

1. **段与段是 0 静音硬接**。Piper 每个段尾会被 `_trim()` 剪掉静音，service 又把段 PCM
   首尾相接，于是"段尾降调 + 末字拉长"直接黏到下一段的起音，听感就是一句话被砍成几句；
2. **Piper 的停顿本身是随机抽的**。同一句连续三次：

| 采样设置 | 三次时长 | 三次内部停顿 |
|---|---|---|
| 默认 0.667 / 0.80 | 4.19 / 4.39 / 4.52s | `[63,82]`｜`[55,61,109,202]`｜`[50,55,75,246]` |
| 0.667 / 0.00 | 3.94 / 3.94 / 3.94s | `[50,287]`｜`[51,55,273]`｜`[51,278]` |
| **0.10 / 0.10** | 3.94 / 3.90 / 3.95s | 主停顿 **190 / 191 / 197ms**，结构一致 |
| 0.30 / 0.10 | 3.92 / 3.91 / 3.96s | 主停顿 201 / 280 / 190ms |

结论：`noise_w`（字长抖动）才是停顿不稳的元凶，`noise_scale` 只影响音色/抑扬。
用户 2026-11-14 选定 **0.10 / 0.10**：停顿抖 ±4ms，同时保留非零随机（不是完全一致的机械音）。

### 17.16.2 符号协议（`tts/segmenter_marks.py`）

| 符号 | 含义 | 实现 |
|---|---|---|
| `~` | 连读 | 删符号、两侧合成同一段，不切、不插静音 |
| `\|` | 短停 150ms | 切块 + 块后补 150ms 零静音 |
| `\|\|` | 中停 300ms（逗号级） | 300ms |
| `\|\|\|` | 长停 500ms（句末级） | 500ms |

符号之外，普通标点也补边界静音（`punct_gap`）：句末 300ms、逗号 150ms、无标点硬切 90ms。
符号本身不进字幕、不进合成文本；`edge` 在线后端也先剥符号（它自己按标点处理停顿）。

配置项（`tts/config.py` local 段，改代码即生效，无 `tts_config.json`）：

```
pause_marks      true     # 是否解析符号
pause_short_ms   150
pause_mid_ms     300
pause_long_ms    500
noise_scale      0.10
noise_w          0.10
```

### 17.16.3 链路改动

| 文件 | 改动 |
|---|---|
| `tts/segmenter_marks.py`（新增） | `parse_marks()` / `punct_gap()` / `split_segments_marked()` |
| `tools/tts_daemon.py` | 改用 `split_segments_marked`，每段回传 `gap_after_ms`；新增 `--noise-scale` / `--noise-w` |
| `tts/engine_piper.py` | `SynthesisConfig(noise_scale, noise_w_scale)` 参数化，默认 0.10/0.10 |
| `tts/service.py` | 段尾按 `gap_after_ms` 补零静音；逐字高亮只铺在真实语音时长上（`speech_ms`，不含静音）；daemon 启动透传噪声参数；`_run_edge` 先剥符号 |
| `tts/config.py` | 新增 pause/noise 六项 |
| `tool.yaml` | `text` 参数描述写入符号协议（智能体据此决定何时加符号） |

### 17.16.4 真机验收（桌宠 + Piper，关闭 warm 干扰后连测）

| 用例 | 段数 | ttfa | 音频总长 | 字幕 |
|---|---|---|---|---|
| 符号版 6 段（含 `~` 连读） | 6 | 474ms | 14,845ms | `不用等整句合成完。`（无符号泄漏） |
| 纯标点 33 字 | 2 | 539ms | 5,735ms | `中间不会有突兀的断点。` |
| 短句 14 字 | 1 | 441ms | 2,107ms | 整句 |

分段与静音实测（`[tts] seg in text` / `dur=` 日志）：

```
seg='背景音乐会提前进场'            samples=38018  gap=150
seg='人声说完以后再慢慢退场'        samples=43200  gap=300
seg='中间不会有突兀的断点'          samples=39764  gap=500
seg='这次我们把本地引擎换成了轻量流式模型' samples=73395 gap=150
seg='所以第一句话大概两百毫秒就能出声'     samples=69210 gap=300
seg='不用等整句合成完。'            samples=33587  gap=0
```

### 17.16.5 已知取舍

- 首段 ttfa 由首段长度决定：22 字 ≈ 0.5s，9~11 字 ≈ 0.3s。若要更低，可给首段单独
  设更小的 `max_chars`（切点仍在标点），代价是多一个短边界——未实施，等用户定；
- 符号只控制"边界静音"，改不了 Piper 段内韵律；句子内部的抑扬仍由模型决定；
- `noise_w=0` 可让节奏完全固定（三次逐字节一致），但听感更平，当前未启用。

---

## §17.17 在线配音（edge）的口型：量程自适应 + 可观测

### 17.17.1 先说结论：在线链路本来就调用了口型，问题在"量程"

`_on_seg()` 对两个后端是同一套逻辑，edge 的每段 PCM 也会走
`pet.tts_lip_start(pcm, sr)` → tick 每 30ms 把振幅写进 `ParamMouthOpenY`。
但旧的振幅换算写死了一个量程：

```
lv = (rms - 0.012) * 5.6      # 假设 TTS 人声 RMS 约 0.05~0.25
```

本地 Piper 的 RMS 就在这个区间（实测本段峰值 0.28），嘴型幅度正常；
在线 Edge 的 mp3 解码后音量明显更大（实测本段峰值 0.19~0.35、瞬时更高），
于是 `lv` 长期撞在 1.0 上 —— 表现为"嘴一直张着不动"或忽张忽闭，看起来就像没有口型。

### 17.17.2 改法：每段自适应基准

`tts/audio_engine.py`

| 改动 | 说明 |
|---|---|
| `_calc_lip_peak(pcm_f)` | 取本段 20ms 窗口 RMS 的 **90 分位** 作为"这段最响"的基准（避开爆破音极值） |
| `enqueue()` | 把 `lip_peak` 写进该段的时间轴记录 |
| `lip_level()` | 按当前播放头找到所处段，用该段 `lip_peak` 换算：`lv=(rms-floor)/(peak-floor)`，`floor=max(0.006, peak*6%)` |
| `reset()` | 清掉 `_lip_peak` |

效果：本地峰值 0.291 / 在线峰值 0.191 两套音量，换算出来的嘴型幅度一致，
不再有"在线把嘴顶到 1.0"的饱和。

### 17.17.3 顺手补的三处

| 位置 | 改动 |
|---|---|
| `tts/service.py::_on_err` | 合成失败（在线 403/超时）也调 `tts_lip_stop()`，嘴不会停在张开状态 |
| `tts/service.py::status()` | 新增 `lip` 字段：`{on, level, peak}`，可随时核对口型是否真的在驱动 |
| `renderer.html` | 新增 `getParamValues` 命令（`sync:true` 时同步返回模型上的**实际当前值**），是这次能定位问题的关键仪器 |
| `tool.yaml` | `lip_sync` 描述改写（旧文案还写着"预留开关"，与实际不符） |

### 17.17.4 真机证据（桌宠窗口钉到 (0.55,0.28)，逐帧截图 + 参数回读）

| 后端 | 帧 | `ParamMouthOpenY`（模型实际值） | `lip.level` | 段峰值 |
|---|---|---|---|---|
| edge | 1 | 0.00 | 0.00 | 0.191 |
| edge | 2 | 0.38 | 0.12 | 0.199 |
| edge | 4 | 0.71 | 0.70 | 0.199 |
| edge | 5 | 1.00 | 0.18 | 0.199 |
| edge | 6 | 0.00 | 0.00 | 0.199 |
| local | 1 | 1.00 | 0.94 | 0.291 |
| local | 5 | 0.00 | 0.33 | 0.291 |
| local | 8 | 0.28 | 1.00 | 0.291 |

截图上：`param=0` 的帧嘴闭合，`param=0.71 / 1.00` 的帧嘴明显张开（证据图
`_workers\lip2_face_strip.png`、`_workers\evis_mouth_compare.png`）。

### 17.17.5 回归

| 用例 | 结果 |
|---|---|
| 在线 + 停顿符号混合文本 | 3 段、8,832ms、字幕 `这一句应该剥掉符号后再念`（无竖线泄漏）、`lip` 正常 |
| 在线单段长句 | 5,160ms、`lip.peak=0.191`，嘴型 0→1 全程变化 |
| 本地单段 | `lip.peak=0.291`，行为与改动前一致 |

注意：在线首段要等 Edge 合成完（约 1.0~1.8s）才开始出声，这段等待期嘴应保持闭合
（`_lead_left` / `clock<0` 期间 `lip_level` 返回 0），这是预期行为，不是没驱动。
## 17.18 说话时的程序化肢体微动作（2026-11-14）

### 17.18.1 问题与前提

`assets\符玄\符玄.model3.json` 的 `Motions = 0`、`Expressions = 0`，模型自身**没有任何动作文件**，
所以"说话时身体有反应"不可能靠播动画，只能程序化写参数。`pet_window.tts_play_motion()` 原来
是 `return False` 的空实现，因此 `speak(animation=..., motion_group=...)` 之前等于没生效。

`符玄.physics3.json` 的**输入**是 `ParamAngleX/Y/Z`、`ParamBodyAngleX/Y/Z`、`ParamBreath`，
**输出**全是发丝/衣摆的 `Param*`。也就是说：只驱动头部与呼吸，头饰和衣摆会由物理自动跟着摆，
这是"看起来自然"的关键，手写那些输出参数反而会和物理打架。

### 17.18.2 分层设计（写哪些参数、谁写）

| 层 | 参数 | 写入方 | 说明 |
|---|---|---|---|
| 口型 | `ParamMouthOpenY` | TTS 服务独占 | §17.17 的 RMS 驱动 |
| 眨眼 | `ParamEyeLOpen/ROpen` | renderer 内置 eyeBlink 独占 | 动作层不碰 |
| 鼠标跟随 | `ParamAngleX/Y/Z` 基值 | renderer 每帧平滑 | 原有能力 |
| 动作层（新） | `ParamAngleX/Y/Z`、`ParamBodyAngleX`、`ParamBreath` **偏移量** | 叠加在跟随目标上 | 只叠加、不直写 |
| 物理 | 发丝/衣摆 `Param*` | Cubism 物理自动派生 | 不手写 |

关键点：**动作层产出的是"偏移量"，由 Python 叠加到 `setTrackTarget` 的目标值上**。
如果直接 `setParams` 写头部参数，renderer 每帧的跟随插值会把动作抹平（和之前口型差点失效同款坑）；
而叠加在目标上，跟随仍是主体，微动作只是细节，两者互不覆盖。

### 17.18.3 动作内容（标准档，用户拍板"默认"）

| 层 | 触发 | 动作 | 幅度/时长 |
|---|---|---|---|
| L0 呼吸 | 说话全程 | `ParamBreath` 正弦 | `0.5±0.35`，周期 3.6s |
| L1 段落起点 | 每段开始 | 轻点头（`ParamAngleY` 单峰） | 峰 −3.6°，420ms |
| L1 段内 | 每段随机（固定种子） | 头微侧 + 轻微转头 | X ±3.6°、Z ±2.4°，1.2s 缓动 |
| L2 句子边界 | 首段 + gap≥500ms + 每 3 段 | 换身体重心（`ParamBodyAngleX`） | ±2.8°，2s 缓动 |
| L3 收尾 | `_on_audio_finished` / `stop` | 600ms 缓出回基准 | 全部参数 |
| L4 指定动作 | `animation` / `motion_group` 非空 | 内置动画预设优先，自动微动作**让位** | — |

方向按段序交替（偶数段偏左、奇数段偏右），避免固定种子下每句都往同一边偏。
参数都 ≤5°（角度参数范围 −30~30），超过 ±6° 就会明显假。

### 17.18.4 全模型适配（用户硬要求）

| 机制 | 位置 | 作用 |
|---|---|---|
| 参数存在性检测 | `renderer.html::paramIndex/paramInfo` | 用 `parameters.ids.indexOf(name)` 判断；模型没有该参数就**静默跳过**，不报错、不乱动 |
| 按模型范围夹取 | `renderer.html::applyParam` | 用模型自己的 `minimumValues/maximumValues` 夹住，参数范围非 ±30 的模型也不会溢出 |
| 换模型清缓存 | `resetParamInfo()`（首载与 `switchModel` 两处） | 参数索引/范围缓存随模型失效重建 |
| 无物理模型 | 动作只写头部/呼吸参数 | 没装 physics 的模型头部照样动，只是没有发丝摆动 |
| 无 motion 文件 | `tts_play_motion` 走内置预设 → 程序化动作 | `Motions=0` 的模型也能响应 `animation` |
| 可观测 | `getParamValues` 新增 `has` / `idx` 字段 | 验收时能证明"这个模型到底有没有该参数" |

**踩到的坑（必记）**：`coreModel.getParameterIndex(name)` 对**不存在的名字也会返回 0**，
按它判断会把"模型没有的参数"写进 0 号参数（实测把 `ParamZZZNoSuch` 当成 `ParamAngleX` 写坏）。
必须用 `core._model.parameters.ids.indexOf(name)`，不存在时返回 −1。

### 17.18.5 另一个坑：跨线程 QTimer 静默失效

`speak()` 从 HTTP 线程进入，如果在里面 `QTimer(self)` 就会得到
`QObject::startTimer: Timers can only be used with threads started with QThread`，
定时器永远不响 —— 第一版真机测出来"参数全是 0"就是这个原因。
正解：定时器在 `_init_qt_parts()`（主线程）创建，跨线程只 `emit` 信号让主线程 start。

### 17.18.6 真机验收（两套模型 × 参数回读）

| 用例 | `ParamAngleX` | `ParamAngleY` | `ParamAngleZ` | `ParamBodyAngleX` | `ParamBreath` | `ParamMouthOpenY` | 念完后 |
|---|---|---|---|---|---|---|---|---|
| 符玄（有物理） | −3.09~0 | −2.39~0 | 0~1.73 | −2.24~0 | 0.16~0.84 | 0~1 | 全部 0 |
| 符玄副本（删掉 Physics） | −3.09~0 | −2.68~0 | 0~1.73 | −2.24~0 | 0.09~0.85 | 0~1 | 全部 0 |

| 通用性用例 | 结果 |
|---|---|
| 不存在参数 `ParamZZZNoSuch` 的存在性检测 | `has=false`（检测正确） |
| 给它写 30 度 | `ParamAngleX` 前后都是 0（**没有**被误写进 0 号参数） |
| 指定 `animation="nod"` | `motion.on=false`（自动微动作让位），内置预设真的在播（`ParamAngleY` 采到 −7.4° 那一帧） |
| 鼠标跟随开着（`--track 1`） | X 0~3.36、Y −1.33~0、Z 0~1.62、Body −2.24~0，跟随与微动作并存 |
| 帧率 | 待机 35.1fps / 说话 42.3fps（微动作不吃帧） |
| 首包 | 本地 ttfa 268ms，无回归 |

证据图：`_workers\mc2_physics_strip.png`（3 帧头部姿态对照）、
`_workers\mc2_nophysics_strip.png`（无物理模型两帧）、`_workers\motion_face_strip.png`。

### 17.18.7 文件清单

| 文件 | 改动 |
|---|---|
| `tts/speech_motion.py`（新） | `SpeechMotion`：`start/stop/on_segment/tick/is_idle/describe`，标准档常量 |
| `tts/service.py` | 引入 `SpeechMotion`；50ms `_motion_timer`（主线程建）+ `motionWake` 信号；`speak()` 决定是否让位；`_on_seg` 点名；`_on_audio_finished`/`stop`/`_on_err` 缓出；`status()` 增 `motion:{on,seg,sway,nod,timer}` |
| `pet_window.py` | `state["gesture"]` + `_send_track_params` 叠加偏移；`tts_gesture()`；`tts_play_motion()` 改真实现（内置预设 → 程序化 nod/shake/tilt/sway/point） |
| `renderer.html` | `paramIndex/paramInfo/applyParam`（存在性+夹取）、`resetParamInfo()` 两处插桩、`getParamValues` 增 `has/idx` |
| `tool.yaml` | `speak` 的 `animation` / `motion_group` 描述改写（说明自动微动作与让位规则） |
| `_tts_env/motion_check.ps1`、`motion_check2.ps1`、`motion_track_check.ps1`（新） | 参数采样 + 双模型回归 + 跟随并存验证 |
| `_workers/motion_fixtures/nophys/` | 无物理模型夹具（删掉 `FileReferences.Physics` 的副本），后续回归复用 |

## 17.19 本地默认语速改为 0.90（2026-11-14）

反馈："本地配音的默认速度太快了"。原来三处都把"没传语速"硬编码成 `1.0`，所以配置改了也不生效：

| 位置 | 旧写法 | 问题 |
|---|---|---|
| `tool.py`（speak payload） | `float(kwargs.get("speed") or 1.0)` | 没传 → 1.0 |
| `pet_window.py`（HTTP `/tts` 转发） | `float(data.get("speed") or 1.0)` | 没传 → 1.0 |
| `tts/service.py::speak` | `speed: float = 1.0` | 默认参数就是 1.0 |

改法：全链路把"没传"保留为 `None`，由 `speak()` 统一取后端默认值 —— 本地读 `config.local.speed`
（新增键，默认 **0.90**），在线保持 1.0（Edge 在 0.9 反而拖沓）。

### 17.19.1 实测

| 用例 | 时长 | ttfa |
|---|---|---|
| 本地，不传 speed（默认 0.90） | 5,172ms | 285ms |
| 本地，显式 speed=1.0 | 4,773ms | 239ms |
| 本地，显式 speed=0.8 | 5,823ms | 265ms |
| 离线 `engine_piper` 同一句 | 1.0 → 4.578s / 0.9 → 5.101s / 0.85 → 5.377s / 0.8 → 5.757s | — |

时长严格按 1/speed 变化（音高不变，WSOLA），默认 0.9 比原速慢 8~11%，听感是"正常说话"，
再往 0.8 以下会明显拖沓。想更快/更慢：调用方显式传 `speed`（0.6~1.6 都真实生效）。

## 17.20 语速对外语义定标：speed = 1.0 就是"正常"（2026-11-14）

§17.19 把本地默认改成 0.90 之后，智能体看到的 `speed` 仍然是"Piper 原速的倍数"，
于是"1.0"这个最自然的数值对应的是偏快的听感，智能体每次都要自己换算。现在把语义倒过来：

| 层面 | 含义 |
|---|---|
| 对外 `speed` | **1.0 = 正常语速（默认）**，<1 更慢、>1 更快，范围 0.5~2.0 |
| 本地内部倍数 | `speed × local.base_speed`（base_speed 默认 0.90），再夹到 WSOLA 可用区间 0.6~1.6 |
| 在线（edge） | `rate = (speed − 1) × 100%`，1.0 → 0%（本来就是"1 = 正常"） |
| 状态回读 | `status.speed` = 对外值，`status.speed_internal` = 实际倍数（排查用） |

`local.base_speed` 是唯一需要调的旋钮：想让"正常"更快/更慢，只改它，智能体侧的 speed 语义永远不变。

### 17.20.1 实测（同一句，本地）

| 请求 | `status.speed` | `status.speed_internal` | 音频时长 |
|---|---|---|---|
| 不传 speed | 1.0 | 0.9 | 5,208ms |
| `speed=1.0` | 1.0 | 0.9 | 5,247ms |
| `speed=0.8` | 0.8 | 0.72 | 6,397ms |

ttfa 277~344ms，与改动前同级。

## 17.21 台词内的内置动画标记（2026-11-14）

需求："支持智能体在朗读的文本里写内置动画标记，读到那几个字时触发对应动作"。

### 17.21.1 语法

| 写法 | 动作 |
|---|---|
| `[[nod]]` / `[[点头]]` | 点头 |
| `[[shake]]` / `[[摇头]]` | 摇头 |
| `[[tilt]]` / `[[歪头]]` | 歪头 |
| `[[sway]]` / `[[摇摆]]` | 摇摆 |
| `[[look_around]]` / `[[张望]]` | 左右张望 |
| `[[cheer]]` / `[[欢呼]]` | 欢呼 |

标记写在想动的**那句话前面**，念到标记后紧跟的字时触发；写在句尾（后面没字）表示"下一段开头再做"。
一句里可放多个；标记本身**绝不会被念出来**，也不会进字幕。不认识的名字会被剥掉并在 `status.anim_mark_error` 里留痕。

```
这个问题问得好。[[nod]]我赞同你的思路，另外还有个细节。[[shake]]不过这里我不建议现在改。
```

### 17.21.2 为什么用"锚点"而不是全局字符下标

本地分段在 `tools/tts_daemon.py` 里做、且会剥掉停顿符号（`~ | || |||`）与标点，
在线分段又是另一条链路 —— 全局下标在两条链路上都会错位。
所以 `tts/markers.py::parse_anim_markers()` 剥标记时顺手记住它后面 1~3 个**内容字**当锚点
（跳过空白、停顿符号、标点），每段到达时用子串查找定位；找不到就留给后面的段（分段可能把词切开），
匹配时依次退化 3→2→1 个字，避免"锚点被切在段边界上"导致永不触发。

### 17.21.3 触发链路

| 步骤 | 位置 |
|---|---|
| 剥标记 + 取锚点 | `tts/markers.py::parse_anim_markers()`，`service.speak()` 里第一件事 |
| 锚点 → 具体段/字 | `service._on_seg()`，写入段条目 `anim:[{name,at,done}]` |
| 播放时钟到点 | `service._tick_body()`：`spoken >= at` 时调 `_fire_anim()` |
| 真播动作 | `pet_window.tts_play_motion(name)` → `renderer.html` 的 `playMotion` 内置预设 |
| 与自动微动作共存 | `SpeechMotion.pause(now, 950ms)`：动作期间不输出偏移量，避免"点头动画 + 转头微动作"打架 |

模型没有 Motions/Expressions 也能用（走的是内置预设参数关键帧），所以对所有 Live2D 模型一致有效。

### 17.21.4 真机验收（符玄，本地 Piper，track=0）

台词：`先把结论说清楚。[[nod]]第二点其实更关键，[[shake]]这件事我不建议现在改。[[cheer]]最后收个尾。`

| 项 | 结果 |
|---|---|
| 念出来的文本 | `先把结论说清楚。第二点其实更关键，这件事我不建议现在改。最后收个尾。`（无 `[[`） |
| 解析出的标记 | nod→"第二点"、shake→"这件事"、cheer→"最后收" |
| 触发时刻 | 1780ms / 3190ms / 5320ms（都在对应段起点附近） |
| 参数实测 | `ParamAngleY` −8.0~+14.0（内置点头关键帧正是 +14/−8）、`ParamAngleX` −6.4~+11.5（摇头） |
| 音频 | 6,378ms，ttfa 237ms |

采样数据在 `_workers\marker_samples.json`，验收脚本 `_tts_env\marker_check.ps1`（端口 8858）。

## 17.22 默认语速再放慢一档：base_speed 0.90 → 0.85（2026-11-14）

反馈"默认还是有一些快"。只动 `local.base_speed` 一个旋钮（对外 `speed=1.0` 的语义不变）：

| base_speed | speed=1 时内部倍数 | 试听时长（同一句） |
|---|---|---|
| 0.90（旧） | 0.90 | 5,208ms（端到端） |
| **0.85（现默认）** | **0.85** | **5,493ms（端到端）/ 5.784s（离线整句）** |
| 0.82 | 0.82 | 5.965s（离线） |
| 0.80 | 0.80 | 6.174s（离线） |

真机：不传 speed → `status.speed=1`、`speed_internal=0.85`、audio 5,493ms、ttfa 284ms；
`speed=0.8` → internal 0.68、6,811ms。语速仍是"1 = 正常"，只是"正常"这一档整体后移了约 5%。
再觉得快/慢，改 `tts/config.py::DEFAULTS["local"]["base_speed"]`（或 `tts_config.json` 的 `{"local":{"base_speed":0.8}}`）即可，
智能体侧不用改任何调用。

### 17.22.1 定稿：base_speed = 0.80

试听 0.85 / 0.82 / 0.80 三档后用户选定 **0.80**（`local.base_speed = 0.80`，对外 `speed=1.0` 仍是正常语速）。

| 请求 | `status.speed` | `speed_internal` | 音频时长 | ttfa |
|---|---|---|---|---|
| 不传 speed | 1.0 | 0.80 | 5,816ms | 366ms |
| `speed=1.0` | 1.0 | 0.80 | 5,854ms | 385ms |
| `speed=0.8` | 0.8 | 0.64 | 7,168ms | 418ms |

内部区间仍夹在 WSOLA 0.6~1.6，所以 `speed=1` 就是 0.80 原速；上界约 2.0（内部 1.6），下界约 0.75（内部 0.6）。
再想调整只改这一个数，不用动调用方。

## 17.23 说话微动作增强：幅度翻倍 + 句内自动点头（2026-11-14）

反馈："说话时的微动作不明显，只剩下模型自带的物理动作"。
原因是默认档幅度（转头 ±3.6°、歪头 ±2.4°、点头 3.6°、重心 2.8°）与模型自带的物理待机摆动（±2~3°）同量级，
被物理动作盖住了；而且头位只在**段落边界**换一次，句子中间几乎静止，视觉上像"只有物理在动"。

### 17.23.1 改动

| 项 | 旧 | 新 |
|---|---|---|
| 点头幅度 `AMP_NOD` | 3.6° | **6.0°** |
| 段内转头 `AMP_SWAY_X` | 3.6° | **7.5°** |
| 段内歪头 `AMP_SWAY_Z` | 2.4° | **5.0°** |
| 身体重心 `AMP_STANCE` | 2.8° | **5.0°** |
| 点头时长 `NOD_MS` | 420ms | **480ms** |
| 句内节奏 | 只有段首点一次 | **每 ~2.2s 自动点一次**（抖动 0.75~1.35 倍，避免节拍器感） |
| 句内细摆 | 无 | `ParamAngleX` ±0.9° / `ParamAngleZ` ±0.6°、0.55Hz 正弦（头不再"死"住） |
| 幅度总开关 | 无 | `motion_gain`（默认 1.0，可 0.2~3.0 整体缩放）+ `motion_nod_every_ms` |

实现：`tts/speech_motion.py` 新增 `gain` / `nod_every_ms` 构造参数与 `_next_nod` 计时；
`tts/service.py` 从配置读 `motion_gain` / `motion_nod_every_ms` 传入；`status.motion.gain` 可回读。
仍然只发**偏移量**、仍走"参数不存在就静默跳过 + 按模型 min/max 夹取"，任何 Live2D 模型都适配。

### 17.23.2 真机实测（符玄，track=0，本地 Piper 0.8 倍速）

| 参数 | 改动前 | 改动后 |
|---|---|---|
| `ParamAngleX` | −3.09 ~ 0 | **−7.67 ~ +0.02** |
| `ParamAngleY` | −2.39 ~ 0 | **−5.03 ~ 0**（50ms 采样漏峰，理论 6.0） |
| `ParamAngleZ` | 0 ~ 1.73 | **0 ~ 5.47** |
| `ParamBodyAngleX` | −2.24 ~ 0 | **−3.50 ~ 0** |
| `ParamBreath` | 0.09 ~ 0.85 | 0 ~ 0.85 |
| `motion.on` 命中率 | — | 115/115 采样帧 |

想更强/更收敛：`tts_config.json` 写 `{"motion_gain": 1.3}`（或 0.8），不用改代码。
数值采样在 `_workers\motion3_samples.json`，验收脚本 `_tts_env\motion_amp_check.ps1`（端口 8862）。

## 17.24 微动作"复现不出来"的排查与再增强（2026-11-14）

§17.23 把幅度翻倍后，我这边采样能看到 −7.7°/+5.5°，用户实测却说"只剩物理动作"。
两条链路都实测过，代码上并没有"跳过微动作"的分支：

| 场景 | `ParamAngleX` | `ParamAngleY` | `ParamAngleZ` | `ParamBodyAngleX` |
|---|---|---|---|---|
| 本地 Piper（track=0） | −9.45 ~ +0.13 | −5.00 ~ 0 | −0.04 ~ +6.32 | −3.50 ~ 0 |
| 本地 Piper（track=1） | −7.65 ~ 0 | −4.87 ~ 0 | −0.05 ~ +5.46 | −3.50 ~ 0 |
| 在线 Edge | −7.85 ~ +6.90 | −4.70 ~ 0 | −5.36 ~ +5.17 | 0 ~ +4.86 |

所以问题出在"看的时候"而不是"发的时候"，两个真实原因：

1. **进程没重启**：桌宠是常驻进程，Python 模块改完必须重开窗口才生效（旧进程跑的还是旧幅度）。
2. **旧版动作只在段落起点"到位一次"**：固定落点 1.2s 缓到位后就不再变化，一句话 2~3 秒的台词
   只有开头动一下，看起来跟物理待机摆动没区别 —— 采样能测出大偏移，肉眼却像静止。

### 17.24.1 这次改了什么

| 项 | 改法 |
|---|---|
| 句内持续轻摆 | 新增 `AMP_ROCK_X=3.4°` / `AMP_ROCK_Z=2.2°`、周期 `ROCK_S=3.4s` 的正弦，叠加在段落落点上 → 说话期间头**一直在往复**，不是到位即停 |
| 固定落点 | 转头 ±6.0°、歪头 ±4.0°（给轻摆留余量，峰值约 ±9.4°，不抽搐） |
| 句内点头 | 每 ~2.2s 一次（原有），振幅 6.0° |
| 身体 | `ParamBodyAngleZ` 新增 ±1.4° 随摆动（有 ParamBodyAngleZ 的模型才有，没有的静默跳过） |
| 参数回零 | 明确每帧都发 `ParamAngleY=0` / `ParamBodyAngleZ`：`setTrackTarget` 是"整批替换目标"，停发某参数会让它**冻结在上一帧**（点头后会一直低着头） |
| 可观测 | `status.motion.out` 回读本帧实际下发的偏移量，排查"看不见"时一眼能确认是没发还是没渲染 |

实测（本地，13s 长句）：`ParamAngleX` −9.45 ~ +0.13、`ParamAngleZ` −0.04 ~ +6.32，
相邻差分**转折 10 次**（说明持续往复而不是一次到位）；`status.motion.out` 末帧
`{"ParamAngleX":-7.65,"ParamAngleY":-0.59,"ParamAngleZ":3.79,"ParamBodyAngleX":-3.48}`。
Edge 后端同样生效（上下波动 ±5~7°）。

**验证方法**：①重开桌宠；②让它念一句 8 秒以上的话；③说话期间 `GET http://127.0.0.1:<port>/tts`，
看 `motion.out` 里的角度值是否在变（在变 = 已下发，渲染端就一定能看到）。
验收脚本 `_tts_env\motion_amp_check.ps1`（8866）。

## 17.25 多角色音色实测（同句 A/B 试听带）

前三节的结论是"换模型不会更快"，这一节回答另一个问题：**换音色值不值**。
做法是把候选模型全部用同一句台词、同一响度、同一采样率拼成一条试听带，
每个音色前面由华颜报编号，人耳顺序听即可 A/B。

- 台词：`符玄在这里。今天天气还不错，我们把这件事慢慢说清楚，不着急。`（30 字）
- 口径：RMS 归一到 0.10、重采样 22050Hz、段间 0.75s、报号 0.25s
- 文件：`_workers\audition\audition_test.wav`（55.6s，9 段，Kokoro 缺失）
- 脚本：`_workers\audition_make.py`（合成 → 归一并带 → 拼带），
  合成底座 `_workers\voices_probe\synth_measure.py`

### 17.25.1 客观指标（全部本轮实测，串行无并行负载）

| # | 音色 | 模型 | 时长s | 字/秒 | 静音占比 | 最长停顿 | F0 均值 | F0 标准差 | RTF | 首句ms | license |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 华颜（现状） | piper zh_CN-huayan | 4.96 | 5.24 | 10.9% | 200ms | 233Hz | 63 | **0.076** | **96** | Unknown |
| 2 | 艾拉 sid0 | sherpa vits-zh-hf-eula | 4.93 | 5.27 | 9.8% | 140ms | 325Hz | **82** | 0.407 | 419 | Unknown |
| 3 | 艾拉 sid400 | 同上（少年音） | 4.06 | **6.40** | 10.3% | 120ms | 220Hz | 77 | 0.408 | 377 | Unknown |
| 4 | 特蕾莎 sid0 | vits-zh-hf-theresa | 5.17 | 5.03 | 10.1% | 160ms | **371Hz** | 77 | 0.466 | 510 | Unknown |
| 5 | 梵尘 C | vits-zh-hf-fanchen-C | **7.83** | **3.32** | 5.1% | 120ms | 260Hz | 36 | 0.380 | 654 | Unknown |
| 6 | zh-ll sid0 | sherpa-onnx-vits-zh-ll | 5.08 | 5.12 | 10.6% | 180ms | 276Hz | 71 | 0.361 | 301 | Unknown |
| 7 | Matcha 烘焙 | matcha-icefall-zh-baker | 6.01 | 4.33 | 9.3% | 140ms | 265Hz | 55 | **0.088** | **114** | **非商用** |
| 8 | Kokoro zf_001 | kokoro-int8-multi-lang | — | — | — | — | — | — | — | — | Apache-2.0 |
| 9 | aishell3 sid99 | vits-icefall-zh-aishell3 | 4.69 | 5.55 | 4.7% | 100ms | 212Hz | 32 | **0.042** | **67** | Apache-2.0（**8kHz**） |

第 8 位 Kokoro 本轮**再次返回空音频**（`OfflineTts.generate` 的 `samples` 长度为 0，
`len(a.samples)/a.sample_rate` 直接除零），和子智能体报告一致，判定为"当前调用不可用"，
不纳入试听带。aishell3 保留作"8kHz 电话音质"下限对照。

### 17.25.2 三条试听带

| 带子 | 文件 | 用途 | 候选 |
|---|---|---|---|
| 常规句 | `audition_test.wav`（55.6s） | 整体自然度、口音、语速 | 1~7、9 共 8 条 |
| 情绪句 | `audition_emotion.wav` | 动漫感/表现力（`诶？你怎么还在这里。我说了让你先回去的。真是的，拿你没办法。`） | 华颜、艾拉 sid0、特蕾莎、Matcha |
| 四声 | `audition_tones.wav` | 声调准不准（`妈。麻。马。骂。`） | 华颜、艾拉、特蕾莎、Matcha、梵尘 |

单人文件：`v_<n>_<名字>.wav`（常规句）、`e_<n>_<名字>.wav`（情绪句）、`t_<n>_<名字>.wav`（四声），
全部 22050Hz / 已响度归一，可直接当 A/B 素材。

### 17.25.3 结论

1. **换音色的收益是音色与表现力，不是延迟。** 华颜 RTF 0.076 / 首句 96ms，
   所有 sherpa 候选 RTF 0.36~0.47 / 首句 301~654ms，慢 4~6 倍。桌宠场景仍可接受
   （0.5s 内出声），但"换个更好的模型会更快"这个假设被数据否掉了。
2. **模型自己几乎不做句间停顿，这条已被独立复现。** 整句合成时括号内最长静音只有
   100~200ms，艾拉在标点处甚至**一次 ≥160ms 的停顿都没有** —— 所以停顿必须继续由
   `tts/segmenter.py` + 符号 gap 协议（`~`0 / `|`150 / `||`300 / `|||`500ms）供给，
   换模型不可能解决 §17.19 的停顿问题。
3. **梵尘 C 淘汰**：3.32 字/秒（正常口语 5~6 字/秒），一句 30 字要 7.83s，
   且 tokens 是注音符号 + 台湾调号（`ˉˊˇˋ˙`），听感偏台湾腔。
4. **声调必须人耳判**。艾拉/特蕾莎的 tokens 是 IPA 音素、**没有声调符号**
   （声调靠模型隐式学），和用户早前"本地合成像外国人说中文"的风险同源；
   梵尘才有注音 + 调号。本轮试过用单字 F0 轨迹自动判调，单字合成太短、结果自相矛盾
   （华颜"骂"被判"没降"，而用户实测华颜声调正常），**该自动判调不可信，已弃用**，
   改用四声试听带由人耳确认。
5. **Matcha 是唯一延迟接近现状的替代**（RTF 0.088 / 首句 114ms、F0 265Hz），
   代价是 license 非商用。
6. 表现力排序（按 F0 标准差，越大起伏越丰富）：艾拉 sid0（82）> 特蕾莎/艾拉 sid400（77）
   > zh-ll（71）> 华颜（63）> Matcha（55）> 梵尘（36）> aishell3（32）。
   音高亮度：特蕾莎 371Hz > 艾拉 325Hz > zh-ll 276 > Matcha 265 > 梵尘 260 > 华颜 233。

### 17.25.4 待用户拍板后才做的一步（尚未实施）

音色选择直接决定引擎架构，所以先听再定，不预设：

- 选艾拉/特蕾莎 → 新增 `tts/engine_sherpa.py`（`sherpa_onnx.OfflineTts`，模型走
  `E:\AgentClusterModels\sherpa-onnx\...`），`tts/config.py` 加 `local.engine=sherpa`
  与 voice 注册表（模型目录 + sid），`tool.yaml` 的 voice enum 扩成
  `Serena / 艾拉 / 艾拉少年 / 特蕾莎`，daemon 常驻加载（避免每段重新 load 130MB）。
- 选 Matcha → 同上但需 `acoustic_model=model-steps-3.onnx` + 外部
  `vocos-22khz-univ.onnx` 两个显式路径，且要接受非商用条款。
- 保持华颜 → 本轮工作只留作备选音色库，插件不动。
- license 现实不变：动漫感强的候选全是 Unknown（eula/theresa 是米哈游角色同人），
  可商用的 aishell3 只有 8kHz、Kokoro 当前不可用。

---

## 17.26 单模型多音色引擎（sherpa · 一个模型 + 多个 sid）

### 17.26.1 为什么走这条路

用户把需求收窄成三条硬指标：**不追商用、多种音色可立刻切换、延迟低 + 音质高**，
并追问"能不能只用一个模型、靠改音色种子（sid）来换音色，以减小内存占用"。

能答"是"。`sherpa-onnx` 的 VITS 模型（`vits-zh-hf-eula`）自带 **804 个 speaker 槽位**，
`OfflineTts.generate(text, sid=N)` 只换一个整数 —— 权重只加载一次，
换音色既不重新读盘、也不多占内存、也没有加载耗时。于是架构从
"多模型常驻池"改成"**一个模型 + 一张 sid 音色表**"。

实测三模型常驻的代价（`_workers/switch_probe.py`，同一进程里逐个加载）：

| 方案 | 常驻 RSS | 说明 |
|---|---|---|
| 只挂 eula | **203~243MB**（权重约 168MB） | 本次采用 |
| eula + theresa | 426MB | 多一份权重，音色库才多一套 |
| eula + theresa + matcha | 583MB（同驻峰值 545MB） | 不划算 |

结论：**单模型 = 200MB 级，加一个模型 = 多 190MB 级**。多音色靠 sid 而不是靠堆模型。

### 17.26.2 sid 抽样筛查（`_workers/sid_screen.py`）

从 0~803 里按步长 40 加若干具名抽了 **28 个 sid**，逐个合成并检查空音频/削顶/语速：

- 全部可用：无空音频、无削顶，语速 5.6~7.0 字/秒，F0 覆盖 207~355Hz。
- **全部是女声** —— eula 没有男声。之前"sid400 = 少年音"的判断作废（那只是 F0 偏低的女声）。
  若以后要男声，只能再挂第二个模型（`vits-zh-ll` 的男声 sid / fanchen-C）或走 edge 的
  `zh-CN-YunxiNeural / YunjianNeural`，前者多约 190MB，后者要联网。

最终注册 4 个音色（`tts/voices.py`，全部 22050Hz、同一模型）：

| voice id | sid | 气质 | F0 中位 |
|---|---|---|---|
| `eula_girl`（默认） | 0 | 清亮偏冷，动漫少女，日常对话 | 315~341Hz |
| `eula_bright` | 40 | 音高最高、明亮有元气 | 302~355Hz |
| `eula_warm` | 401 | 中音区、温柔不甜，讲解/安抚 | 246~297Hz |
| `eula_low` | 720 | 低音区、成熟冷淡，毒舌/严肃 | 181~207Hz |

`huayan` 保留在清单里只作兜底（`local.engine=piper` 时才生效）。

### 17.26.3 代码落点

新增/改动（均不改 daemon 线协议，Piper 那条路仍可整体回退）：

- **新增 `tts/voices.py`**：`MODELS`（模型目录、onnx 文件名、license）、`WARM_MODELS=["eula"]`、
  `VOICES`（4 个 sid 音色）、`voice_list()`（给 `/tts` 与 `tool.yaml` 用）、
  `resolve(name, engine)`（不认识的名字回落默认，**绝不把错 name 传到 daemon**）；
  模型根目录可用 `DESKTOP_PET_MODELS_ROOT` 覆盖。
- **新增 `tts/engine_sherpa.py`**：`SherpaEnginePool`。
  `_load_lock`(RLock) 防"预热线程 + 首个 speak"各加载一份；
  `keep_warm=True` 让 daemon 空闲时**不卸载**（重载要 1.2~4s，桌宠不该为此卡壳）；
  语速复用 Piper 的 `_stretch`(WSOLA) 保持音高；输出 int16；
  `status()` 带 `ram_rss_mb / ram_models_mb / models`。
- **`tools/tts_daemon.py`**：加 `--backend sherpa`、`--threads`；sherpa 分支不校验 Qwen 目录，
  改为校验 `WARM_MODELS` 的权重是否齐全；`serve()` 起 `_warm_load` 后台线程预热；
  `ready` 事件的 `model` 字段改用引擎 `status.model`。
- **`tts/service.py`**：`LOCAL_VOICES = voices.voice_list()`（import 失败时回退旧清单，插件不能因此起不来）；
  `_resolve_voice` 对 sherpa 一律走 `resolve()`；
  `spd_internal` 分引擎取基准（piper `base_speed` / sherpa `sherpa_speed_scale`）。
- **`tts/config.py`**：`local.engine="sherpa"`、`voice.local="eula_girl"`、
  新增 `sherpa_speed_scale`、`prebuffer_ms` 320→220。
- **`tool.yaml`**：voice enum 加入 4 个 id 并按气质重写描述（含"本地只有女声"这条事实），
  backend / speed 的描述同步成 sherpa 现实。

### 17.26.4 语速基准的对齐（重要，容易踩）

换了引擎，"正常语速"必须重新定标，否则智能体说 `speed=1` 会突然变快：

| 引擎 | 内部速率 | 同一句 25 字耗时 | 字/秒 |
|---|---|---|---|
| Piper @ base_speed 0.80（用户认可的"正常"） | 0.80 | 5.95~6.00s | 4.2~4.4 |
| sherpa 原生（speed=1.0） | 1.00 | **4.31s** | 5.8 |
| sherpa 校准后 | **0.72** | 5.42~6.25s | 4.6~5.4 |

所以 `sherpa_speed_scale = 0.72 = 4.31 / 6.00`。对外 `speed` 语义不变（1 = 正常），
以后要整体调快慢只改这个数。

### 17.26.5 真机验收（`_tts_env/sherpa_pet_check.ps1`，窗口 8876）

4 个音色**在同一个窗口、同一个 daemon、同一份权重**里连续说话：

| voice | ttfa | 段数 | speed / internal | 口型 peak |
|---|---|---|---|---|
| `eula_girl` | 480~590ms（首个冷启动 2348ms） | 2 | 1 / 0.72 | 0.18 |
| `eula_bright` | 530ms | 2 | 1 / 0.72 | 0.18 |
| `eula_warm` | 480ms | 2 | 1 / 0.72 | 0.21 |
| `eula_low` | 566ms | 2 | 1 / 0.72 | 0.25 |

- 换音色**没有加载事件**：`ready` 事件里 `load_ms=0.0 / cached=true`。
- `|` 段间 gap 150ms 正确落到 `segments_timeline`（1433ms → 下一段起始）。
- 口型 peak 0.18~0.25，说明说话微动作 / 口型链路在新引擎下照常工作。
- daemon 层单独测（`_workers/daemon_sherpa_test.py`）四个音色 365~430ms，与真机一致。

**一个必须知道的代价**：daemon 刚起来的第一次 speak 会等预热（2041ms load + 2584ms warm），
所以插件启动时应立刻 `warmup()`；之后每次都在 0.5s 级。

### 17.26.6 新增音色 / 新增模型怎么做

- **只加音色**（同一模型）：在 `tts/voices.py` 的 `VOICES` 里加一行 `(id, sid, 气质)`，
  `tool.yaml` 的 enum 加同一个 id，重启窗口即可。零内存代价。
- **加一套音色（第二个模型）**：`MODELS` 加一条 + `WARM_MODELS` 追加模型名，
  参照 eula 的字段填目录/onnx/license；代价约 +190MB、首次加载 +4~5s。
- **回退到 Piper**：`local.engine="piper"`，音色回落 `huayan`，其余逻辑不变。

### 17.26.7 遗留与边界（如实记录）

1. eula 全女声；无男声需求时够用，需要男声要另挂模型或走 edge。
2. `eula / theresa` 的 license 是 Unknown（米哈游角色同人微调），**只能自用**；
   用户已明确"不追商用"，所以这是可接受的取舍。
3. 首段延迟 0.48~0.59s，仍比 Piper（0.44~0.54s）略高；换音色本身零开销，
   这点差距来自 sherpa 合成本身（RTF 约 0.4 vs Piper 0.076）。
4. 降 prebuffer 到 220ms 的依据是"合成耗时已足够填缓冲"；若以后出现断音，
   把 `local.prebuffer_ms` 调回 320 即可。
5. 试听带 `_workers/audition/voices_sid_tape.wav`（31.81s，顺序：清亮 → 明快 → 柔缓 →
   低沉 → 华颜@0.80 参照）与 4 个单文件 `v_eula_*.wav`，等用户人耳拍板最终组合。

## 17.27 台词内置动画"没播出来"的定位与修复（真机已验证）

### 17.27.1 现象

智能体在配音文字里写 `[[nod]]` / `[[shake]]` / `[[cheer]]` 等标记，`/tts` 回报
`anim_marks` 正常、`anim_fired` 也全是 `ok:true`，但**画面上看不到动作**；
用参数采样才看得出：`ParamAngleY` 只到 −4.6 左右（内置 nod 是 ±14），
即实际播的是"程序化兜底动作"，内置动画根本没到渲染页。
另一路故障是**触发时机错**：动画在台词开口瞬间就播完了，用户完全看不到。

### 17.27.2 根因（三条，缺一不可）

1. **动画命令走了会静默失效的通道**
   `tts_play_motion` 原来只往 `pet._cmd_page` 上 `runJavaScript`。
   `_cmd_page` 是**建窗那一刻抓的一次性引用**，页面重建/换模型后它就是旧对象；
   往旧对象 `runJavaScript` **不抛异常、也不会有任何效果**，函数还照样 `return True`，
   于是 `anim_fired` 显示成功、画面却没动。真正的"当前页"只存在于命令队列
   （`/cmd` → `_poll_cmds` → `page.runJavaScript`）里，那个 `page` 是闭包变量，换页会自动跟着变。

2. **标记定位用的是"锚点子串 + 单字兜底"**
   旧逻辑 `for L in (len(anchor), 3, 2, 1)` 最后一定会落到 `L=1` 甚至 `at=0`：
   锚点 `第二点` 的首字`第`在前面"第一点先说清楚"里就能匹配上，
   结果 nod 被挂到**前一段**、clock≈10ms 就触发，动画在开口瞬间演完。

3. **前缀字符位算错**（新方案落地时踩到）
   计算标记位置时把标记自己的字符（`[[nod]]` 共 7 个字符）也算进了内容位，
   导致 `pos` 偏大 7，标记被挂到后面的段或直接越界。

### 17.27.3 修复

| 位置 | 改动 |
|---|---|
| `pet_window.py` 建窗处 | `state["send_cmd"] = self.send_command`（**并用** `pet._send_cmd`），把命令队列暴露给 PetWidget |
| `pet_window.tts_play_motion` | 通道顺序改为 **①命令队列 → ②直连 `_cmd_page` → ③程序化动作**；队列失败写 `_log_line("WARN", ...)`，不再静默 |
| `tts/markers.py` | 每段标记新增 `pos`＝标记在干净文本里的**内容字符位**（只跳过空白与 `~`/`|`，标点保留）；新增 `content_index()` / `char_at_content()`；`pos` 计算时**不计标记自身字符** |
| `tts/service.py` `speak` | 初始化 `self._anim_base = 0`（"已消化内容位数"） |
| `tts/service.py` `_on_seg` | 改为按内容位挂段：`base <= pos <= base+本段内容位` 才算本段，再用 `char_at_content(line, pos-base)` 换算段内字符下标，然后 `self._anim_base += 本段内容位`；锚点子串只在旧数据（无 `pos`）时兜底，且**长度必须 ≥2**，彻底去掉单字/`at=0` 兜底 |

设计要点：**不再依赖锚点子串**。分段器会剥 `~`/`|`、会在标点处切段，
锚点被切断或撞到别的段都会错位；"内容位 + 按段累加"与分段怎么切无关。

### 17.27.4 真机验证（port 8877，profile-tag `sherpa_final`）

单段（22 字，"第一点先说清楚，[[nod]]第二点要记牢，[[shake]]这件事慢慢来。"）：

- `anim_fired`：`nod @clock 1200ms`、`shake @2630ms`（正好落在"二""这件"）。
- 参数采样（`/cmd_sync` → `getParamValues`）：**`max|ParamAngleY| = 13.0`、`max|ParamAngleX| = 15.0`**
  —— 只在**内置动画**才可能到这两位（程序化兜底上限 ±5），确认内置动画真的到了模型。
- 无任何 `WARN`，说明命令队列通道一次成功。

多段（3 段：`…慢慢来。` / `第二点，这里要记牢…` / `说完这段，最后收个尾。`）：

| 标记 | `pos` | 命中段 | 段内 `at` | 触发 clock |
|---|---|---|---|---|
| `[[nod]]` | 20 | 段 2（base 16） | 4（"里"） | 4640ms |
| `[[cheer]]` | 36 | 段 3（base 31） | 5（"最后收"的"最"…取到"收"前一字位） | 8590ms |

`cheer` 采样同时看到 `ParamAngleY 4.6→7.5` 与 `ParamMouthOpenY 0.69`（欢呼预设是 Y+张口），
说明"多参数预设"也照常生效。

`subtitle=false` 单独复测：`anim_fired` 仍为 `nod@2040 / shake@3400`，**动画不依赖字幕开关**。

### 17.27.5 现在怎么写标记

```
[[nod]]第二点要记牢        → 念到"第二点"时点头
[[shake]]这件事别忘        → 念到"这件事"时摇头
[[cheer]]最后收个尾        → 念到"最后收"时欢呼
[[tilt]] / [[sway]] / [[look_around]]   其余三个同名
```

- 别名：`[[点头]] [[摇头]] [[歪头]] [[摇摆]] [[张望]] [[欢呼]]` 等价。
- 标记自身不会被念出来；不认识的标记同样剥掉，并记入 `anim_mark_error`。
- 标记后面跟随的具体文字决定触发时刻；标记放在句尾时，`pos` 落在段尾（≈段尾前一字）。

### 17.27.6 边界与注意

1. `anim_fired.ok` 只代表"命令已投递"，**不再能作为画面验证依据**；
   要看效果必须用参数采样（`/cmd_sync` + `getParamValues`），这是本轮唯一可信的验收口径。
2. `_cmd_page` 只保留作兜底，**任何新增的"窗口推命令"逻辑都应走命令队列**；
   直连 `_cmd_page` 的行为是"成功静默 / 失败也静默"，极难排查。
3. 标记位置按"内容位"计算，与 `~`/`|` 停顿符号无关；但不要在一个标记和它后面第一个字之间塞标点，
   否则 `pos` 会多吃一个标点，动作提前 1 个字。
