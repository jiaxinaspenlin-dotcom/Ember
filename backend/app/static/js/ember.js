/**
 * Ember client behaviour.
 *
 * Deliberately small: this file handles presentation only — scrolling,
 * keyboard shortcuts, and surfacing server errors. It never decides
 * permissions, computes unread counts, or transforms application data.
 * The Python backend is the source of truth for all of that.
 */
(function () {
  "use strict";

  var SCROLL_THRESHOLD = 120;

  function scroller() {
    return document.getElementById("message-scroll");
  }

  function isAtBottom(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_THRESHOLD;
  }

  window.emberScrollToBottom = function () {
    var el = scroller();
    if (el) {
      el.scrollTop = el.scrollHeight;
    }
  };

  window.emberScrollIfAtBottom = function () {
    var el = scroller();
    if (el && isAtBottom(el)) {
      el.scrollTop = el.scrollHeight;
    }
  };

  document.addEventListener("DOMContentLoaded", function () {
    window.emberScrollToBottom();

    // Enter sends, Shift+Enter adds a newline.
    document.addEventListener("keydown", function (event) {
      var target = event.target;
      if (
        event.key === "Enter" &&
        !event.shiftKey &&
        target &&
        target.matches &&
        target.matches("textarea[data-submit-on-enter]")
      ) {
        var form = target.closest("form");
        if (form) {
          event.preventDefault();
          form.requestSubmit();
        }
      }
    });
  });

  /**
   * Surface failures instead of swallowing them. HTMX swaps error responses
   * only for 422 (validation) — everything else gets an explicit toast so a
   * failed write is never mistaken for a successful one.
   */
  document.addEventListener("htmx:beforeSwap", function (event) {
    var status = event.detail.xhr.status;
    if (status === 422) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });

  document.addEventListener("htmx:responseError", function (event) {
    var xhr = event.detail.xhr;
    var message = "Something went wrong. Please try again.";
    try {
      var payload = JSON.parse(xhr.responseText);
      if (payload && payload.error && payload.error.message) {
        message = payload.error.message;
      }
    } catch (err) {
      if (xhr.status === 401) {
        message = "Your session expired. Please sign in again.";
      } else if (xhr.status === 403) {
        message = "You do not have permission to do that.";
      }
    }
    if (xhr.status === 401) {
      window.location.href = "/signin";
      return;
    }
    showToast(message, "error");
  });

  document.addEventListener("htmx:sendError", function () {
    showToast("Connection lost. Check your network and try again.", "error");
  });

  function showToast(message, tone) {
    var container = document.getElementById("ember-toast");
    if (!container) {
      return;
    }
    var toast = document.createElement("div");
    toast.setAttribute("role", "status");
    toast.className =
      "pointer-events-auto rounded-lg px-4 py-3 text-sm shadow-lift " +
      (tone === "error"
        ? "bg-red-600 text-white"
        : "bg-charcoal-950 text-white");
    toast.textContent = message;
    container.appendChild(toast);
    window.setTimeout(function () {
      toast.remove();
    }, 6000);
  }

  window.emberToast = showToast;
})();
