import { createRouter, createWebHashHistory } from 'vue-router'
import { useAuth } from './stores/auth.js'
import Login from './views/Login.vue'
import Manage from './views/Manage.vue'
import Messages from './views/Messages.vue'
import Packets from './views/Packets.vue'

// Hash history so the SPA serves cleanly from FastAPI StaticFiles with no
// server-side catch-all fallback needed.
const routes = [
  { path: '/login', component: Login, meta: { public: true } },
  { path: '/', redirect: '/messages' },
  { path: '/manage', component: Manage },
  { path: '/messages', component: Messages },
  { path: '/packets', component: Packets },
]

const router = createRouter({ history: createWebHashHistory(), routes })

router.beforeEach((to) => {
  const auth = useAuth()
  if (!to.meta.public && !auth.token) return '/login'
  if (to.path === '/login' && auth.token) return '/'
})

export default router
