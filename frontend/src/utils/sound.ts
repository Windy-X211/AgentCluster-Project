/** 画布运行状态提示音 — 基于 Web Audio API 合成，零外部依赖
 *  - playSuccess()  愉悦：C-E-G-C 大三和弦琶音，清脆上扬
 *  - playError()    急促：E2 三连发短低音，警示急促
 *  注：开关判断由调用方（conversation.ts）负责，本模块只负责发声。
 */

let _ctx: AudioContext | null = null

function _getCtx(): AudioContext | null {
  if (typeof window === 'undefined') return null
  // @ts-ignore
  const AC = window.AudioContext || (window as any).webkitAudioContext
  if (!AC) return null
  if (!_ctx) _ctx = new AC()
  // 浏览器策略：用户交互后才能 resume（否则被挂起）
  if (_ctx.state === 'suspended') _ctx.resume().catch(() => {})
  return _ctx
}

/** 生成一个包络音（ADSR 简化版 Attack-Decay） */
function _beep(
  ctx: AudioContext,
  freq: number,
  startTime: number,
  duration: number,
  type: OscillatorType = 'sine',
  peak = 0.25,
) {
  const osc = ctx.createOscillator()
  const gain = ctx.createGain()
  osc.type = type
  osc.frequency.setValueAtTime(freq, startTime)
  gain.gain.setValueAtTime(0, startTime)
  gain.gain.linearRampToValueAtTime(peak, startTime + 0.015)      // Attack 15ms
  gain.gain.exponentialRampToValueAtTime(0.001, startTime + duration) // Decay
  osc.connect(gain).connect(ctx.destination)
  osc.start(startTime)
  osc.stop(startTime + duration + 0.02)
}

/** 愉悦：C5-E5-G5-C6 大三和弦琶音，清脆上扬 */
export function playSuccess() {
  const ctx = _getCtx()
  if (!ctx) return
  const now = ctx.currentTime
  // 琶音：C5 → E5 → G5 → C6，每个 0.13s 间隔，尾音加强
  const notes = [523.25, 659.25, 783.99, 1046.5] // C5 E5 G5 C6
  notes.forEach((f, i) => {
    _beep(ctx, f, now + i * 0.13, 0.22, 'triangle', 0.22)
  })
  // 尾音稍加强 + 正弦泛音
  _beep(ctx, 1046.5, now + 0.52, 0.45, 'sine', 0.18)
}

/** 急促：高频短促三连发 + 低频收尾，警示感强、不易漏听 */
export function playError() {
  const ctx = _getCtx()
  if (!ctx) return
  const now = ctx.currentTime
  // B3（247Hz）短方波三连发 — 中频警示，笔记本也清晰
  for (let i = 0; i < 3; i++) {
    const t = now + i * 0.14
    _beep(ctx, 246.94, t, 0.08, 'square', 0.35)      // B3
    _beep(ctx, 329.63, t, 0.08, 'sawtooth', 0.22)   // E4 锯齿叠加，更刺耳
  }
  // 收尾：双层音 — 高频 E4 + 低频 C3，强调"结束"
  _beep(ctx, 329.63, now + 0.48, 0.35, 'square', 0.30)   // E4
  _beep(ctx, 130.81, now + 0.48, 0.35, 'triangle', 0.25) // C3
}

/** 调试/设置页试听：直接播放（不限开关） */
export function testSuccess() { playSuccess() }
export function testError() { playError() }
