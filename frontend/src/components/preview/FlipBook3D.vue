<script setup lang="ts">
import * as THREE from 'three'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { useReducedMotion } from '../../composables/useReducedMotion'
import {
  applyBend,
  bendDepthMaterial,
  bendUniforms,
  setSpineEnd,
  type BendUniforms,
} from './bendMaterial'
import {
  COVER_CANVAS_H,
  COVER_CANVAS_W,
  COVER_FALLBACK_FAMILY,
  COVER_FALLBACK_URL,
  coverBgUrl as coverBgSrcOf,
  paintCover as paintCoverShared,
} from './coverArt'

/**
 * 3D 翻页预览：把整本书放进一个 WebGL 场景里翻。
 *
 * **页面纹理来自后端的截图端点**（`/render/page/{n}/shot.png`），而那个端点
 * 打开的是和 iframe 预览、和导出 PDF **同一个渲染端点**。所以 3D 里的字和
 * 版式就是印出来的样子 —— 前端自己用 canvas 重画一遍页面内容的话，那才是
 * 真正的第二套模板，也正是这个项目一直在避免的事。
 *
 * （唯一的例外是精装封面的布面和书名，见 coverTexture —— 那是**装订**不是页面，
 * 后端没有"封面模板"这个概念，第 1 页就是普通页。）
 *
 * 但要清楚 3D 模式的定位：它给的是"像一本书"的观感，画面是位图，放大看会
 * 糊，排版细节也不如 iframe 锐利。平面预览才是核对细节的那一份。两者分工，
 * 不是替代关系 —— 所以 FlipBook 里保留了「平面 / 3D」开关。
 *
 * 世界单位：1 = 100mm。页面尺寸本来就在 mm 里，除一下就行，不用另立一套。
 */
const props = defineProps<{
  bookId: string
  pageCount: number
  /** 当前屏的左页页码；单页模式下它就是当前页。 */
  leftPage: number
  /** 对开时的右页；null = 这一屏只有一页（封面、末页落单、单页模式）。 */
  rightPage: number | null
  mode: 'preview' | 'print'
  trimWidthMm: number
  trimHeightMm: number
  bleedMm: number
  /** 只用来画封面上的字，缺了就是"无题"。 */
  bookTitle?: string
  bookAuthor?: string
  /**
   * 封面书名/作者用的字体族，以及它 700 字重的 woff2 地址。
   *
   * 这个组件拿不到 `book.pages`（只拿到几何和标题），所以字体由调用方从
   * `book.pages[0].options.fontFamily` 解析后传进来。不传就退回宋体 ——
   * 写死宋体是不行的：默认字体已经是楷体，写死会让新书"正文楷体、封面宋体"。
   */
  fontFamily?: string
  fontUrl?: string
  /** AI 生成的封面背景图 id；没有时封面退回布面底色。 */
  coverImageId?: string
}>()

const emit = defineEmits<{ prev: []; next: [] }>()

/**
 * 系统开了"减弱动态效果"。这个组件里有两处**前庭反应**级别的动效：进场时整本书
 * 从远处飞进来、翻页时一张纸转 180°。开了这个开关的人是真的会不适，所以给的是
 * **跳过**而不是"慢一点"：不飞入、不掀封面，翻页直接换。
 *
 * 拖拽翻页不在此列 —— 那是用户自己拖的，跟手是预期行为，不是"动效"。
 *
 * 必须在这里（`setup()` 作用域）调用：它内部要用 `onBeforeUnmount` 摘 matchMedia
 * 的监听器。e2e 跑在 headless 里、默认 `no-preference`，所以那些"进场真的掀了
 * 封面"的断言不受影响。
 */
const reducedMotion = useReducedMotion()

/** defineEmits 的类型是重载式的，吃不下联合类型的参数名，所以收一个口。 */
function go(dir: 1 | -1) {
  if (dir === 1) emit('next')
  else emit('prev')
}

const MM = 100
/** 页面横向细分段数。弯曲是靠逐顶点位移做的，段数太少会折成棱角。 */
const SEGMENTS = 32
/** 翻页过程中纸张拱起的最大幅度，相对页宽。 */
const CURL = 0.14
/**
 * **静止时**书脊那侧页面抬起的幅度，相对页宽。
 *
 * 真书摊开不是两块平板：页面本来是平的，是书脊的圆背把它顶起来，从书脊向书口
 * 是一条很浅的弧。这条弧就是"像一本书"和"像一张贴图"的分界 —— 少了它，再怎么
 * 斜放、有厚度、有阴影，页面本身还是平的。
 *
 * 12% 是目视调的。**别按"明暗差多少"去估这个值** —— 4.5% 那一版就是这么废掉的：
 * 它只走"法线偏转 → 漫射光 → 明暗"这一条路，而偏转角只有 `atan(A·π/2)`，4.5%
 * 折合 4°，页面又是近白色，实测整个页面横切上蓝通道只差 1 个灰阶，等于没有。
 * 真正把弧读出来的是**轮廓**：抬到 12% 页宽时，天头地脚那条边在中缝处比书口高出
 * 一截，投影成一道很明显的折线 —— 那才是"这是一本书"的信号。
 *
 * **方向只能是"书脊抬起"，不能是"书口垂下"**：页面底下就是纸垛，往下垂会沉进去，
 * 被垛挡住（见 `bendMaterial.ts` 里 BEND_POSITION 那段）。抬起还有个白送的好处 —— 书脊那圈本来就要靠
 * `spine` 那道布面把缝填上，页面抬起来正好把它露出来一点。
 *
 * 没跟 `stackThickness()` 挂钩：19 页的书垛才 3mm，真按"圆背顶起多少"折算这条弧
 * 该小到看不见，而薄册子恰恰是最需要它的时候。这是个刻意的风格化取值。
 */
const REST_CURL = 0.12

const FLIP_MS = 720
/** 进场总时长。飞入 630ms + 掀封面 540ms + 交接 330ms，见 render()。 */
const INTRO_MS = 1500

/**
 * 进场时相机的额外偏移：起点比静止机位**更近、更低**，随进场阻尼回静止机位。
 * 起点近 16% 是"推近看"，低 10% 让俯角更斜 —— 两者叠起来才有纵深，光靠书自己
 * 从 z = -6 飞进来是"一个平面贴过来"，没有"镜头在动"的感觉。
 *
 * **静止机位仍由 `fitCamera()` 定，`CAMERA_MARGIN = 1.5` 不要动** —— 它的推导
 * 前提（"纸是逐顶点弯出来的，没有现成包围盒"）在弯折搬到 GPU 之后依然成立。
 */
const INTRO_CAM_PULL = 0.16
const INTRO_CAM_DROP = 0.1
/** 还没贴上纹理时的纸色。 */
const PAPER = 0xf7f3ea
/**
 * 翻动中的那张纸抬离页面多少。**这个值同时是阴影贴图能不能分辨的门槛**：
 * 阴影相机 4 个世界单位宽 + 1024 贴图 → 一个纹素约 0.004；只抬 0.002 的话
 * 纸的影子和纸自己落在同一个纹素里，会被 bias 抹掉（现象是"翻页时没有影子"，
 * **不报错**）。0.012 = 1.2mm 约 3 个纹素，扣掉 normalBias 还剩 2 个。
 */
const LIFT = 0.012
/** 页面抬离页垛顶面，免得共面打架。 */
const PAGE_Z = 0.004

// ---------------------------------------------------------------- 书的身体尺寸

/** 内页纸厚。真书 80g 胶版纸约 0.1mm —— 世界单位 1 = 100mm，所以是 0.001。 */
const SHEET_MM = 0.1
/** 再薄也要看起来是"一本册子"：3 页按真厚度算只有 0.3mm，侧面看是一条线。 */
const STACK_MIN_MM = 3

/**
 * 单侧纸垛的最小厚度（场景单位）。**不能是 0** —— `scale.z = 0` 会让盒子退化，
 * 页口贴图也会被压成一条线。0.008 场景单位 ≈ 0.8mm：比一张纸厚，比最薄的一整本
 * 书（`STACK_MIN_MM` = 3mm）薄，所以只在一侧快翻完时才起作用。
 */
const MIN_HALF_T = 0.008
/** 再厚就成砖了：16mm 的垛配上 A5 页宽，斜放时页口吃掉小半个画面。 */
const STACK_MAX_MM = 16
/** 精装封面纸板厚。 */
const BOARD_MM = 2.4
/** 包边（飘口）：封面比内页大出来的一圈，只出在页口/天头/地脚，书脊那侧不出。 */
const OVERHANG_MM = 2.5

const BOARD_T = BOARD_MM / MM
const OVER = OVERHANG_MM / MM
/** 页口贴图的边长。128 够画 140 条线还不出摩尔纹，再大是浪费。 */
const EDGE_SIZE = 128

/**
 * 静止视角：镜头平着正对书页（yaw / pitch 都是 0）。
 *
 * 立体感不靠斜放了 —— 书口的纸垛厚度、书脊、阴影和翻页时纸张自己转过来的
 * 角度已经足够。正对也让左右两侧的翻页完全对称。斜放的角度只在进场动画里
 * 短暂出现，落定后回到正对。
 */
const REST_YAW = 0
const REST_PITCH = 0

/**
 * 悬停时书朝镜头推近一点、转正一点 —— 静止姿态是"斜放在桌上"，悬停是"拿起来看"。
 * 沿用参考实现的量级（`position.z += 0.08` / `rotation.y += 0.14`）：0.08 相对相机
 * 距离（约 4）是 2%，刚好看得出来，又不至于像缩放。
 */
const HOVER_PUSH = 0.08
const HOVER_YAW = 0.14
/** 悬停阻尼速率（1/秒）。8 ≈ 0.12 秒走到 63%、0.4 秒基本到位。 */
const HOVER_RATE = 8

/**
 * 相机留白系数。这个数有推导，不是试出来的：
 *
 * 纸转到约 2/3 时自由边离镜头最近，距镜头只剩 `d - w`，被放大 `d/(d-w)` 倍。
 * 要它仍装得下得满足 `(h/2)·d ≤ tan(vFov/2)·(d-w)²`。代入 A5 对开
 * （w=1.48, h=2.10, vFov=20°）解出 d ≥ 8.66，而只按静态包围盒算是 5.95 ——
 * 1.5 就是给这段留的（8.93 > 8.66）。斜放之后近角又朝镜头挪了约 0.54，
 * 1.35 只剩不到 1% 余量，实测会贴框。
 *
 * **这个数没有解析解**：纸是逐顶点弯出来的，没有现成的包围盒可算。
 */
const CAMERA_MARGIN = 1.5
/** 接影子的平面放在书背面之后多远。太小影子贴着书，看不出"浮起来"。 */
const SHADOW_GAP = 0.18

// ---------------------------------------------------------------- 拖拽

/** 越线才认作"拖动"，不然点击会被拖拽逻辑吃掉（手抖两像素就翻不动页）。 */
const DRAG_SLOP_PX = 6
/** 抬手时的落点：拖过一半就翻过去，没到一半弹回。 */
const RELEASE_PROGRESS = 0.5
/**
 * 甩动的判定阈值，单位是「每毫秒推进的进度」。翻一屏是 1.0，所以 0.0015/ms
 * 意味着约 670ms 甩完一屏 —— 比自动翻页的 FLIP_MS(720ms) 还快，是明确的"甩"。
 */
const FLICK_VELOCITY = 0.0015
/** 抬手前静止了这么久就不算甩（见 onPointerUp）。 */
const VELOCITY_STALE_MS = 90
/** 弹回/补完的基准时长，按剩余距离缩，见 render()。 */
const SETTLE_MS = 420

// ---------------------------------------------------------------- 进场时间线

const container = ref<HTMLElement | null>(null)
const loading = ref(true)
const failed = ref('')

// ---------------------------------------------------------------- 场景

let renderer: THREE.WebGLRenderer | null = null
let scene: THREE.Scene | null = null
let camera: THREE.PerspectiveCamera | null = null
/** 静止机位的 z（`fitCamera()` 算出来的）。进场时相机绕着它做偏移。 */
let cameraRestZ = 0
let bookGroup: THREE.Group | null = null
let leftMesh: THREE.Mesh | null = null
let rightMesh: THREE.Mesh | null = null
let flipPivot: THREE.Group | null = null
let flipFront: THREE.Mesh | null = null
let flipBack: THREE.Mesh | null = null

// 书的身体（几何只建一次，位置靠 mesh.scale/position，见 placeBody）
let spine: THREE.Mesh | null = null
let stackL: THREE.Mesh | null = null
let stackR: THREE.Mesh | null = null
let boardFront: THREE.Mesh | null = null
let boardBack: THREE.Mesh | null = null
let shadowCatcher: THREE.Mesh | null = null
let blob: THREE.Mesh | null = null
/**
 * 页口图层。**左右两半垛各有一套**（V = 书口那面，H = 天头地脚那两面）—— 摊开的
 * 书左边是已翻过的那摞、右边是没翻的，厚度不同，页口的线数也就该不同。共用一套的
 * 话，薄的那侧贴图会被拉长、线变稀，看着像纸被抻了。
 */
let edgeVL: EdgeLayer | null = null
let edgeHL: EdgeLayer | null = null
let edgeVR: EdgeLayer | null = null
let edgeHR: EdgeLayer | null = null
/** 上一次画页口用的垛厚，左右各记一份。厚度没变就不重画（重画要重传纹理）。 */
let edgeStackTL = -1
let edgeStackTR = -1
/** 进场时从右往左掀开的那块封面。铰链在书脊上，只在首屏出现。 */
let coverPivot: THREE.Group | null = null
let coverMesh: THREE.Mesh | null = null
/** 封面的三个材质（布面/封面画/衬页），淡出时一起改 opacity。 */
let coverFaces: THREE.MeshStandardMaterial[] = []

let frame = 0
let observer: ResizeObserver | null = null

/**
 * 悬停的位移和转角加在**外层**这一层上。
 *
 * 进场动画整个在写 `bookGroup` 的 position/rotation（见 `stepIntro`），悬停也写
 * 同一个对象的话两边会打架 —— 分成父子两层就各管各的。`layout()` 里所有坐标都是
 * `bookGroup` 的局部空间，多一层父级对它完全透明。
 */
let hoverGroup: THREE.Group | null = null
/** 指针是不是停在画布上。 */
let hovering = false
/** 悬停量 0→1，阻尼过渡。进场期间会被压成 0。 */
let hoverAmt = 0
let lastFrameAt = 0

/**
 * 只创建一次、**场景遍历够不着**的资源：`scene.background` 和几个材质的 map。
 * 几何和材质本体一律靠 onBeforeUnmount 里的 scene.traverse 释放 —— 见那里的
 * 注释（disposables 数组那套是错的）。
 */
const offScene: { dispose: () => void }[] = []

/**
 * 截图 URL → 已加载的纹理。同一页翻回来时不必再请求一次。
 *
 * 键是 URL 而不是页码：URL 里带着 `mode`（成品/含出血），换版式后同一页的图是
 * 另一张 —— 用页码做键会拿到上一个版式的画面。
 *
 * 存 `number` 是为了让滑动窗口能按页码淘汰（见 `evictTextures`）。
 */
const textures = new Map<string, { number: number; texture: THREE.Texture }>()

/**
 * 纹理滑动窗口的半径（页）。`startFlip` 最多用到 `right + 2` / `left - 2`，所以
 * ±2 就够覆盖一次翻页的全部用图；取 4 是给"翻过去又翻回来"留余量，免得刚淘汰
 * 完就要重新请求。稳态下最多 9 页页面纹理在显存里。
 */
const TEXTURE_WINDOW = 4

/** 屏幕上当前显示的页码。翻页动画结束后才更新，动画期间仍是旧值。 */
let shown: { left: number; right: number | null } = { left: 1, right: null }

let introStart = 0
/** 'cover' = 首屏，飞入后掀封面；'fly' = 其他屏，只有飞入。 */
let introKind: 'cover' | 'fly' = 'fly'

/**
 * 翻页的唯一状态。`progress ∈ [0,1]` 是**唯一真值**：auto 由时间推进、drag 由
 * 指针推进、settle 由时间从松手处补完。角度和拱起量都从它派生 —— 三条路径共用
 * 同一套几何，不会出现"拖到一半松手，纸的形状和自动翻页不一样"。
 *
 * 没有另立 drag 状态：两者差别只有"谁在写 progress"，几何准备（哪几张面贴哪几页、
 * pivot 摆哪、谁可见）一模一样。分两处写迟早会漂，而漂的表现是"拖动翻页时露出的
 * 下一页不对"，很难查。
 */
let flip: {
  dir: 1 | -1
  /** 这次要翻到哪一屏 —— 不能到落地时再去读 props，那样动画途中用户又点了的话，
   *  这次翻页会直接"跳"到最新页码。 */
  to: { left: number; right: number | null }
  progress: number
  mode: 'auto' | 'drag' | 'settle'
  startedAt: number
  settleFrom: number
  settleTo: number
  settleAt: number
} | null = null

/** 正在按着的那根手指。`active` = 已经越过 slop、真正在写 flip.progress。 */
let drag: {
  pointerId: number
  x0: number
  baseProgress: number
  active: boolean
  /** 按下时已经有翻页在跑 → 那次点击推迟到抬手才发，见 onPointerDown。 */
  deferred: boolean
  dir: 1 | -1
  travel: number
  lastAt: number
  /** 每毫秒推进的进度，抬手时靠它判断"甩"。 */
  velocity: number
} | null = null

/** 翻页途中用户又点了：记下最新的目标，等这次翻完接着翻。 */
let pending: { left: number; right: number | null } | null = null

/**
 * 封面开合动画。封面现在是首屏的静止状态（不再只是进场效果），所以从封面
 * 翻进内页、或从内页翻回封面都走它。
 *
 * 相机正对书页时，封面转到 90°（progress = 0.5）正好侧对镜头、投影成一条线 ——
 * 底下的单页/对开几何就在那一刻切换，眼睛抓不住。封面在侧对前后淡出/淡入。
 */
let coverFlip: {
  dir: 1 | -1
  to: { left: number; right: number | null }
  progress: number
  startedAt: number
  switched: boolean
} | null = null

/** 封面 AI 背景图；加载完成后触发重画。null = 用布面底色兜底。 */
let coverBgImg: HTMLImageElement | null = null
let coverBgUrl = ''
let coverRedraw: (() => void) | null = null

/** DEV 构建里挂在 canvas 上的只读状态，给 e2e 用，见 onMounted。 */
let debug: Record<string, unknown> | null = null

function pageSizeMm() {
  const bleed = props.mode === 'print' ? props.bleedMm : 0
  return { w: (props.trimWidthMm + 2 * bleed) / MM, h: (props.trimHeightMm + 2 * bleed) / MM }
}

/** 垛厚 = 线性于页数，只卡两头：下限挡"3 页薄如纸"，上限挡"200 页厚成砖"。 */
function stackThickness() {
  const mm = Math.min(Math.max(props.pageCount * SHEET_MM, STACK_MIN_MM), STACK_MAX_MM)
  return mm / MM
}

/**
 * 已经翻过去的比例（0 = 刚打开，1 = 翻到末屏）。左右两垛的厚度按它分。
 *
 * `shown.left` 在两种版式下都正好是"当前左页的页码"（对开时 2、4、6…，单页时
 * 1、2、3…），所以 `shown.left - 1` 就是**已经翻过去的页数**，一条公式两种版式通用。
 * 别按"第几屏"算 —— 单页模式下一屏一页、对开下一屏两页，切版式时厚度会跳一下。
 */
function flippedRatio() {
  return clamp01((shown.left - 1) / Math.max(props.pageCount - 1, 1))
}

function shotUrl(number: number) {
  return `/api/books/${props.bookId}/render/page/${number}/shot.png?mode=${props.mode}&scale=2`
}

function clamp01(value: number) {
  return value < 0 ? 0 : value > 1 ? 1 : value
}

// ---------------------------------------------------------------- 纸

/** 还没加载出纹理时的纸色。用暖白而不是纯白 —— 纯白在暖色场景里会发灰。 */
function paperMaterial(uniforms: BendUniforms) {
  return applyBend(
    new THREE.MeshStandardMaterial({ color: PAPER, roughness: 0.92, metalness: 0 }),
    uniforms,
  )
}

/** 硬壳书外面的布面。比纸暗得多，这样书的轮廓才立得住。 */
function clothMaterial() {
  return new THREE.MeshStandardMaterial({ color: 0x7a6448, roughness: 0.85, metalness: 0 })
}

/** 衬页（封二）：比内页暖、没有内容，掀开封面第一眼看到的就是它。 */
function endpaperMaterial() {
  return new THREE.MeshStandardMaterial({ color: 0xefe6d4, roughness: 0.95, metalness: 0 })
}

/** 页口的一张贴图 + 它的画布 + 材质。两个朝向各一张。 */
type EdgeLayer = {
  texture: THREE.CanvasTexture
  ctx: CanvasRenderingContext2D
  material: THREE.MeshStandardMaterial
}

/**
 * 页口 = 一摞纸的侧断面。
 *
 * 线的条数跟着厚度走：60 页和 3 页画一样多的线，一眼就假。所以画布留着、厚度变了
 * 重画（`paintEdge`），而不是一次画死。
 *
 * `roughness` 高、颜色比纸略深 —— 一摞纸的侧面本来就比正面暗。
 */
function edgeLayer(): EdgeLayer {
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = EDGE_SIZE
  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  offScene.push(texture)
  return {
    texture,
    ctx: canvas.getContext('2d')!,
    material: new THREE.MeshStandardMaterial({
      map: texture,
      color: 0xf2ecdf,
      roughness: 0.95,
      metalness: 0,
    }),
  }
}

/**
 * `vertical` 决定线是竖着还是横着：盒子 ±x 面（页口）的 u 轴是深度，线要竖着；
 * ±y 面（天头地脚）反过来。**同一个 canvas 转 90° 复用不了**（`texture.rotation`
 * 会和 `repeat` 打架），画两张 128² 比算旋转便宜。
 */
function paintEdge(layer: EdgeLayer, vertical: boolean, stackT: number) {
  const ctx = layer.ctx
  const size = EDGE_SIZE
  ctx.fillStyle = '#e8e0cf'
  ctx.fillRect(0, 0, size, size)

  // 140 是摩尔纹上限：再密下去 128 像素的贴图自己就会打架
  const count = Math.min(Math.max(Math.round((stackT * MM) / SHEET_MM), 4), 140)
  // 固定种子的伪随机：同一个厚度每次画出来一致，不然换开本重画时页口会"抖"一下
  let seed = 20260925
  const rnd = () => {
    seed = (seed * 1103515245 + 12345) & 0x7fffffff
    return seed / 0x7fffffff
  }

  for (let i = 0; i < count; i++) {
    // 等距的线会变成梳子/摩尔纹，所以间距和深浅都加抖动
    const at = ((i + rnd() * 0.7) / count) * size
    ctx.strokeStyle = `rgba(120,105,84,${0.1 + rnd() * 0.26})`
    ctx.lineWidth = rnd() < 0.25 ? 1.6 : 0.8
    ctx.beginPath()
    if (vertical) {
      ctx.moveTo(at, 0)
      ctx.lineTo(at, size)
    } else {
      ctx.moveTo(0, at)
      ctx.lineTo(size, at)
    }
    ctx.stroke()
  }
  layer.texture.needsUpdate = true
}

/**
 * 背景：暖米色的径向渐变。
 *
 * 纯色背景配 3D 书会显得像"模型摆在空房间里"；一圈渐晕能把视线收到书上，
 * 也更接近绘本那种柔和的调子。
 */
function backdropTexture() {
  const size = 512
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = size
  const ctx = canvas.getContext('2d')!
  const gradient = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2)
  gradient.addColorStop(0, '#fdfbf6')
  gradient.addColorStop(0.55, '#ece4d6')
  gradient.addColorStop(1, '#d8cdba')
  ctx.fillStyle = gradient
  ctx.fillRect(0, 0, size, size)

  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  offScene.push(texture)
  return texture
}

/**
 * 假的接触阴影：一圈径向渐变。
 *
 * 单靠方向光的影子边界是硬的（PCFSoft 也只是几像素过渡）。这一层比真影子大
 * 一圈、朝光的方向偏一点，负责"光是柔的、书是浮着的"；真影子负责"书是个立体
 * 的东西"。想省的话把 opacity 调到 0 就行，几何留着不碍事。
 */
function blobTexture() {
  const size = 256
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = size
  const ctx = canvas.getContext('2d')!
  const gradient = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2)
  gradient.addColorStop(0, 'rgba(107,90,68,0.75)')
  gradient.addColorStop(0.55, 'rgba(107,90,68,0.35)')
  gradient.addColorStop(1, 'rgba(107,90,68,0)')
  ctx.fillStyle = gradient
  ctx.fillRect(0, 0, size, size)

  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  offScene.push(texture)
  return texture
}

/**
 * 精装封面。**这是整个 3D 视图里唯一一处前端自己画的内容**，所以说清楚它为什么
 * 不违反"页面纹理必须来自后端"：它画的不是页面，是**装订** —— 书名、作者、一块
 * 布面底色。后端没有"封面模板"这个东西（第 1 页是普通页，不是封面），为了这块
 * 布面多开一个渲染端点等于凭空多一套模板 —— 那才是这条规矩要防的事。
 *
 * 字体用**和页面同一个**自托管 woff2（`/api/assets/fonts/...`，vite 的 /api 代理
 * 直接能取），具体是哪个由 `fontFamily`/`fontUrl` 两个 prop 传进来 —— 封面是书的
 * 一部分，正文换了字体它就该跟着换。用系统字体栈的话 headless Chromium 里没装
 * 中文字体会画出一排豆腐块，而这块封面正是进场第一眼看到的东西。
 *
 * 字体是异步的：先用系统字体画一版，加载完再重画一次 —— **不阻塞进场**，
 * 等字体到位再显示封面等于第一眼是空的。
 */
function coverTexture() {
  const cw = COVER_CANVAS_W
  const ch = COVER_CANVAS_H
  const canvas = document.createElement('canvas')
  canvas.width = cw
  canvas.height = ch
  const ctx = canvas.getContext('2d')!

  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  offScene.push(texture)

  const family = props.fontFamily || COVER_FALLBACK_FAMILY
  const url = props.fontUrl || COVER_FALLBACK_URL
  const draw = () => {
    paintCoverShared(ctx, cw, ch, {
      title: props.bookTitle ?? '',
      author: props.bookAuthor ?? '',
      fontFamily: family,
      bg: coverBgImg,
    })
    texture.needsUpdate = true
  }
  coverRedraw = draw
  loadCoverBg()
  draw()

  const face = new FontFace(family, `url(${url})`, { weight: '700' })
  face
    .load()
    .then((loaded) => {
      document.fonts.add(loaded)
      draw()
    })
    .catch(() => {
      // 取不到就留系统字体版，不报错
    })

  return texture
}

/** 加载（或切换）AI 封面背景；失败静默留布面底色。 */
function loadCoverBg() {
  const imageId = props.coverImageId?.trim()
  if (!imageId) {
    coverBgImg = null
    coverBgUrl = ''
    coverRedraw?.()
    return
  }
  const src = coverBgSrcOf(props.bookId, imageId)
  if (coverBgUrl === src && coverBgImg) return
  coverBgUrl = src
  const img = new Image()
  img.onload = () => {
    if (coverBgUrl !== src) return
    coverBgImg = img
    coverRedraw?.()
  }
  img.src = src
}

// ---------------------------------------------------------------- 弯曲

/**
 * 弯折现在全在顶点着色器里做（见 `bendMaterial.ts`）。这里只留三个 uniform 持有者
 * —— 形状的推导、为什么法线要解析求解、为什么阴影必须单独注入，都在那个文件里。
 *
 * 每个面一份，因为 `uSpineEnd` 是**逐面**不同的（左页的书脊在哪一缘随版式变）。
 * `flipFront`/`flipBack` 共用一份：它俩的几何和姿态永远一致，分开写只会多一处
 * 可能漏改的地方。
 */
// 静止页不再做书脊抬起（uRest = 0）：正对视角下那道弯让页面呈倾斜状，而
// 底下的纸垛是平的，两者不贴合。需要"翻开的书"观感时由翻页动画自己承担。
const leftBend = bendUniforms('right')
const rightBend = bendUniforms('left')
const flipBend = bendUniforms('left')

// ---------------------------------------------------------------- 纹理

/**
 * 给一个面贴上某一页的截图。
 *
 * 贴图是异步的，回来的时候这一面可能已经该显示别的页码了。所以用
 * `mesh.userData.want` 记下"这次要的是哪一页"、回来时对一下 —— 而不是去比对
 * `shown`。翻页动画期间贴的全是 `shown` 之外的页码（纸的正反面、掀开后露出来
 * 的那一页），拿 `shown` 比对会把它们统统挡掉，翻到一半的纸就是空白的。
 */
function applyTexture(mesh: THREE.Mesh, number: number) {
  const material = mesh.material as THREE.MeshStandardMaterial
  const url = shotUrl(number)
  mesh.userData.want = number

  const cached = textures.get(url)
  if (cached) {
    material.map = cached.texture
    // 纹理和 material.color 是**相乘**的。纸色是"还没贴图"时的占位，贴上白纸
    // 纹理后不退成白色的话整页会被染成米黄（白纸 × 暖白 = 暖白）
    material.color.set(0xffffff)
    material.needsUpdate = true
    return
  }

  // **不把已有图清成空白**：这一面之前显示的页面继续留着，直到新页的纹理到位
  // 再换。卡顿/慢网时（后端截图并发只有 2，一次翻页会排队好几个请求）清成空白
  // 的表现就是"左页一直白着" —— 用户看到的正是这个。留着旧图最多短暂显示上一
  // 页，比空白好得多。只有从没有过图（map 为空）时才用纸色打底。
  if (!material.map) {
    material.color.set(PAPER)
    material.needsUpdate = true
  }

  // **同一 URL 的在飞请求必须复用**：卡顿/慢网时一次翻页会对同一页调用多次
  // applyTexture（startCoverFlip 一次、中点 paintShown 一次），每次各发一个
  // 请求会产生竞态，且回来的先后可能让某个 mesh 一直停在纸色占位上。
  let loadingPromise = inFlightTextures.get(url)
  if (!loadingPromise) {
    lastAttemptAt.set(url, performance.now())
    loadingPromise = fetchShotWithRetry(url, number)
      .then((texture) => {
        texture.colorSpace = THREE.SRGBColorSpace
        texture.anisotropy = renderer?.capabilities.getMaxAnisotropy() ?? 1
        textures.set(url, { number, texture })
        inFlightTextures.delete(url)
        return texture
      })
      .catch((error) => {
        inFlightTextures.delete(url)
        throw error
      })
    inFlightTextures.set(url, loadingPromise)
  }

  loadingPromise
    .then(() => {
      // 纹理已进缓存。**不再因为守卫静默 return**：want 在加载期间可能已变
      // （连续翻页），静默 return 会让这一面停在纸色占位。按当前 want 重新
      // 解析，want 指向别的页就贴缓存里的那一张。
      attachByWant(mesh)
      loading.value = false
    })
    .catch(() => {
      failed.value = `第 ${number} 页的画面加载失败`
      loading.value = false
    })
}

/** 前端最多同时挂几个截图请求。后端自身并发是 2，这里排队免得把请求堆爆。 */
const MAX_TEXTURE_LOADS = 4
let activeLoads = 0
const loadQueue: (() => void)[] = []

function acquireLoadSlot(): Promise<void> {
  if (activeLoads < MAX_TEXTURE_LOADS) {
    activeLoads++
    return Promise.resolve()
  }
  return new Promise<void>((resolve) => {
    loadQueue.push(() => {
      activeLoads++
      resolve()
    })
  })
}

function releaseLoadSlot() {
  activeLoads--
  loadQueue.shift()?.()
}

/** 带重试的截图加载：偶发失败（后端排队超时、连接抖动）不该让某页永久空白。 */
async function fetchShotWithRetry(url: string, number: number): Promise<THREE.Texture> {
  let lastError: unknown = null
  for (let attempt = 0; attempt < 3; attempt++) {
    await acquireLoadSlot()
    try {
      return await new Promise<THREE.Texture>((resolve, reject) => {
        new THREE.TextureLoader().load(url, resolve, undefined, () =>
          reject(new Error(`第 ${number} 页截图请求失败`)),
        )
      })
    } catch (error) {
      lastError = error
      await new Promise((resolve) => setTimeout(resolve, 400 * (attempt + 1)))
    } finally {
      releaseLoadSlot()
    }
  }
  throw lastError
}

/**
 * 按 mesh 当前 want 贴缓存中的纹理。want 为 null/未缓存时不动（保留占位或
 * 等在飞请求完成）。这是所有贴附最终汇合的唯一出口。
 */
function attachByWant(mesh: THREE.Mesh) {
  const want = mesh.userData.want as number | null
  if (want === null) return
  const entry = textures.get(shotUrl(want))
  if (!entry) return
  const material = mesh.material as THREE.MeshStandardMaterial
  material.map = entry.texture
  material.color.set(0xffffff)
  material.needsUpdate = true
}

/** 在飞截图请求，按 URL 去重。 */
const inFlightTextures = new Map<string, Promise<THREE.Texture>>()

/** 每个 URL 最近一次发起加载的时间。对账靠它做冷却，避免失败后每帧重试。 */
const lastAttemptAt = new Map<string, number>()

/** 把一面还原成"这一面没有内容"的纸色。翻到书末时纸的背面就是这样。 */
function resetFace(mesh: THREE.Mesh) {
  const material = mesh.material as THREE.MeshStandardMaterial
  mesh.userData.want = null
  material.map = null
  material.color.set(PAPER)
  material.needsUpdate = true
}

/** 贴某一页；页码越界（已经翻到书外了）就还原成纸色。 */
function setFace(mesh: THREE.Mesh, number: number | null) {
  if (number !== null && number >= 1 && number <= props.pageCount) applyTexture(mesh, number)
  else resetFace(mesh)
}

// ---------------------------------------------------------------- 布局

/**
 * 摆放书的身体（书脊、两个半垛、封面封底、接影平面）。
 *
 * 几何**只建一次**（单位立方体 + scale），不像页面那样每次重建 —— 身体的形状
 * 不随页数变化，只是尺寸变。
 *
 * 对开时左右各半垛、书脊在中间；单页时整垛挪到中间（左边缘就是书脊），书脊和
 * 前半块藏起来。合着的书 = 两半相加 = 整垛，自洽。
 */
function placeBody(w: number, h: number, spread: boolean, stackT: number) {
  if (!spine || !stackL || !stackR || !boardFront || !boardBack || !shadowCatcher || !blob)
    return

  // 摊开的书：左边是**已经翻过去**的那摞、右边是还没翻的，厚度按进度分。**总厚度
  // 守恒**（`rightT` 是减出来的），不然翻到一半整本书会变厚或变薄。
  //
  // 单页模式下左半垛整个不出现（`stackL.visible = false`），按进度分会让唯一可见
  // 的那一摞一路薄到没有 —— 所以那时两边都取一半，和从前一样。
  const ratio = flippedRatio()
  const leftT = spread ? Math.max(stackT * ratio, MIN_HALF_T) : stackT / 2
  const rightT = spread ? Math.max(stackT - leftT, MIN_HALF_T) : stackT / 2

  // 页口的线数跟着**各自那侧**的厚度走。厚度没变就不重画 —— 重画要把整张贴图重传
  // 一遍显存。一次翻页左右各重画两张，不在渲染热路径上。
  if (leftT !== edgeStackTL && edgeVL && edgeHL) {
    edgeStackTL = leftT
    paintEdge(edgeVL, true, leftT)
    paintEdge(edgeHL, false, leftT)
  }
  if (rightT !== edgeStackTR && edgeVR && edgeHR) {
    edgeStackTR = rightT
    paintEdge(edgeVR, true, rightT)
    paintEdge(edgeHR, false, rightT)
  }

  const boardW = w + OVER
  const boardH = h + 2 * OVER
  // 两块封面各自跟着自己那侧的垛走 —— 摊开时左薄右厚，封面就是一高一低，真书也这样
  const boardZL = -leftT - BOARD_T / 2
  const boardZR = -rightT - BOARD_T / 2
  // 书脊和接影平面取**较厚**的那侧：取平均或取薄的会让它们陷进厚的那半本书里
  const deepT = Math.max(leftT, rightT)
  const boardZ = -deepT - BOARD_T / 2

  spine.visible = spread
  stackL.visible = spread
  boardFront.visible = spread

  if (spread) {
    stackL.scale.set(w, h, leftT)
    stackL.position.set(-w / 2, 0, -leftT / 2)
    // 书脊：把两块封面在书脊那一侧连起来，宽度取两倍纸板厚（一道可见的棱）
    spine.scale.set(2 * BOARD_T, boardH, BOARD_T)
    spine.position.set(0, 0, boardZ)
    // 包边只出在页口那侧，书脊那侧要和垛对齐 —— 所以中心是 ±(w+over)/2 而不是 ±w/2
    boardFront.scale.set(boardW, boardH, BOARD_T)
    boardFront.position.set(-(w + OVER) / 2, 0, boardZL)
  }

  stackR.scale.set(w, h, rightT)
  stackR.position.set(spread ? w / 2 : 0, 0, -rightT / 2)
  boardBack.scale.set(boardW, boardH, BOARD_T)
  boardBack.position.set(spread ? (w + OVER) / 2 : OVER / 2, 0, boardZR)

  const bookW = spread ? 2 * w : w
  const zBack = -deepT - BOARD_T - SHADOW_GAP
  // 放够大，免得影子边缘被平面自己的边界切掉
  shadowCatcher.scale.set(bookW * 3, h * 3, 1)
  shadowCatcher.position.set(0, 0, zBack)
  // 朝光的方向偏（主光在左上前方，影子就落在右下后方），两个影子才不打架
  blob.scale.set(bookW * 2.2, h * 2.2, 1)
  blob.position.set(bookW * 0.06, -h * 0.06, zBack + 0.002)
}

const unitBox = () => new THREE.BoxGeometry(1, 1, 1)

/**
 * 构建封面网格（书封面板 + 封二衬页 + 书口布边）。幂等：已建过就直接返回。
 *
 * 封面不只用于进场动画，它是首屏的静止状态，也可能被任何一屏翻回。
 * 所以一定不能在"挂载时正好在封面"这个前提下才创建 —— 那会让从内容页切进 3D
 * 的会话翻回封面时空白、并卡住。
 */
function buildCoverMesh() {
  if (coverMesh || !bookGroup) return
  const art = new THREE.MeshStandardMaterial({
    map: coverTexture(),
    roughness: 0.75,
    metalness: 0,
    transparent: true,
  })
  const back = endpaperMaterial()
  back.transparent = true
  const rimCloth = clothMaterial()
  rimCloth.transparent = true
  coverFaces = [art, back, rimCloth]

  coverPivot = new THREE.Group()
  coverMesh = new THREE.Mesh(unitBox(), [
    rimCloth,
    rimCloth,
    rimCloth,
    rimCloth,
    art,
    back,
  ])
  // 不投阴影：材质为淡出开了 transparent，three 对透明物体仍按不透明写深度阴影
  // （除非换 customDepthMaterial），封面淡到 10% 影子还是实的，反而更显眼。
  // 书底下那圈阴影由真实身体提供，够用。
  coverPivot.add(coverMesh)
  bookGroup.add(coverPivot)
}

/** 摆放进场用的封面。铰链在书脊 x = -w/2，合着时正好盖在页垛上。 */
function placeCover(w: number, h: number) {
  if (!coverPivot || !coverMesh) return
  // +0.002 让封面的内表面抬离第 1 页，不然合着时两面精确共面会打架（闪条纹）
  coverPivot.position.set(-w / 2, 0, PAGE_Z + 0.002)
  coverMesh.scale.set(w + OVER, h + 2 * OVER, BOARD_T)
  // +BOARD_T/2：板厚长在朝外那一面，铰链落在封面的内侧表面上
  coverMesh.position.set((w + OVER) / 2, 0, BOARD_T / 2)
}

/** 按当前开本和版式重建几何与位置。开本、出血、单页/对开一变就要走这里。 */
function layout() {
  if (!leftMesh || !rightMesh || !flipPivot || !flipFront || !flipBack || !camera) return

  const { w, h } = pageSizeMm()
  const spread = props.rightPage !== null
  const stackT = stackThickness()

  // 单位平面 + scale。几何体**不再随开本重建**：弯折搬进着色器之后几何里没有任何
  // 逐版式的东西了，尺寸全在 scale 上 —— 以前每次 layout 都 dispose 重建一遍。
  for (const mesh of [leftMesh, rightMesh, flipFront, flipBack]) {
    mesh.scale.set(w, h, w)
  }
  // 静止弧度：书脊那一缘抬起来（见 bendMaterial 里 BEND_POSITION 的说明）。**左页的
  // "书脊在哪一缘"是随版式变的**：对开时左页在 x=-w/2、书脊在它右缘；单页时它挪到
  // x=0 独占一屏，而身体（stackR/boardBack）也在 x=0、包边只出在右边 —— 装订边变成
  // 了**左缘**。写死 'right' 的现象是单页屏"书口翘起来、装订边趴平"，页面从纸垛上
  // 翘出去。右页和翻动的那张纸永远取 'left'，建 uniform 时就定死了。
  setSpineEnd(leftBend, spread ? 'right' : 'left')
  // 翻动的那张纸在这里是收起来的（翻完 / 换版式都会走到 layout），拱起量和弧度都
  // 归零。翻转过程中它们由 applyFlipPose 写。
  flipBend.uAmount.value = 0
  flipBend.uRest.value = 0

  // 对开时两页并排、书脊在中缝；单页时整页居中
  const offset = spread ? w / 2 : 0
  leftMesh.position.set(spread ? -offset : 0, 0, PAGE_Z)
  rightMesh.position.set(spread ? offset : w / 2, 0, PAGE_Z)

  flipPivot.position.set(spread ? 0 : -w / 2, 0, 0)
  flipFront.position.set(w / 2, 0, PAGE_Z + LIFT)
  flipBack.position.set(w / 2, 0, PAGE_Z + LIFT)

  placeBody(w, h, spread, stackT)
  placeCover(w, h)
  fitCamera()
}

function fitCamera() {
  if (!camera || !container.value) return

  const { w, h } = pageSizeMm()
  // 包边要算进取景，不然封面会贴着画面边
  const contentW = (props.rightPage !== null ? 2 * w : w) + 2 * OVER
  const contentH = h + 2 * OVER

  const aspect = container.value.clientWidth / Math.max(container.value.clientHeight, 1)
  camera.aspect = aspect
  const vFov = THREE.MathUtils.degToRad(camera.fov)
  const distance = Math.max(
    contentH / 2 / Math.tan(vFov / 2),
    contentW / 2 / Math.tan(vFov / 2) / Math.max(aspect, 0.01),
  )
  // 静止机位的距离存一份：进场时相机绕着它做偏移（见 INTRO_CAM_PULL），
  cameraRestZ = distance * CAMERA_MARGIN
  camera.position.set(0, 0, cameraRestZ)
  // lookAt 原点而不是书的中心：书脊过原点，所以中缝在屏幕上的投影仍在正中，
  // 点左/右半边翻页的判定和视觉中缝不会错位。**加 roll 或挪书脊就会踩到这条。**
  camera.lookAt(0, 0, 0)
  camera.updateProjectionMatrix()
}

/** 把 shown 里的页码贴到静态的两页上。 */
/**
 * 纹理滑动窗口：只留当前屏前后各 `TEXTURE_WINDOW` 页，其余 dispose 掉。
 *
 * 页面纹理是显存的大头：一页 A5、`scale=2` 的 PNG 约 1118×1588，解成 RGBA 约
 * 7MB —— 30 页的书翻一遍就攒下 200MB，反复开关预览会一路涨到上下文丢失。而这个
 * Map 原本**只增不减**（连换版式那处的 `clear()` 都没有 dispose）。
 *
 * **挂在四个面上的纹理一律不删**，哪怕它在窗口外。翻页动画期间贴的正是 `shown`
 * 之外的页码（纸的正反面、掀开后露出来的那一页），按窗口一刀切会把它们抽走 ——
 * 现象是翻到一半的纸变成空白，而且只在特定方向上复现。
 *
 * 淘汰之后翻回去会重新请求一次，服务端有截图缓存，代价可接受。
 */
function evictTextures() {
  const pinned = new Set<THREE.Texture>()
  for (const mesh of [leftMesh, rightMesh, flipFront, flipBack]) {
    const map = (mesh?.material as THREE.MeshStandardMaterial | undefined)?.map
    if (map) pinned.add(map)
  }
  const center = shown.left
  for (const [url, entry] of textures) {
    if (pinned.has(entry.texture)) continue
    if (Math.abs(entry.number - center) <= TEXTURE_WINDOW) continue
    entry.texture.dispose()
    textures.delete(url)
  }
}

function paintShown() {
  if (!leftMesh || !rightMesh) return
  if (shown.left === 1 && shown.right === null) {
    // 封面屏：页面藏起来，只留合着的封面
    leftMesh.visible = false
    rightMesh.visible = false
    if (coverMesh && coverPivot) {
      coverMesh.visible = true
      coverPivot.rotation.y = 0
      for (const material of coverFaces) material.opacity = 1
    }
    evictTextures()
    return
  }
  if (coverMesh) coverMesh.visible = false
  applyTexture(leftMesh, shown.left)
  // 落单的一屏（封面、末页）右边没有内容，把右页挪走，否则会露出一张白纸
  if (shown.right === null) {
    rightMesh.visible = false
  } else {
    rightMesh.visible = true
    applyTexture(rightMesh, shown.right)
  }
  evictTextures()
}

/**
 * 每帧对账：当前可见的页面只要缺纹理（map 为空），就按 want 补图。
 *
 * 这是兜底，不是主路径 —— 主路径的 applyTexture 本该贴好。但在渲染卡顿、
 * 大图慢加载时，异步回调和 want 守卫之间可能出现某个 mesh 长时间停在纸色
 * 占位（不报错、也不失败）。对账保证画面最终一定收敛，不需要用户刷新。
 * 已在飞的请求会被 inFlightTextures 去重，重复调用不会多发网络请求。
 */
function reconcileFaces() {
  const onCover = shown.left === 1 && shown.right === null
  if (onCover || (coverFlip && !coverFlip.switched)) return
  // 普通翻页/拖拽进行中几何由翻页逻辑接管，落定后（无 flip）才对账
  if (flip) return
  const candidates: [THREE.Mesh | null, number | null][] = [
    [leftMesh, shown.left],
    [rightMesh, shown.right],
  ]
  for (const [mesh, number] of candidates) {
    if (!mesh || !mesh.visible || number === null) continue
    const material = mesh.material as THREE.MeshStandardMaterial
    const want = mesh.userData.want as number | null
    const attached = material.map ? pageOfTexture(material.map) : null
    // 已经贴着的就是这一页、且 want 也对，才算到位
    if (want === number && attached === number) continue
    // 正在飞的就别重复发起
    const url = shotUrl(number)
    if (inFlightTextures.has(url)) {
      attachByWant(mesh)
      continue
    }
    // 冷却：失败后不要每帧重试，隔几秒再补
    const last = lastAttemptAt.get(url) ?? -Infinity
    if (performance.now() - last < 3000) continue
    applyTexture(mesh, number)
  }
  prefetchNeighbors()
}

/**
 * 空闲时预取相邻对开的两页。来回翻页是最高频的操作，提前把相邻页的截图放进
 * 缓存后，正向/反向翻页都不必等网络 —— 这从根上消掉了"翻回去时某页还没加载
 * 完、短暂空白"的窗口。只缓存不贴附，贴附仍由 applyTexture/reconcile 决定。
 */
let lastPrefetchAt = 0
function prefetchNeighbors() {
  const now = performance.now()
  if (now - lastPrefetchAt < 500) return
  lastPrefetchAt = now
  const targets = new Set<number>()
  targets.add(shown.left + 2)
  targets.add(shown.left - 2)
  if (shown.right !== null) {
    targets.add(shown.right + 2)
    targets.add(shown.right - 2)
  }
  for (const number of targets) {
    if (number < 1 || number > props.pageCount) continue
    const url = shotUrl(number)
    if (textures.has(url) || inFlightTextures.has(url)) continue
    const last = lastAttemptAt.get(url) ?? -Infinity
    if (now - last < 3000) continue
    lastAttemptAt.set(url, now)
    const promise = fetchShotWithRetry(url, number)
      .then((texture) => {
        texture.colorSpace = THREE.SRGBColorSpace
        texture.anisotropy = renderer?.capabilities.getMaxAnisotropy() ?? 1
        textures.set(url, { number, texture })
        inFlightTextures.delete(url)
        return texture
      })
      .catch((error) => {
        inFlightTextures.delete(url)
        throw error
      })
    // 预取失败不该冒泡成未处理的 rejection
    promise.catch(() => {})
    inFlightTextures.set(url, promise)
  }
}

/** 反查某个纹理贴的是第几页。找不到返回 null。 */
function pageOfTexture(texture: THREE.Texture): number | null {
  for (const entry of textures.values()) {
    if (entry.texture === texture) return entry.number
  }
  return null
}

// ---------------------------------------------------------------- 翻页

function easeInOut(t: number) {
  return t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2
}

function easeOutCubic(t: number) {
  return 1 - Math.pow(1 - t, 3)
}

/**
 * 唯一的姿态计算。两个方向都挂在书脊右侧、差别只在起始角（见 startFlip 里那段
 * 注释），这里就是把那句话写成一行。
 *
 * 拱起量走 `sin(π·progress)`：起手和落定时都是平的，中间最鼓 —— 拖动时也跟着
 * 走，拖到一半最鼓、拖回 0 或推到 1 自动变平。注意这里是**线性量**，不取 eased。
 */
function applyFlipPose() {
  if (!flip || !flipPivot || !flipFront || !flipBack) return
  flipPivot.rotation.y = -Math.PI * (flip.dir === 1 ? flip.progress : 1 - flip.progress)
  const curl = Math.sin(Math.PI * flip.progress) * CURL
  // 纸的静止弧度要**跟着它翻过 180° 一起翻号**：起手时它是右页（局部 z 就是世界 z，
  // 弧度取正），落定时它是左页、pivot 已经转了 180°（局部 +z 指向世界的 -z，弧度
  // 得取负）。少了这一下，纸落定后会和它盖着的那页差一个弧度 —— 看着是浮在书面上
  // 而不是贴在书上。`cos` 正好从 +1 走到 -1，中间（纸立起来那一刻）归零，而那正是
  // 看不出弧度的时候，两段过渡都平滑。
  const rest = Math.cos(Math.PI * flip.progress) * REST_CURL
  flipBend.uAmount.value = curl
  flipBend.uRest.value = rest
}

/**
 * 播放一次翻页动画。
 *
 * 翻的是**装订好的那一张纸**：一本书里右页 R 和它背面的 R+1 印在同一张纸上，
 * 所以纸的正面是当前的右页、背面就是下一页的左页 —— 转到一半时看见的正是
 * 背面那一页，和真书一样。
 *
 * 同时把"掀开之后露出来的那一页"先铺到底下（往后翻是 R+2，往前翻是 L-2）。
 * 它一开始正好被那张纸盖住，纸转开时从外缘往里一点点露出来 —— 少了这一步，
 * 右边会空掉半张，看着像 bug 而不是翻页。
 */
function startFlip(dir: 1 | -1, to: { left: number; right: number | null }) {
  if (!flipPivot || !flipFront || !flipBack || !rightMesh || !leftMesh || flip) return

  const left = shown.left
  const right = shown.right
  // 单页屏（封面、末页落单）没有"书脊两边"，和翻页的几何对不上。见 watch 里
  // 的说明 —— 这种切换本来就不该走到这里。
  if (right === null) return

  // 纸的正面是最后停在右边的那一页，背面是停在左边的那一页
  const front = dir === 1 ? right : right - 2
  const back = front + 1

  setFace(flipFront, front)
  setFace(flipBack, back)
  if (dir === 1) {
    setFace(leftMesh, left) // 左边那页不动，纸最后会盖在它上面
    setFace(rightMesh, right + 2) // 掀开后露出来的下一页
  } else {
    setFace(rightMesh, right) // 右边那页不动，纸最后会落在它上面
    setFace(leftMesh, left - 2) // 掀开后露出来的上一页
  }

  flipFront.visible = true
  flipBack.visible = true

  const { w } = pageSizeMm()
  // 纸**始终**挂在书脊右侧（局部 x ∈ [0, w]），两个方向的差别只在旋转角：
  //
  //   往后翻 θ: 0 → -π   右半边 → 左半边，起手看到正面、落定看到背面
  //   往前翻 θ: -π → 0   左半边 → 右半边，起手看到背面、落定看到正面
  //
  // 别按方向去改 position.x —— 那样往前翻时纸会挂在书脊左侧，转起来是
  // 从右往左扫（方向和往后翻一样），而且 z 一路为负、整张纸躲在页面**背后**，
  // 看上去就是"往前翻没有动画"。
  flipFront.position.x = w / 2
  flipBack.position.x = w / 2

  flip = {
    dir,
    to,
    progress: 0,
    mode: 'auto',
    startedAt: performance.now(),
    settleFrom: 0,
    settleTo: 1,
    settleAt: 0,
  }
  applyFlipPose()
}

function finishFlip() {
  if (!flip || !flipPivot || !leftMesh || !rightMesh || !flipFront || !flipBack) return

  const committed = flip.progress >= 1
  const landed = flip.to
  const dir = flip.dir

  flipPivot.rotation.y = 0
  flipFront.visible = false
  flipBack.visible = false

  leftMesh.visible = true
  rightMesh.visible = true
  flip = null

  // 弹回时**不改 shown**：startFlip 往 rightMesh 上预铺的 R+2 是"掀开后露出来的
  // 那一页"，纸没翻过去它就不该露出来。paintShown() 会按旧的 shown 重新贴一遍，
  // 等于把预铺撤销 —— 不需要单独写撤销逻辑。
  if (committed) shown = landed
  paintShown()
  layout() // 顺带把弯了的纸抹平（layout 把 flipBend 的拱起量和弧度都归零）

  // 翻的过程中用户又点了：接着翻，而不是"跳"过去
  const next = pending
  pending = null
  if (next) applyTarget(next.left, next.right)
  else if (!committed) go(dir === 1 ? -1 : 1)
  //                        ↑ 弹回了，但 FlipBook 那边的页码计数器已经在 pointerdown
  //                        就前进了（点击语义，见 onPointerDown）。不把它退回去的话，
  //                        计数器和画面对不上 —— 用户下一次点击会从"画面显示的那屏"
  //                        直接跳到计数器算出来的目标，中间整屏被跳过。
  //
  //                        这一步是安全的：applyTarget 开头就是
  //                        `if (left === shown.left && right === shown.right) return`，
  //                        退回去的目标正好等于 shown，watch 那边会直接返回，不会再起
  //                        一次翻页。有 pending 时**不退** —— 那次点击的目标已经由
  //                        pending 补上了，退了反而少翻一屏。
}

/**
 * 把画面切到某一屏：能翻就翻，翻不了就直接换。
 *
 * 两种"翻不了"：跨了好几屏（连翻好几下反而看不清），以及单页 ↔ 对开
 * （书的几何整个变了 —— 单页时整页居中、对开时两页分列书脊两侧 ——
 * 一边翻一边变形很怪）。
 */
function applyTarget(left: number, right: number | null) {
  if (left === shown.left && right === shown.right) return

  const fromCover = shown.left === 1 && shown.right === null
  const toCover = left === 1 && right === null

  // 封面屏 ↔ 内页对开：走封面开合动画（封面绕书脊转 180°，在侧对镜头的
  // 那一刻切换底下的书体几何）。
  if (!reducedMotion.value && (fromCover || toCover)) {
    if (startCoverFlip(toCover ? -1 : 1, { left, right })) return
    // 起不了动画（极端情况）：落到下面的直接切换，绝不能什么都不做
  }

  // **其余单页屏直接换，不翻页。** 两种情况：
  //   - 末页落单 ↔ 对开：书的几何变了，而普通翻页需要"书脊两边"
  //   - 跨屏太多：连翻好几下反而看不清
  // 减弱动态效果时也走这条：直接换、不播任何翻页动画。
  if (
    reducedMotion.value ||
    Math.abs(left - shown.left) > 2
    || shown.right === null
    || right === null
  ) {
    shown = { left, right }
    paintShown()
    layout()
    return
  }

  // 用**左页页码**量距离，不能用页码之和：封面只有一页、对开有两页，求和的话
  // "封面 → 第 2-3 页"这一步会从 2 跳到 5，明明只翻了一屏却被当成跨了好几屏，
  // 动画直接被跳过 —— 实测就是这个 bug，翻页从来没播过。
  // 左页的步长单页模式下是 1、对开是 2，阈值取 2 两种模式都合适。
  startFlip(left > shown.left ? 1 : -1, { left, right })
}

// ---------------------------------------------------------------- 封面开合

/** 封面淡出/淡入区间：侧对镜头（progress 0.5）前后各 14%。 */
const COVER_FADE_BAND = 0.14

/** 返回是否真的起了动画；起不了时调用方要走直接切换的兜底。 */
function startCoverFlip(dir: 1 | -1, to: { left: number; right: number | null }): boolean {
  if (coverFlip) return false
  // 兜底构建：正常在 onMounted 已建好；这里只是防止任何漏建的路径
  if (!coverMesh) buildCoverMesh()
  if (!coverMesh || !coverPivot) return false
  coverMesh.visible = true

  // 打开之前先把目标内容页铺好：封面转开后，左侧（封二底下）和右侧都
  // 必须已经是内容页，不能只在右边有图。
  if (dir === 1 && leftMesh && rightMesh) {
    leftMesh.visible = true
    rightMesh.visible = true
    setFace(leftMesh, to.left)
    if (to.right !== null) setFace(rightMesh, to.right)
  }

  coverFlip = {
    dir,
    to,
    progress: 0,
    startedAt: performance.now(),
    switched: false,
  }
  applyCoverFlipPose()
  return true
}

function applyCoverFlipPose() {
  if (!coverFlip || !coverPivot) return
  const { dir, progress } = coverFlip
  coverPivot.rotation.y = -Math.PI * (dir === 1 ? progress : 1 - progress)

  // 打开：侧对前淡出；合上：侧对后淡入。到 0.5 时封面不可见，底下正好换几何。
  let opacity: number
  if (dir === 1) {
    opacity =
      progress < 0.5 - COVER_FADE_BAND
        ? 1
        : progress >= 0.5
          ? 0
          : 1 - (progress - (0.5 - COVER_FADE_BAND)) / COVER_FADE_BAND
  } else {
    opacity =
      progress < 0.5
        ? 0
        : progress >= 0.5 + COVER_FADE_BAND
          ? 1
          : (progress - 0.5) / COVER_FADE_BAND
  }
  for (const material of coverFaces) material.opacity = opacity
}

function stepCoverFlip(now: number) {
  if (!coverFlip) return
  coverFlip.progress = easeInOut(Math.min((now - coverFlip.startedAt) / FLIP_MS, 1))

  // 封面侧对镜头的那一刻把底下的书体换掉。layout() 会重摆书体、重算相机，
  // 但不动 coverPivot 的旋转 —— 紧接着 applyCoverFlipPose 把姿态写回来。
  if (!coverFlip.switched && coverFlip.progress >= 0.5) {
    coverFlip.switched = true
    shown = coverFlip.to
    paintShown()
    layout()
  }
  applyCoverFlipPose()

  if (coverFlip.progress >= 1) {
    const openedIntoPages = coverFlip.to.left !== 1
    coverFlip = null
    if (openedIntoPages && coverMesh) coverMesh.visible = false
    // 开合途中用户又点了：落地后接着翻
    const next = pending
    pending = null
    if (next) applyTarget(next.left, next.right)
  }
}

// ---------------------------------------------------------------- 渲染循环

/** 进场：飞入 → （首屏才有的）掀封面 → 交接淡出。 */
function stepIntro(now: number) {
  if (!bookGroup) return
  const t = Math.min((now - introStart) / INTRO_MS, 1)
  // 飞入铺满整段：封面不再在进场时自动掀开 —— 它是首屏静止状态，翻开
  // 交给用户自己的翻页操作（coverFlip）。
  const fly = t
  const eased = easeOutCubic(fly)
  bookGroup.position.z = (1 - eased) * -6
  bookGroup.rotation.y = REST_YAW + (1 - eased) * -0.8
  bookGroup.rotation.x = REST_PITCH + (1 - eased) * 0.25

  // 相机同时从更近、更低的机位退回静止机位。**复用同一条缓动曲线 `eased`**，
  // 不另起一条：两条曲线不同步的话会出现"书已经到了、镜头还在飘"。
  if (camera && cameraRestZ) {
    const back = 1 - eased
    camera.position.set(
      0,
      -INTRO_CAM_DROP * cameraRestZ * back,
      cameraRestZ * (1 - INTRO_CAM_PULL * back),
    )
    camera.lookAt(0, 0, 0)
  }

  if (t >= 1) {
    introStart = 0
  }
}

function render(now: number) {
  frame = requestAnimationFrame(render)
  if (!renderer || !scene || !camera) return

  if (introStart) stepIntro(now)

  // 悬停：朝镜头推近一点、转正一点。**进场期间压成 0** —— 那段时间姿态归 stepIntro
  // 全权接管，而且用户往往在进场还没跑完时鼠标就已经在画布上了，不压的话书会一边
  // 飞进来一边前倾，两个动效叠在一起。
  //
  // 阻尼用 `1 - exp(-rate·dt)` 而不是固定系数：headless 里帧率只有个位数，固定
  // 系数会让过渡时长随帧率变（掉帧时"慢慢飘过去"）。
  const dt = Math.min((now - lastFrameAt) / 1000, 0.1)
  lastFrameAt = now
  const hoverTarget = hovering && !introStart && !reducedMotion.value ? 1 : 0
  hoverAmt += (hoverTarget - hoverAmt) * (1 - Math.exp(-HOVER_RATE * dt))
  if (hoverGroup) {
    hoverGroup.position.z = hoverAmt * HOVER_PUSH
    hoverGroup.rotation.y = hoverAmt * HOVER_YAW
  }

  if (flip) {
    if (flip.mode === 'auto') {
      flip.progress = easeInOut(Math.min((now - flip.startedAt) / FLIP_MS, 1))
      applyFlipPose()
    } else if (flip.mode === 'settle') {
      // 补完时长按剩余距离缩：从 0.95 补到 1 和从 0.5 补到 1 花一样久的话，
      // 前者会显得"卡了一下"
      const span = Math.max(0.35, Math.abs(flip.settleTo - flip.settleFrom))
      const t = Math.min((now - flip.settleAt) / (SETTLE_MS * span), 1)
      flip.progress = flip.settleFrom + (flip.settleTo - flip.settleFrom) * easeOutCubic(t)
      applyFlipPose()
    }
    // drag：progress 由 pointermove 写，这一帧什么都不做
    if (flip.progress >= 1 && flip.mode !== 'drag') finishFlip()
    else if (flip.mode === 'settle' && flip.settleTo === 0 && flip.progress <= 0) finishFlip()
  }
  if (coverFlip) stepCoverFlip(now)
  reconcileFaces()

  if (debug) {
    debug.intro = introStart ? introKind : 'none'
    // 进场跑到第几秒（0→1）。headless 里帧率只有个位数，靠 wall-clock 卡时间点
    // 采样是采不准的 —— e2e 得看这个值，而不是"等了 600ms 就截图"。
    debug.introT = introStart ? Math.min((now - introStart) / INTRO_MS, 1) : 1
    debug.flipMode = flip?.mode ?? null
    debug.progress = flip?.progress ?? 0
    debug.angle = flipPivot?.rotation.y ?? 0
    // 相机距离。进场时相机在动（INTRO_CAM_PULL），而这个数**只影响画面**，
    // 没有任何语义状态跟着变 —— 除了"进场期间它在从小变大"这一条，别的断言都
    // 抓不到它，所以得挂出来。
    debug.camZ = camera?.position.z ?? 0
    debug.spread = props.rightPage !== null
    debug.shownLeft = shown.left
    debug.shownRight = shown.right
    // 静止弧度是**几何**上的事，截图上量不出来 —— 页面近白、光又漫，整条横切上的
    // 明暗差只有一两个灰阶（4.5% 那一版就是这么废掉的）。
    //
    // 这两个数原本是从 CPU 顶点缓冲里读的（`pos.getZ(i)`），弯折搬到 GPU 之后顶点
    // 缓冲里的 z **恒为 0**，读不到了。改成按同一个公式求值：形状是
    // `uRest·(1 - sin(tπ/2))`，t=0（书脊端）处正好等于 uRest，t=1（书口端）恒为 0。
    //
    // **书脊端是从版式（`props.rightPage`）独立判一次出来的**，这是这条断言唯一的
    // 牙齿所在，别"顺手"把它和 `layout()` 里那份合并成一个变量 —— 合并之后写入端
    // 和读取端同错同消：`setSpineEnd` 传反时读取端跟着一起反，`pageRiseSpine` 照报
    // uRest、断言照绿，而那正是它存在的理由。两边各判一次，写入端一写反，位移就落
    // 到几何书口端，读数掉到 0，`expect_rest_curl` 的 `>= 0.06` 立刻红。
    //
    // 判据本身（"对开时左页书脊在右缘"）两边仍然是同一条，所以哪天版式规则真变了
    // （比如 2 页的书不再对开），两处会一起跟着变，不会误报。
    //
    // 仍然**抓不住**的是：GLSL 位移那段本身写错（比如 `1.0 - sin(...)` 少个 1）而
    // TS 这边常量不动。这种错在截图里是响的（页面直接变成一块平板），不是静默的
    // 数据错，所以留给目视验收 —— 像素级的轮廓断言试过，见 README「3D 预览」一节
    // 里那份实测数据：关掉弧度只让天头轮廓在中缝附近动 4px，而斜放透视本身贡献
    // 44px，拿它写断言等于在测透视。
    const spineU = props.rightPage !== null ? 1 : 0
    const spineT = leftBend.uSpineEnd.value > 0 ? spineU : 1 - spineU
    const riseAt = (t: number) => leftBend.uRest.value * (1 - Math.sin((t * Math.PI) / 2))
    debug.pageRiseSpine = riseAt(spineT)
    debug.pageRiseFore = riseAt(1 - spineT)
    debug.geometries = renderer.info.memory.geometries
    debug.textures = renderer.info.memory.textures
    debug.leftMap = !!(leftMesh?.material as THREE.MeshStandardMaterial | undefined)?.map
    debug.rightMap = !!(rightMesh?.material as THREE.MeshStandardMaterial | undefined)?.map
    debug.leftWant = (leftMesh?.userData.want as number | null) ?? null
    debug.leftVisible = leftMesh?.visible ?? false
  }

  renderer.render(scene, camera)
}

// ---------------------------------------------------------------- 交互

function onPointerDown(event: PointerEvent) {
  const el = container.value
  if (!el || !event.isPrimary || event.button !== 0) return

  const rect = el.getBoundingClientRect()
  // 点右半边往后翻、左半边往前翻 —— 和真实翻书的手势一致
  const dir: 1 | -1 = event.clientX - rect.left > rect.width / 2 ? 1 : -1

  // 抓住指针：拖到画布外面还得跟手，否则一滑出容器 pointermove 就断，纸卡在半空。
  // 配套的三件：抬手时 releasePointerCapture、CSS 的 touch-action: none
  //（不加的话触屏上浏览器会把横向拖动当滚动手势，直接发 pointercancel ——
  // 拖拽在触屏上完全不工作，而桌面上测不出来）、以及 user-select: none。
  el.setPointerCapture(event.pointerId)
  drag = {
    pointerId: event.pointerId,
    x0: event.clientX,
    baseProgress: 0,
    active: false,
    deferred: flip !== null,
    dir,
    travel: 0,
    lastAt: event.timeStamp,
    velocity: 0,
  }

  // 已经有翻页在跑 → 这次按下可能是"抓住那张正在翻的纸"，先不发翻页请求。
  // 等抬手还没越过 slop 才补发（见 onPointerUp）—— 否则拖动松手时 finishFlip
  // 又会链式执行 pending，一次拖动翻两屏。
  if (!flip) go(dir)
}

function onPointerMove(event: PointerEvent) {
  if (!drag || !container.value || event.pointerId !== drag.pointerId) return

  if (!drag.active) {
    if (Math.abs(event.clientX - drag.x0) < DRAG_SLOP_PX) return
    // 没在翻页就无从接管（比如这一下是"跳屏"而不是翻页，没有纸可拖）
    if (!flip) return
    // 越线才真正开始拖：基准挪到越线处，那 6px 不算进行程
    drag.active = true
    drag.x0 = event.clientX
    drag.baseProgress = flip.progress
    drag.travel = Math.max(container.value.clientWidth * 0.5, 1)
    drag.lastAt = event.timeStamp
    drag.velocity = 0
    flip.mode = 'drag'
    return
  }

  // 增量映射而不是指针的绝对位置：绝对位置在按下的瞬间和当前进度对不上，纸会
  // 先跳一下；而且同样的手势按在画面左边还是右边，纸转的角度会不一样。
  // 往前翻的纸从 -π 出发这件事已经被 applyFlipPose 包掉了 —— 这里只管把
  // progress 从 0 推到 1，两个方向是同一套，只有手指的**物理方向**相反。
  const delta = event.clientX - drag.x0
  const forward = flip && flip.dir === 1 ? -delta : delta
  const next = clamp01(drag.baseProgress + forward / drag.travel)

  const dt = event.timeStamp - drag.lastAt
  if (dt > 0 && flip) {
    drag.velocity = (next - flip.progress) / dt
    drag.lastAt = event.timeStamp
  }
  if (flip) {
    flip.progress = next
    applyFlipPose()
  }
}

function onPointerUp(event: PointerEvent) {
  if (!drag || event.pointerId !== drag.pointerId) return
  const ended = drag
  drag = null
  container.value?.releasePointerCapture(event.pointerId)

  if (!ended.active) {
    // 按下时已经有翻页在跑 → 现在补发这次点击（当时没发，见 onPointerDown）
    if (ended.deferred) go(ended.dir)
    return
  }
  if (!flip) return

  // 抬手前静止了 90ms 以上就不算甩：少了这一步，慢慢拖到一半、停住、松手会被
  // 当成甩，纸"自己飞过去"，用户完全预期不到。
  const velocity = event.timeStamp - ended.lastAt > VELOCITY_STALE_MS ? 0 : ended.velocity
  // 位置优先，速度只在两端翻盘：慢慢拖到 90% 再松手，速度几乎为 0，必须翻过去。
  const committed =
    velocity > FLICK_VELOCITY
      ? true
      : velocity < -FLICK_VELOCITY
        ? false
        : flip.progress >= RELEASE_PROGRESS

  flip.mode = 'settle'
  flip.settleFrom = flip.progress
  flip.settleTo = committed ? 1 : 0
  flip.settleAt = event.timeStamp
}

/**
 * 悬停：只记状态，实际的位移和阻尼在 `render` 里按帧算 —— 在这里直接改
 * `hoverGroup` 的话，鼠标不动时没有帧来收尾，书会停在半路。
 *
 * 用 `pointerenter`/`pointerleave` 而不是 `pointermove` 判断"在不在画布上"：
 * 后者要自己处理"移出去但没派发事件"的各种边角，前者是浏览器直接给的。
 */
function onPointerEnter() {
  hovering = true
}

function onPointerLeave() {
  hovering = false
}

function onPointerCancel(event: PointerEvent) {
  if (!drag || event.pointerId !== drag.pointerId) return
  const ended = drag
  drag = null
  // 系统把手势抢走了（触屏上没写 touch-action: none 就会这样）。不补发点击 ——
  // 那不是一次点击；正在拖的那张纸也交还给时间轴，让它按当前位置弹回或补完。
  if (ended.active && flip) {
    flip.mode = 'settle'
    flip.settleFrom = flip.progress
    flip.settleTo = flip.progress >= RELEASE_PROGRESS ? 1 : 0
    flip.settleAt = performance.now()
  }
}

// ---------------------------------------------------------------- 生命周期

onMounted(() => {
  const el = container.value
  if (!el) return

  scene = new THREE.Scene()
  scene.background = backdropTexture()

  // 视场角刻意开得很窄（长焦）：翻页时纸的自由边会朝镜头转过来，广角下这一边
  // 会被近大远小放大到比书还高、还顶出画面（34° 实测就是这样）。长焦把它压平，
  // 顺带也更像"绘本摊在桌上"的观感。斜放之后立体感来自姿态 + 厚度 + 阴影，
  // 不再需要靠广角撑透视 —— 而广角会把这个余量吃掉。见 fitCamera。
  camera = new THREE.PerspectiveCamera(20, 1, 0.1, 100)

  renderer = new THREE.WebGLRenderer({ antialias: true })
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
  renderer.setSize(el.clientWidth, el.clientHeight)
  renderer.outputColorSpace = THREE.SRGBColorSpace
  // 阴影贴图必须在第一次 render 之前开，之后再开要遍历所有材质 needsUpdate。
  // PCFSoft 而不是 VSM：绘本要漫射、没有明确边界的光，硬边阴影会让纸变塑料；
  // VSM 更软但会漏光（书脊那圈会发亮），不值。
  renderer.shadowMap.enabled = true
  renderer.shadowMap.type = THREE.PCFSoftShadowMap
  el.appendChild(renderer.domElement)

  // 柔和的环境光 + 一盏偏暖的主光。硬阴影会让纸看起来像塑料，
  // 绘本的调子要的是漫射、没有明确的高光点。
  scene.add(new THREE.AmbientLight(0xffffff, 2.0))
  const key = new THREE.DirectionalLight(0xfff2df, 1.5)
  key.position.set(-1.2, 1.8, 2.4)
  key.castShadow = true
  // 1024 而不是 2048：正交相机宽 4 个世界单位，1024 的贴图下书宽 1.48 占 379 个
  // 纹素 —— 和它在屏幕上的像素数基本 1:1，再往上加是纯浪费（软件渲染下 2048 会
  // 让整帧掉到个位数 fps，而画面上看不出区别）。
  key.shadow.mapSize.set(1024, 1024)
  // 正交阴影相机给 ±2.0，**不是按书的尺寸算的**：纸转到一半时自由边能到书脊
  // 前方约一个页宽（A5 对开就是 1.0），给小了的现象是"翻到一半纸的影子突然
  // 缺一块"，**不报错**。
  const shadowCam = key.shadow.camera
  shadowCam.left = -2.0
  shadowCam.right = 2.0
  shadowCam.top = 2.0
  shadowCam.bottom = -2.0
  shadowCam.near = 0.1
  shadowCam.far = 12
  shadowCam.updateProjectionMatrix()
  // bias 是一对矛盾：小了纸面上会出现自己投影自己的横条纹（acne，弯曲的纸尤其
  // 明显），大了影子会和物体脱开（peter-panning）—— 而这张纸只抬离页面 1.2mm，
  // 脱开一点点影子就没了。0.004 是实测的折中。
  key.shadow.bias = -0.0004
  key.shadow.normalBias = 0.004
  // target 要挂进场景：不挂的话它的 matrixWorld 永远是单位阵，现在恰好等价于
  // (0,0,0)，但那是巧合。
  scene.add(key, key.target)
  const rim = new THREE.DirectionalLight(0xdfe8ff, 0.6)
  rim.position.set(1.6, -1.2, -1.5)
  scene.add(rim)

  hoverGroup = new THREE.Group()
  bookGroup = new THREE.Group()
  // 悬停加在 hoverGroup 上（见它的声明），进场加在 bookGroup 上，互不覆盖
  hoverGroup.add(bookGroup)
  // 斜放。姿态加在 bookGroup 上，而 layout() 里所有坐标都是 bookGroup 的**局部**
  // 空间，倾斜对它们透明；flipPivot.rotation.y 同理（链上是 bookGroup.matrix ·
  // flipPivot.matrix，互不干扰）。弯折沿局部 z 位移，斜放后这个 z 不再和镜头
  // 轴重合 —— 但这正是想要的：拱起方向始终垂直于纸面。
  bookGroup.rotation.y = REST_YAW
  bookGroup.rotation.x = REST_PITCH
  scene.add(hoverGroup)

  // ---- 页面
  leftMesh = new THREE.Mesh(
    new THREE.PlaneGeometry(1, 1, SEGMENTS, 1),
    paperMaterial(leftBend),
  )
  rightMesh = new THREE.Mesh(
    new THREE.PlaneGeometry(1, 1, SEGMENTS, 1),
    paperMaterial(rightBend),
  )
  // 收翻动那张纸的投影。**自己不投** —— 体量阴影交给身体，两张零厚度的面投出来
  // 的影子和身体重叠只会让影子变脏。
  leftMesh.receiveShadow = true
  rightMesh.receiveShadow = true
  bookGroup.add(leftMesh, rightMesh)

  flipPivot = new THREE.Group()
  bookGroup.add(flipPivot)

  flipFront = new THREE.Mesh(new THREE.PlaneGeometry(1, 1, SEGMENTS, 1), paperMaterial(flipBend))
  // 背面朝后：绕 y 转 180° 的第二个平面。一个 mesh 只能有一个材质，
  // 想正反面贴不同东西就得背靠背放两个
  flipBack = new THREE.Mesh(new THREE.PlaneGeometry(1, 1, SEGMENTS, 1), paperMaterial(flipBend))
  flipBack.rotation.y = Math.PI
  // 投是核心需求。**不收**：逐顶点弯出来的零厚度面自己投自己，法线一直在变，
  // bias 调不出干净结果，而且收不到有用信息。
  flipFront.castShadow = true
  flipBack.castShadow = true
  // 阴影是 three 另外拿一张 `MeshDepthMaterial` 渲的，`onBeforeCompile` 管不到它 ——
  // 不挂这个的话纸本身是弯的、它投出来的影子却是一块不弯的平板矩形。两个面共用一份
  // uniforms：几何一样、各自的 mesh 变换会分别作用上去。
  flipFront.customDepthMaterial = bendDepthMaterial(flipBend)
  flipBack.customDepthMaterial = bendDepthMaterial(flipBend)
  flipFront.visible = false
  flipBack.visible = false
  flipPivot.add(flipFront, flipBack)

  // ---- 书的身体
  const cloth = clothMaterial()
  edgeVL = edgeLayer()
  edgeHL = edgeLayer()
  edgeVR = edgeLayer()
  edgeHR = edgeLayer()
  const boxGeometry = () => new THREE.BoxGeometry(1, 1, 1)
  const paperEdge = () => new THREE.MeshStandardMaterial({ color: PAPER, roughness: 0.95 })

  // BoxGeometry 的材质数组顺序是 [+x, -x, +y, -y, +z, -z]。
  // 左半垛的页口在 -x、书脊面在 +x；右半垛正好反过来 —— 这就是要两个数组的原因。
  // 垛的 +z 面被页面盖住、-z 面贴着封底，都用纸色占位。
  // 单页屏时 stackR 挪到 x=0、左边就是书脊，所以它的 -x 面用布面色正好读作
  // "这页是从书里拆出来的"，不用为单页另做一套材质。
  stackL = new THREE.Mesh(boxGeometry(), [
    cloth,
    edgeVL.material,
    edgeHL.material,
    edgeHL.material,
    paperEdge(),
    paperEdge(),
  ])
  stackR = new THREE.Mesh(boxGeometry(), [
    edgeVR.material,
    cloth,
    edgeHR.material,
    edgeHR.material,
    paperEdge(),
    paperEdge(),
  ])
  spine = new THREE.Mesh(boxGeometry(), cloth)
  boardFront = new THREE.Mesh(boxGeometry(), cloth)
  boardBack = new THREE.Mesh(boxGeometry(), cloth)
  for (const mesh of [stackL, stackR, spine, boardFront, boardBack]) {
    mesh.castShadow = true
    mesh.receiveShadow = true
    bookGroup.add(mesh)
  }

  // ---- 影子
  // ShadowMaterial 只在有影子的地方着色、其余全透明，所以不会糊住背后的渐变背景
  // —— 换 MeshBasicMaterial 就会是一块灰板。放在书背后 SHADOW_GAP 处而不是紧贴：
  // 影子会顺着光的方向偏出去，那圈偏移才是"书浮在暖色背景上"的来源。
  // color 用暖褐而不是纯黑：纯黑的影子和暖米色背景拼在一起发脏。
  // （背景是 scene.background，走的是背景 pass、不参与深度，任何几何都"落"不到
  // 它上面 —— 所以才需要这层专门的接影几何。）
  shadowCatcher = new THREE.Mesh(
    new THREE.PlaneGeometry(1, 1),
    new THREE.ShadowMaterial({ color: 0x6b5a44, opacity: 0.3, depthWrite: false }),
  )
  shadowCatcher.receiveShadow = true
  shadowCatcher.renderOrder = 2
  blob = new THREE.Mesh(
    new THREE.PlaneGeometry(1, 1),
    new THREE.MeshBasicMaterial({
      map: blobTexture(),
      transparent: true,
      depthWrite: false,
      opacity: 0.34,
    }),
  )
  blob.renderOrder = 1
  bookGroup.add(shadowCatcher, blob)

  // ---- 进场封面
  // **不做成"把真实的页面垛合起来再翻开"**：那样要动画化整个身体的几何，而身体
  // 的形状（一垛 vs 两垛、书脊有没有）在合/开两态根本不同。
  //
  // 也不用另建一套"合着的书"再淡出交接：进场只在首屏播，而**首屏在单页和对开
  // 两种模式下都是单页屏**（FlipBook 里 leftPage <= 1 → rightPage = null）。
  // 单页屏的真实身体本来就是一垛 + 一块封底，跨度正好是 [-w/2, w/2] —— 和合着的
  // 书只差一块封面。所以直接给真实身体补上这块封面就行：掀开它，底下露出的就是
  // 真实的 leftMesh（第 1 页），**不需要任何淡出交接，页面一像素都不动**。
  introKind = props.leftPage === 1 && props.rightPage === null ? 'cover' : 'fly'
  // 封面**总是**建出来，不管挂载时在哪一屏。之前的条件建造会在"从内容页切到
  // 3D"时漏建封面网格：之后翻回封面是空白（paintShown 的封面分支被 !coverMesh
  // 跳过），从封面再往前翻也会因为 startCoverFlip 直接 return 而卡住。
  //
  // 封面是首屏的静止状态（开了"减弱动态效果"只是不播飞入，封面照样合在那里；
  // 翻页时的淡出也仍要用 transparent 材质）。
  buildCoverMesh()

  shown = { left: props.leftPage, right: props.rightPage }
  layout()
  paintShown()

  // 减弱动态效果：**不启动进场**。`bookGroup` 建出来就已经是静止姿态（见上面那两行
  // REST_YAW/REST_PITCH），`stepIntro` 不跑就一直是终态，不需要"先摆到起点再补回来"。
  // `introStart` 保持 0，`debug.intro` 于是报 'none' —— 语义正好。
  if (!reducedMotion.value) introStart = performance.now()
  frame = requestAnimationFrame(render)

  observer = new ResizeObserver(() => {
    if (!renderer || !container.value) return
    renderer.setSize(container.value.clientWidth, container.value.clientHeight)
    fitCamera()
  })
  observer.observe(el)

  el.addEventListener('pointerdown', onPointerDown)
  el.addEventListener('pointermove', onPointerMove)
  el.addEventListener('pointerup', onPointerUp)
  el.addEventListener('pointercancel', onPointerCancel)
  el.addEventListener('pointerenter', onPointerEnter)
  el.addEventListener('pointerleave', onPointerLeave)

  if (import.meta.env.DEV) {
    /**
     * 给 e2e 看的只读状态。挂在 canvas 元素上而不是 window 上：作用域天然跟着
     * 组件生命周期走，组件一卸载就没了，不会污染全局。每帧原地改同一个对象
     * （不新建），没有 GC 开销。
     *
     * 拖拽的语义断言（"纸是**因为我的拖动**才转的"）用像素做不到 —— 自动翻页
     * 动画也在改像素，两者在截图里长得一样。
     */
    debug = {
      intro: 'none',
      introT: 1,
      flipMode: null,
      progress: 0,
      angle: 0,
      camZ: 0,
      spread: false,
      // 画面上**实际**摊着的是哪两页。和 FlipBook 那边的页码计数器是两回事：
      // 计数器在 pointerdown 就前进（点击语义），而这里只在纸真的翻过去时才动。
      // 两者对不上就是 bug —— 弹回时没人把计数器退回去，用户下一次点击会整屏跳过去。
      shownLeft: 1,
      shownRight: null as number | null,
      // 左页几何两端顶点的 z 位移（书脊那缘 / 书口那缘）。静止弧度是**几何**上的
      // 事，截图上量不出来 —— 页面近白、光又漫，整条横切上的明暗差只有一两个灰阶
      // （4.5% 那一版就是这么废掉的）。所以直接把两个数交出去给 e2e 看。
      pageRiseSpine: 0,
      pageRiseFore: 0,
      geometries: 0,
      textures: 0,
      leftMap: false,
      rightMap: false,
      leftWant: null as number | null,
      leftVisible: false,
    }
    ;(renderer.domElement as unknown as Record<string, unknown>).__flipbook3d = debug
  }
})

onBeforeUnmount(() => {
  cancelAnimationFrame(frame)
  observer?.disconnect()
  const el = container.value
  el?.removeEventListener('pointerdown', onPointerDown)
  el?.removeEventListener('pointermove', onPointerMove)
  el?.removeEventListener('pointerup', onPointerUp)
  el?.removeEventListener('pointercancel', onPointerCancel)
  el?.removeEventListener('pointerenter', onPointerEnter)
  el?.removeEventListener('pointerleave', onPointerLeave)

  // WebGL 资源不手动释放的话，反复开关预览会一直攒显存直到上下文丢失。
  //
  // **遍历场景**，而不是靠一个 disposables 数组：layout() 每次都会 dispose 掉旧
  // 的几何换成新的，数组里存的引用早就是旧的，于是真正在用的那批从来没被释放过
  // —— 反复开关预览会按实例攒显存。场景里的几何/材质都是这个组件独有的（没有跨
  // 实例共享），遍历是安全的。
  const geometries = new Set<THREE.BufferGeometry>()
  const materials = new Set<THREE.Material>()
  scene?.traverse((object) => {
    const mesh = object as THREE.Mesh
    if (!mesh.isMesh) return
    if (mesh.geometry) geometries.add(mesh.geometry)
    for (const material of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) {
      if (material) materials.add(material)
    }
    // 投影用的深度材质**不在 mesh.material 里**，遍历是收不到的 —— 漏了它的话
    // 每开关一次预览就漏一张阴影用的程序 + 材质。
    if (mesh.customDepthMaterial) materials.add(mesh.customDepthMaterial)
    if (mesh.customDistanceMaterial) materials.add(mesh.customDistanceMaterial)
  })
  for (const geometry of geometries) geometry.dispose()
  for (const material of materials) material.dispose()

  // scene.background 和几个材质的 map 遍历找不到，单独收
  for (const item of offScene) item.dispose()
  offScene.length = 0
  for (const entry of textures.values()) entry.texture.dispose()
  textures.clear()

  renderer?.dispose()
  renderer?.domElement.remove()
  renderer = null
  scene = null
  camera = null
  bookGroup = null
  hoverGroup = null
  hovering = false
  hoverAmt = 0
  lastFrameAt = 0
  flip = null
  coverFlip = null
  drag = null
  pending = null
  coverRedraw = null
  debug = null
  coverFaces = []
  edgeVL = null
  edgeHL = null
  edgeVR = null
  edgeHR = null
  edgeStackTL = -1
  edgeStackTR = -1
})

// 页码变了就翻页。用"目标页码 vs 正在显示的页码"判断方向，而不是看 step()
// 传了什么 —— 键盘、按钮、点击画布最终都汇到 FlipBook 的 current 上，
// 这里只认结果，不关心是哪条路径来的。
watch(
  () => [props.leftPage, props.rightPage] as const,
  ([left, right]) => {
    if (flip) {
      // 拖拽途中用户按了方向键或点了按钮：把这次拖动**按它自己的方向补完**，
      // 新目标排进 pending。不能就地跳到新目标 —— 纸还在半空、几何是半开的；
      // 也不能丢掉这次拖动 —— 用户的手还按着。
      if (flip.mode === 'drag') {
        flip.mode = 'settle'
        flip.settleFrom = flip.progress
        flip.settleTo = 1
        flip.settleAt = performance.now()
        drag = null // 指针还按着，但这次拖拽已交还给时间轴
      }
      pending = { left, right }
      return
    }
    // 封面正在开合：同上，等动画落地后再补目标，不就地跳变
    if (coverFlip) {
      pending = { left, right }
      return
    }
    applyTarget(left, right)
  },
)

// 封面背景图 id 变了（任务刚生成完）：重新加载并重画封面
watch(
  () => props.coverImageId,
  () => loadCoverBg(),
)

watch(
  () => [props.mode, props.trimWidthMm, props.trimHeightMm, props.bleedMm] as const,
  () => {
    // 换成品/含出血、换开本：纹理 URL 和几何都变了，重新来一遍。
    // 四个面都先退回纸色 —— 不然旧纹理还挂在材质上，会顶着新几何显示
    //
    // **必须 dispose 再 clear**：URL 里带着 `mode`，换成品/含出血之后旧的那些再也
    // 查不到了，只 clear 的话它们永远留在显存里 —— 换一次版式漏掉当时缓存的所有页。
    // 换开本时 URL 不变，清掉只是让它们重新请求一遍（和原来行为一致）。
    // 顺序上先 dispose 再 resetFace：中间不渲染，所以不会有"材质挂着已释放纹理"的帧。
    for (const entry of textures.values()) entry.texture.dispose()
    textures.clear()
    for (const mesh of [leftMesh, rightMesh, flipFront, flipBack]) {
      if (mesh) resetFace(mesh)
    }
    shown = { left: props.leftPage, right: props.rightPage }
    layout()
    paintShown()
  },
)
</script>

<template>
  <div ref="container" class="stage3d">
    <div v-if="loading" class="hint">正在把页面渲染成书页…</div>
    <div v-else-if="failed" class="hint error">{{ failed }}</div>
    <div class="tip">点击左右半边翻页，也可以按住拖动</div>
  </div>
</template>

<style scoped>
.stage3d {
  position: relative;
  width: 100%;
  height: 100%;
  overflow: hidden;
  /* 背景是 WebGL 画的，这里只兜底：canvas 还没挂上时不至于闪白 */
  background: #e9e2d5;
  cursor: grab;
  /* touch-action: none 是拖拽跟手的前提：不加的话触屏上浏览器会把横向拖动当成
     滚动手势，直接发 pointercancel —— 拖拽在触屏上完全不工作，而桌面上测不出来 */
  touch-action: none;
  user-select: none;
}

.stage3d:active {
  cursor: grabbing;
}

.stage3d :deep(canvas) {
  display: block;
}

.hint {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  color: #8b7f6b;
  pointer-events: none;
}

.hint.error {
  color: #c45656;
}

.tip {
  position: absolute;
  left: 50%;
  bottom: 16px;
  transform: translateX(-50%);
  font-size: 12px;
  color: #9c8f79;
  pointer-events: none;
}
</style>
