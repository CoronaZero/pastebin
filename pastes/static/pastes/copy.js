/* 复制分享链接：渐进增强，无 JS 时链接框仍可手动复制 */
(function () {
  "use strict";

  var button = document.getElementById("copy-btn");
  var input = document.getElementById("share-url");
  var status = document.getElementById("copy-status");
  if (!button || !input) {
    return;
  }

  function report(message) {
    if (status) {
      status.textContent = message;
    }
  }

  function markDone() {
    var original = button.textContent;
    var successText = button.getAttribute("data-success-text") || "已复制";
    button.textContent = successText;
    report("链接已复制到剪贴板。");
    window.setTimeout(function () {
      button.textContent = original;
    }, 2000);
  }

  function fallbackCopy() {
    input.focus();
    input.select();
    input.setSelectionRange(0, input.value.length);
    try {
      if (document.execCommand("copy")) {
        markDone();
        return;
      }
    } catch (err) {
      /* 继续走提示路径 */
    }
    report("自动复制失败，请手动全选复制输入框中的链接。");
  }

  button.addEventListener("click", function () {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(input.value).then(markDone, fallbackCopy);
    } else {
      fallbackCopy();
    }
  });
})();
