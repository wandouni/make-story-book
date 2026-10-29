import { computed, shallowRef, toRaw } from 'vue'

/**
 * 快照式撤销栈。
 *
 * 为什么是快照而不是操作级 diff：拖拽编辑器里一次"移动"就是改四个数字，
 * 为每种操作写正反两个函数，等于把几何逻辑实现两遍，两边迟早不一致。
 * 整页块结构的深拷贝在几十个块这个量级上完全不值一提，换来的是
 * "撤销永远等于恢复当时的样子"，不会有反算误差。
 *
 * 快照里带上 `key`（这里是页面 id），是因为撤销栈只对**同一页**有意义：
 * 用户在第 3 页拖了两下，切到第 5 页，此时按 Ctrl+Z 应该什么都不做，
 * 而不是把第 3 页的内容贴到第 5 页上。key 对不上就清栈。
 */
export function useUndoRedo<T>(key: () => string | null, limit = 30) {
  interface Entry {
    key: string
    value: T
  }

  // shallowRef 而不是 ref：栈里装的是普通数据，不需要 Vue 深度代理它。
  // 用 ref 的话 Ref<Entry[]> 的深度解包类型会和泛型 T 打架。
  const past = shallowRef<Entry[]>([])
  const future = shallowRef<Entry[]>([])

  const canUndo = computed(() => past.value.length > 0)
  const canRedo = computed(() => future.value.length > 0)

  /**
   * 深拷贝一份快照。
   *
   * 必须先 toRaw：传进来的往往是 Pinia 里的响应式代理，直接把 Proxy 交给
   * structuredClone 会踩到内部槽，拿不到真正的数据。
   */
  const snapshot = (value: T): T => structuredClone(toRaw(value))

  /** 在**改变之前**调用，把当前状态存进来。 */
  function record(value: T) {
    const k = key()
    if (k === null) return

    // 换了页就把栈清掉，避免把上一页的快照贴到这一页
    const stack = past.value
    const stale = stack.length > 0 && stack[stack.length - 1].key !== k
    const base = stale ? [] : stack

    const next = [...base, { key: k, value: snapshot(value) }]
    // 超出上限就丢最老的
    past.value = next.length > limit ? next.slice(next.length - limit) : next
    // 新的改动会让"重做"分支失效
    future.value = []
  }

  /** 传入当前状态，返回要恢复到的状态；没有可撤销的返回 null。 */
  function undo(current: T): T | null {
    const k = key()
    const entry = past.value[past.value.length - 1]
    if (!entry || entry.key !== k) return null

    past.value = past.value.slice(0, -1)
    future.value = [...future.value, { key: k, value: snapshot(current) }]
    return entry.value
  }

  function redo(current: T): T | null {
    const k = key()
    const entry = future.value[future.value.length - 1]
    if (!entry || entry.key !== k) return null

    future.value = future.value.slice(0, -1)
    past.value = [...past.value, { key: k, value: snapshot(current) }]
    return entry.value
  }

  function clear() {
    past.value = []
    future.value = []
  }

  return { canUndo, canRedo, record, undo, redo, clear }
}
