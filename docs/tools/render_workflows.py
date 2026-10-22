"""Draw each n8n workflow in workflows/*.json as an n8n-style canvas (SVG + PNG via headless Chrome).

Positions, names and connections come straight from the JSON, so the pictures always match
what is deployed.   python3 docs/tools/render_workflows.py   ->  docs/workflows/<name>.svg/.png
"""
import html, json, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "workflows"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
NODE = 80                     # node square, px
PAD = 90

def kind(n):
    t, name = n["type"], n["name"]
    if t.endswith(("webhook", "Trigger", "scheduleTrigger", "manualTrigger", "errorTrigger")) or "Trigger" in t:
        return "trigger"
    if "lmChat" in t: return "model"
    if "chainLlm" in t: return "ai"
    if t.endswith("postgres"): return "db"
    if t.endswith("httpRequest"): return "api"
    if t.endswith(("if", "switch")): return "branch"
    if t.endswith("executeWorkflow"): return "sub"
    if t.endswith("respondToWebhook"): return "reply"
    return "code"

STYLE = {  # fill, stroke, tag
    "trigger": ("#2b2320", "#ea8a4f", "TRIGGER"), "db": ("#1d2633", "#6fa3e0", "POSTGRES"),
    "ai": ("#1f2a24", "#5fbf8f", "AI"), "model": ("#1f2a24", "#5fbf8f", "LLM"),
    "api": ("#2a2433", "#b08ae0", "HTTP"), "branch": ("#2d2a1c", "#d8b84a", "IF / SWITCH"),
    "sub": ("#2a1f2a", "#e07ab0", "SUB"), "reply": ("#262626", "#9a968e", "REPLY"),
    "code": ("#222222", "#7d7a74", "CODE"),
}

def wrap(text, width=17):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + (1 if cur else 0) > width and cur:
            lines.append(cur); cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    return lines + [cur]

def render(path):
    wf = json.loads(path.read_text())
    nodes = {n["name"]: n for n in wf["nodes"]}
    xs = [n["position"][0] for n in wf["nodes"]]; ys = [n["position"][1] for n in wf["nodes"]]
    x0, y0 = min(xs) - PAD, min(ys) - PAD - 40
    W = max(xs) - x0 + NODE + PAD + 40; H = max(ys) - y0 + NODE + PAD + 70
    pos = {k: (n["position"][0] - x0, n["position"][1] - y0) for k, n in nodes.items()}
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
           f'font-family="-apple-system, Helvetica, Arial, sans-serif">',
           f'<rect width="{W}" height="{H}" fill="#161616"/>',
           '<defs><pattern id="g" width="20" height="20" patternUnits="userSpaceOnUse">'
           '<circle cx="1" cy="1" r="1" fill="#2a2a2a"/></pattern>'
           '<marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
           '<path d="M0 0 L10 5 L0 10z" fill="#8a867e"/></marker>'
           '<marker id="e" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
           '<path d="M0 0 L10 5 L0 10z" fill="#d9634c"/></marker></defs>',
           f'<rect width="{W}" height="{H}" fill="url(#g)"/>',
           f'<text x="24" y="40" fill="#ece9e3" font-size="22" font-weight="600">{html.escape(wf["name"])}</text>',
           f'<text x="24" y="64" fill="#8a867e" font-size="13">{len(nodes)} nodes · drawn from workflows/{path.name} · '
           f'red = error output · dashed green = LLM used by an AI node</text>']
    for src, outs in wf["connections"].items():
        for ctype, lists in outs.items():
            for idx, lst in enumerate(lists):
                for c in lst or []:
                    if src not in pos or c["node"] not in pos: continue
                    (sx, sy), (tx, ty) = pos[src], pos[c["node"]]
                    if ctype == "main":
                        n_out = max(1, len(lists))
                        y1 = sy + NODE * (idx + 1) / (n_out + 1); x1 = sx + NODE
                        x2, y2 = tx, ty + NODE / 2
                        dx = max(40, abs(x2 - x1) / 2)
                        # the last output of a node with "continue (using error output)" is its error branch
                        err = nodes[src].get("onError") == "continueErrorOutput" and idx == n_out - 1
                        col, mk = ("#d9634c", "e") if err else ("#8a867e", "a")
                        out.append(f'<path d="M{x1} {y1} C{x1+dx} {y1} {x2-dx} {y2} {x2-2} {y2}" fill="none" '
                                   f'stroke="{col}" stroke-width="2" marker-end="url(#{mk})"/>')
                    else:   # AI sub-node (model) feeding a chain from below
                        # start below the chain's own label so the line never crosses its text
                        label_h = 18 + 15 * len(wrap(c["node"]))
                        x1, y1 = sx + NODE / 2, sy; x2, y2 = tx + NODE / 2, ty + NODE + label_h
                        out.append(f'<path d="M{x1} {y1} L{x2} {y2}" stroke="#5fbf8f" stroke-width="2" '
                                   f'stroke-dasharray="6 5" fill="none"/>')
    for name, n in nodes.items():
        x, y = pos[name]; fill, stroke, tag = STYLE[kind(n)]
        shape = (f'<circle cx="{x+NODE/2}" cy="{y+NODE/2}" r="{NODE/2}"' if kind(n) == "model" else
                 f'<rect x="{x}" y="{y}" width="{NODE}" height="{NODE}" rx="{30 if kind(n)=="trigger" else 12}"')
        out.append(f'{shape} fill="{fill}" stroke="{stroke}" stroke-width="2.5"/>')
        out.append(f'<text x="{x+NODE/2}" y="{y+NODE/2+4}" text-anchor="middle" fill="{stroke}" '
                   f'font-size="10" font-weight="700" letter-spacing="0.5">{tag}</text>')
        for i, line in enumerate(wrap(name)):
            out.append(f'<text x="{x+NODE/2}" y="{y+NODE+18+i*15}" text-anchor="middle" fill="#ece9e3" '
                       f'font-size="13">{html.escape(line)}</text>')
    out.append("</svg>")
    svg = OUT / f"{path.stem}.svg"; svg.write_text("\n".join(out))
    png = OUT / f"{path.stem}.png"
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2",
                    f"--window-size={int(W)},{int(H)}", f"--screenshot={png}", svg.as_uri()],
                   check=True, capture_output=True)
    print(f"{png.relative_to(ROOT)}  {int(W)}x{int(H)}")

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for p in sorted((ROOT / "workflows").glob("*.json")):
        render(p)
