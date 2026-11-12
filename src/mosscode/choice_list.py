"""The option list behind every picker: command hints, approvals, questions, models, members."""

from rich.cells import cell_len
from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.strip import Strip
from textual.widgets import OptionList

from .appearance import MUTED_COLOR

POINTER = "❯ "
MUTED = Color.parse(MUTED_COLOR).triplet


class KeyboardOptions(OptionList):
    """Choices are confirmed with Enter, never by a click; the highlighted one is marked by ❯ as well
    as by its color, so the selection does not rest on color alone."""

    async def _on_click(self, event: events.Click) -> None:
        # Mouse clicks may focus the list but cannot execute an action.
        event.prevent_default()
        event.stop()

    def _get_left_gutter_width(self) -> int:
        return cell_len(POINTER)

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        try:
            index, offset = self._lines[self.scroll_offset.y + y]
        except IndexError:
            return strip  # Below the last option: already a blank line of the full width.
        chosen = index == self.highlighted and not self.options[index].disabled
        classes = ("option-list--option", "option-list--option-highlighted") if chosen else ("option-list--option",)
        style = self.get_visual_style(*classes).rich_style
        segments = list(strip)
        if chosen:
            # Supporting text of the highlighted row takes the highlight too, so the row reads as one.
            accent = Style(color=style.color)
            segments = [
                Segment(text, part + accent, control)
                if part and part.color and part.color.triplet == MUTED
                else Segment(text, part, control)
                for text, part, control in segments
            ]
        gutter = POINTER if chosen and offset == 0 else " " * cell_len(POINTER)
        return Strip([Segment(gutter, style), *segments])
