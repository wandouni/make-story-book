import * as THREE from 'three'

/**
 * 纸面弯折：把逐顶点的位移从 CPU 搬到顶点着色器。
 *
 * **形状一个字都没改** —— 和 FlipBook3D 里原来那个 `bend()` 逐字一致（见下面
 * BEND_POSITION 和它的对照）。换过来只为一件事：每帧不再跑一遍 CPU 顶点循环 +
 * `computeVertexNormals()`，而且法线从"平均相邻面法线"变成解析解。
 *
 * 用 `onBeforeCompile` 注入 `MeshStandardMaterial`，而不是自己写 ShaderMaterial：
 * PBR 光照、阴影、色彩空间（贴图的 SRGBColorSpace 与 `material.color` 相乘那套）
 * 全都白拿，自己写等于把这些重做一遍。
 */

/** 弯折的三个 uniform。调用方持有对象，改 `.value` 即可 —— **不需要** `needsUpdate`。 */
export type BendUniforms = {
  /** 翻页时纸张拱起的幅度（相对页宽）。静止的页面上恒为 0。 */
  uAmount: { value: number }
  /** 静止时书脊那侧抬起的幅度（相对页宽）。可以为负（翻动中的纸翻过 180° 时）。 */
  uRest: { value: number }
  /** 书脊在几何的哪一端：+1 = u=0（x=-0.5），-1 = u=1（x=+0.5）。 */
  uSpineEnd: { value: number }
}

/**
 * `spineEnd` 用字符串而不是 ±1 是为了和 FlipBook3D 里的说法对上（'left' = 书脊
 * 在几何左端 = u=0）。**左页的书脊在哪一缘是随版式变的**，别在调用处手写 ±1。
 */
export function bendUniforms(spineEnd: 'left' | 'right', rest = 0): BendUniforms {
  const uniforms: BendUniforms = {
    uAmount: { value: 0 },
    uRest: { value: rest },
    uSpineEnd: { value: 0 },
  }
  setSpineEnd(uniforms, spineEnd)
  return uniforms
}

/** ±1 和 'left'/'right' 的换算只此一处，调用方永远用字符串。 */
export function setSpineEnd(uniforms: BendUniforms, spineEnd: 'left' | 'right') {
  uniforms.uSpineEnd.value = spineEnd === 'left' ? 1 : -1
}

/**
 * 声明。**自带 PI 而不是用 `<common>` 里的**：深度材质那边虽然也有 `<common>`，
 * 但把依赖摊开写更省事，而且名字加了前缀，不会和任何 chunk 里的常量撞名。
 *
 * 这段是**拼在整个着色器最前面**的（在 `#include <common>` 之前）。three 的
 * `#version` 和 precision 是编译时另外加的前缀，所以这里就是普通全局作用域，
 * 声明 const 和 uniform 都合法。
 */
const BEND_DECL = /* glsl */ `
const float BEND_PI = 3.141592653589793;
const float BEND_HALF_PI = 1.5707963267948966;
uniform float uAmount;
uniform float uRest;
uniform float uSpineEnd;
`

/**
 * 顶点位移。对应原来 `bend()` 里的这一行：
 *
 *     position.setZ(i, Math.sin(u * Math.PI) * amount + rest * (1 - Math.sin((t * Math.PI) / 2)))
 *
 * `u` 用 `transformed.x + 0.5`，和 CPU 版的 `position.getX(i) + 0.5` 一致 ——
 * 位移不改 x，所以这里读位移前还是位移后都一样。
 *
 * 深度材质**没有** `beginnormal_vertex`，所以这段必须是自足的，不能依赖法线那段
 * 里声明的变量。
 *
 * `uRest` 是**书脊那侧抬起来多少**（书口保持不动）。方向只能是"书脊抬起"，不能
 * 反过来让书口垂下去：页面本来就贴在垛顶上，往下垂会直接沉进纸垛里 —— 纸垛是块
 * 实心的 Box，挡在页面前面，现象是中缝两侧露出垛顶、只剩书脊附近一条页面还看得见
 * （垛顶和纸面同色，所以看着只是"变暗了"，很容易当成光照问题）。而书脊抬起才是
 * 真书的形状：页面本来是平的，是圆背把它顶起来的。
 */
const BEND_POSITION = /* glsl */ `
#include <begin_vertex>
{
  float bendU = transformed.x + 0.5;
  float bendT = uSpineEnd > 0.0 ? bendU : 1.0 - bendU;
  transformed.z += sin(bendU * BEND_PI) * uAmount + uRest * (1.0 - sin(bendT * BEND_HALF_PI));
}
`

/**
 * 解析法线。原来靠 `computeVertexNormals()`，GPU 上没有等价物，只能自己推。
 *
 * 曲面是 `z = sin(uπ)·A + R·(1 - sin(tπ/2))`，只有 x 一个自由度（沿 y 平移生成），
 * 所以切向量是 `(1, 0, dz/du)`、法线是 `normalize(-dz/du, 0, 1)`：
 *
 *     dz/du = π·cos(uπ)·A  −  R·cos(tπ/2)·(π/2)·(dt/du)
 *
 * 其中 `dt/du` 就是 `uSpineEnd` 的符号（书脊在 u=1 时 t = 1-u，导数取负）。
 *
 * 数值上和 `computeVertexNormals()` 几乎一样：那个是相邻两个面的**割线**斜率取
 * 平均，对光滑曲面等于中心差分，和解析导数差 O(Δu²)；只有最边缘那两个顶点会
 * 从"单侧割线"变成真导数，而实测两者在书脊端只差 0.05%。所以换过来画面不变。
 *
 * **不能省** —— 省了 `objectNormal` 就退回平面法线，整页光照全错（页面会像块
 * 平板，正好把静止弧度这件事抹掉）。
 */
const BEND_NORMAL = /* glsl */ `
#include <beginnormal_vertex>
{
  float bendU = position.x + 0.5;
  float bendT = uSpineEnd > 0.0 ? bendU : 1.0 - bendU;
  float bendDz =
    BEND_PI * cos(bendU * BEND_PI) * uAmount -
    uRest * cos(bendT * BEND_HALF_PI) * BEND_HALF_PI * uSpineEnd;
  objectNormal = normalize(vec3(-bendDz, 0.0, 1.0));
}
`

/**
 * 把弯折注入一个材质。
 *
 * **表面材质和阴影用的深度材质必须走同一个函数。** `onBeforeCompile` 只管表面
 * 材质，阴影是 three 另外拿 `MeshDepthMaterial` 渲的 —— 只注入前者的话，纸本身
 * 是弯的、它投出来的影子却是一块不弯的平板矩形（不报错，但一眼假）。
 *
 * `MeshDepthMaterial` 的顶点着色器里没有 `beginnormal_vertex`，所以下面那个
 * replace 对它是空操作 —— 这正是想要的，深度不需要法线。
 */
export function applyBend<T extends THREE.Material>(
  material: T,
  uniforms: BendUniforms,
): T {
  material.onBeforeCompile = (shader) => {
    shader.uniforms.uAmount = uniforms.uAmount
    shader.uniforms.uRest = uniforms.uRest
    shader.uniforms.uSpineEnd = uniforms.uSpineEnd

    let source = shader.vertexShader
    if (!source.includes('#include <begin_vertex>')) {
      // three 改了 chunk 名就会走到这里。**故意抛**：不抛的话 replace 是空操作、
      // 页面安安静静地变成一块平板，而那是这个文件唯一要防的失败模式。
      throw new Error('three 的顶点着色器里找不到 begin_vertex，弯折注入失效')
    }
    source = source.replace('#include <beginnormal_vertex>', BEND_NORMAL)
    source = source.replace('#include <begin_vertex>', BEND_POSITION)
    shader.vertexShader = BEND_DECL + source
  }

  // 程序缓存键里**没有** onBeforeCompile 这一项 —— three 只收
  // `material.customProgramCacheKey()`（见 WebGLPrograms.getProgramCacheKey）。
  // 更糟的是缓存命中时 `WebGLRenderer.getProgram` 会直接 early-out、**根本不调用**
  // onBeforeCompile。所以不设这个键的话，一个参数相同但没注入过的材质（比如垛顶
  // 那个 `paperEdge()`，它也没有贴图）会先编译出程序并占住缓存键，之后纸面的
  // 材质直接复用它 —— 现象是页面平的、而且只在特定加载顺序下复现。
  material.customProgramCacheKey = () => 'bend'
  return material
}

/**
 * 投影用的深度材质。挂到 `mesh.customDepthMaterial` 上，`MeshDepthMaterial` 的
 * 默认打包方式是 `BasicDepthPacking`，而 three 内部给阴影用的那张是
 * `RGBADepthPacking` —— 对不上会导致影子完全不对（不是"差一点"，是错位/全黑），
 * 所以这里必须显式写出来。
 */
export function bendDepthMaterial(uniforms: BendUniforms): THREE.MeshDepthMaterial {
  return applyBend(
    new THREE.MeshDepthMaterial({ depthPacking: THREE.RGBADepthPacking }),
    uniforms,
  )
}
