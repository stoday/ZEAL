"""Visible, per-page handoff between the account holder and Playwright.

The browser stays visible. Chromium's input gate blocks physical input to a
page while ZEAL works; individual Playwright input actions briefly reopen it.
The in-page veil is informational and does not intercept Playwright clicks.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import Any, TypeVar


_T = TypeVar("_T")

GATE_SCRIPT = r"""
(() => {
  if (window !== window.top || window.__zealGate) return;
  const state = { mode: 'automation', message: 'ZEAL 正在操作此頁面' };
  let veil;
  let label;
  function mount() {
    if (veil || !document.documentElement) return;
    const host = document.createElement('div');
    host.id = 'zeal-browser-gate';
    host.style.cssText = 'position:fixed;inset:0;z-index:2147483647;pointer-events:none';
    const shadow = host.attachShadow({mode: 'closed'});
    veil = document.createElement('div');
    veil.style.cssText = 'position:absolute;inset:0;display:flex;align-items:flex-start;justify-content:center;box-sizing:border-box;padding:18px;background:rgba(16,24,40,.18);font:600 16px/1.4 system-ui,sans-serif';
    label = document.createElement('div');
    label.style.cssText = 'max-width:min(90vw,560px);padding:12px 18px;border-radius:12px;background:#17212e;color:#fff;box-shadow:0 4px 18px #0005;text-align:center';
    veil.appendChild(label);
    shadow.appendChild(veil);
    document.documentElement.appendChild(host);
    render();
  }
  function render() {
    if (!veil) return;
    veil.style.background = state.mode === 'automation' ? 'rgba(16,24,40,.18)' : 'transparent';
    veil.style.alignItems = state.mode === 'automation' ? 'flex-start' : 'flex-end';
    veil.style.justifyContent = state.mode === 'automation' ? 'center' : 'flex-start';
    veil.style.padding = state.mode === 'automation' ? '18px' : '12px';
    label.style.background = state.mode === 'automation' ? '#17212e' : '#176247';
    label.style.maxWidth = state.mode === 'automation' ? 'min(90vw,560px)' : 'min(45vw,320px)';
    label.style.padding = state.mode === 'automation' ? '12px 18px' : '8px 12px';
    label.style.fontSize = state.mode === 'automation' ? '16px' : '13px';
    label.textContent = state.message;
  }
  window.__zealGate = {
    setMode(mode, message) {
      state.mode = mode;
      state.message = message;
      mount();
      render();
    }
  };
  mount();
  if (!veil) document.addEventListener('DOMContentLoaded', mount, {once:true});
})();
"""


class BrowserGate:
    """Control physical page input without closing or hiding Chromium."""

    def __init__(self, context: Any) -> None:
        self.context = context
        self.sessions: dict[Any, Any] = {}
        self.mode = "automation"
        self.message = "ZEAL 正在操作此頁面"
        self.human_page: Any | None = None
        context.add_init_script(script=GATE_SCRIPT)
        context.on("page", self._on_new_page)
        for page in context.pages:
            self._attach(page)

    def _on_new_page(self, page: Any) -> None:
        if self.mode == "human" and self.human_page is not None:
            old_page = self.human_page
            with contextlib.suppress(Exception):
                self.sessions[old_page].send(
                    "Input.setIgnoreInputEvents", {"ignore": True}
                )
            self.human_page = page
            self._render(old_page)
        self._attach(page)

    def _attach(self, page: Any) -> Any:
        if page in self.sessions:
            return self.sessions[page]
        session = self.context.new_cdp_session(page)
        self.sessions[page] = session
        page.on("domcontentloaded", lambda: self._render(page))
        page.on("close", lambda: self._on_close(page))
        session.send(
            "Input.setIgnoreInputEvents",
            {"ignore": self.mode != "human" or page is not self.human_page},
        )
        self._render(page)
        return session

    def _on_close(self, page: Any) -> None:
        self.sessions.pop(page, None)
        if self.mode != "human" or page is not self.human_page:
            return
        self.human_page = next(
            (candidate for candidate in reversed(self.context.pages) if not candidate.is_closed()),
            None,
        )
        if self.human_page is not None:
            with contextlib.suppress(Exception):
                self._attach(self.human_page).send(
                    "Input.setIgnoreInputEvents", {"ignore": False}
                )
                self._render(self.human_page)

    def _render(self, page: Any) -> None:
        permitted = self.mode == "human" and page is self.human_page
        with contextlib.suppress(Exception):
            page.evaluate(
                "([mode, message]) => window.__zealGate?.setMode(mode, message)",
                [
                    "human" if permitted else "automation",
                    self.message if permitted else "ZEAL 正在操作此頁面",
                ],
            )

    def automation(self, message: str = "ZEAL 正在操作此頁面") -> None:
        self.mode = "automation"
        self.message = message
        self.human_page = None
        for page in list(self.context.pages):
            if page.is_closed():
                continue
            self._attach(page).send("Input.setIgnoreInputEvents", {"ignore": True})
            self._render(page)

    def human(self, page: Any, message: str = "現在可由你操作此頁面") -> None:
        self.mode = "human"
        self.message = message
        self.human_page = page
        for candidate in list(self.context.pages):
            if candidate.is_closed():
                continue
            self._attach(candidate).send(
                "Input.setIgnoreInputEvents", {"ignore": candidate is not page}
            )
            self._render(candidate)
        page.bring_to_front()

    @contextlib.contextmanager
    def playwright_input(self, page: Any) -> Iterator[None]:
        """Allow one Playwright action, then restore the input gate."""
        if self.mode != "automation":
            yield
            return
        session = self._attach(page)
        session.send("Input.setIgnoreInputEvents", {"ignore": False})
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                session.send("Input.setIgnoreInputEvents", {"ignore": True})

    def action(self, page: Any, operation: Callable[[], _T]) -> _T:
        with self.playwright_input(page):
            return operation()
