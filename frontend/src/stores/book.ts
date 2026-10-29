import { defineStore } from 'pinia'
import { ref } from 'vue'

import * as booksApi from '@/api/books'
import * as pagesApi from '@/api/pages'
import type { Block, Book, Page, PresetPageOptions, UpdateBookRequest } from '@/types/book'

/**
 * 当前打开的绘本。整个工作区共用这一份状态。
 *
 * 页面操作都返回整页/整份页面列表，这里直接替换，不做增量合并 ——
 * 后端是唯一真源，前端不做第二套状态推导，避免两边算不一致。
 */
export const useBookStore = defineStore('book', () => {
  const book = ref<Book | null>(null)
  const loading = ref(false)
  // 最近一次分页返回的提示（页数不足、超出容量等），由 StoryStep 展示
  const warnings = ref<string[]>([])

  async function load(id: string) {
    loading.value = true
    try {
      book.value = await booksApi.getBook(id)
      warnings.value = []
    } finally {
      loading.value = false
    }
  }

  function reset() {
    book.value = null
    warnings.value = []
  }

  async function patchBook(payload: UpdateBookRequest) {
    if (!book.value) return
    book.value = await booksApi.updateBook(book.value.id, payload)
  }

  // 挂起中的原文保存。文本域的 @blur 会先发一次 PUT，紧接着的"分页"请求
  // 若不等它落盘，后端可能读到旧原文 —— 分页前的 flushStory() 就是为这个。
  let storySave: Promise<unknown> | null = null

  /**
   * 存故事原文，返回**入库后**的文本（后端会剥掉 Markdown 标记）。
   *
   * 拿返回值而不是原样回显：后端把 `## 标题` 清成 `标题` 之后，本地若还存着
   * 带标记的版本，StoryStep 的"页面与原文不一致"就会一直误报。
   */
  async function saveStory(text: string): Promise<string | null> {
    if (!book.value) return null
    const bookId = book.value.id
    book.value.storyText = text

    const promise = pagesApi.putStory(bookId, text).then((resp) => {
      // 期间可能已经切走了别的书，回来时别再写进去
      if (book.value?.id === bookId) book.value.storyText = resp.text
      return resp.text
    })
    storySave = promise
    try {
      return await promise
    } finally {
      storySave = null
    }
  }

  /** 等挂起的原文保存落盘。分页前必须调用。 */
  async function flushStory() {
    if (!storySave) return
    try {
      await storySave
    } catch {
      // 失败提示由 axios 拦截器统一弹出，这里只负责不阻塞后续流程
    }
  }

  async function runPaginate(pageCount: number, preset: string) {
    if (!book.value) return []
    await flushStory()
    const result = await pagesApi.paginate(book.value.id, pageCount, preset)
    book.value.pages = result.pages
    book.value.pageCount = pageCount
    warnings.value = result.warnings
    return result.warnings
  }

  function setPages(pages: Page[]) {
    if (book.value) book.value.pages = pages
  }

  /**
   * 保存单页文字。先本地乐观更新，请求回来后**只在用户没继续打字时**才用
   * 服务端结果覆盖 —— 否则慢请求返回会把用户刚敲的字回滚掉。
   */
  async function savePageText(number: number, text: string) {
    if (!book.value) return
    const target = book.value.pages[number - 1]
    if (!target) return
    target.text = text

    const saved = await pagesApi.updatePage(book.value.id, number, { text })
    const index = book.value.pages.findIndex((p) => p.id === saved.id)
    if (index >= 0 && book.value.pages[index].text === text) {
      book.value.pages[index] = saved
    }
  }

  /** 同 savePageText，只是改的是提示词。 */
  async function savePagePrompt(number: number, prompt: string) {
    if (!book.value) return
    const target = book.value.pages[number - 1]
    if (!target) return
    target.imagePrompt = prompt

    const saved = await pagesApi.updatePage(book.value.id, number, { imagePrompt: prompt })
    const index = book.value.pages.findIndex((p) => p.id === saved.id)
    if (index >= 0 && book.value.pages[index].imagePrompt === prompt) {
      book.value.pages[index] = saved
    }
  }

  /** 重新拉整本书。生图任务结束后调用，用服务端状态覆盖本地推测。 */
  async function reload() {
    if (book.value) await load(book.value.id)
  }

  /**
   * 改单页的版式或样式。返回服务端保存后的那一页。
   *
   * 不做防回滚守卫（对比 savePageText）：这里的值来自选择器/滑块，不是
   * 用户连续敲的文本，不存在"慢请求回来把刚输的字冲掉"的问题。
   */
  async function updatePageStyle(
    number: number,
    payload: { preset?: string; options?: PresetPageOptions },
  ) {
    if (!book.value) return null
    const saved = await pagesApi.updatePage(book.value.id, number, payload)
    const index = book.value.pages.findIndex((p) => p.id === saved.id)
    if (index >= 0) book.value.pages[index] = saved
    return saved
  }

  // ------------------------------------------------------------ 模式转换

  /**
   * 预设页 → 自由页。展开在后端做（render/presets.py），前端不碰几何 ——
   * 视觉等价性由后端测试保证，前端另算一遍只会多一个不一致的来源。
   */
  async function toFreePage(number: number) {
    if (!book.value) return null
    const page = await pagesApi.toFreePage(book.value.id, number)
    replacePage(page)
    return page
  }

  /** 自由页 → 预设页。**有损**：块的位置、旋转、逐块样式都会丢。 */
  async function toPresetPage(number: number, preset: string) {
    if (!book.value) return null
    const page = await pagesApi.toPresetPage(book.value.id, number, preset)
    replacePage(page)
    return page
  }

  // ------------------------------------------------------------ 块

  /**
   * 改一个块。**同时更新本地状态**，拖拽结束后才调用。
   *
   * 不做 savePageText 那种防回滚守卫：这里的值来自拖拽/控件，不是用户
   * 连续敲的文本，不存在"慢响应回来把刚输的字冲掉"。而拖拽过程中前端
   * 只改本地 draft（见 BlockOverlay），不发请求。
   */
  async function patchBlock(
    number: number,
    blockId: string,
    payload: pagesApi.UpdateBlockRequest,
  ) {
    if (!book.value) return null
    applyBlockPatch(number, blockId, payload)
    const page = await pagesApi.updateBlock(book.value.id, number, blockId, payload)
    replacePage(page)
    return page
  }

  /** 只改本地状态，不发请求。拖拽过程中用。 */
  function applyBlockPatch(
    number: number,
    blockId: string,
    payload: pagesApi.UpdateBlockRequest,
  ) {
    const page = book.value?.pages[number - 1]
    if (!page || page.mode !== 'free') return
    const block = page.blocks.find((b) => b.id === blockId)
    if (!block) return
    Object.assign(block, payload)
  }

  async function addBlock(number: number, payload: pagesApi.NewBlockRequest) {
    if (!book.value) return null
    const page = await pagesApi.addBlock(book.value.id, number, payload)
    replacePage(page)
    return page
  }

  async function removeBlock(number: number, blockId: string) {
    if (!book.value) return null
    const page = await pagesApi.deleteBlock(book.value.id, number, blockId)
    replacePage(page)
    return page
  }

  /**
   * 用整页快照覆盖当前页（撤销/重做用）。
   *
   * 撤销栈存的是块的深拷贝，这里先写本地让界面立刻回退，再整体推给后端
   * —— 逐块 diff 再发请求既慢又容易和后端状态错位。
   */
  async function restorePageBlocks(number: number, blocks: Block[]) {
    if (!book.value) return null
    const page = book.value.pages[number - 1]
    if (!page || page.mode !== 'free') return null

    page.blocks = structuredClone(blocks)

    const pageId = page.id
    const server = await pagesApi.updatePage(book.value.id, number, { blocks })
    const index = book.value.pages.findIndex((p) => p.id === pageId)
    if (index >= 0) book.value.pages[index] = server
    return server
  }

  /** 后端按 id 定位页面（用户可能在两次请求之间删过页）。 */
  function replacePage(page: Page) {
    if (!book.value) return
    const index = book.value.pages.findIndex((p) => p.id === page.id)
    if (index >= 0) book.value.pages[index] = page
  }

  async function removePage(number: number) {
    if (!book.value) return
    setPages(await pagesApi.deletePage(book.value.id, number))
  }

  /** 一次删多页。页码是**当前**页码（1-based），后端按下标倒序删。 */
  async function removePages(numbers: number[]) {
    if (!book.value || !numbers.length) return
    setPages(await pagesApi.batchDeletePages(book.value.id, numbers))
  }

  async function splitPage(number: number, atCharIndex: number) {
    if (!book.value) return
    setPages(await pagesApi.splitPage(book.value.id, number, atCharIndex))
  }

  async function mergeNext(number: number) {
    if (!book.value) return
    setPages(await pagesApi.mergeNext(book.value.id, number))
  }

  return {
    book,
    loading,
    warnings,
    load,
    reset,
    patchBook,
    saveStory,
    flushStory,
    runPaginate,
    setPages,
    savePageText,
    savePagePrompt,
    updatePageStyle,
    reload,
    removePage,
    removePages,
    splitPage,
    mergeNext,
    toFreePage,
    toPresetPage,
    patchBlock,
    applyBlockPatch,
    addBlock,
    removeBlock,
    restorePageBlocks,
  }
})
