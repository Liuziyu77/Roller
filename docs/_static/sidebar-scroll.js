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
      fitCurrentGroup(sidebar, item.parentElement);
    }
  }

  function syncNavButtons() {
    document.querySelectorAll(".wy-menu-vertical button.toctree-expand").forEach(syncNavButton);
  }

  // The custom stylesheet only reveals a submenu when its <li> carries the
  // "roller-expanded" class, which is normally added by clicking the toggle.
  // Re-apply it to the current branch on every page load so that following a
  // cross-page sidebar link keeps the tree expanded instead of collapsing.
  function expandCurrentBranch() {
    ["toctree-l1", "toctree-l2", "toctree-l3"].forEach(function (level) {
      document
        .querySelectorAll(".wy-menu-vertical li." + level + ".current")
        .forEach(function (item) {
          item.classList.add("roller-expanded");
          const button = item.querySelector(":scope > a > button.toctree-expand");
          if (button) syncNavButton(button);
        });
    });
  }

  function fitCurrentGroup(sidebar, group) {
    const current = group.querySelector(":scope > li.current.roller-expanded");
    const submenu = current?.querySelector(":scope > ul");

    if (submenu) {
      submenu.classList.remove("roller-scrollable-submenu");
      submenu.style.removeProperty("max-height");
    }

    const sidebarRect = sidebar.getBoundingClientRect();
    let groupRect = group.getBoundingClientRect();
    if (groupRect.height > sidebar.clientHeight && submenu) {
      const fixedHeight = groupRect.height - submenu.getBoundingClientRect().height;
      const availableHeight = Math.floor(sidebar.clientHeight - fixedHeight - 16);
      if (availableHeight >= 96 && submenu.scrollHeight > availableHeight) {
        submenu.style.maxHeight = availableHeight + "px";
        submenu.classList.add("roller-scrollable-submenu");
        groupRect = group.getBoundingClientRect();
      }
    }

    if (groupRect.height > sidebar.clientHeight) return;
    if (groupRect.top < sidebarRect.top) {
      sidebar.scrollTop += Math.floor(groupRect.top - sidebarRect.top);
    } else if (groupRect.bottom > sidebarRect.bottom) {
      sidebar.scrollTop += Math.ceil(groupRect.bottom - sidebarRect.bottom);
    }
  }

  function currentGroup() {
    const sidebar = document.querySelector(".wy-side-scroll");
    const current = document.querySelector(".wy-menu-vertical li.toctree-l1.current");
    if (!sidebar || !current) return null;
    return { sidebar: sidebar, group: current.parentElement };
  }

  function restoreInitialScroll() {
    const active = currentGroup();
    if (!active) return;
    const { sidebar, group } = active;
    const saved = JSON.parse(sessionStorage.getItem(navPositionKey) || "null");
    sessionStorage.removeItem(navPositionKey);
    if (saved && saved.pathname === location.pathname) {
      sidebar.scrollTop += group.getBoundingClientRect().top - saved.groupTop;
    }
    fitCurrentGroup(sidebar, group);
    syncNavButtons();
  }

  function refitCurrentGroup() {
    const active = currentGroup();
    if (active) {
      fitCurrentGroup(active.sidebar, active.group);
    }
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

    expandCurrentBranch();
    syncNavButtons();
    let initialScrollRestored = false;
    new MutationObserver(function () {
      // The theme injects/rewrites nav nodes after load; re-assert the
      // expanded state of the current branch after every nav change.
      expandCurrentBranch();
      syncNavButtons();
      if (!initialScrollRestored && nav.querySelector("button.toctree-expand")) {
        initialScrollRestored = true;
        restoreInitialScroll();
        window.addEventListener("hashchange", restoreHashScroll);
      } else if (initialScrollRestored) {
        refitCurrentGroup();
      }
    }).observe(nav, {
      childList: true,
      subtree: true,
    });
    window.addEventListener("resize", refitCurrentGroup);
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
