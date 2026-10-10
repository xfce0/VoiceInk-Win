"""Fixed presentation geometry shared by the desktop shell and list pages."""

from __future__ import annotations

from typing import Final

MAIN_WINDOW_WIDTH: Final = 950
MAIN_WINDOW_HEIGHT: Final = 750
SIDEBAR_WIDTH: Final = 208
PAGE_VIEWPORT_WIDTH: Final = MAIN_WINDOW_WIDTH - SIDEBAR_WIDTH
PAGE_HORIZONTAL_INSET: Final = 30
LIST_PAGE_CONTENT_WIDTH: Final = PAGE_VIEWPORT_WIDTH - (2 * PAGE_HORIZONTAL_INSET)
