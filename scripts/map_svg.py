"""A very small SVG toolkit for the pipeline map's block diagrams.

The map is a technical drawing, so the diagrams are built from primitives with explicit
coordinates instead of being hand-written markup: primitives are reusable, deterministic
and impossible to leave in an inconsistent state. Nothing here does layout. You place a
box at (x, y), you draw an edge between two points, and the result is the same every run.

    fig = Fig(1120, 470, "FIG. 1", "system schematic")
    fig.node(110, 16, 290, 52, "masters/*.csv", ["customers, suppliers, ..."])
    fig.edge(255, 68, 255, 92)
    fig.render()
"""

from __future__ import annotations

INK = "#dfe4de"
DIM = "#8b978f"
FAINT = "#5f6d66"
LINE = "#2b342f"
PANEL = "#0f1412"
ACC = "#e2603d"
MONO = "ui-monospace, SFMono-Regular, Menlo, monospace"


class Fig:
    """One diagram. Coordinates are absolute in the viewBox."""

    def __init__(self, width: int, height: int, fig_id: str = "", title: str = "", desc: str = ""):
        self.w = width
        self.h = height
        self.fig_id = fig_id
        self.title = title or fig_id or "diagram"
        self.desc = desc or self.title
        self.parts: list[str] = []
        self._markers: dict[str, str] = {}

    # ------------------------------------------------------------- primitives
    def _marker(self, color: str) -> str:
        key = "arw" + color.lstrip("#")
        self._markers[key] = color
        return key

    def rect(self, x, y, w, h, *, fill="none", stroke=LINE, dash=None, sw=1, opacity=None) -> None:
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        op = f' opacity="{opacity}"' if opacity is not None else ""
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="{sw}"{extra}{op}/>'
        )

    def text(
        self,
        x,
        y,
        value,
        *,
        size=10,
        fill=INK,
        anchor="start",
        weight=None,
        family=MONO,
        opacity=None,
    ) -> None:
        if value is None or value == "":
            return
        w = f' font-weight="{weight}"' if weight else ""
        op = f' opacity="{opacity}"' if opacity is not None else ""
        self.parts.append(
            f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" fill="{fill}" '
            f'text-anchor="{anchor}"{w}{op}>{_esc(value)}</text>'
        )

    def node(
        self,
        x,
        y,
        w,
        h,
        title,
        lines=(),
        *,
        tag=None,
        dash=False,
        tone="normal",
        sub_size=8.6,
    ) -> None:
        """A labelled block: bold-ish title on the first baseline, dim detail below."""
        fill = PANEL if tone != "void" else "none"
        stroke = {"normal": LINE, "accent": "#7c3a25", "done": "#3c5a45", "void": LINE}[tone]
        if tone == "accent":
            fill = "#15100e"
        self.rect(x, y, w, h, fill=fill, stroke=stroke, dash="4 3" if dash else None)
        if tag:
            self.text(x + 8, y + 13, tag, size=8, fill=FAINT)
            self.text(x + 8, y + 27, title, size=10.5, fill=INK if not dash else DIM)
            top = y + 40
        else:
            self.text(x + 8, y + (17 if lines else h / 2 + 3.5), title, size=10.5, fill=INK if not dash else DIM)
            top = y + 30
        for i, line in enumerate(lines):
            self.text(x + 8, top + i * (sub_size + 3.2), line, size=sub_size, fill=DIM if not dash else FAINT)

    def diamond(self, cx, cy, w, h, label, sub=None, *, dash=False) -> None:
        pts = f"{cx},{cy - h / 2} {cx + w / 2},{cy} {cx},{cy + h / 2} {cx - w / 2},{cy}"
        extra = ' stroke-dasharray="4 3"' if dash else ""
        self.parts.append(
            f'<polygon points="{pts}" fill="{PANEL}" stroke="{LINE}" stroke-width="1"{extra}/>'
        )
        self.text(cx, cy + 3.5, label, size=9, fill=INK if not dash else DIM, anchor="middle")
        if sub:
            self.text(cx, cy + h / 2 + 11, sub, size=8.2, fill=FAINT, anchor="middle")

    def edge(self, x1, y1, x2, y2, *, color=LINE, dash=None, arrow=True, sw=1, attrs="") -> None:
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        marker = f' marker-end="url(#{self._marker(color)})"' if arrow else ""
        pad = f" {attrs}" if attrs else ""
        self.parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
            f'stroke-width="{sw}"{extra}{marker}{pad}/>'
        )

    def poly(self, points, *, color=LINE, dash=None, arrow=True, sw=1) -> None:
        pts = " ".join(f"{x},{y}" for x, y in points)
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        marker = f' marker-end="url(#{self._marker(color)})"' if arrow else ""
        self.parts.append(
            f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{sw}"{extra}{marker}/>'
        )

    def label_on(self, x, y, value, *, color=DIM, size=8.4, anchor="middle") -> None:
        """A label sitting inside an edge gap: drawn with the panel behind it to stay legible."""
        pad = len(str(value)) * size * 0.31 + 4
        self.rect(x - pad, y - 8, pad * 2, 11, fill="#0b0d0c")
        self.text(x, y, value, size=size, fill=color, anchor=anchor)

    def rule(self, x1, y1, x2, y2, *, color=LINE, dash="2 4") -> None:
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="1"{extra}/>'
        )

    # --------------------------------------------------------------- assembly
    def _defs(self) -> str:
        out = []
        for key, color in sorted(self._markers.items()):
            out.append(
                f'<marker id="{key}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" '
                f'markerHeight="6" orient="auto"><path d="M0 0 L8 4 L0 8 z" fill="{color}"/></marker>'
            )
        return f"      <defs>\n        " + "\n        ".join(out) + "\n      </defs>\n" if out else ""

    def render(self) -> str:
        body = "\n        ".join(self.parts)
        return (
            f'    <svg viewBox="0 0 {self.w} {self.h}" role="img" '
            f'aria-labelledby="{self.fig_id}-t {self.fig_id}-d" class="fig-svg">\n'
            f'      <title id="{self.fig_id}-t">{_esc(self.title)}</title>\n'
            f'      <desc id="{self.fig_id}-d">{_esc(self.desc)}</desc>\n'
            f"{self._defs()}"
            f"      <g>\n        {body}\n      </g>\n"
            f"    </svg>"
        )


def _esc(value) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


__all__ = ["Fig", "INK", "DIM", "FAINT", "LINE", "PANEL", "ACC", "MONO"]
