(function () {
  function contentRoot() {
    const rootAttr = document.documentElement.getAttribute("data-content_root") || "./";
    return new URL(rootAttr, window.location.href);
  }

  function currentRelPath(root) {
    const rootPath = root.pathname.endsWith("/") ? root.pathname : root.pathname + "/";
    const rel = window.location.pathname.slice(rootPath.length);
    return rel || "index.html";
  }

  function counterpartUrl() {
    const root = contentRoot();
    const rel = currentRelPath(root);
    const isZh = (document.documentElement.lang || "").toLowerCase().startsWith("zh");
    const target = isZh ? new URL("../" + rel, root) : new URL("zh/" + rel, root);
    target.search = window.location.search;
    return target.href;
  }

  function uiCopy() {
    const locales = window.DOCS_UI_LOCALES || {};
    const isZh = (document.documentElement.lang || "").toLowerCase().startsWith("zh");
    return locales[isZh ? "zh" : "en"] || locales.en || {};
  }

  function mount() {
    const isZh = (document.documentElement.lang || "").toLowerCase().startsWith("zh");
    const other = counterpartUrl();
    const copy = uiCopy();
    const box = document.createElement("div");
    box.className = "lang-switcher";
    box.setAttribute("role", "navigation");
    box.setAttribute("aria-label", copy.langNav || "Language");

    const en = document.createElement("a");
    en.textContent = copy.langEn || "EN";
    en.href = isZh ? other : window.location.href;
    if (!isZh) {
      en.className = "is-active";
      en.setAttribute("aria-current", "true");
    }

    const zh = document.createElement("a");
    zh.textContent = copy.langZh || "中文";
    zh.href = isZh ? window.location.href : other;
    if (isZh) {
      zh.className = "is-active";
      zh.setAttribute("aria-current", "true");
    }

    box.appendChild(en);
    box.appendChild(zh);
    document.body.appendChild(box);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
