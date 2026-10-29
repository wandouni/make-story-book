/**
 * 封面画面绘制（AI 背景 + 书名 + 作者）。
 *
 * 纯 Canvas，不依赖 three —— 3D 纹理和平面预览共用同一份，避免两套封面
 * 画出来不一致。
 */

export const COVER_CANVAS_W = 768
export const COVER_CANVAS_H = 1080

/** 封面书名的兜底字体。 */
export const COVER_FALLBACK_FAMILY = 'Noto Serif SC'
export const COVER_FALLBACK_URL = '/api/assets/fonts/noto-serif-sc-700.woff2'

export interface CoverSpec {
  title: string
  author: string
  fontFamily?: string
  fontUrl?: string
  /** 已加载的 AI 封面背景图；null = 布面兜底。 */
  bg: HTMLImageElement | null
}

/** 逐字断行 —— 中文没有词边界。 */
function wrapCjk(ctx: CanvasRenderingContext2D, text: string, maxWidth: number) {
  const lines: string[] = []
  let line = ''
  for (const ch of text) {
    if (line && ctx.measureText(line + ch).width > maxWidth) {
      lines.push(line)
      line = ch
    } else {
      line += ch
    }
  }
  if (line) lines.push(line)
  return lines
}

export function paintCover(
  ctx: CanvasRenderingContext2D,
  cw: number,
  ch: number,
  spec: CoverSpec,
) {
  const { title, author, bg } = spec
  const family = spec.fontFamily || COVER_FALLBACK_FAMILY
  const font = (size: number) => `700 ${size}px "${family}", "Songti SC", serif`

  ctx.clearRect(0, 0, cw, ch)

  if (bg) {
    const scale = Math.max(cw / bg.width, ch / bg.height)
    const drawW = bg.width * scale
    const drawH = bg.height * scale
    ctx.drawImage(bg, (cw - drawW) / 2, (ch - drawH) / 2, drawW, drawH)

    const veil = ctx.createLinearGradient(0, ch * 0.12, 0, ch * 0.74)
    veil.addColorStop(0, 'rgba(20,16,10,0)')
    veil.addColorStop(0.32, 'rgba(20,16,10,0.28)')
    veil.addColorStop(0.62, 'rgba(20,16,10,0.24)')
    veil.addColorStop(1, 'rgba(20,16,10,0)')
    ctx.fillStyle = veil
    ctx.fillRect(0, 0, cw, ch)
  } else {
    ctx.fillStyle = '#4a3b2a'
    ctx.fillRect(0, 0, cw, ch)

    ctx.save()
    ctx.globalAlpha = 0.045
    ctx.strokeStyle = '#ffffff'
    ctx.lineWidth = 1
    for (let x = -ch; x < cw; x += 7) {
      ctx.beginPath()
      ctx.moveTo(x, 0)
      ctx.lineTo(x + ch, ch)
      ctx.stroke()
    }
    ctx.restore()

    ctx.strokeStyle = '#c8a86a'
    ctx.lineWidth = 3
    ctx.strokeRect(cw * 0.07, ch * 0.055, cw * 0.86, ch * 0.89)
    ctx.strokeStyle = 'rgba(200,168,106,0.45)'
    ctx.lineWidth = 1
    ctx.strokeRect(cw * 0.086, ch * 0.069, cw * 0.828, ch * 0.862)
  }

  const maxWidth = cw * 0.68
  const name = title.trim() || '无题'
  let size = Math.round(cw * 0.13)
  let lines: string[] = []
  while (size > 22) {
    ctx.font = font(size)
    lines = wrapCjk(ctx, name, maxWidth)
    if (lines.length <= 3) break
    size -= 6
  }
  ctx.font = font(size)

  ctx.fillStyle = bg ? '#fbf6ea' : '#f0e6d2'
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  const lineHeight = size * 1.35
  const top = ch * 0.34 - ((lines.length - 1) * lineHeight) / 2
  lines.forEach((line, i) => ctx.fillText(line, cw / 2, top + i * lineHeight))

  const ruleY = top + (lines.length - 1) * lineHeight + lineHeight * 1.18
  ctx.strokeStyle = 'rgba(200,168,106,0.7)'
  ctx.lineWidth = 2
  ctx.beginPath()
  ctx.moveTo(cw * 0.42, ruleY)
  ctx.lineTo(cw * 0.58, ruleY)
  ctx.stroke()

  const who = author.trim()
  if (who) {
    ctx.font = font(Math.round(cw * 0.055))
    ctx.fillStyle = bg ? 'rgba(251,246,234,0.92)' : 'rgba(240,230,210,0.8)'
    ctx.fillText(who, cw / 2, ruleY + size * 0.8)
  }
}

export function coverBgUrl(bookId: string, imageId: string) {
  return `/api/assets/books/${bookId}/images/${imageId}.png`
}
