import { ref } from 'vue'

/**
 * 全屏翻页预览是否开着。
 *
 * 它是**模块级**的，不是某个组件的局部状态 —— 因为抢键的是两个互不相识的
 * 监听器：`FlipBook` 和 `EditStep` 各自在 `window` 上挂了 keydown，都吃左右
 * 方向键。预览开着时用户按右键，本该只翻页，但 `EditStep` 那个监听器照样会跑，
 * 把选中的块悄悄挪 1mm。实测过：一次 `ArrowRight` 计数器从「第 1 / 5 页」翻到
 * 「第 2-3 页」，**同一个块**的 `xMm` 从 0 变成 1。不报错、不弹提示，用户只会
 * 觉得"块自己动了"，而且那一下还进了撤销栈。
 *
 * 用共享 ref 而不是 `provide`/`inject`：这个项目里没用过 provide/inject，而
 * 「全屏弹窗开着时键盘归弹窗」本来就该是全局的一条规矩，不是某棵子树的事。
 * 也不是 Pinia store —— 一个布尔值不值得开一个 store。
 *
 * 只有 `FlipBook` 写它，写的地方就一处 `watch(visible)`。谁在别处也写，谁就
 * 得负责把"弹窗还开着吗"这件事答对。
 */
export const previewOpen = ref(false)
