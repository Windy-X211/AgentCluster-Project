import { reactive } from 'vue'

/**
 * 自定义 prompt / confirm 全局状态（模块级单例，任何组件可直接 import 调用）。
 *
 * 为什么不用 window.prompt / window.confirm？
 * pywebview frameless 窗口中 WebView2 会抑制原生对话框（被宿主窗口遮挡或不渲染），
 * 导致用户点击后"没有反应"。用自绘 Vue 弹窗可以在任何宿主环境下正常显示。
 */

interface PromptState {
  open: boolean
  message: string
  defaultValue: string
  placeholder?: string
  result: string | null       // null = 等待中 / 取消，string = 确认返回值
  resolve: ((value: string | null) => void) | null
}

interface ConfirmState {
  open: boolean
  message: string
  title?: string
  confirmText?: string
  cancelText?: string
  danger?: boolean
  result: boolean | null
  resolve: ((value: boolean) => void) | null
}

export const promptState = reactive<PromptState>({
  open: false,
  message: '',
  defaultValue: '',
  placeholder: undefined,
  result: null,
  resolve: null,
})

export const confirmState = reactive<ConfirmState>({
  open: false,
  message: '',
  title: undefined,
  confirmText: '确定',
  cancelText: '取消',
  danger: false,
  result: null,
  resolve: null,
})

/**
 * 替代 window.prompt：弹出一个输入框，返回 Promise<string | null>。
 * @param message 提示文字
 * @param defaultValue 初始值
 * @param placeholder 占位符
 * @returns 用户输入的字符串（trim 后），取消则返回 null
 */
export function usePrompt(
  message: string,
  defaultValue: string = '',
  placeholder?: string,
): Promise<string | null> {
  return new Promise((resolve) => {
    promptState.message = message
    promptState.defaultValue = defaultValue
    promptState.placeholder = placeholder
    promptState.result = null
    promptState.resolve = resolve
    promptState.open = true
  })
}

/** PromptDialog 组件内部调用：确认 */
export function __promptConfirm(value: string) {
  const r = promptState.resolve
  promptState.open = false
  promptState.resolve = null
  if (r) r(value)
}

/** PromptDialog 组件内部调用：取消 */
export function __promptCancel() {
  const r = promptState.resolve
  promptState.open = false
  promptState.resolve = null
  if (r) r(null)
}

export interface ConfirmOptions {
  title?: string
  confirmText?: string
  cancelText?: string
  /** 危险操作：确认按钮变红 */
  danger?: boolean
}

/**
 * 替代 window.confirm：弹出一个确认框，返回 Promise<boolean>。
 */
export function useConfirm(message: string, opts?: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    confirmState.message = message
    confirmState.title = opts?.title
    confirmState.confirmText = opts?.confirmText ?? '确定'
    confirmState.cancelText = opts?.cancelText ?? '取消'
    confirmState.danger = opts?.danger ?? false
    confirmState.result = null
    confirmState.resolve = resolve
    confirmState.open = true
  })
}

/** ConfirmDialog 组件内部调用：确认 */
export function __confirmConfirm() {
  const r = confirmState.resolve
  confirmState.open = false
  confirmState.resolve = null
  if (r) r(true)
}

/** ConfirmDialog 组件内部调用：取消 */
export function __confirmCancel() {
  const r = confirmState.resolve
  confirmState.open = false
  confirmState.resolve = null
  if (r) r(false)
}

/** 关闭所有弹窗（Esc 或外部调用） */
export function __dialogCloseAll() {
  if (promptState.open) __promptCancel()
  if (confirmState.open) __confirmCancel()
}
