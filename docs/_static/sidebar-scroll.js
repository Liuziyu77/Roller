(function () {
  const navPositionKey = "roller-docs-nav-position";

  function uiCopy() {
    const locales = window.DOCS_UI_LOCALES || {};
    const isZh = (document.documentElement.lang || "").toLowerCase().startsWith("zh");
    return locales[isZh ? "zh" : "en"] || locales.en || {};
  }

  function syncNavButton(button) {
    const item = button.closest("li");
    const expanded = item.classList.contains("roller-expanded");
    const copy = uiCopy();
    const label = expanded
      ? copy.collapseNav || "Collapse section"
      : copy.expandNav || "Expand section";
    button.setAttribute("aria-expanded", String(expanded));
    item.setAttribute("aria-expanded", String(expanded));
    button.setAttribute("aria-label", label);
    button.title = label;
  }

  function syncMobileMenu() {
    const toggle = document.querySelector('.wy-nav-top > [data-toggle="wy-nav-top"]');
    if (!toggle) return;
    const open = document.querySelector(".wy-nav-side.shift") !== null;
    const copy = uiCopy();
    const label = open
      ? copy.closeMobileNav || "Close navigation"
      : copy.openMobileNav || "Open navigation";
    toggle.setAttribute("role", "button");
    toggle.setAttribute("tabindex", "0");
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", label);
    toggle.title = label;
  }

  function toggleSection(button) {
    const item = button.closest("li");
    const sidebar = document.querySelector(".wy-side-scroll");
    const buttonTop = button.getBoundingClientRect().top;
    const willExpand = !item.classList.contains("roller-expanded");

    if (willExpand) {
      Array.from(item.parentElement.children).forEach(function (sibling) {
        if (sibling !== item && sibling.classList.contains("roller-expanded")) {
          sibling.classList.remove("roller-expanded");
          const siblingButton = sibling.querySelector(":scope > a > button.toctree-expand");
          if (siblingButton) syncNavButton(siblingButton);
        }
      });
    }

    item.classList.toggle("roller-expanded", willExpand);
    syncNavButton(button);
    if (sidebar) {
      sidebar.scrollTop += button.getBoundingClientRect().top - buttonTop;
    }
  }

  function syncNavButtons() {
    document.querySelectorAll(".wy-menu-vertical button.toctree-expand").forEach(syncNavButton);
  }

  function fitCurrentGroup(sidebar, group) {
    const sidebarRect = sidebar.getBoundingClientRect();
    const groupRect = group.getBoundingClientRect();
    if (groupRect.height > sidebar.clientHeight) return;
    if (groupRect.top < sidebarRect.top) {
      sidebar.scrollTop += Math.floor(groupRect.top - sidebarRect.top);
    } else if (groupRect.bottom > sidebarRect.bottom) {
      sidebar.scrollTop += Math.ceil(groupRect.bottom - sidebarRect.bottom);
    }
  }

  function restoreInitialScroll() {
    const sidebar = document.querySelector(".wy-side-scroll");
    const current = document.querySelector(".wy-menu-vertical li.toctree-l1.current");
    if (!sidebar || !current) return;
    const group = current.parentElement;
    const saved = JSON.parse(sessionStorage.getItem(navPositionKey) || "null");
    sessionStorage.removeItem(navPositionKey);
    if (saved && saved.pathname === location.pathname) {
      sidebar.scrollTop += group.getBoundingClientRect().top - saved.groupTop;
    }
    fitCurrentGroup(sidebar, group);
    syncNavButtons();
  }

  let scrollTopBeforeHash = null;
  window.addEventListener("hashchange", function () {
    const sidebar = document.querySelector(".wy-side-scroll");
    scrollTopBeforeHash = sidebar ? sidebar.scrollTop : null;
  });

  function restoreHashScroll() {
    const sidebar = document.querySelector(".wy-side-scroll");
    if (sidebar && scrollTopBeforeHash !== null) {
      sidebar.scrollTop = scrollTopBeforeHash;
      scrollTopBeforeHash = null;
    }
    syncNavButtons();
  }

  document.addEventListener("click", function (event) {
    const button = event.target.closest(".wy-menu-vertical button.toctree-expand");
    if (button) {
      event.preventDefault();
      event.stopImmediatePropagation();
      toggleSection(button);
      return;
    }

    const link = event.target.closest(".wy-menu-vertical a[href]");
    if (link && event.button === 0 && !event.ctrlKey && !event.metaKey &&
        !event.shiftKey && !event.altKey) {
      const destination = new URL(link.href);
      if (destination.pathname !== location.pathname) {
        const sidebar = document.querySelector(".wy-side-scroll");
        const group = link.closest("li.toctree-l1")?.parentElement;
        if (sidebar && group) {
          sessionStorage.setItem(navPositionKey, JSON.stringify({
            pathname: destination.pathname,
            groupTop: group.getBoundingClientRect().top,
          }));
        }
      }
    }

  }, true);

  document.addEventListener("keydown", function (event) {
    if (event.target.matches('.wy-nav-top > [data-toggle="wy-nav-top"]') &&
        (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      event.target.click();
    }
  });

  function initialize() {
    const nav = document.querySelector(".wy-menu-vertical");
    if (!nav) return;

    syncNavButtons();
    let initialScrollRestored = false;
    new MutationObserver(function () {
      syncNavButtons();
      if (!initialScrollRestored && nav.querySelector("button.toctree-expand")) {
        initialScrollRestored = true;
        restoreInitialScroll();
        window.addEventListener("hashchange", restoreHashScroll);
      }
    }).observe(nav, {
      childList: true,
      subtree: true,
    });
    const sideNav = document.querySelector(".wy-nav-side");
    if (sideNav) {
      new MutationObserver(syncMobileMenu).observe(sideNav, {
        attributes: true,
        attributeFilter: ["class"],
      });
    }
    syncMobileMenu();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize);
  } else {
    initialize();
  }
})();
