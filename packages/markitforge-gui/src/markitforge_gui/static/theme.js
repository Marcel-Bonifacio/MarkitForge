/* SPDX-FileCopyrightText: 2026-present MarkitForge contributors
 * SPDX-License-Identifier: Apache-2.0
 */
/* Loaded synchronously before the stylesheet: apply the saved Light/Dark
 * choice early so the page never flashes in the wrong theme. "System" (the
 * default) leaves data-theme unset and the CSS follows prefers-color-scheme. */
(function () {
  "use strict";
  try {
    var choice = window.localStorage.getItem("markitforge-gui.theme");
    if (choice === "light" || choice === "dark") {
      document.documentElement.setAttribute("data-theme", choice);
    }
  } catch (e) { /* storage blocked: follow the system */ }
})();
