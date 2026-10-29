/* ==========================================================================
   文字自动缩字 + 溢出标记。

   预览 iframe 和 Playwright 导出跑的是同一份代码（导出前会显式调用
   window.fitAllText()），所以两边结果必然一致 —— 不会出现"预览里放得下、
   导出后被截断"的情况。

   中文没有空格断行，长段落极易溢出，这是绘本排版里第一号视觉 bug。
   三重防线：CSS overflow:hidden 兜底、这里自动降字号、编辑器红框提示。
   ========================================================================== */
(function () {
  var MIN_SCALE = 0.55; // 最多缩到原始字号的 55%，再小就没法看了
  var STEP = 0.5; // 每次减 0.5px，缩得太快会跳字
  var MAX_STEPS = 80;

  function isOverflowing(el, box) {
    return el.scrollHeight > box.clientHeight + 1 || el.scrollWidth > box.clientWidth + 1;
  }

  // 量溢出的"盒子"：文字必须装进它。
  // .overlay-zone 是叠字带（白板是它的子元素），.block 是自由页的块，
  // .slot-text 是普通版式的文字槽。三者都是 flex 容器，白板/内容在里面
  // 撑不开时才说明真的放不下。
  function fitOne(el) {
    var box = el.closest('.block, .slot-text, .overlay-zone');
    if (!box) return;

    // 记住原始字号：每次重算都从它开始，否则反复缩字会越缩越小
    if (!el.dataset.baseSize) {
      el.dataset.baseSize = String(parseFloat(getComputedStyle(el).fontSize));
    }
    var base = parseFloat(el.dataset.baseSize);
    if (!base || isNaN(base)) return;

    el.style.fontSize = base + 'px';
    var size = base;
    var steps = 0;

    // 用户手动调过字号后 autoFit 会被关掉，这时不自动改，只如实标记溢出
    if (el.dataset.autofit === '1') {
      while (isOverflowing(el, box) && size > base * MIN_SCALE && steps < MAX_STEPS) {
        size -= STEP;
        el.style.fontSize = size + 'px';
        steps++;
      }
    }

    el.dataset.overflow = isOverflowing(el, box) ? '1' : '0';
    box.classList.toggle('has-overflow', el.dataset.overflow === '1');
  }

  function notifyParent(root) {
    var ids = [];
    var els = root.querySelectorAll('[data-overflow="1"]');
    for (var i = 0; i < els.length; i++) {
      if (els[i].dataset.blockId) ids.push(els[i].dataset.blockId);
    }
    if (window.parent && window.parent !== window) {
      try {
        window.parent.postMessage({ type: 'msb:overflow', ids: ids }, '*');
      } catch (err) {
        /* 父窗口不可达时忽略，不影响渲染 */
      }
    }
  }

  function fitAll(root) {
    root = root || document;
    var els = root.querySelectorAll('[data-autofit]');
    for (var i = 0; i < els.length; i++) {
      fitOne(els[i]);
    }
    notifyParent(root);
  }

  // Playwright 导出前会调用这个
  window.fitAllText = fitAll;

  function run() {
    fitAll(document);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
    run();
  }

  // 字体加载完成后文字的度量会变，必须重算一次
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(run);
  }
})();
