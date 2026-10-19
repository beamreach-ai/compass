"""The map as a file: JSON, Graphviz DOT, or one HTML page that draws it.

The product shows the map in its dashboard; the open mapper writes it out so
it can be looked at, diffed, or fed to other tools (open_source_split_spec.md D9).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from infra_map.graph_store import open_store


def load_graph(config: Dict[str, Any], *, org_id: str, workspace_id: str) -> Dict[str, List[Dict[str, Any]]]:
    graph = open_store(config, workspace_id=workspace_id).fetch_graph(org_id)
    nodes = [
        {
            "id": n.resource_id,
            "type": n.type,
            "name": n.name,
            "account_id": n.account_id,
            "region": n.region,
            "terraform": bool(n.terraform),
            "tf_address": getattr(n.terraform, "resource_address", None) if n.terraform else None,
        }
        for n in graph.get("nodes", [])
    ]
    edges = [
        {"from": e.from_resource_id, "to": e.to_resource_id, "type": e.relationship_type, "confidence": e.confidence}
        for e in graph.get("edges", [])
    ]
    return {"nodes": nodes, "edges": edges}


def to_dot(graph: Dict[str, List[Dict[str, Any]]]) -> str:
    def q(value: Any) -> str:
        return '"' + str(value).replace('"', '\\"') + '"'

    lines = ["digraph infra {", "  rankdir=LR;", "  node [shape=box, fontsize=10];"]
    for n in graph["nodes"]:
        label = f"{n['type']}\\n{n['name'] or n['id']}"
        style = ', style=filled, fillcolor="#dbeafe"' if n["terraform"] else ""
        lines.append(f"  {q(n['id'])} [label={q(label)}{style}];")
    for e in graph["edges"]:
        lines.append(f"  {q(e['from'])} -> {q(e['to'])} [label={q(e['type'])}, fontsize=8];")
    lines.append("}")
    return "\n".join(lines) + "\n"


_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Infra map</title>
<style>
  :root { --bg:#0f172a; --fg:#e2e8f0; --muted:#94a3b8; --panel:#1e293b; --accent:#60a5fa; }
  html,body { margin:0; height:100%; background:var(--bg); color:var(--fg); font:14px/1.4 system-ui,sans-serif; }
  #top { position:fixed; inset:0 0 auto 0; padding:10px 16px; background:var(--panel); display:flex; gap:12px; align-items:center; flex-wrap:wrap; z-index:2; }
  #top input, #top select { background:var(--bg); color:var(--fg); border:1px solid #334155; border-radius:6px; padding:6px 8px; }
  #stats { color:var(--muted); }
  #side { position:fixed; right:0; top:52px; bottom:0; width:340px; background:var(--panel); padding:12px 16px; overflow:auto; font-size:13px; display:none; }
  #side.open { display:block; }
  #side h3 { margin:0 0 8px; font-size:14px; } #side dt { color:var(--muted); margin-top:6px; } #side dd { margin:0; word-break:break-all; }
  svg { width:100vw; height:100vh; } .link { stroke:#475569; stroke-opacity:.6; } .node circle { stroke:#0f172a; stroke-width:1px; cursor:pointer; }
  .node text { font-size:9px; fill:var(--muted); pointer-events:none; } .dim { opacity:.15; }
</style></head><body>
<div id="top"><strong>Infra map</strong><span id="stats"></span>
  <input id="q" placeholder="filter by name, id, type…" size="32"><select id="t"><option value="">all types</option></select>
  <label><input type="checkbox" id="tfonly"> in Terraform only</label></div>
<div id="side"></div><svg></svg>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"></script>
<script>
const DATA = __DATA__;
const types = [...new Set(DATA.nodes.map(n => n.type))].sort();
const color = d3.scaleOrdinal(types, d3.schemeTableau10.concat(d3.schemeSet3));
const sel = document.getElementById('t'); types.forEach(t => { const o = document.createElement('option'); o.value = t; o.textContent = t; sel.appendChild(o); });
document.getElementById('stats').textContent = `${DATA.nodes.length} nodes · ${DATA.edges.length} edges · ${types.length} types`;
const ids = new Set(DATA.nodes.map(n => n.id));
const links = DATA.edges.filter(e => ids.has(e.from) && ids.has(e.to)).map(e => ({source: e.from, target: e.to, type: e.type}));
const degree = {}; links.forEach(l => { degree[l.source] = (degree[l.source]||0)+1; degree[l.target] = (degree[l.target]||0)+1; });
const svg = d3.select('svg'), W = innerWidth, H = innerHeight;
const g = svg.append('g');
svg.call(d3.zoom().scaleExtent([0.1, 6]).on('zoom', ev => g.attr('transform', ev.transform)));
const link = g.append('g').selectAll('line').data(links).join('line').attr('class', 'link');
const node = g.append('g').selectAll('g').data(DATA.nodes).join('g').attr('class', 'node');
node.append('circle').attr('r', d => 4 + Math.min(10, Math.sqrt(degree[d.id]||0)*2)).attr('fill', d => color(d.type)).attr('stroke', d => d.terraform ? '#93c5fd' : '#0f172a').attr('stroke-width', d => d.terraform ? 2 : 1);
node.append('text').attr('dx', 8).attr('dy', 3).text(d => (d.name || d.id).slice(0, 40));
const sim = d3.forceSimulation(DATA.nodes).force('link', d3.forceLink(links).id(d => d.id).distance(60)).force('charge', d3.forceManyBody().strength(-120)).force('center', d3.forceCenter(W/2, H/2)).force('collide', d3.forceCollide(14));
sim.on('tick', () => { link.attr('x1', d => d.source.x).attr('y1', d => d.source.y).attr('x2', d => d.target.x).attr('y2', d => d.target.y); node.attr('transform', d => `translate(${d.x},${d.y})`); });
node.call(d3.drag().on('start', (ev, d) => { if (!ev.active) sim.alphaTarget(.3).restart(); d.fx = d.x; d.fy = d.y; }).on('drag', (ev, d) => { d.fx = ev.x; d.fy = ev.y; }).on('end', (ev, d) => { if (!ev.active) sim.alphaTarget(0); d.fx = d.fy = null; }));
const side = document.getElementById('side');
node.on('click', (ev, d) => {
  const out = links.filter(l => l.source.id === d.id).map(l => `${l.type} → ${l.target.name || l.target.id}`);
  const inn = links.filter(l => l.target.id === d.id).map(l => `${l.source.name || l.source.id} → ${l.type}`);
  side.className = 'open';
  side.innerHTML = `<h3>${d.name || d.id}</h3><dl><dt>type</dt><dd>${d.type}</dd><dt>id</dt><dd>${d.id}</dd><dt>account / region</dt><dd>${d.account_id||''} ${d.region||''}</dd><dt>terraform</dt><dd>${d.tf_address || (d.terraform ? 'yes' : 'not in Terraform')}</dd><dt>outgoing (${out.length})</dt><dd>${out.join('<br>')||'—'}</dd><dt>incoming (${inn.length})</dt><dd>${inn.join('<br>')||'—'}</dd></dl>`;
});
function applyFilter() {
  const q = document.getElementById('q').value.toLowerCase(), t = sel.value, tf = document.getElementById('tfonly').checked;
  const keep = d => (!t || d.type === t) && (!tf || d.terraform) && (!q || (d.name||'').toLowerCase().includes(q) || d.id.toLowerCase().includes(q) || d.type.includes(q));
  node.classed('dim', d => !keep(d)); link.classed('dim', l => !keep(l.source) || !keep(l.target));
}
['q','t','tfonly'].forEach(id => document.getElementById(id).addEventListener('input', applyFilter));
</script></body></html>
"""


def to_html(graph: Dict[str, List[Dict[str, Any]]]) -> str:
    return _HTML.replace("__DATA__", json.dumps(graph).replace("</", "<\\/"))


def write_map(config: Dict[str, Any], *, org_id: str, workspace_id: str, fmt: str, out: str) -> Dict[str, Any]:
    graph = load_graph(config, org_id=org_id, workspace_id=workspace_id)
    if fmt == "json":
        text = json.dumps(graph, indent=2)
    elif fmt == "dot":
        text = to_dot(graph)
    elif fmt == "html":
        text = to_html(graph)
    else:
        raise ValueError("format must be json, dot or html")
    path = Path(out)
    path.write_text(text, encoding="utf-8")
    return {"out": str(path.resolve()), "format": fmt, "nodes": len(graph["nodes"]), "edges": len(graph["edges"])}
