"""
Kernell OS SDK — Browser Controller (Phase 6)
═══════════════════════════════════════════════
Playwright-based browser automation that integrates with the Agent Runtime.

This closes the critical gap: the SDK can now interact with the real web,
not just execute code in a sandbox.

Capabilities:
  - Navigate to URLs
  - Click elements by selector or text
  - Type text into inputs
  - Extract page content (text, HTML, structured)
  - Take screenshots
  - Execute JavaScript
  - Handle forms, dropdowns, waits
  - Session persistence (cookies, auth state)

Architecture:
  BrowserController registers itself as Tools in the Agent's ToolRegistry,
  making browser actions available to the Planner automatically.

Usage:
    from kernell_sdk.browser import BrowserController

    browser = BrowserController()
    await browser.start()

    # Direct API
    await browser.navigate("https://example.com")
    content = await browser.get_text()
    await browser.click("button#submit")

    # Or register as agent tools
    browser.register_tools(agent.tools)

    await browser.stop()
"""

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("kernell.browser")


@dataclass
class BrowserResult:
    """Result of a browser action."""
    success: bool
    data: Any = None
    url: str = ""
    title: str = ""
    error: str = ""
    duration_ms: float = 0.0
    screenshot_b64: str = ""


@dataclass
class PageInfo:
    """Current page state summary."""
    url: str = ""
    title: str = ""
    text_length: int = 0
    links_count: int = 0
    forms_count: int = 0
    inputs_count: int = 0


class BrowserController:
    """
    Playwright-based browser controller for agent web interaction.

    Designed to:
      1. Work standalone for direct browser automation
      2. Register as Tools in the Agent's ToolRegistry
      3. Persist sessions across agent steps
    """

    def __init__(
        self,
        headless: bool = True,
        timeout_ms: int = 30000,
        viewport: Optional[Dict[str, int]] = None,
        user_agent: str = "Kernell-OS-SDK/3.0",
    ):
        self._headless = headless
        self._timeout = timeout_ms
        self._viewport = viewport or {"width": 1280, "height": 720}
        self._user_agent = user_agent
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._started = False

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self):
        """Start the browser. Must be called before any actions."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise ImportError(
                "playwright is required for browser control. "
                "Install with: pip install playwright && python -m playwright install chromium"
            )

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self._headless)
        self._context = await self._browser.new_context(
            viewport=self._viewport,
            user_agent=self._user_agent,
        )
        self._page = await self._context.new_page()
        self._page.set_default_timeout(self._timeout)
        self._started = True
        logger.info("[Browser] Started (Chromium, headless=%s)", self._headless)

    async def stop(self):
        """Stop the browser and clean up resources."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._started = False
        logger.info("[Browser] Stopped")

    def _ensure_started(self):
        if not self._started:
            raise RuntimeError("Browser not started. Call await browser.start() first.")

    # ── Navigation ───────────────────────────────────────────────────

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> BrowserResult:
        """Navigate to a URL."""
        self._ensure_started()
        t0 = time.time()
        try:
            await self._page.goto(url, wait_until=wait_until)
            return BrowserResult(
                success=True, url=self._page.url, title=await self._page.title(),
                duration_ms=round((time.time() - t0) * 1000, 1),
            )
        except Exception as e:
            return BrowserResult(
                success=False, error=str(e), url=url,
                duration_ms=round((time.time() - t0) * 1000, 1),
            )

    async def go_back(self) -> BrowserResult:
        """Go back in browser history."""
        self._ensure_started()
        try:
            await self._page.go_back()
            return BrowserResult(success=True, url=self._page.url)
        except Exception as e:
            return BrowserResult(success=False, error=str(e))

    # ── Interaction ──────────────────────────────────────────────────

    async def click(self, selector: str) -> BrowserResult:
        """Click an element by CSS selector."""
        self._ensure_started()
        t0 = time.time()
        try:
            await self._page.click(selector)
            await self._page.wait_for_load_state("domcontentloaded")
            return BrowserResult(
                success=True, url=self._page.url, title=await self._page.title(),
                duration_ms=round((time.time() - t0) * 1000, 1),
            )
        except Exception as e:
            return BrowserResult(
                success=False, error=str(e),
                duration_ms=round((time.time() - t0) * 1000, 1),
            )

    async def click_text(self, text: str) -> BrowserResult:
        """Click an element by its visible text content."""
        self._ensure_started()
        t0 = time.time()
        try:
            await self._page.click(f"text={text}")
            await self._page.wait_for_load_state("domcontentloaded")
            return BrowserResult(
                success=True, url=self._page.url,
                duration_ms=round((time.time() - t0) * 1000, 1),
            )
        except Exception as e:
            return BrowserResult(success=False, error=str(e))

    async def type_text(self, selector: str, text: str, clear: bool = True) -> BrowserResult:
        """Type text into an input element."""
        self._ensure_started()
        t0 = time.time()
        try:
            if clear:
                await self._page.fill(selector, text)
            else:
                await self._page.type(selector, text)
            return BrowserResult(
                success=True,
                duration_ms=round((time.time() - t0) * 1000, 1),
            )
        except Exception as e:
            return BrowserResult(success=False, error=str(e))

    async def press_key(self, key: str) -> BrowserResult:
        """Press a keyboard key (Enter, Tab, Escape, etc.)."""
        self._ensure_started()
        try:
            await self._page.keyboard.press(key)
            return BrowserResult(success=True)
        except Exception as e:
            return BrowserResult(success=False, error=str(e))

    async def select_option(self, selector: str, value: str) -> BrowserResult:
        """Select an option in a dropdown."""
        self._ensure_started()
        try:
            await self._page.select_option(selector, value)
            return BrowserResult(success=True)
        except Exception as e:
            return BrowserResult(success=False, error=str(e))

    # ── Extraction ───────────────────────────────────────────────────

    async def get_text(self, selector: str = "body") -> str:
        """Get visible text content from the page or a specific element."""
        self._ensure_started()
        try:
            element = await self._page.query_selector(selector)
            if element:
                return (await element.inner_text()).strip()
            return ""
        except Exception as e:
            logger.error(f"[Browser] get_text error: {e}")
            return ""

    async def get_html(self, selector: str = "body") -> str:
        """Get HTML content from the page or a specific element."""
        self._ensure_started()
        try:
            element = await self._page.query_selector(selector)
            if element:
                return await element.inner_html()
            return ""
        except Exception:
            return ""

    async def get_attribute(self, selector: str, attribute: str) -> Optional[str]:
        """Get an attribute value from an element."""
        self._ensure_started()
        try:
            return await self._page.get_attribute(selector, attribute)
        except Exception:
            return None

    async def get_links(self) -> List[Dict[str, str]]:
        """Get all links on the page."""
        self._ensure_started()
        try:
            return await self._page.evaluate("""
                () => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                    text: a.innerText.trim().substring(0, 100),
                    href: a.href
                })).filter(l => l.text && l.href)
            """)
        except Exception:
            return []

    async def get_inputs(self) -> List[Dict[str, str]]:
        """Get all input fields on the page."""
        self._ensure_started()
        try:
            return await self._page.evaluate("""
                () => Array.from(document.querySelectorAll('input, textarea, select')).map(el => ({
                    tag: el.tagName.toLowerCase(),
                    type: el.type || '',
                    name: el.name || '',
                    id: el.id || '',
                    placeholder: el.placeholder || '',
                    value: el.value || ''
                }))
            """)
        except Exception:
            return []

    async def page_info(self) -> PageInfo:
        """Get summary of current page state."""
        self._ensure_started()
        try:
            info = await self._page.evaluate("""
                () => ({
                    url: window.location.href,
                    title: document.title,
                    textLength: document.body.innerText.length,
                    linksCount: document.querySelectorAll('a[href]').length,
                    formsCount: document.querySelectorAll('form').length,
                    inputsCount: document.querySelectorAll('input, textarea, select').length,
                })
            """)
            return PageInfo(
                url=info["url"], title=info["title"],
                text_length=info["textLength"], links_count=info["linksCount"],
                forms_count=info["formsCount"], inputs_count=info["inputsCount"],
            )
        except Exception:
            return PageInfo(url=self._page.url if self._page else "")

    # ── Screenshot ───────────────────────────────────────────────────

    async def screenshot(self, path: Optional[str] = None, full_page: bool = False) -> BrowserResult:
        """Take a screenshot. Returns base64-encoded image."""
        self._ensure_started()
        try:
            raw = await self._page.screenshot(path=path, full_page=full_page)
            b64 = base64.b64encode(raw).decode() if raw else ""
            return BrowserResult(
                success=True, data=b64,
                url=self._page.url, screenshot_b64=b64,
            )
        except Exception as e:
            return BrowserResult(success=False, error=str(e))

    # ── JavaScript ───────────────────────────────────────────────────

    async def evaluate(self, script: str) -> BrowserResult:
        """Execute JavaScript on the page."""
        self._ensure_started()
        try:
            result = await self._page.evaluate(script)
            return BrowserResult(success=True, data=result)
        except Exception as e:
            return BrowserResult(success=False, error=str(e))

    # ── Waiting ──────────────────────────────────────────────────────

    async def wait_for(self, selector: str, timeout_ms: int = 10000) -> BrowserResult:
        """Wait for an element to appear."""
        self._ensure_started()
        try:
            await self._page.wait_for_selector(selector, timeout=timeout_ms)
            return BrowserResult(success=True)
        except Exception as e:
            return BrowserResult(success=False, error=str(e))

    # ── Agent Tool Registration ──────────────────────────────────────

    def register_tools(self, tool_registry):
        """
        Register browser actions as tools in an Agent's ToolRegistry.
        This makes browser capabilities available to the Planner.
        """
        from kernell_sdk.agent_runtime import Tool

        def _sync_wrap(coro_func):
            """Wrap async browser methods for sync tool calls."""
            def wrapper(**kwargs):
                loop = None
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    pass
                if loop and loop.is_running():
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        future = pool.submit(asyncio.run, coro_func(**kwargs))
                        return future.result(timeout=30)
                else:
                    return asyncio.run(coro_func(**kwargs))
            return wrapper

        tool_registry.register(Tool(
            name="browser_navigate",
            func=_sync_wrap(self.navigate),
            description="Navigate to a URL. Returns page title and URL.",
            parameters={"url": "The URL to navigate to"},
        ))
        tool_registry.register(Tool(
            name="browser_click",
            func=_sync_wrap(self.click),
            description="Click an element by CSS selector.",
            parameters={"selector": "CSS selector of the element to click"},
        ))
        tool_registry.register(Tool(
            name="browser_click_text",
            func=_sync_wrap(self.click_text),
            description="Click an element by its visible text content.",
            parameters={"text": "The visible text of the element to click"},
        ))
        tool_registry.register(Tool(
            name="browser_type",
            func=_sync_wrap(self.type_text),
            description="Type text into an input field.",
            parameters={"selector": "CSS selector of the input", "text": "Text to type"},
        ))
        tool_registry.register(Tool(
            name="browser_get_text",
            func=_sync_wrap(self.get_text),
            description="Get visible text from the page or a specific element.",
            parameters={"selector": "CSS selector (default: body)"},
        ))
        tool_registry.register(Tool(
            name="browser_get_links",
            func=_sync_wrap(self.get_links),
            description="Get all links on the current page.",
            parameters={},
        ))
        tool_registry.register(Tool(
            name="browser_screenshot",
            func=_sync_wrap(self.screenshot),
            description="Take a screenshot of the current page.",
            parameters={},
        ))
        tool_registry.register(Tool(
            name="browser_page_info",
            func=_sync_wrap(self.page_info),
            description="Get summary of current page (URL, title, links count, forms count).",
            parameters={},
        ))

        logger.info("[Browser] 8 browser tools registered in ToolRegistry")
