(function () {
  var SCROLL_KEY = "roller-docs-sidebar-scroll";
  var ANCHOR_KEY = "roller-docs-sidebar-anchor";
  var originalScrollIntoView = Element.prototype.scrollIntoView;

  // RTD theme calls scrollIntoView() while expanding the current nav item.
  // That yanks .wy-side-scroll upward. Keep normal in-page scrolling.
  Element.prototype.scrollIntoView = function () {
    if (this.closest && this.closest(".wy-menu, .wy-side-scroll, .wy-nav-side")) {
      return;
    }
    return originalScrollIntoView.apply(this, arguments);
  };

  function sideScroll() {
    return document.querySelector(".wy-side-scroll");
  }

  function saveNavClick(link) {
    var el = sideScroll();
    if (!el || !link) {
      return;
    }
    try {
      sessionStorage.setItem(SCROLL_KEY, String(el.scrollTop));
      sessionStorage.setItem(
        ANCHOR_KEY,
        String(Math.round(link.getBoundingClientRect().top))
      );
    } catch (err) {
      /* ignore quota / private mode */
    }
  }

  function readNumber(key) {
    try {
      var raw = sessionStorage.getItem(key);
      if (raw === null) {
        return null;
      }
      var value = parseInt(raw, 10);
      return isNaN(value) ? null : value;
    } catch (err) {
      return null;
    }
  }

  function currentNavLink() {
    return (
      document.querySelector(".wy-menu li.current > a") ||
      document.querySelector(".wy-menu a.current")
    );
  }

  function restoreSidebar() {
    var el = sideScroll();
    if (!el) {
      return;
    }

    var anchorY = readNumber(ANCHOR_KEY);
    var savedScroll = readNumber(SCROLL_KEY);
    var current = currentNavLink();

    // Prefer keeping the clicked item at the same viewport Y after the
    // theme collapses the previous page and expands the new one.
    if (current && anchorY !== null) {
      var delta = Math.round(current.getBoundingClientRect().top - anchorY);
      if (delta !== 0) {
        el.scrollTop += delta;
      }
      return;
    }

    if (savedScroll !== null) {
      el.scrollTop = savedScroll;
    }
  }

  function restoreSoon() {
    restoreSidebar();
    window.requestAnimationFrame(restoreSidebar);
    window.setTimeout(restoreSidebar, 0);
    window.setTimeout(restoreSidebar, 50);
    window.setTimeout(restoreSidebar, 150);
  }

  document.addEventListener(
    "click",
    function (event) {
      var expand = event.target.closest("span.toctree-expand");
      if (expand) {
        // Expanding/collapsing in place should not jump the sidebar.
        var el = sideScroll();
        var before = el ? el.scrollTop : 0;
        window.requestAnimationFrame(function () {
          if (el) {
            el.scrollTop = before;
          }
        });
        return;
      }

      var link = event.target.closest("a");
      if (!link || !link.closest(".wy-menu")) {
        return;
      }
      // Only remember position for real page navigations.
      if (link.getAttribute("href") && link.getAttribute("href").charAt(0) !== "#") {
        saveNavClick(link);
      }
    },
    true
  );

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", restoreSoon);
  } else {
    restoreSoon();
  }
  window.addEventListener("load", restoreSoon);
})();
