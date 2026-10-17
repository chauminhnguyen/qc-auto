"""
visualizer.py
--------------
Standalone visual graph generator.

Produces an interactive HTML file with a D3.js force-directed
graph showing the state graph, edges, and execution status.

Can be used independently or via ReportGenerator.generate_visual_graph().
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from capture_app.state.fail_stack import FailPool
from capture_app.state.state_graph import StateGraph


class GraphVisualizer:
    """Generates an interactive HTML visualization of the state graph."""

    def __init__(self, vault_path: Path | None = None):
        self.vault_path = vault_path or Path("./ObsidianVault")
        self.good_states: set[str] = set()
        self.bad_states: set[str] = set()

    def add_good(self, state_name: str):
        self.good_states.add(state_name)

    def add_bad(self, state_name: str):
        self.bad_states.add(state_name)

    def generate(self, graph: StateGraph, fail_pool: FailPool | None = None,
                  output_path: Path | str | None = None) -> str:
        """Generate the interactive HTML graph visualization."""
        if output_path is not None:
            output_path = Path(output_path)

        nodes = []
        for name, state in graph.states.items():
            if name in self.good_states:
                color = "#2ecc71"
            elif name in self.bad_states:
                color = "#e74c3c"
            elif state.is_composite:
                color = "#e67e22"
            else:
                color = "#3498db"

            nodes.append({
                "id": name,
                "label": name.replace("_", " "),
                "color": color,
                "is_composite": state.is_composite,
                "conditions": state.composite_conditions,
                "description": state.description,
                "api_note_path": state.metadata.get("api_note_path", ""),
                "vault_path": state.metadata.get("vault_path", str(self.vault_path)).replace("\\", "/"),
                "domain": state.metadata.get("domain", ""),
                "method": state.metadata.get("method", ""),
                "status": state.metadata.get("status", ""),
            })

        links = []
        for edge in graph.edges:
            label = edge.action_label or f"{edge.api_method} {edge.api_url}".strip()
            links.append({
                "source": edge.source,
                "target": edge.target,
                "api": label,
                "trigger": edge.trigger,
            })

        html = self._build_html(nodes, links, self._build_static_svg(nodes, links))

        if output_path is None:
            output_path = self.vault_path / "state_graph_visualization.html"
        output_path.write_text(html, encoding="utf-8")

        # Copy local d3.min.js next to the HTML file
        d3_src = Path(__file__).resolve().parents[2] / "d3.min.js"
        if d3_src.exists():
            d3_dst = output_path.parent / "d3.min.js"
            shutil.copy2(d3_src, d3_dst)

        return html

    @staticmethod
    def _build_static_svg(nodes: list[dict], links: list[dict]) -> str:
        """Server-side static SVG so the graph is visible even when
        JavaScript is blocked (e.g. file opened inside Obsidian preview).
        Simple deterministic grid layout; D3 hides this once it starts."""
        import html as _html
        import math
        n = len(nodes)
        if n == 0:
            return '<svg width="100%" height="200"><text x="20" y="40" fill="#888">No states in graph</text></svg>'
        cols = max(1, math.ceil(math.sqrt(n)))
        dx, dy = 240, 150
        pad_x, pad_y = 90, 70
        pos = {}
        for i, nd in enumerate(nodes):
            pos[nd["id"]] = (pad_x + (i % cols) * dx, pad_y + (i // cols) * dy)
        rows = math.ceil(n / cols)
        w = pad_x * 2 + (cols - 1) * dx
        h = pad_y * 2 + (rows - 1) * dy
        parts = [f'<svg width="100%" viewBox="0 0 {w} {h}" style="background:#0f0f23">']
        parts.append('<defs><marker id="static-arrow" viewBox="0 -5 10 10" refX="20" refY="0" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,-5L10,0L0,5" fill="#555"/></marker></defs>')
        for lk in links:
            a = pos.get(lk["source"])
            b = pos.get(lk["target"])
            if not a or not b:
                continue
            parts.append(
                f'<line x1="{a[0]}" y1="{a[1]}" x2="{b[0]}" y2="{b[1]}" stroke="#555" stroke-width="1.5" marker-end="url(#static-arrow)"/>'
            )
        for nd in nodes:
            x, y = pos[nd["id"]]
            r = 18 if nd.get("is_composite") else 14
            color = _html.escape(str(nd.get("color", "#3498db")))
            label = _html.escape(str(nd.get("label", nd["id"]))[:22])
            full = _html.escape(str(nd.get("id", "")))
            parts.append(f'<g><title>{full}</title>')
            parts.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{color}" stroke="{color}" stroke-opacity="0.4" stroke-width="3"/>')
            parts.append(f'<text x="{x}" y="{y + r + 16}" font-size="11" fill="#ccc" text-anchor="middle">{label}</text></g>')
        parts.append('</svg>')
        return "".join(parts)

    @staticmethod
    def _build_html(nodes: list[dict], links: list[dict], static_svg: str = "") -> str:
        """Build the full HTML document with simplified D3.js code."""
        nodes_json = json.dumps(nodes).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
        links_json = json.dumps(links).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>State Graph Visualization</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #0f0f23; color: #eee; overflow: hidden; }}
  #header {{ padding: 14px 22px; background: #1a1a3e; border-bottom: 2px solid #e94560; display: flex; align-items: center; justify-content: space-between; }}
  #header h1 {{ font-size: 18px; color: #e94560; }}
  #legend {{ display: flex; gap: 14px; font-size: 12px; }}
  .legend-item {{ display: flex; align-items: center; gap: 5px; }}
  .legend-dot {{ width: 10px; height: 10px; border-radius: 50%; }}
  #graph {{ width: 100vw; height: calc(100vh - 46px); position: relative; }}
  svg {{ width: 100%; height: 100%; }}
  .node-circle {{ stroke-width: 3px; cursor: pointer; }}
  .node-circle:hover {{ stroke-width: 5px; }}
  .node-label {{ font-size: 10px; fill: #ccc; pointer-events: none; text-anchor: middle; }}
  .link {{ fill: none; stroke: #444; stroke-width: 1.5px; }}
  .link-label {{ font-size: 8px; fill: #999; pointer-events: none; paint-order: stroke; stroke: #0f0f23; stroke-width: 3px; stroke-linejoin: round; }}
  .node.node-dimmed {{ opacity: 0.12; }}
  .link.link-dimmed {{ opacity: 0.06; }}
  .link-label.link-dimmed {{ opacity: 0.06; }}
  #tooltip {{ position: absolute; background: #1a1a3e; border: 1px solid #e94560; border-radius: 8px; padding: 12px 16px; font-size: 13px; pointer-events: none; opacity: 0; transition: opacity 0.15s; max-width: 320px; box-shadow: 0 4px 16px rgba(0,0,0,0.5); z-index: 100; }}
  #tooltip.visible {{ opacity: 1; }}
  #tooltip h3 {{ color: #e94560; margin-bottom: 6px; font-size: 14px; }}
  #tooltip .info {{ color: #aaa; line-height: 1.6; }}
  #controls {{ position: absolute; top: 52px; right: 16px; display: flex; flex-direction: column; gap: 6px; align-items: stretch; }}
  #controls button {{ background: #1a1a3e; color: #eee; border: 1px solid #333; border-radius: 6px; padding: 8px 14px; cursor: pointer; font-size: 12px; }}
  #controls button:hover {{ background: #2a2a5e; }}
  #search-wrap {{ display: flex; align-items: center; gap: 4px; background: #1a1a3e; border: 1px solid #333; border-radius: 6px; padding: 4px 6px; }}
  #search-wrap input {{ background: transparent; border: none; outline: none; color: #eee; font-size: 12px; width: 130px; }}
  #search-wrap input::placeholder {{ color: #777; }}
  #search-wrap button {{ background: none; border: none; color: #888; cursor: pointer; font-size: 14px; line-height: 1; padding: 0 4px; }}
  #search-wrap button:hover {{ color: #e94560; }}
  #stats {{ position: absolute; bottom: 12px; left: 16px; font-size: 11px; color: #666; }}
  #static-graph {{ position: absolute; inset: 0; overflow: auto; padding: 12px; }}
  #detail-panel {{ position: absolute; top: 52px; right: 16px; width: 360px; max-height: calc(100vh - 60px); background: #16213e; border: 1px solid #0f3460; border-radius: 10px; box-shadow: 0 4px 20px rgba(0,0,0,0.5); overflow-y: auto; display: none; z-index: 200; }}
  #detail-panel.visible {{ display: block; }}
  #detail-panel .dp-header {{ padding: 14px 16px; border-bottom: 1px solid #0f3460; display: flex; justify-content: space-between; align-items: center; }}
  #detail-panel .dp-header h3 {{ color: #e94560; margin: 0; font-size: 14px; }}
  #detail-panel .dp-close {{ background: none; border: none; color: #888; cursor: pointer; font-size: 20px; }}
  #detail-panel .dp-close:hover {{ color: #e94560; }}
  #detail-panel .dp-body {{ padding: 14px 16px; font-size: 13px; }}
  #detail-panel .dp-body .dp-section {{ margin-bottom: 12px; }}
  #detail-panel .dp-body .dp-label {{ color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }}
  #detail-panel .dp-body .dp-value {{ color: #eee; margin-top: 2px; }}
  #detail-panel .dp-body .dp-link {{ color: #4fc3f7; cursor: pointer; text-decoration: underline; }}
  #detail-panel .dp-body .dp-link:hover {{ color: #e94560; }}
  #detail-panel .dp-body .dp-status-success {{ color: #2ecc71; }}
  #detail-panel .dp-body .dp-status-fail {{ color: #e74c3c; }}
  #detail-panel .dp-body pre {{ background: #0f0f23; padding: 8px; border-radius: 4px; overflow-x: auto; font-size: 11px; color: #aaa; }}
  #detail-panel .dp-obsidian-btn {{ display: inline-block; background: #0f3460; color: #4fc3f7; border: 1px solid #1a5276; border-radius: 4px; padding: 4px 10px; cursor: pointer; font-size: 11px; text-decoration: none; margin-top: 6px; }}
  #detail-panel .dp-obsidian-btn:hover {{ background: #1a5276; }}
  #detail-edge-panel {{ position: absolute; top: 52px; right: 16px; width: 360px; background: #16213e; border: 1px solid #0f3460; border-radius: 10px; box-shadow: 0 4px 20px rgba(0,0,0,0.5); padding: 16px; display: none; z-index: 200; }}
  #detail-edge-panel.visible {{ display: block; }}
</style>
</head>
<body>
<div id="header">
  <h1>⚡ State Graph Visualization</h1>
  <div id="legend">
    <div class="legend-item"><div class="legend-dot" style="background:#2ecc71"></div> Success</div>
    <div class="legend-item"><div class="legend-dot" style="background:#e74c3c"></div> Failed</div>
    <div class="legend-item"><div class="legend-dot" style="background:#e67e22"></div> Composite</div>
    <div class="legend-item"><div class="legend-dot" style="background:#3498db"></div> Pending</div>
  </div>
</div>
<div id="graph">
  <svg id="svg" width="100%" height="100%"></svg>
  <div id="static-graph">{static_svg}</div>
  <div id="tooltip"></div>
  <div id="detail-panel">
    <div class="dp-header">
      <h3 id="dp-title">Node Details</h3>
      <button class="dp-close" onclick="closeDetail()">x</button>
    </div>
    <div class="dp-body" id="dp-body"></div>
  </div>
  <div id="detail-edge-panel">
    <div class="dp-header">
      <h3 id="de-title">Edge Details</h3>
      <button class="dp-close" onclick="closeEdge()">x</button>
    </div>
    <div class="dp-body" id="de-body"></div>
  </div>
  <div id="controls">
    <div id="search-wrap">
      <input id="search-input" type="text" placeholder="Search states…" oninput="onSearch(this.value)">
      <button onclick="clearSearch()" title="Clear search">×</button>
    </div>
    <button onclick="zoomIn()">+ Zoom</button>
    <button onclick="zoomOut()">- Zoom</button>
    <button onclick="resetZoom()">Reset View</button>
    <button onclick="toggleLabels()">Toggle Labels</button>
    <button onclick="toggleEdgeLabels()" title="Show or hide the API request label on each edge">Toggle API Labels</button>
  </div>
  <div id="stats"></div>
</div>
<script src="d3.min.js"></script>
<script>
try {{
const nodes = {nodes_json};
const links = {links_json};

function escapeHtml(value) {{
  return String(value ?? "").replace(/[&<>"']/g, char => {{
    if (char === "&") return "&amp;";
    if (char === "<") return "&lt;";
    if (char === ">") return "&gt;";
    if (char === '"') return "&quot;";
    return "&#39;";
  }});
}}

// Group edges between the same pair of nodes so duplicate/parallel edges
// (e.g. page->contact and contact->page) don't render as coincident lines
// with their labels stacked exactly on top of one another. Each edge in a
// group gets a curvature offset so it fans out from its siblings.
const pairGroups = {{}};
links.forEach(l => {{
  const a = l.source, b = l.target;
  const key = a < b ? a + "|" + b : b + "|" + a;
  (pairGroups[key] = pairGroups[key] || []).push(l);
}});
Object.values(pairGroups).forEach(group => {{
  const n = group.length;
  group.forEach((l, i) => {{
    l._curveOffset = n === 1 ? 0 : (i - (n - 1) / 2) * 34;
  }});
}});

function truncateLabel(s) {{
  if (!s) return "";
  return s.length > 30 ? s.slice(0, 28) + "…" : s;
}}

const width = window.innerWidth;
const height = window.innerHeight - 46;
const svg = d3.select("#svg").attr("width", width).attr("height", height);
const g = svg.append("g");
var _staticEl = document.getElementById("static-graph");
if (_staticEl) {{ _staticEl.style.display = "none"; }}

let currentZoomK = 1;

const zoom = d3.zoom().scaleExtent([0.1, 5]).on("zoom", e => {{
  g.attr("transform", e.transform);
  currentZoomK = e.transform.k;
}});
svg.call(zoom);

function zoomIn() {{ svg.transition().call(zoom.scaleBy, 1.4); }}
function zoomOut() {{ svg.transition().call(zoom.scaleBy, 0.7); }}
function resetZoom() {{ svg.transition().call(zoom.transform, d3.zoomIdentity); }}

let showLabels = true, showEdgeLabels = false;
function toggleLabels() {{ showLabels = !showLabels; d3.selectAll(".node-label").attr("display", showLabels ? "block" : "none"); }}
function toggleEdgeLabels() {{ showEdgeLabels = !showEdgeLabels; updateEdgeLabelVisibility(); }}
function updateEdgeLabelVisibility() {{
  linkLabel.attr("display", showEdgeLabels ? "block" : "none");
}}

const defs = svg.append("defs");
defs.append("marker").attr("id", "arrow").attr("viewBox", "0 -5 10 10").attr("refX", 22)
  .attr("refY", 0).attr("markerWidth", 6).attr("markerHeight", 6).attr("orient", "auto")
  .append("path").attr("d", "M0,-5L10,0L0,5").attr("fill", "#555");

const link = g.selectAll(".link").data(links).join("path").attr("class", "link").attr("marker-end", "url(#arrow)");
const linkLabel = g.selectAll(".link-label").data(links).join("text").attr("class", "link-label")
  .attr("display", "none")
  .text(d => truncateLabel(d.api));
linkLabel.append("title").text(d => d.api);
updateEdgeLabelVisibility();

const node = g.selectAll(".node").data(nodes).join("g").attr("class", "node")
  .call(d3.drag().on("start", dragstart).on("drag", drag).on("end", dragend));

node.append("circle").attr("class", "node-circle")
  .attr("r", d => d.is_composite ? 20 : 15)
  .attr("fill", d => d.color)
  .attr("stroke", d => d.color).attr("stroke-opacity", 0.4);

node.append("text").attr("class", "node-label")
  .text(d => d.label.split(" ").pop().slice(0, 14))
  .attr("dy", d => d.is_composite ? 30 : 26);

const tooltip = d3.select("#tooltip");
node.on("mouseover", (e, d) => {{
  tooltip.classed("visible", true).html(`<h3>${{escapeHtml(d.label)}}</h3>
    <div class="info">${{d.is_composite ? '<b>⚠ Composite State</b><br>' : ''}}
    Conditions: ${{escapeHtml(d.conditions.length ? d.conditions.join(', ') : 'None')}}<br>
    ${{d.description ? 'Desc: ' + escapeHtml(d.description) : ''}}</div>`);
}}).on("mousemove", (e) => {{
  tooltip.style("left", (e.pageX+14)+"px").style("top", (e.pageY-14)+"px");
}}).on("mouseout", () => tooltip.classed("visible", false));

node.on("click", (e, d) => {{
  e.stopPropagation();
  showNodeDetail(d);
}});

link.on("click", (e, d) => {{
  e.stopPropagation();
  showEdgeDetail(d);
}});

svg.on("click", () => {{ closeDetail(); closeEdge(); }});

function showNodeDetail(d) {{
  const panel = d3.select("#detail-panel");
  panel.classed("visible", true);
  const body = d3.select("#dp-body");
  const statusClass = d.color === "#2ecc71" ? "dp-status-success" : d.color === "#e74c3c" ? "dp-status-fail" : "";
  let html = `<div class="dp-section"><div class="dp-label">State</div><div class="dp-value">${{escapeHtml(d.id)}}</div></div>`;
  html += `<div class="dp-section"><div class="dp-label">Status</div><div class="dp-value ${{statusClass}}">${{d.color === "#2ecc71" ? "Success" : d.color === "#e74c3c" ? "Failed" : d.color === "#e67e22" ? "Composite" : "Pending"}}</div></div>`;
  html += `<div class="dp-section"><div class="dp-label">Description</div><div class="dp-value">${{escapeHtml(d.description || "N/A")}}</div></div>`;
  html += `<div class="dp-section"><div class="dp-label">Composite</div><div class="dp-value">${{d.is_composite ? "Yes" : "No"}}</div></div>`;
  html += `<div class="dp-section"><div class="dp-label">Conditions</div><div class="dp-value">${{escapeHtml(d.conditions.length ? d.conditions.join(', ') : 'None (no conditions required)')}}</div></div>`;
  if (d.api_note_path) {{
    html += `<div class="dp-section"><div class="dp-label">API Note</div><div class="dp-value"><span class="dp-link">${{escapeHtml(d.api_note_path)}}</span></div></div>`;
    html += `<button class="dp-obsidian-btn" type="button">Open in Obsidian</button>`;
  }}
  if (d.vault_path) {{
    html += `<div class="dp-section"><div class="dp-label">Vault</div><div class="dp-value">${{escapeHtml(d.vault_path)}}</div></div>`;
  }}
  body.html(html);
  body.selectAll(".dp-link, .dp-obsidian-btn")
    .on("click", () => openObsidian(d.api_note_path, d.vault_path));
}}

function showEdgeDetail(d) {{
  const panel = d3.select("#detail-edge-panel");
  panel.classed("visible", true);
  const body = d3.select("#de-body");
  let html = `<div class="dp-section"><div class="dp-label">Source</div><div class="dp-value">${{escapeHtml(d.source.id || d.source)}}</div></div>`;
  html += `<div class="dp-section"><div class="dp-label">Target</div><div class="dp-value">${{escapeHtml(d.target.id || d.target)}}</div></div>`;
  html += `<div class="dp-section"><div class="dp-label">API</div><div class="dp-value">${{escapeHtml(d.api)}}</div></div>`;
  html += `<div class="dp-section"><div class="dp-label">Trigger</div><div class="dp-value">${{escapeHtml(d.trigger || 'N/A')}}</div></div>`;
  body.html(html);
}}

function openObsidian(path, vaultPath) {{
  const vaultName = (vaultPath || "").split(/[\\\\/]/).pop() || 'Capture';
  const safePath = (path || "").replace(/\\\\/g, '/');
  const uri = `obsidian://advanced-uri?vault=${{encodeURIComponent(vaultName)}}&path=${{encodeURIComponent(safePath)}}`;
  window.open(uri, '_blank');
}}

function closeDetail() {{ d3.select("#detail-panel").classed("visible", false); }}
function closeEdge() {{ d3.select("#detail-edge-panel").classed("visible", false); }}

function onSearch(query) {{
  query = (query || "").trim().toLowerCase();
  if (!query) {{ clearSearch(); return; }}
  const matched = new Set();
  nodes.forEach(n => {{
    if (n.id.toLowerCase().includes(query) || n.label.toLowerCase().includes(query)) matched.add(n.id);
  }});
  node.classed("node-dimmed", d => !matched.has(d.id));
  link.classed("link-dimmed", d => {{
    const s = d.source.id || d.source, t = d.target.id || d.target;
    return !(matched.has(s) || matched.has(t));
  }});
  linkLabel.classed("link-dimmed", d => {{
    const s = d.source.id || d.source, t = d.target.id || d.target;
    return !(matched.has(s) || matched.has(t));
  }});
  if (matched.size > 0) {{
    const first = nodes.find(n => matched.has(n.id));
    if (first) centerOn(first);
  }}
}}

function clearSearch() {{
  const input = document.getElementById('search-input');
  if (input) input.value = '';
  node.classed("node-dimmed", false);
  link.classed("link-dimmed", false);
  linkLabel.classed("link-dimmed", false);
}}

function centerOn(d) {{
  const scale = Math.max(currentZoomK, 1.5);
  const transform = d3.zoomIdentity.translate(width / 2 - d.x * scale, height / 2 - d.y * scale).scale(scale);
  svg.transition().duration(500).call(zoom.transform, transform);
}}

d3.select("#stats").text(`Nodes: ${{nodes.length}} | Edges: ${{links.length}}`);

const sim = d3.forceSimulation(nodes)
  .force("link", d3.forceLink(links).id(d=>d.id).distance(140))
  .force("charge", d3.forceManyBody().strength(-500))
  .force("center", d3.forceCenter(width/2, height/2))
  .force("collision", d3.forceCollide().radius(35));

function linkPath(d) {{
  const sx = d.source.x, sy = d.source.y, tx = d.target.x, ty = d.target.y;
  const off = d._curveOffset || 0;
  if (!off) return `M${{sx}},${{sy}} L${{tx}},${{ty}}`;
  const mx = (sx + tx) / 2, my = (sy + ty) / 2;
  const dx = tx - sx, dy = ty - sy;
  const len = Math.sqrt(dx*dx + dy*dy) || 1;
  const nx = -dy / len, ny = dx / len;
  const cx = mx + nx * off, cy = my + ny * off;
  return `M${{sx}},${{sy}} Q${{cx}},${{cy}} ${{tx}},${{ty}}`;
}}

function linkLabelPos(d) {{
  const sx = d.source.x, sy = d.source.y, tx = d.target.x, ty = d.target.y;
  const mx = (sx + tx) / 2, my = (sy + ty) / 2;
  const off = d._curveOffset || 0;
  if (!off) return {{ x: mx, y: my - 6 }};
  const dx = tx - sx, dy = ty - sy;
  const len = Math.sqrt(dx*dx + dy*dy) || 1;
  const nx = -dy / len, ny = dx / len;
  return {{ x: mx + nx * off * 0.5, y: my + ny * off * 0.5 - 6 }};
}}

sim.on("tick", () => {{
  link.attr("d", linkPath);
  linkLabel.attr("x", d => linkLabelPos(d).x).attr("y", d => linkLabelPos(d).y);
  node.attr("transform",d=>`translate(${{d.x}},${{d.y}})`);
}});

function dragstart(e,d) {{ if(!e.active) sim.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; }}
function drag(e,d) {{ d.fx=e.x; d.fy=e.y; }}
function dragend(e,d) {{ if(!e.active) sim.alphaTarget(0); d.fx=null; d.fy=null; }}
}} catch(err) {{
  document.getElementById('svg').style.display = 'none';
  const error = document.createElement('div');
  error.style.cssText = 'color:#e74c3c;padding:40px;font-size:16px;text-align:center;';
  error.textContent = 'Visualization error: ' + err.message + '. Check browser console for details.';
  document.getElementById('graph').appendChild(error);
}}
</script>
</body>
</html>"""