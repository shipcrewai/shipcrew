#!/usr/bin/env python3
"""Generate the Excalidraw diagrams used across the Shipply docs.

Run from the repo root:

    python docs/diagrams/generate.py

The output files are written to docs/diagrams/ and are intended to be checked
into git. To update a diagram, edit this script and re-run it, or open the
.excalidraw file in Excalidraw and edit it by hand.

Layout rules that keep these diagrams readable:
  * All text is measured (Virgil averages ~0.63em per glyph) so boxes always
    fit their labels and arrow labels land exactly where intended.
  * Arrow labels sit ABOVE horizontal lines and BESIDE vertical lines, never
    struck through by the line.
  * Text elements are never bound to containers; positions are computed
    explicitly. (excalidraw-render centers bound text on the whole container,
    which collides with child boxes.)
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

# Color palette — hand-drawn, friendly, high contrast for both devs and non-devs.
COLORS = {
    "people": "#e3f2fd",
    "bot": "#fff3e0",
    "gate": "#e8f5e9",
    "artifact": "#f3e5f5",
    "system": "#e0f7fa",
    "action": "#fffde7",
    "external": "#ffebee",
    "storage": "#f5f5f5",
    "network": "#e8eaf6",
    "text": "#1a1a1a",
    "muted": "#6b6b6b",
    "border": "#2c2c2c",
    "arrow": "#5c5c5c",
    "subgraph_border": "#9e9e9e",
    "subgraph_fill": "#fafafa",
}

FONT_FAMILY = 1  # Virgil (Excalidraw hand-drawn font)
CHAR_W = 0.63  # average glyph width as a fraction of font size


def tw(text, font_size):
    """Estimated pixel width of (possibly multi-line) text."""
    return max(len(line) for line in text.split("\n")) * font_size * CHAR_W


def th(text, font_size):
    """Estimated pixel height of (possibly multi-line) text."""
    return len(text.split("\n")) * font_size * 1.25


def _el(id_, type_, x, y, w, h):
    return {
        "id": id_,
        "type": type_,
        "x": x,
        "y": y,
        "width": w,
        "height": h,
        "angle": 0,
        "strokeColor": COLORS["border"],
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 2,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": {"type": 3, "value": 12},
        "seed": 1,
        "version": 1,
        "versionNonce": 1,
        "isDeleted": False,
        "boundElements": [],
        "updated": 1,
        "link": None,
        "locked": False,
    }


def text_el(cx, cy, s, size=13, color=None, align="center", id=None):
    """Text centered at (cx, cy). align='left' anchors the left edge at cx."""
    w = tw(s, size)
    h = th(s, size)
    x = cx if align == "left" else cx - w / 2
    el = _el(id or f"text_{cx}_{cy}", "text", x, cy - h / 2, w, h)
    el.update(
        {
            "strokeColor": color or COLORS["text"],
            "strokeWidth": 1,
            "roughness": 0,
            "roundness": None,
            "text": s,
            "fontSize": size,
            "fontFamily": FONT_FAMILY,
            "textAlign": align,
            "verticalAlign": "middle",
            "containerId": None,
            "originalText": s,
            "lineHeight": 1.25,
            "baseline": int(size * 0.9),
        }
    )
    return el


def box(x, y, w, h, label, fill, fs=14, id=None):
    """Rounded rect with centered label. Never bound — positions are explicit."""
    r = _el(id or f"box_{x}_{y}", "rectangle", x, y, w, h)
    r["backgroundColor"] = fill
    return [r, text_el(x + w / 2, y + h / 2, label, fs, id=f"{r['id']}_t")]


def gbox(x, y, label, fill, fs=14, w=None, h=None, pad_x=28, pad_y=18, id=None):
    """Box auto-sized to fit its label."""
    w = w or tw(label, fs) + pad_x
    h = h or th(label, fs) + pad_y
    return box(x, y, w, h, label, fill, fs=fs, id=id)


def caption(cx, y_top, s, fs=11, id=None):
    """Small muted caption with its TOP at y_top, centered on cx."""
    return text_el(cx, y_top + th(s, fs) / 2, s, fs, COLORS["muted"], id=id or f"cap_{cx}_{y_top}")


def subgraph(x, y, w, h, title, fill=None, stroke=None, id=None, fs=14):
    """Container rect with an unbound title at the top-left inside."""
    r = _el(id or f"sub_{x}_{y}", "rectangle", x, y, w, h)
    r["backgroundColor"] = fill or COLORS["subgraph_fill"]
    r["strokeColor"] = stroke or COLORS["subgraph_border"]
    r["strokeStyle"] = "dashed"
    t = text_el(x + 14, y + 18, title, fs, COLORS["muted"], align="left", id=f"{r['id']}_t")
    return [r, t]


def title_bar(w, title, fs=20):
    els = box(0, 0, w, 54, title, "#ffffff", fs=fs, id="title")
    return els


def _arrow_el(id_, pts, dashed=False, both=False):
    x0, y0 = pts[0]
    rel = [[px - x0, py - y0] for px, py in pts]
    xs = [p[0] for p in rel]
    ys = [p[1] for p in rel]
    el = _el(id_, "arrow", x0, y0, max(xs), max(ys))
    el.update(
        {
            "strokeColor": COLORS["arrow"],
            "strokeStyle": "dashed" if dashed else "solid",
            "roundness": {"type": 2, "value": 8},
            "startArrowhead": "arrow" if both else None,
            "endArrowhead": "arrow",
            "points": rel,
            "startBinding": None,
            "endBinding": None,
            "lastCommittedPoint": None,
        }
    )
    # Negative extents: shift origin so all points are non-negative.
    min_x, min_y = min(xs), min(ys)
    if min_x < 0 or min_y < 0:
        el["x"] = x0 + min_x
        el["y"] = y0 + min_y
        el["width"] = max(xs) - min_x
        el["height"] = max(ys) - min_y
        el["points"] = [[p[0] - min_x, p[1] - min_y] for p in rel]
    return el


def harrow(x1, x2, y, label=None, fs=12, dashed=False, both=False, id=None, label_dy=None):
    """Horizontal arrow at height y. Label centered above the line."""
    els = [_arrow_el(id or f"ha_{x1}_{y}_{x2}", [(x1, y), (x2, y)], dashed, both)]
    if label:
        ly = y - 10 - th(label, fs) / 2 if label_dy is None else y + label_dy
        els.append(text_el((x1 + x2) / 2, ly, label, fs, id=f"{id or 'ha'}_lbl_{x1}_{y}"))
    return els


def varrow(x, y1, y2, label=None, fs=12, dashed=False, both=False, id=None, side="right"):
    """Vertical arrow at column x. Label beside the midpoint."""
    els = [_arrow_el(id or f"va_{x}_{y1}_{y2}", [(x, y1), (x, y2)], dashed, both)]
    if label:
        lx = x + 12 if side == "right" else x - 12 - tw(label, fs)
        els.append(text_el(lx if side == "right" else lx + tw(label, fs) / 2, (y1 + y2) / 2, label, fs,
                           align="left" if side == "right" else "center",
                           id=f"{id or 'va'}_lbl_{x}_{y1}"))
    return els


def elbow(pts, label=None, fs=12, dashed=False, both=False, id=None, label_seg=None, label_side="above"):
    """Polyline arrow through absolute points [(x,y), ...].

    The label sits on the midpoint of segment `label_seg` (default: the middle
    segment). label_side: 'above'/'below' for horizontal segments,
    'right'/'left' for vertical ones.
    """
    els = [_arrow_el(id or f"el_{pts[0][0]}_{pts[0][1]}", pts, dashed, both)]
    if label:
        seg = label_seg if label_seg is not None else (len(pts) - 1) // 2
        (ax, ay), (bx, by) = pts[seg], pts[seg + 1]
        mx, my = (ax + bx) / 2, (ay + by) / 2
        if ay == by:  # horizontal segment
            ly = my - 10 - th(label, fs) / 2 if label_side == "above" else my + 10 + th(label, fs) / 2
            els.append(text_el(mx, ly, label, fs, id=f"{id or 'el'}_lbl"))
        else:  # vertical segment
            lx = mx + 12 + tw(label, fs) / 2 if label_side == "right" else mx - 12 - tw(label, fs) / 2
            els.append(text_el(lx, my, label, fs, id=f"{id or 'el'}_lbl"))
    return els


def vline(x, y1, y2, dashed=True, id=None):
    """Thin vertical line (lifelines, spines)."""
    el = _arrow_el(id or f"ln_{x}_{y1}", [(x, y1), (x, y2)], dashed=dashed)
    el["endArrowhead"] = None
    el["strokeWidth"] = 1
    return el


def sequence_diagram(title, participants, messages, fs_box=13, fs_msg=12, step=58, top=150, min_gap=190):
    """UML-style sequence diagram.

    participants: [name, ...]
    messages: (from_idx, to_idx, label) or (from_idx, to_idx, label, "dashed");
              from_idx == to_idx draws a self-loop.
    Labels sit above message lines; lifelines stop just past the last message.
    """
    els = []
    gap = max(min_gap, max((tw(m[2], fs_msg) for m in messages), default=0) + 90)
    box_h = 46
    xs = [110 + i * gap for i in range(len(participants))]

    for name, px in zip(participants, xs):
        w = tw(name, fs_box) + 30
        els += box(px - w / 2, top - 60, w, box_h, name, COLORS["system"], fs=fs_box, id=f"p_{name[:8]}")

    last_y = top + step * (len(messages) - 1)
    for px in xs:
        els.append(vline(px, top - 60 + box_h, last_y + 40, id=f"ll_{px}"))

    y = top
    for i, m in enumerate(messages):
        a, b, label = m[0], m[1], m[2]
        dashed = len(m) > 3 and m[3] == "dashed"
        xa, xb = xs[a], xs[b]
        if a == b:
            loop = [(xa, y), (xa + 46, y), (xa + 46, y + 26), (xa, y + 26)]
            els.append(_arrow_el(f"self_{i}", loop, dashed=dashed))
            els.append(text_el(xa + 58, y + 13, label, fs_msg, align="left", id=f"self_lbl_{i}"))
        else:
            ly = y - 10 - th(label, fs_msg) / 2
            els.append(_arrow_el(f"msg_{i}", [(xa, y), (xb, y)], dashed=dashed))
            els.append(text_el((xa + xb) / 2, ly, label, fs_msg, id=f"msg_lbl_{i}"))
        y += step

    width = xs[-1] + 140
    return els, width, last_y + 80


def make_canvas(elements, name="diagram", pad=40):
    xs, ys = [], []
    for el in elements:
        if isinstance(el, dict) and "x" in el and "y" in el:
            xs += [el["x"], el["x"] + el.get("width", 0)]
            ys += [el["y"], el["y"] + el.get("height", 0)]
    return {
        "type": "excalidraw",
        "version": 2,
        "source": "shipply-docs-generator",
        "elements": elements,
        "appState": {
            "gridSize": 20,
            "viewBackgroundColor": "#ffffff",
            "zoom": {"value": 1},
            "scroll": {"x": (min(xs) - pad) if xs else 0, "y": (min(ys) - pad) if ys else 0},
            "theme": "light",
            "name": name,
        },
        "files": {},
    }


# ---------------------------------------------------------------------------
# Visual guide diagrams
# ---------------------------------------------------------------------------


def pipeline_at_a_glance():
    els = []
    W = 1400
    els += title_bar(W, "Shipply pipeline at a glance")

    bw, bh, gap = 160, 58, 64
    step = bw + gap
    x0 = 40
    row1 = [
        ("People chat", COLORS["people"], "any channel"),
        ("Spark", COLORS["artifact"], "the idea"),
        ("Scout", COLORS["bot"], "interviews"),
        ("Requirements", COLORS["artifact"], "goals + scope"),
        ("Doc Review", COLORS["bot"], "audits"),
        ("Gate 1", COLORS["gate"], "community votes"),
    ]
    row2 = [
        ("Product RFC", COLORS["artifact"], "approved spec"),
        ("Blueprint", COLORS["bot"], "technical plan"),
        ("Gate 2", COLORS["gate"], "maintainers"),
        ("Forge swarm", COLORS["bot"], "parallel beads"),
        ("Gate 3", COLORS["gate"], "PR review"),
        ("Merged PR", COLORS["gate"], "shipped"),
    ]
    y1, y2 = 120, 350
    centers1, centers2 = [], []
    for i, (label, color, cap) in enumerate(row1):
        x = x0 + i * step
        els += box(x, y1, bw, bh, label, color, fs=14, id=f"r1_{i}")
        els.append(caption(x + bw / 2, y1 + bh + 8, cap, id=f"r1c_{i}"))
        centers1.append(x + bw / 2)
    for i, (label, color, cap) in enumerate(row2):
        x = x0 + i * step
        els += box(x, y2, bw, bh, label, color, fs=14, id=f"r2_{i}")
        els.append(caption(x + bw / 2, y2 + bh + 8, cap, id=f"r2c_{i}"))
        centers2.append(x + bw / 2)

    for i in range(5):
        els += harrow(x0 + i * step + bw, x0 + (i + 1) * step, y1 + bh / 2, id=f"a1_{i}")
        els += harrow(x0 + i * step + bw, x0 + (i + 1) * step, y2 + bh / 2, id=f"a2_{i}")

    # Gate 1 approval wraps to row 2.
    els += elbow(
        [(centers1[-1], y1 + bh), (centers1[-1], y2 - 60), (centers2[0], y2 - 60), (centers2[0], y2)],
        "approved",
        label_seg=1,
        id="wrap",
    )
    # Doc Review bounce loop (above row 1).
    els += elbow(
        [(centers1[4], y1), (centers1[4], y1 - 32), (centers1[2], y1 - 32), (centers1[2], y1)],
        "gaps found",
        dashed=True,
        label_seg=1,
        id="bounce",
    )
    # Gate 3 changes loop (below row 2 captions).
    y_loop = y2 + bh + 44
    els += elbow(
        [(centers2[4], y2 + bh + 26), (centers2[4], y_loop), (centers2[3], y_loop), (centers2[3], y2 + bh + 26)],
        "changes requested",
        dashed=True,
        label_seg=1,
        label_side="below",
        id="changes",
    )
    return make_canvas(els, "Shipply pipeline at a glance")


def governance_and_swarm():
    els = []
    W = 1120
    els += title_bar(W, "Gates, governance & swarm execution")

    gw, gh, gy = 300, 116, 100
    gates = [
        ("Gate 1 · Product RFC\nCommunity Squad votes\nQuorum required", 40),
        ("Gate 2 · Technical RFC\nMaintainer Squad reviews\n/authorize to build", 410),
        ("Gate 3 · PR Review\nGitHub review + CI\nHuman merge", 780),
    ]
    for label, x in gates:
        els += box(x, gy, gw, gh, label, COLORS["gate"], fs=13, id=f"g_{x}")
    els += harrow(340, 410, gy + gh / 2, "approved", id="g12")
    els += harrow(710, 780, gy + gh / 2, "approved", id="g23")

    fw, fh, fx, fy = 440, 190, 410, 330
    els += box(fx, fy, fw, fh, "", COLORS["bot"], id="forge")
    els.append(text_el(fx + fw / 2, fy + 26, "Forge · parallel bead swarm", 15, id="forge_t"))
    bead_w = 84
    for i in range(4):
        els += box(fx + 24 + i * (bead_w + 16), fy + 52, bead_w, 36, f"bead {i + 1}", COLORS["artifact"], fs=11, id=f"bead_{i}")
    els.append(text_el(fx + fw / 2, fy + 122, "self-heal ×2, then human gate", 12, COLORS["muted"], id="forge_c1"))
    els.append(text_el(fx + fw / 2, fy + 146, "each bead: plan → code → test → commit", 12, COLORS["muted"], id="forge_c2"))

    # Gate 2 authorizes Forge.
    els += varrow(560, gy + gh, fy, "/authorize", id="auth")
    # Forge opens the PR (up into Gate 3).
    els += varrow(880, fy, gy + gh, "PR opened", id="pr")
    # Gate 3 sends changes back (dashed, separate lane).
    els += varrow(830, gy + gh, fy, "changes", dashed=True, side="left", id="chg")
    return make_canvas(els, "Gates, governance & swarm execution")


def immutability_contract():
    els = []
    W = 1060
    els += title_bar(W, "Gate-transition immutability")

    cols = [
        ("Requirements\nrev 1 · frozen", "Gate 1", 60),
        ("Product RFC\nrev 2 · frozen", "Gate 2", 420),
        ("Blueprint\nrev 3 · frozen", "Forge", 780),
    ]
    bw, ay, gy = 220, 100, 280
    for art, gate, x in cols:
        els += box(x, ay, bw, 76, art, COLORS["artifact"], fs=14, id=f"a_{x}")
        els += box(x, gy, bw, 84, f"{gate}\nlate change → new proposal", COLORS["gate"], fs=13, id=f"g_{x}")
        els += varrow(x + bw / 2, ay + 76, gy, "freeze", id=f"f_{x}")
    els += harrow(280, 420, ay + 38, "approved · re-frozen", id="h1")
    els += harrow(640, 780, ay + 38, "approved · re-frozen", id="h2")
    return make_canvas(els, "Gate-transition immutability")


def channel_topology():
    els = []
    W = 860
    els += title_bar(W, "Channels: where the conversation happens")

    rows = [
        ("Scout interview", COLORS["people"], "Nostr DM", "author + contributors"),
        ("Gate 1", COLORS["gate"], "MLS Squad", "community + sponsors"),
        ("Gate 2", COLORS["gate"], "MLS Squad", "maintainers only"),
        ("Forge failure", COLORS["bot"], "Nostr DM to Gate 2", "diagnostic card"),
        ("Gate 3", COLORS["system"], "GitHub PR", "human review + CI"),
    ]
    bw, bh, x0, tx = 230, 54, 40, 400
    y = 100
    for stage, color, channel, who in rows:
        els += box(x0, y, bw, bh, stage, color, fs=14, id=f"s_{stage[:6]}")
        els += harrow(x0 + bw, tx - 30, y + bh / 2, id=f"a_{stage[:6]}")
        els.append(text_el(tx, y + 16, channel, 14, align="left", id=f"c_{stage[:6]}"))
        els.append(text_el(tx, y + 38, who, 11, COLORS["muted"], align="left", id=f"w_{stage[:6]}"))
        y += 86
    return make_canvas(els, "Channels: where the conversation happens")


def bead_lifecycle():
    els = []
    W = 900
    els += title_bar(W, "Bead lifecycle in the pipeline")

    els += box(40, 100, 240, 60, "Blueprint\nemits BeadSpec[]", COLORS["bot"], fs=13, id="bp")
    els += harrow(280, 380, 130, "bd mol pour", id="pour")
    els += box(380, 100, 240, 60, "Molecule\nepic + children", COLORS["artifact"], fs=13, id="mol")

    els += subgraph(40, 220, 640, 330, "Forge execution loop", id="loop")
    rows = [
        ("bd ready", "query the claimable frontier"),
        ("bd update --claim", "claim one bead atomically"),
        ("bd close", "success → unblock dependents"),
        ("bd gate create", "failure → human gate"),
        ("bd dolt push", "sync to the shared remote"),
    ]
    y = 268
    for cmd, desc in rows:
        els += box(70, y, 210, 40, cmd, COLORS["system"], fs=12, id=f"c_{cmd[3:8]}")
        els.append(text_el(310, y + 20, desc, 13, align="left", id=f"d_{cmd[3:8]}"))
        y += 54
    els += varrow(500, 160, 220, id="into_loop")
    return make_canvas(els, "Bead lifecycle in the pipeline")


# ---------------------------------------------------------------------------
# README diagrams
# ---------------------------------------------------------------------------


def readme_architecture():
    els = []
    W = 1720
    els += title_bar(W, "Shipply — how the pieces fit")

    # Band A: channels
    els += subgraph(40, 90, W - 80, 110, "Channels", id="channels")
    ch = [("Nostr DMs", 200), ("MLS Squads", 700), ("GitHub PRs", 1200)]
    for label, x in ch:
        els += box(x, 126, 300, 52, label, COLORS["people"], fs=14, id=f"ch_{label[:5]}")

    # Band B: handlers
    els += subgraph(40, 260, W - 80, 120, "Shipply bot handlers", id="handlers")
    bots = ["Scout", "Doc Review", "Blueprint", "Forge", "Gate 1", "Gate 2", "Gate 3"]
    bw, gap = 200, 22
    x = 70
    for b in bots:
        color = COLORS["gate"] if b.startswith("Gate") else COLORS["bot"]
        els += box(x, 302, bw, 52, b, color, fs=13, id=f"h_{b[:5]}")
        x += bw + gap

    # Band C: harness
    els += box(40, 440, 620, 64, "ACP harness\nspawns `omp acp` per task · JSON-RPC stdio", COLORS["system"], fs=13, id="harness")

    # Band D: storage + external
    els += box(700, 600, 300, 60, "SQLite\nproposals", COLORS["storage"], fs=13, id="sqlite")
    els += box(1060, 600, 300, 60, "Beads / Dolt\ntask graph", COLORS["storage"], fs=13, id="dolt")
    els += box(1400, 600, 280, 60, "GitHub", COLORS["external"], fs=14, id="gh")

    # Arrows
    els += varrow(850, 200, 260, "DM · Squad · PR events", id="ev")
    els += varrow(350, 380, 440, "spawn agent · stream results", both=True, id="acp")
    els += varrow(850, 380, 600, "persist proposals", id="sql")
    els += elbow([(920, 380), (920, 540), (1210, 540), (1210, 600)], "bd CLI", label_seg=1, id="bd")
    els += elbow([(1530, 380), (1530, 540), (1540, 540), (1540, 600)], "gh CLI", label_seg=1, id="ghc")
    return make_canvas(els, "Shipply — how the pieces fit")


def readme_proposal_lifecycle():
    els = []
    W = 1690
    els += title_bar(W, "Proposal lifecycle")

    states = [
        ("INTAKE", "Scout interview"),
        ("DOC_REVIEW", "requirements audit"),
        ("GATE_1", "product vote"),
        ("BLUEPRINT", "technical plan"),
        ("GATE_2", "maintainer auth"),
        ("FORGE", "bead swarm"),
        ("GATE_3", "PR review"),
        ("CLOSED", "shipped"),
    ]
    bw, bh, gap, y = 160, 54, 50, 100
    centers = []
    for i, (name, cap) in enumerate(states):
        x = 40 + i * (bw + gap)
        color = COLORS["gate"] if "GATE" in name else COLORS["system"]
        els += box(x, y, bw, bh, name, color, fs=13, id=f"s_{name[:6]}")
        els.append(caption(x + bw / 2, y + bh + 8, cap, id=f"c_{name[:6]}"))
        centers.append(x + bw / 2)
        if i:
            els += harrow(x - gap, x, y + bh / 2, id=f"f_{i}")

    def loop(a, b, label, depth):
        els_ = elbow(
            [(centers[a], y + bh), (centers[a], depth), (centers[b], depth), (centers[b], y + bh)],
            label,
            dashed=True,
            label_seg=1,
            label_side="below",
            id=f"loop_{a}",
        )
        return els_

    els += loop(1, 0, "gaps found", y + bh + 44)
    els += loop(6, 5, "changes requested", y + bh + 44)
    return make_canvas(els, "Proposal lifecycle")


# ---------------------------------------------------------------------------
# Plan 001 — orchestration engine
# ---------------------------------------------------------------------------


def plan_001_component_topology():
    els = []
    W = 1560
    els += title_bar(W, "Plan 001 · Shipply runtime topology")

    # External column
    els += subgraph(40, 90, 300, 400, "External", id="ext")
    els += box(70, 130, 240, 52, "Nostr relays", COLORS["network"], fs=13, id="relays")
    els += box(70, 230, 240, 52, "GitHub", COLORS["external"], fs=13, id="gh")
    els += box(70, 330, 240, 52, "LLM providers", COLORS["network"], fs=13, id="llm")

    # Shipply host
    els += subgraph(420, 90, W - 460, 620, "Shipply host", id="host")
    els += box(760, 130, 420, 52, "pacto-bot-api daemon", COLORS["system"], fs=14, id="daemon")

    els += subgraph(460, 240, 1020, 150, "Bot handlers", id="bots")
    bots = ["Scout", "Doc Review", "Gate 1", "Blueprint", "Gate 2", "Forge", "Gate 3"]
    bw, gap = 130, 12
    x = 478
    for b in bots:
        color = COLORS["gate"] if b.startswith("Gate") else COLORS["bot"]
        els += box(x, 292, bw, 48, b, color, fs=12, id=f"b_{b[:5]}")
        x += bw + gap

    els += box(640, 450, 580, 56, "ACP harness — spawns `omp acp` per task\nJSON-RPC over stdio", COLORS["system"], fs=13, id="harness")
    els += box(520, 600, 300, 56, "SQLite · proposals", COLORS["storage"], fs=13, id="sqlite")
    els += box(900, 600, 300, 56, "Beads / Dolt server", COLORS["storage"], fs=13, id="dolt")

    # Wiring
    els += harrow(310, 760, 156, "Nostr events", both=True, id="w_relays")
    els += varrow(970, 182, 240, "socket RPC · events", both=True, id="w_daemon")
    els += varrow(920, 390, 450, "spawn ACP", id="w_acp")
    els += harrow(310, 640, 478, "LLM APIs", id="w_llm")
    els += varrow(560, 390, 600, "SQL", id="w_sql")
    els += elbow(
        [(1300, 390), (1300, 530), (1050, 530), (1050, 600)],
        "bd CLI",
        label_seg=1,
        label_side="below",
        id="w_bd",
    )
    els += elbow([(478, 316), (380, 316), (380, 256), (310, 256)], "gh CLI", label_seg=0, id="w_gh")
    return make_canvas(els, "Plan 001 · Shipply runtime topology")


def plan_001_state_machine():
    els = []
    states = ["INTAKE", "DOC_REVIEW", "GATE_1", "BLUEPRINT", "GATE_2", "FORGE", "GATE_3", "CLOSED"]
    labels = ["interview done", "audit passed", "quorum", "blueprint ready", "/authorize", "beads done", "merged"]
    bw, bh, y = 160, 54, 120
    # Gap per transition sized so the label fits between boxes.
    gaps = [tw(label, 11) + 18 for label in labels]
    xs = [40]
    for g in gaps:
        xs.append(xs[-1] + bw + g)
    els += title_bar(xs[-1] + bw, "Plan 001 · proposal state machine")
    centers = []
    for i, name in enumerate(states):
        x = xs[i]
        color = COLORS["gate"] if "GATE" in name else COLORS["system"]
        els += box(x, y, bw, bh, name, color, fs=13, id=f"s_{name[:6]}")
        centers.append(x + bw / 2)
        if i:
            els += harrow(x - gaps[i - 1], x, y + bh / 2, labels[i - 1], fs=11, id=f"f_{i}")

    loops = [
        (1, 0, "gaps found", 230),
        (2, 1, "rejected", 268),
        (4, 3, "rejected", 230),
        (6, 5, "changes requested", 230),
    ]
    for a, b, label, depth in loops:
        els += elbow(
            [(centers[a], y + bh), (centers[a], depth), (centers[b], depth), (centers[b], y + bh)],
            label,
            fs=11,
            dashed=True,
            label_seg=1,
            label_side="below",
            id=f"loop_{a}",
        )
    return make_canvas(els, "Plan 001 · proposal state machine")


def plan_001_acp_handshake():
    els = []
    msgs = [
        (0, 1, "initialize {protocolVersion: 1}"),
        (1, 0, "protocolVersion · agentInfo · authMethods", "dashed"),
        (0, 1, 'authenticate {method: "agent"}'),
        (1, 0, "authenticated", "dashed"),
        (0, 1, "session/new {cwd, mcpServers}"),
        (1, 0, "sessionId", "dashed"),
        (0, 1, "session/prompt [content blocks]"),
        (1, 0, "session/update stream · tool calls · usage", "dashed"),
        (1, 0, "stopReason: end_turn + usage", "dashed"),
        (0, 1, "session/cancel (notification)"),
    ]
    seq, w, h = sequence_diagram("Plan 001 · ACP handshake", ["Handler", "omp (ACP)"], msgs)
    els += title_bar(w, "Plan 001 · ACP handshake")
    els += seq
    return make_canvas(els, "Plan 001 · ACP handshake")


# ---------------------------------------------------------------------------
# Plan 002 — shared OMP config & GitHub App
# ---------------------------------------------------------------------------


def plan_002_component_topology():
    els = []
    W = 1500
    els += title_bar(W, "Plan 002 · deployment topology")

    # Column 1: secrets, init, volumes
    els += subgraph(40, 90, 300, 210, "Docker secrets", id="secrets")
    for i, s in enumerate(["github-app-private-key", "github-webhook-secret", "omp-broker-token"]):
        els += box(70, 130 + i * 52, 240, 40, s, COLORS["external"], fs=11, id=f"sec_{i}")

    els += box(70, 350, 240, 52, "init-omp\n(one-shot provisioner)", COLORS["action"], fs=12, id="init")

    els += subgraph(40, 440, 300, 210, "Shared volumes", id="vols")
    for i, s in enumerate(["omp-config (read-only)", "pacto socket", "bridge-dedup.db"]):
        els += box(70, 480 + i * 52, 240, 40, s, COLORS["storage"], fs=11, id=f"vol_{i}")

    # Column 2: handlers
    els += subgraph(420, 180, 380, 300, "Bot handlers (7)", id="handlers")
    bots = ["scout", "doc-review", "gate-1", "blueprint", "gate-2", "forge", "gate-3"]
    for i, b in enumerate(bots):
        x = 440 + (i % 2) * 180
        y = 230 + (i // 2) * 58
        els += box(x, y, 168, 44, b, COLORS["bot"], fs=11, id=f"hb_{b[:6]}")

    # Column 3: services
    els += box(880, 120, 300, 56, "omp-auth-broker", COLORS["system"], fs=13, id="broker")
    els += box(880, 250, 300, 56, "github-app-token-manager", COLORS["system"], fs=13, id="tm")
    els += box(880, 420, 300, 56, "github-webhook-bridge", COLORS["system"], fs=13, id="bridge")

    # Column 4: external
    els += subgraph(1260, 90, 220, 440, "External", id="ext2")
    els += box(1290, 130, 160, 56, "GitHub\nApp / API", COLORS["external"], fs=12, id="ghapp")
    els += box(1290, 250, 160, 56, "LLM\nproviders", COLORS["network"], fs=12, id="llms")
    els += box(1290, 370, 160, 56, "HTTPS\ningress", COLORS["external"], fs=12, id="ingress")

    # Wiring
    els += varrow(190, 300, 350, "mounted at boot", fs=11, id="w_s2i")
    els += varrow(190, 402, 440, "provisions config", fs=11, id="w_i2v")
    els += harrow(340, 420, 470, "read-only mount", fs=11, id="w_v2h")
    els += elbow(
        [(340, 120), (820, 120), (820, 148), (880, 148)],
        "broker token",
        fs=11,
        label_seg=0,
        id="w_s2b",
    )
    els += elbow(
        [(340, 165), (850, 165), (850, 278), (880, 278)],
        "github app key",
        fs=11,
        label_seg=0,
        id="w_s2tm",
    )
    els += elbow(
        [(800, 240), (835, 240), (835, 156), (880, 156)],
        "auth",
        fs=11,
        label_seg=0,
        id="w_h2b",
    )
    els += elbow(
        [(1180, 278), (1245, 278), (1245, 158), (1290, 158)],
        "installation tokens",
        fs=11,
        label_seg=1,
        label_side="right",
        id="w_t2g",
    )
    els += elbow(
        [(1180, 148), (1220, 148), (1220, 265), (1290, 265)],
        "provider creds",
        fs=11,
        label_seg=1,
        label_side="left",
        id="w_b2l",
    )
    els += elbow(
        [(1180, 448), (1240, 448), (1240, 398), (1290, 398)],
        "webhooks",
        fs=11,
        both=True,
        label_seg=0,
        id="w_b2i",
    )
    els += elbow(
        [(880, 448), (845, 448), (845, 585), (610, 585), (610, 480)],
        "Nostr DM (via messenger bot)",
        fs=11,
        dashed=True,
        label_seg=2,
        id="w_b2h",
    )
    return make_canvas(els, "Plan 002 · deployment topology")


def plan_002_token_lifecycle():
    els = []
    msgs = [
        (0, 1, "get_source_token(repo)"),
        (1, 1, "cache hit → return cached"),
        (1, 2, "POST /app/installations/{id}/access_tokens"),
        (2, 1, "token + expires_at", "dashed"),
        (1, 1, "cache token (TTL 55 min)"),
        (1, 0, "installation token", "dashed"),
    ]
    seq, w, h = sequence_diagram(
        "Plan 002 · installation token lifecycle",
        ["Handler", "Token manager", "GitHub"],
        msgs,
    )
    els += title_bar(w, "Plan 002 · installation token lifecycle")
    els += seq
    return make_canvas(els, "Plan 002 · installation token lifecycle")


def plan_002_pr_lifecycle():
    els = []
    W = 1000
    els += title_bar(W, "Plan 002 · PR lifecycle")

    bw, bh = 220, 56
    els += box(40, 110, bw, bh, "BRANCH_PUSHED", COLORS["system"], fs=13, id="bp")
    els += box(400, 110, bw, bh, "PR_OPEN", COLORS["system"], fs=13, id="po")
    els += box(760, 110, bw, bh, "PR_MERGED", COLORS["gate"], fs=13, id="pm")
    els += box(400, 300, bw, bh, "CHANGES_REQUESTED", COLORS["external"], fs=13, id="cr")
    els += box(40, 300, bw, bh, "PR_UPDATED", COLORS["system"], fs=13, id="pu")

    els += harrow(260, 400, 138, "gh pr create", fs=11, id="create")
    els += harrow(620, 760, 138, "merged", fs=11, id="merge")
    els += varrow(510, 166, 300, "review: changes", fs=11, id="review1")
    els += harrow(400, 260, 328, "review again", fs=11, id="review2")
    els += elbow([(510, 300), (510, 230), (150, 230), (150, 166)], "re-push", fs=11, label_seg=1, id="repush")
    els += varrow(120, 166, 300, "synchronize (existing PR)", fs=11, dashed=True, side="left", id="sync")
    return make_canvas(els, "Plan 002 · PR lifecycle")


# ---------------------------------------------------------------------------
# Plan 003 — stateless webhook bridge
# ---------------------------------------------------------------------------


def plan_003_webhook_bridge():
    els = []
    msgs = [
        (0, 1, "POST /webhook (HMAC signed)"),
        (1, 1, "validate HMAC · dedup"),
        (1, 2, "kind=4 encrypted DM"),
        (2, 3, "deliver DM", "dashed"),
        (3, 3, "parse bridge_event · route by repo"),
        (3, 4, "MLS group message or DM"),
        (4, 4, "handle bridge_event"),
    ]
    seq, w, h = sequence_diagram(
        "Plan 003 · webhook bridge to Nostr",
        ["GitHub", "webhook-bridge", "Nostr relay", "messenger bot", "handler bot"],
        msgs,
    )
    els += title_bar(w, "Plan 003 · webhook bridge to Nostr")
    els += seq
    return make_canvas(els, "Plan 003 · webhook bridge to Nostr")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main():
    out_dir = Path("docs/diagrams")
    os.makedirs(out_dir, exist_ok=True)

    files = {
        # Visual guide
        "shipply-pipeline-at-a-glance.excalidraw": pipeline_at_a_glance(),
        "governance-and-swarm-execution.excalidraw": governance_and_swarm(),
        "gate-transition-immutability.excalidraw": immutability_contract(),
        "channel-topology.excalidraw": channel_topology(),
        "bead-lifecycle.excalidraw": bead_lifecycle(),
        # README
        "readme-architecture.excalidraw": readme_architecture(),
        "readme-proposal-lifecycle.excalidraw": readme_proposal_lifecycle(),
        # Plan 001
        "plan-001-component-topology.excalidraw": plan_001_component_topology(),
        "plan-001-state-machine.excalidraw": plan_001_state_machine(),
        "plan-001-acp-handshake.excalidraw": plan_001_acp_handshake(),
        # Plan 002
        "plan-002-component-topology.excalidraw": plan_002_component_topology(),
        "plan-002-token-lifecycle.excalidraw": plan_002_token_lifecycle(),
        "plan-002-pr-lifecycle.excalidraw": plan_002_pr_lifecycle(),
        # Plan 003
        "plan-003-webhook-bridge.excalidraw": plan_003_webhook_bridge(),
    }

    for fname, data in files.items():
        path = out_dir / fname
        if not all(isinstance(e, dict) for e in data["elements"]):
            raise ValueError(f"{fname} contains non-dict elements")
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"Wrote {path}")

    # Render SVG previews for embedding in markdown. PNG needs Cairo and may fail
    # on macOS without Homebrew's cairo package; SVG is sufficient for docs.
    excalidraw_render = shutil.which("excalidraw-render")
    if excalidraw_render:
        print("Rendering SVG previews...")
        subprocess.run(
            [excalidraw_render, str(out_dir), "-o", str(out_dir), "-f", "svg"],
            check=True,
        )
    else:
        print("excalidraw-render not found; SVG previews not generated.")
        print("Install it with: python -m pip install excalidraw-render")


if __name__ == "__main__":
    main()
