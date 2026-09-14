# tui/screens/base.py — shared base screen for all wizard steps
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Button, RadioButton, RadioSet


class _ButtonNavMixin:
    """Shared button navigation behaviour for all wizard screens and modals.

    Provides left/right arrow key navigation between buttons, Enter/Space
    press handling, and smart Tab behaviour that skips the Back button when
    first entering the button row from a non-button widget.
    """

    def action_focus_prev_button(self) -> None:
        self._move_button_focus(-1)

    def action_focus_next_button(self) -> None:
        self._move_button_focus(1)

    def _move_button_focus(self, delta: int) -> None:
        focused = self.focused
        if not isinstance(focused, Button):
            return
        buttons = [b for b in self.query(Button) if not b.disabled]
        idx = next((i for i, b in enumerate(buttons) if b is focused), -1)
        target = idx + delta
        if 0 <= target < len(buttons):
            buttons[target].focus()

    def action_press_focused(self) -> None:
        focused = self.focused
        if isinstance(focused, Button) and not focused.disabled:
            focused.press()

    def action_focus_next_smart(self) -> None:
        """Tab: advance focus, skipping the Back button when first entering the button row."""
        prev = self.focused
        self.app.action_focus_next()
        if (
            not isinstance(prev, Button)
            and isinstance(self.focused, Button)
            and self.focused.id == "btn-back"
        ):
            self.app.action_focus_next()


class BaseWizardScreen(Screen):
    """Base class for every wizard step screen.

    Overrides ``active_bindings`` to guarantee that the F1 (About) binding
    always appears first in the Footer, regardless of which widget currently
    holds focus.  Textual builds the binding dict in widget→Screen→App order,
    so widget-level keys (e.g. RadioSet's ``space``) get early dict positions
    and can push screen-level keys like ``f1`` further right.  Moving ``f1``
    to the front of the returned dict corrects the display order without
    touching Textual internals beyond this one property.

    Arrow-key navigation
    --------------------
    ``up``/``down`` are bound here with ``priority=True`` so they fire before
    any widget-level bindings (including RadioSet's internal up/down handling).
    The actions implement continuous arrow-key navigation across all sections:

    * When a **RadioSet** is focused and the cursor is *not* at a boundary,
      the key is delegated back to the RadioSet so it moves between options
      within the set (normal behaviour).
    * When the cursor *is* at the boundary (first option on ``up``, last on
      ``down``), focus jumps to the adjacent focusable widget instead of
      wrapping inside the RadioSet.
    * For every other focusable widget (Button, Checkbox, Input, …), the key
      always moves to the previous/next focusable widget in DOM order.

    This means users never need Tab to cross section boundaries.
    """

    BINDINGS = [
        Binding("up",   "navigate_up",   "", show=False, priority=True),
        Binding("down", "navigate_down", "", show=False, priority=True),
    ]

    def action_navigate_up(self) -> None:
        """Up arrow: move within RadioSet, or step out to the previous focusable."""
        focused = self.focused
        if isinstance(focused, RadioSet):
            enabled_indices = [
                i for i, c in enumerate(focused.children)
                if isinstance(c, RadioButton) and not c.disabled
            ]
            sel = focused._selected
            if not enabled_indices or sel is None or sel <= enabled_indices[0]:
                self.focus_previous()
            else:
                focused.action_previous_button()
        else:
            self.focus_previous()

    def _focus_next_skip_back(self) -> None:
        """Advance focus, skipping btn-back when first entering the button row from content."""
        prev = self.focused
        self.focus_next()
        if (
            not isinstance(prev, Button)
            and isinstance(self.focused, Button)
            and self.focused.id == "btn-back"
        ):
            self.focus_next()

    def action_navigate_down(self) -> None:
        """Down arrow: move within RadioSet, or step out to the next focusable."""
        focused = self.focused
        if isinstance(focused, RadioSet):
            enabled_indices = [
                i for i, c in enumerate(focused.children)
                if isinstance(c, RadioButton) and not c.disabled
            ]
            sel = focused._selected
            if not enabled_indices or sel is None or sel >= enabled_indices[-1]:
                self._focus_next_skip_back()
            else:
                focused.action_next_button()
        else:
            self._focus_next_skip_back()

    @property
    def active_bindings(self):  # type: ignore[override]
        bindings = dict(super().active_bindings)
        if "f1" in bindings:
            f1 = bindings.pop("f1")
            return {"f1": f1, **bindings}
        return bindings
