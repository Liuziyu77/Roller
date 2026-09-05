(function () {
  function contentRoot() {
    const rootAttr = document.documentElement.getAttribute("data-content_root") || "./";
    return new URL(rootAttr, window.location.href);
  }

  function currentRelPath(root) {
    const rootHref = root.href.endsWith("/") ? root.href : root.href + "/";
    let rel = window.location.href.slice(rootHref.length);
    rel = rel.split("#")[0].split("?")[0];
    return rel || "index.html";
  }

  function counterpartUrl() {
    const root = contentRoot();
    const rel = currentRelPath(root);
    const isZh = (document.documentElement.lang || "").toLowerCase().startsWith("zh");
    return isZh ? new URL("../" + rel, root).href : new URL("zh/" + rel, root).href;
  }

  function mount() {
    const isZh = (document.documentElement.lang || "").toLowerCase().startsWith("zh");
    const other = counterpartUrl();
    const box = document.createElement("div");
    box.className = "lang-switcher";
    box.setAttribute("role", "navigation");
    box.setAttribute("aria-label", "Language");

    const en = document.createElement("a");
    en.textContent = "EN";
    en.href = isZh ? other : window.location.href;
    if (!isZh) {
      en.className = "is-active";
      en.setAttribute("aria-current", "true");
    }

    const zh = document.createElement("a");
    zh.textContent = "中文";
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
