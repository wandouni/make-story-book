<script setup lang="ts"></script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <RouterLink to="/" class="app-title">绘本工坊</RouterLink>
      <span class="app-subtitle">把已有的故事，做成一本绘本</span>
    </header>
    <main class="app-main">
      <RouterView />
    </main>
  </div>
</template>

<style scoped>
/*
  固定高度，不是 min-height —— 这一条决定了"整页到底滚不滚"。

  以前写 min-height: 100vh，内容一超高就是整个 body 在滚：顶栏、工作区的步骤条
  跟着一起滚走，各页面里那些 height: 100% 也建立在一个会变的高度上。改成固定
  高度之后，.app-main 拿到的是确定的高度，滚动责任下放给每一页自己（工作区是
  .body，列表页是 .page）。

  height: 100% 而不是 100vh：html/body/#app 已经是 height: 100%，链条完整，
  也避开移动端地址栏那套 vh 的坑。

  ⚠️ 这一层（以及 .workspace / .body）**绝不能加 transform / filter /
  backdrop-filter / will-change: transform / contain: paint**。所有 el-dialog、
  el-drawer 的遮罩都是 position: fixed，靠"包含块是视口"逃过 overflow: hidden
  的裁剪；祖先一旦有 transform，包含块变成该祖先，弹窗会被裁掉且定位错乱。
  overflow 本身不裁剪 fixed 元素，是安全的。
*/
.app-shell {
  height: 100%;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  background: #f5f6f8;
}

.app-header {
  display: flex;
  align-items: baseline;
  gap: 12px;
  padding: 14px 24px;
  background: #fff;
  border-bottom: 1px solid #e6e8eb;
  /* 严格说不需要（.app-main 是 flex:1，basis 0，不会产生负自由空间）。
     写上是为了文档价值：将来有人把 .app-main 改成 flex-basis: auto，
     这个 bug 会以"顶栏被压扁"的形式出现，很难查。 */
  flex-shrink: 0;
}

.app-title {
  font-size: 17px;
  font-weight: 600;
  color: #2c3e50;
  text-decoration: none;
}

.app-subtitle {
  font-size: 13px;
  color: #909399;
}

/* 不要给这里加 overflow：它的直接子元素（工作区 / 列表页）都是 height: 100%，
   加了也不会滚，只会多一层无用的 scroll container。 */
.app-main {
  flex: 1;
  min-height: 0;
}
</style>
