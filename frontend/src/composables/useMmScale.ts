/**
 * mm → px 的换算常数。
 *
 * CSS 的 `mm` 在 Chromium 里固定映射 `96/25.4` px，预览的 iframe 和导出 PDF
 * 都按这个走 —— 项目里所有几何都是 mm，唯一的换算就是这一个数。
 *
 * 抽出来是因为它有两个使用者（PagePreview 自己算自适应缩放、FlipBook 算对开
 * 的共享缩放），两处各写一份迟早会漂。后端 `render/geometry.py` 里有对应的
 * 一份，改这里记得看那边。
 */
export const PX_PER_MM = 96 / 25.4
