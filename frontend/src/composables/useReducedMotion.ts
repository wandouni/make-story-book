import { onBeforeUnmount, ref, type Ref } from 'vue'

/**
 * 系统是否开了"减弱动态效果"。
 *
 * 3D 翻页那边有两处**前庭反应**级别的动效：进场时书从远处飞进来、翻页时整张纸
 * 转 180°。开了这个开关的人是真的会不适，不是"觉得花哨"。所以给的是**跳过**，
 * 不是"慢一点"。
 *
 * 返回的是 ref 而不是读一次的值：用户可能在预览开着的时候去改系统设置，Chromium
 * 会派发 `change`。`matchMedia` 的监听器要在组件卸载时摘掉 —— 这个 composable
 * 必须在 `setup()` 里调用才能拿到 `onBeforeUnmount`。
 */
export function useReducedMotion(): Ref<boolean> {
  const query = window.matchMedia('(prefers-reduced-motion: reduce)')
  const reduced = ref(query.matches)

  const onChange = (event: MediaQueryListEvent) => {
    reduced.value = event.matches
  }
  query.addEventListener('change', onChange)
  onBeforeUnmount(() => query.removeEventListener('change', onChange))

  return reduced
}
