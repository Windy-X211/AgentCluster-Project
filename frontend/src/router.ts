import { createRouter, createWebHashHistory } from 'vue-router'

export default createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', component: () => import('@/views/Home.vue') },
    { path: '/settings', component: () => import('@/views/Settings.vue') }
  ]
})
