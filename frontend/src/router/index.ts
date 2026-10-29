import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'books',
      component: () => import('@/views/BookListView.vue'),
    },
    {
      path: '/books/:id',
      name: 'workspace',
      component: () => import('@/views/workspace/BookWorkspace.vue'),
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/',
    },
  ],
})

export default router
