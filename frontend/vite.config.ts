import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  // Electron 生产模式需要相对路径（file:// 协议下绝对路径找不到资源）
  const isElectron = !!env.ELECTRON
  return {
    plugins: [vue()],
    base: isElectron ? './' : '/',
    resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
    server: {
      host: '127.0.0.1',
      port: 5173,
      strictPort: true,
      proxy: { '/api': 'http://127.0.0.1:' + (process.env.PORT || '8870') }
    },
    build: {
      // Electron asar 打包后资源路径是 file://，需要相对 base
      rollupOptions: isElectron ? {
        output: {
          manualChunks: {
            vendor: ['vue', 'vue-router', 'pinia'],
            xlsx: ['xlsx'],
          },
        },
      } : undefined,
    },
  }
})
