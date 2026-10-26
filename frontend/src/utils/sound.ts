/** 画布运行状态提示音 — 基于 Web Audio API 合成，零外部依赖
 *  - playSuccess()  愉悦：两声 — 第一声起（中频上扬），第二声落（高频圆满收束）
 *  - playError()    急促：两声 — 第一声起（高频），第二声落（低频）
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

/** 愉悦：两声 — 第一声起（中频上扬），第二声落（高频圆满收束） */
export function playSuccess() {
  const ctx = _getCtx()
  if (!ctx) return
  const now = ctx.currentTime
  // 第一声：中频"起" — C5 + E5 大三叠加，明亮启动
  _beep(ctx, 523.25, now, 0.12, 'triangle', 0.28)    // C5
  _beep(ctx, 659.25, now, 0.12, 'sine', 0.20)        // E5
  // 第二声：高频"落" — G5 + C6，属到主圆满收束
  _beep(ctx, 783.99, now + 0.18, 0.28, 'triangle', 0.28)   // G5
  _beep(ctx, 1046.5, now + 0.18, 0.35, 'sine', 0.22)       // C6
}

/** 急促：两声短音 — 第一声起（高频），第二声落（低频），警示清晰 */
export function playError() {
  const ctx = _getCtx()
  if (!ctx) return
  const now = ctx.currentTime
  // 第一声：高频"起" — E4 + G4 叠加，明亮上扬
  _beep(ctx, 329.63, now, 0.10, 'square', 0.35)     // E4
  _beep(ctx, 392.00, now, 0.10, 'sawtooth', 0.22)   // G4 锯齿叠加
  // 第二声：低频"落" — C3 + E3，沉重下行
  _beep(ctx, 130.81, now + 0.18, 0.22, 'square', 0.32)   // C3
  _beep(ctx, 164.81, now + 0.18, 0.22, 'triangle', 0.25) // E3
}

/** 调试/设置页试听：直接播放（不限开关） */
export function testSuccess() { playSuccess() }
export function testError() { playError() }
