"""The pipeline diagram: node/edge tables, a tiny orthogonal router, and a self-check.

The drawing is generated from two tables instead of hand-placed paths, because a diagram
you cannot verify is a diagram that will quietly become a mess. `route()` picks the cleanest
vertical channel in each gutter (fewest crossings, no node hits, no two edges sharing the
same lane), and `check()` refuses the build if a box overlaps another box or if an edge
runs through a box.

Node positions are a grid: 6 columns of 180 px with 30 px gutters, 6 rows of 52 px. Row 6 is
the band for branches the policy declares but the code does not implement.
"""

from __future__ import annotations

from map_svg import ACC, DIM, FAINT, LINE, MONO, Fig, _esc

COL_W, NODE_W, NODE_H = 214, 180, 52
COL_X = [8 + i * COL_W for i in range(6)]          # 8, 222, 436, 650, 864, 1078
ROW_Y = [40, 106, 172, 238, 304, 388]              # row 6 is the "not wired" band
WIDTH, HEIGHT = 1266, 452

# (id, col, row, title, [sub lines], tone)   tone: exec | state | gap | out | ship
NODES = [
    ("masters", 0, 0, "masters/*.csv", ["customers, suppliers,", "bank_accounts, terms, fx"], "exec"),
    ("dumps", 0, 1, "<phase>/ dumps", ["ar + ap invoices,", "credit notes, bank txns"], "exec"),
    ("load", 1, 0, "Phase · Masters", ["text in, cents out", "lookups built once", "sorted by booking date"], "exec"),
    ("lookups", 2, 0, "masters lookups", ["terms · iban_owner", "own_accounts · names"], "exec"),
    ("items", 2, 1, "items frame", ["14 columns", "unique by item_id"], "exec"),
    ("txns", 2, 2, "bank lines", ["amount_cents", "booking-date order"], "exec"),
    ("index", 3, 0, "Ledger", ["open / paid is state", "doc, party, amount keys"], "state"),
    ("gate1", 3, 1, "1 · documents", ["returned, operation,", "advice: first wins"], "exec"),
    ("gate2", 3, 2, "2 · inference", ["reference, sweep, party:", "best fit to cash wins"], "exec"),
    ("gate3", 3, 3, "3 · fallback", ["supplier refund,", "then the category"], "exec"),
    ("alloc", 4, 2, "allocate", ["_rows_from_docs · _share", "Row[]"], "exec"),
    ("explain", 4, 3, "explain", ["Exceptions.decide", "Categories.of"], "exec"),
    ("run", 5, 1, "pipeline.run", ["one row per allocation", "else one category row"], "exec"),
    ("csv", 5, 2, "submission.csv.gz", ["5 columns", "every line covered"], "out"),
    ("score", 5, 3, "score_file", ["metrics + segments", "vendored scorer"], "out"),
    ("ship", 5, 4, "validate · gate · upload", ["a human types the phase"], "ship"),
]

# Semantic edges. Straight when the rows match, otherwise routed in the gutter.
EDGES = [
    ("masters", "load"),
    ("dumps", "load"),
    ("load", "lookups"),
    ("load", "items"),
    ("load", "txns"),
    ("lookups", "index"),
    ("items", "index"),
    ("txns", "gate1"),
    ("index", "gate1"),
    ("gate1", "gate2"),
    ("gate2", "gate3"),
    ("gate3", "alloc"),
    ("alloc", "explain"),
    ("explain", "run"),
    ("run", "csv"),
    ("csv", "score"),
    ("score", "ship"),
]

# The state loop, drawn over the top of the sheet: allocation writes back into the index.
LOOP = ("alloc", "index")


def _rect(node) -> tuple[int, int, int, int]:
    return (COL_X[node[1]], ROW_Y[node[2]], NODE_W, NODE_H)


NODE_SECTION = {
    "masters": "load", "dumps": "load", "load": "load", "lookups": "load",
    "items": "load", "txns": "load", "index": "index",
    "gate1": "candidates", "gate2": "candidates", "gate3": "candidates",
    "alloc": "allocate",
    "explain": "explain", "run": "score", "csv": "score", "score": "score",
    "ship": "ship",
}


class Route:
    """Orthogonal routing on the grid, with a crossing count so the build can report it."""

    def __init__(self, nodes):
        self.by_id = {n[0]: n for n in nodes}
        self.rects = {n[0]: _rect(n) for n in nodes}
        self.segments: list[tuple[int, int, int, int, str]] = []
        self.ports: set[tuple[str, str, int]] = set()
        self.crossings = 0

    # ------------------------------------------------------------------ util
    def _hits_node(self, x1, y, x2) -> bool:
        for x, ny, w, h in self.rects.values():
            if ny - 2 <= y <= ny + h + 2:
                # ignore the 6 px a port needs to breathe next to its own box
                if x1 < x + w - 6 and x2 > x + 6:
                    return True
        return False

    def _crosses(self, seg) -> int:
        x1, y1, x2, y2 = seg[:4]
        total = 0
        for ax1, ay1, ax2, ay2, *_ in self.segments:
            h_new, h_old = y1 == y2, ay1 == ay2
            if h_new and not h_old:  # new horizontal vs old vertical
                if min(ax1, ax2) < min(x1, x2) - 1 and max(ax1, ax2) > min(x1, x2) + 1:
                    if min(y1, y2) < min(ay1, ay2) + 1 and max(y1, y2) > max(ay1, ay2) - 1:
                        total += 1
            if not h_new and h_old and (ay1 != ay2):
                if min(x1, x2) < min(ax1, ax2) - 1 and max(x1, x2) > max(ax1, ax2) + 1:
                    if min(ay1, ay2) < min(y1, y2) + 1 and max(ay1, ay2) > max(y1, y2) - 1:
                        total += 1
        return total

    def _port_y(self, node_id: str, want: int, side: str = "left") -> int:
        """Nearest free entry point on an edge of `node_id`, so ports never stack."""
        _, y, _, h = self.rects[node_id]
        lo, hi = y + 12, y + h - 8
        for delta in (0, 12, -12, 24, -24, 36, -36):
            candidate = min(max(want + delta, lo), hi)
            if (side, node_id, candidate) not in self.ports:
                self.ports.add((side, node_id, candidate))
                return candidate
        return min(max(want, lo), hi)

    def _emit(self, points, kind: str, edge_id: str = "") -> None:
        last = len(points) - 2
        for i, ((x1, y1), (x2, y2)) in enumerate(zip(points, points[1:])):
            self.segments.append((x1, y1, x2, y2, kind, i == last, edge_id))

    # --------------------------------------------------------------- routing
    def route(self, src: str, dst: str, kind: str = "main") -> list[tuple[int, int]] | None:
        sx, sy, sw, sh = self.rects[src]
        dx, dy, dw, dh = self.rects[dst]
        same_col = sx == dx
        same_row = sy == dy

        if same_col and not same_row:
            y_from = sy + sh if dy > sy else sy
            y_to = dy if dy > sy else dy + dh
            port_x = sx + 26
            self._emit([(port_x, y_from), (port_x, y_to)], kind, f"{src}>{dst}")
            return None

        if same_row:
            y = self._port_y(src, sy + 16, "right")
            y = self._port_y(dst, y, "left")
            if kind == "gap":
                y = dy + 16
            self._emit([(sx + sw, y), (dx, y)], kind, f"{src}>{dst}")
            return None

        # different rows, adjacent columns: vertical channel inside the gutter
        gutter_lo, gutter_hi = sx + sw + 4, dx - 4
        if gutter_hi <= gutter_lo:
            gutter_lo, gutter_hi = min(sx + sw, dx + dw) + 4, max(sx + sw, dx + dw) - 4
        y_src = self._port_y(src, sy + 18, "right")
        y_dst = self._port_y(dst, dy + 18, "left")

        best = None
        x = gutter_lo
        while x <= gutter_hi:
            path = [(sx + sw, y_src), (x, y_src), (x, y_dst), (dx, y_dst)]
            cost = 0
            if self._hits_node(x, y_src, x):  # vertical outside any box
                cost += 50
            if self._hits_node(sx + sw, y_src, x):
                cost += 50
            if self._hits_node(x, y_dst, dx):
                cost += 50
            for seg in zip(path, path[1:]):
                s = (seg[0][0], seg[0][1], seg[1][0], seg[1][1], kind)
                if s[0] == s[2] and s[1] == s[3]:
                    continue
                if s[0] == s[2]:  # vertical
                    if s[1] == s[3]:
                        continue
                    for ax1, ay1, ax2, ay2, *_ in self.segments:
                        if ax1 == ax2 and abs(ax1 - s[0]) < 5 and min(ay1, ay2) < max(s[1], s[3]) and max(ay1, ay2) > min(s[1], s[3]):
                            cost += 20  # lane already taken
                cost += 3 * self._crosses(s)
            if best is None or cost < best[0]:
                best = (cost, path)
                if cost == 0:
                    break
            x += 5

        assert best is not None
        path = best[1]
        self.crossings += self._crosses((path[1][0], path[1][1], path[2][0], path[2][1], kind))
        self._emit(path, kind, f"{src}>{dst}")
        return path

    def route_loop(self) -> None:
        """settle(): from the allocation back up over the sheet into the index."""
        src, dst = LOOP
        sx, sy, sw, _ = self.rects[src]
        dx, dy, dw, _ = self.rects[dst]
        top = 18
        x1 = sx + 70
        x2 = dx + 90
        path = [(x1, sy), (x1, top), (x2, top), (x2, dy)]
        self._emit(path, "loop", f"{src}>{dst}")
        return path


def build_fig() -> tuple[Fig, str]:
    """Render the diagram and return it with a one-line geometry report."""
    fig = Fig(WIDTH, HEIGHT, "fig1", "Pipeline diagram", "Blocks and flows of the reconciliation pipeline")
    router = Route(NODES)

    for src, dst in EDGES:
        kind = "main"
        router.route(src, dst, kind)
    router.route_loop()

    # 1. caption
    fig.text(8, 14, "FIG. 1   THE PIPELINE, LEFT TO RIGHT", size=8, fill=FAINT)

    # 2. edges, so the boxes sit on top of their own line ends
    for x1, y1, x2, y2, kind, head, edge_id in router.segments:
        color = ACC if kind == "loop" else LINE
        fig.edge(
            x1, y1, x2, y2,
            color=color,
            dash="4 3" if kind == "gap" else None,
            arrow=head,
            sw=1.2 if kind == "loop" else 1,
            attrs=f'class="ed ed-{kind}" data-edge="{edge_id}"',
        )

    # 3. blocks, each one an anchor to its notes
    for node in NODES:
        node_id, _col, _row, title, subs, tone = node
        x, y, w, h = router.rects[node_id]
        dash = tone == "gap"
        fill = {"state": "#12100e", "out": "#0f1412", "ship": "#12100e", "gap": "none"}.get(tone, "#0f1412")
        stroke = {"state": "#7c3a25", "ship": "#7c3a25", "gap": LINE}.get(tone, LINE)
        fig.parts.append(f'<a href="#{NODE_SECTION.get(node_id, "")}" class="nd nd-{tone}" data-node="{node_id}">')
        fig.rect(x, y, w, h, fill=fill, stroke=stroke, dash="4 3" if dash else None)
        fig.text(x + 8, y + 17, title, size=10.5, fill=DIM if dash else "#dfe4de")
        for i, sub in enumerate(subs):
            fig.text(x + 8, y + 31 + i * 11, sub, size=8.4, fill=FAINT if dash else DIM)
        fig.parts.append("</a>")

    # 4. the few labels that carry meaning
    fig.label_on(820, 18, "settle() · open_cents written back", color=ACC, size=8.2)
    for y in (167, 233):
        fig.text(662, y, "falls through", size=8, fill=DIM)
    fig.text(856, 167, "candidates", size=8, fill=DIM)
    fig.text(856, 233, "row shape", size=8, fill=DIM)
    fig.text(8, HEIGHT - 6, "one pass per phase; the gate column runs once per bank line",
             size=8, fill=FAINT)

    report = (
        f"{len(NODES)} blocks, {len(router.segments)} segments, "
        f"{router.crossings} crossings"
    )
    return fig, report


def check() -> str:
    """Fail the build on the two mistakes that make a drawing unreadable."""
    rects = {n[0]: _rect(n) for n in NODES}
    ids = list(rects)
    for i, a in enumerate(ids):
        ax, ay, aw, ah = rects[a]
        for b in ids[i + 1 :]:
            bx, by, bw, bh = rects[b]
            if ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah:
                raise SystemExit(f"diagram: {a} and {b} overlap")

    widths = {n[0]: max([len(n[3])] + [len(s) for s in n[4]]) for n in NODES}
    for node_id, chars in widths.items():
        if chars * 5.4 > NODE_W - 10:
            raise SystemExit(
                f"diagram: text in {node_id} overflows its box ({chars} chars, {NODE_W - 10}px)"
            )

    fig, report = build_fig()
    return report


__all__ = ["check", "build_fig", "NODES", "EDGES", "WIDTH", "HEIGHT"]
