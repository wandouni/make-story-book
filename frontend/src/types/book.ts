// 与 backend/app/models/book.py 一一对应。
// 改动这里时务必同步改那边，反之亦然。

export type ImageStatus = 'none' | 'pending' | 'running' | 'done' | 'failed'

export interface TextStyle {
  fontFamily: string
  fontSizePt: number
  fontWeight: 400 | 700
  color: string
  align: 'left' | 'center' | 'right' | 'justify'
  verticalAlign: 'top' | 'middle' | 'bottom'
  lineHeight: number
  letterSpacing: number
  writingMode: 'horizontal-tb' | 'vertical-rl'
  backgroundColor: string | null
  paddingMm: number
  /** 底衬圆角。满版叠字的白色底衬要它，纯文字块用不到。 */
  borderRadiusMm: number
  autoFit: boolean
}

export interface BlockBase {
  id: string
  xMm: number
  yMm: number
  widthMm: number
  heightMm: number
  rotationDeg: number
  zIndex: number
  locked: boolean
  opacity: number
}

export interface TextBlock extends BlockBase {
  type: 'text'
  content: string
  style: TextStyle
}

export interface ImageBlock extends BlockBase {
  type: 'image'
  imageId: string | null
  fit: 'cover' | 'contain'
  focusX: number
  focusY: number
  borderRadiusMm: number
  /** 满版：图片向四周溢出出血区。坐标仍按成品区记，溢出量由渲染端按模式加。 */
  bleed: boolean
}

export type Block = TextBlock | ImageBlock

export interface Background {
  type: 'none' | 'color'
  color: string
}

export interface PageBase {
  id: string
  text: string
  imageId: string | null
  imagePrompt: string
  imageStatus: ImageStatus
  background: Background
}

/**
 * 叠字区的显式矩形，mm，相对成品区。智能排版挑中的落点就存在这里。
 *
 * 越界的值不用前端操心：后端 `presets._clamp_rect_mm` 会把它夹进成品区
 * （所以类型上不加约束，手改 book.json 打错一个负号不该让书打不开）。
 */
export interface OverlayRect {
  xMm: number
  yMm: number
  widthMm: number
  heightMm: number
}

export interface PresetPageOptions {
  fontFamily: string
  fontSizePt: number
  color: string
  align: 'left' | 'center' | 'right'
  lineHeight: number
  paddingMm: number
  /**
   * 叠字落在哪里。'auto' 让后端分析插图、把文字放进细节最少的那条横带里；
   * 没有插图时自动退回 'bottom'。
   *
   * **`overlayRect` 非 null 时它不起作用** —— 矩形压过它。所以改这个值时
   * 必须同一次把 `overlayRect` 清成 null（见 TextStylePanel），否则用户点了
   * 「上」屏幕上毫无变化。
   */
  overlayPosition: 'auto' | 'top' | 'bottom' | 'center'
  /**
   * 智能配置写下的固定落点。null = 按 `overlayPosition` 的规则自动算。
   *
   * 用 `| null` 而不是 `?`：pydantic 的 Optional 序列化成 null，
   * 与上面 Background.color 的既有写法一致。
   */
  overlayRect: OverlayRect | null
  /** 叠字底衬（半透明白色圆角板）的不透明度。0 = 不要底衬，纯靠字色压图。 */
  overlayPanelOpacity: number
  imageFocusX: number
  imageFocusY: number
}

export interface PresetPage extends PageBase {
  mode: 'preset'
  preset: string
  options: PresetPageOptions
}

export interface FreePage extends PageBase {
  mode: 'free'
  blocks: Block[]
}

export type Page = PresetPage | FreePage

export interface TrimSize {
  id: string
  label: string
  widthMm: number
  heightMm: number
}

export interface Book {
  id: string
  schemaVersion: number
  title: string
  author: string
  protagonist: string
  characterDescription: string
  style: string
  trimSize: TrimSize
  bleedMm: number
  pageCount: number
  storyText: string
  coverImageId: string | null
  pages: Page[]
  imageService: 'doubao' | 'tongyi'
  createdAt: string
  updatedAt: string
}

export interface BookSummary {
  id: string
  title: string
  author: string
  style: string
  pageCount: number
  coverImageId: string | null
  /** 封面缩略图按开本比例裁，拿它去 meta.trimSizes 里查宽高。 */
  trimSizeId: string
  updatedAt: string
}

export interface CreateBookRequest {
  title: string
  author?: string
  protagonist?: string
  style?: string
  trimSizeId?: string
  pageCount?: number
  imageService?: 'doubao' | 'tongyi'
}

export interface UpdateBookRequest {
  title?: string
  author?: string
  protagonist?: string
  characterDescription?: string
  style?: string
  trimSizeId?: string
  bleedMm?: number
  pageCount?: number
  imageService?: 'doubao' | 'tongyi'
}

export function isFreePage(page: Page): page is FreePage {
  return page.mode === 'free'
}

export function isPresetPage(page: Page): page is PresetPage {
  return page.mode === 'preset'
}
