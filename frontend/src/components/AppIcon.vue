<script setup lang="ts">
import { computed } from 'vue'
import {
  Bot, Settings, Palette, Map, Brain, Wrench, Target, FileCode, Sparkles, Package,
  Folder, FolderOpen, ClipboardList, Clipboard, ClipboardPaste, Search, Rocket, Trash2, Bandage,
  RefreshCw, ScrollText, Mail, Send, BookOpen, Pencil, PencilLine, Pin, Users,
  Eye, EyeOff, X, Link, Scissors, SquareStop, Globe, Camera, BarChart3, Egg, Image,
  Film, Mic, MicOff, Calculator, Handshake, Upload, Flame, Zap, Monitor, MousePointer2,
  AlertTriangle, MessageSquare, MessageSquarePlus, Lock, FlaskConical, Keyboard, FileText, FileUp,
  CircleCheckBig, CircleCheck, CircleX, Loader2, CirclePlus, CircleMinus, PawPrint, Plug,
  ArrowLeft, ArrowRight, Save, FileCog, Settings2, Bell, ShieldCheck, Music, TriangleAlert,
  Terminal, Bug, ChevronDown, ChevronRight, Star, Info, Play, Square, Cpu, HardDrive,
  Cog, Compass, Waypoints, Network, Database, Archive, Download, ExternalLink, Home,
  GripVertical, MoreHorizontal, MoreVertical, Plus, Minus, CheckCheck, Circle,
  ShoppingCart, HardDriveDownload, Paperclip, Timer, CornerDownLeft, Pause,
  type LucideIcon,
} from '@lucide/vue'

const _MAP: Record<string, any> = {
  bot: Bot, settings: Settings, palette: Palette, map: Map, brain: Brain,
  wrench: Wrench, target: Target, puzzle: FileCode, sparkles: Sparkles, package: Package,
  folder: Folder, 'folder-open': FolderOpen, 'clipboard-list': ClipboardList,
  clipboard: Clipboard, 'clipboard-paste': ClipboardPaste, search: Search, rocket: Rocket,
  'trash-2': Trash2, bandage: Bandage, 'refresh-cw': RefreshCw, 'scroll-text': ScrollText,
  mail: Mail, send: Send, 'book-open': BookOpen, pencil: Pencil, 'pencil-line': PencilLine,
  pin: Pin, users: Users, eye: Eye, 'eye-off': EyeOff, x: X, link: Link, scissors: Scissors,
  'octagon-stop': SquareStop, 'square-stop': SquareStop, globe: Globe, camera: Camera,
  'bar-chart-3': BarChart3, egg: Egg, image: Image, film: Film, mic: Mic, 'mic-off': MicOff,
  calculator: Calculator, handshake: Handshake, upload: Upload, flame: Flame, zap: Zap,
  monitor: Monitor, mouse: MousePointer2, mouse2: MousePointer2,
  'alert-triangle': AlertTriangle, alert: AlertTriangle,
  'message-square': MessageSquare, 'message-square-plus': MessageSquarePlus,
  lock: Lock, 'flask-conical': FlaskConical, keyboard: Keyboard, 'file-text': FileText,
  'file-up': FileUp, 'circle-check-big': CircleCheckBig, 'circle-check': CircleCheck,
  'circle-x': CircleX, loader: Loader2, 'circle-plus': CirclePlus, 'circle-minus': CircleMinus,
  paw: PawPrint, plug: Plug, 'arrow-left': ArrowLeft, 'arrow-right': ArrowRight,
  save: Save, 'file-cog': FileCog, 'settings-2': Settings2, bell: Bell,
  'shield-check': ShieldCheck, music: Music, 'triangle-alert': TriangleAlert,
  terminal: Terminal, bug: Bug, 'chevron-down': ChevronDown, 'chevron-right': ChevronRight,
  star: Star, info: Info, play: Play, square: Square, cpu: Cpu, 'hard-drive': HardDrive,
  cog: Cog, compass: Compass, waypoints: Waypoints, network: Network, database: Database,
  archive: Archive, download: Download, 'external-link': ExternalLink, home: Home,
  'grip-vertical': GripVertical, 'more-horizontal': MoreHorizontal,
  'more-vertical': MoreVertical, plus: Plus, minus: Minus, 'check-check': CheckCheck,
  circle: Circle,
  'shopping-cart': ShoppingCart, 'hard-drive-download': HardDriveDownload,
  paperclip: Paperclip, timer: Timer, 'corner-down-left': CornerDownLeft, pause: Pause,
}

// emoji → lucide 图标名（兼容模板中硬写 emoji + 后端/manifest 下发的 emoji 字符串）
const _EMOJI: Record<string, string> = {
  '🤖': 'bot', '🧠': 'brain', '🎨': 'palette', '🗺': 'map', '🗺️': 'map',
  '🔧': 'wrench', '🧩': 'puzzle', '🔌': 'plug', '⚡': 'zap', '✨': 'sparkles',
  '🎯': 'target', '📦': 'package', '💬': 'message-square', '📋': 'clipboard-list',
  '📁': 'folder', '📂': 'folder-open', '🗂': 'folder', '🗂️': 'folder',
  '🔍': 'search', '🔄': 'refresh-cw', '🚀': 'rocket', '📤': 'upload',
  '👁': 'eye', '👁️': 'eye', '🙈': 'eye-off', '🗑': 'trash-2', '🗑️': 'trash-2',
  '✂️': 'scissors', '✏️': 'pencil', '📄': 'file-text', '📝': 'file-text',
  '📊': 'bar-chart-3', '🖼': 'image', '🖼️': 'image', '🎬': 'film', '🎞': 'film',
  '🎙': 'mic', '🎙️': 'mic', '🎤': 'mic', '🔇': 'mic-off', '🌐': 'globe',
  '⚠️': 'alert-triangle', '⚠': 'alert-triangle', '❌': 'circle-x', '✕': 'x',
  '✓': 'circle-check', '✅': 'circle-check-big', '📧': 'mail', '📨': 'mail',
  '📖': 'book-open', '⚙️': 'settings', '🖥️': 'monitor',
  '🖱️': 'mouse', '🧪': 'flask-conical', '🔐': 'lock', '🧮': 'calculator',
  '👥': 'users', '🤝': 'handshake', '🐣': 'egg', '👤': 'paw', '👈': 'arrow-left',
  '📥': 'clipboard-paste', '🔔': 'bell', '🛡': 'shield-check',
  '🗜': 'settings-2', '🎵': 'music', '🧭': 'compass', '📍': 'pin',
  '📅': 'calendar-days', '🛠': 'wrench',
}

function resolveName(raw: string): string {
  if (!raw) return 'package'
  if (_MAP[raw]) return raw
  if (_EMOJI[raw]) return _EMOJI[raw]
  return 'package'
}

const props = withDefaults(defineProps<{
  name: string
  size?: number | string
  strokeWidth?: number | string
}>(), { size: 16, strokeWidth: 2 })

const Comp = computed(() => {
  const name = resolveName(props.name)
  return _MAP[name] || _MAP['package']
})
</script>

<template>
  <component :is="Comp" :size="size" :stroke-width="strokeWidth" class="app-icon" />
</template>
