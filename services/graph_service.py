import logging
import os
from collections import defaultdict
from typing import Dict, List, Set, Any, Optional, Tuple

import networkx as nx

logger = logging.getLogger(__name__)

NODE_COLORS = {
    "name": "#4CAF50",
    "ip": "#42A5F5",
    "hwid": "#FFA726",
    "uid": "#BDBDBD",
}

NODE_SHAPES = {
    "name": "dot",
    "ip": "square",
    "hwid": "diamond",
    "uid": "triangle",
}

EDGE_COLORS = {
    "name-ip": "#64B5F6",
    "name-ip-vpn": "#78909C",
    "name-hwid": "#FFB74D",
    "name-uid": "#BDBDBD",
    "ip-hwid": "#EF5350",
    "ip-hwid-vpn": "#78909C",
    "uid-ip": "#90A4AE",
    "uid-hwid": "#A1887F",
}


def _get_node_type(node: str) -> str:
    if node.startswith("name:"):
        return "name"
    if node.startswith("ip:"):
        return "ip"
    if node.startswith("hwid:"):
        return "hwid"
    if node.startswith("uid:"):
        return "uid"
    return "name"


def _get_node_label(node: str) -> str:
    for prefix in ("name:", "ip:", "hwid:", "uid:"):
        if node.startswith(prefix):
            return node[len(prefix):]
    return node


def _get_edge_type(u: str, v: str) -> str:
    ut = _get_node_type(u)
    vt = _get_node_type(v)
    return f"{min(ut, vt)}-{max(ut, vt)}"


def build_graph_from_identity_graph(
    identity_graph: Dict[str, Set[str]],
) -> nx.Graph:
    G = nx.Graph()
    for node, neighbors in identity_graph.items():
        ntype = _get_node_type(node)
        label = _get_node_label(node)
        G.add_node(node, label=label, type=ntype)
        for neighbor in neighbors:
            ntype2 = _get_node_type(neighbor)
            label2 = _get_node_label(neighbor)
            G.add_node(neighbor, label=label2, type=ntype2)
            G.add_edge(node, neighbor, type=_get_edge_type(node, neighbor))
    return G


def build_graph_from_player_data(
    players: List[Dict[str, Any]],
    primary_nickname: Optional[str] = None,
) -> nx.Graph:
    G = nx.Graph()

    for player in players:
        nicknames = player.get("nicknames", player.get("initial_account", {}).get("nicknames", []))
        associated_ips = player.get("associated_ips", player.get("initial_account", {}).get("associated_ips", {}))
        associated_hwids = player.get("associated_hwids", player.get("initial_account", {}).get("associated_hwids", {}))

        for nick in nicknames:
            name_node = f"name:{nick}"
            G.add_node(name_node, label=nick, type="name")

            for ip in associated_ips:
                ip_node = f"ip:{ip}"
                G.add_node(ip_node, label=ip, type="ip")
                G.add_edge(name_node, ip_node, type="name-ip")

            for hwid in associated_hwids:
                hwid_node = f"hwid:{hwid}"
                G.add_node(hwid_node, label=hwid, type="hwid")
                G.add_edge(name_node, hwid_node, type="name-hwid")

        for ip, nicks_on_ip in associated_ips.items():
            ip_node = f"ip:{ip}"
            G.add_node(ip_node, label=ip, type="ip")
            for nick in nicks_on_ip:
                name_node = f"name:{nick}"
                G.add_node(name_node, label=nick, type="name")
                G.add_edge(name_node, ip_node, type="name-ip")

        for hwid, nicks_on_hwid in associated_hwids.items():
            hwid_node = f"hwid:{hwid}"
            G.add_node(hwid_node, label=hwid, type="hwid")
            for nick in nicks_on_hwid:
                name_node = f"name:{nick}"
                G.add_node(name_node, label=nick, type="name")
                G.add_edge(name_node, hwid_node, type="name-hwid")

    if primary_nickname and G.has_node(f"name:{primary_nickname}"):
        center = f"name:{primary_nickname}"
        nodes_to_keep = {center}
        for neighbor in G.neighbors(center):
            nodes_to_keep.add(neighbor)
            for n2 in G.neighbors(neighbor):
                nodes_to_keep.add(n2)
        G = G.subgraph(nodes_to_keep).copy()

    return G


def _short_hwid(hwid: str, max_len: int = 16) -> str:
    if len(hwid) > max_len:
        return hwid[:max_len] + "..."
    return hwid


def _build_pyvis_network(G: nx.Graph) -> Any:
    from pyvis.network import Network

    net = Network(height="700px", width="100%", directed=False, bgcolor="#1a1a2e", font_color="#ffffff")

    net.set_options("""
    {
      "physics": {
        "stabilization": {"iterations": 150},
        "barnesHut": {
          "gravitationalConstant": -3000,
          "centralGravity": 0.3,
          "springLength": 150,
          "springConstant": 0.04,
          "damping": 0.5
        }
      },
      "edges": {
        "smooth": {"type": "continuous"},
        "color": {"inherit": false}
      }
    }
    """)

    for node, data in G.nodes(data=True):
        ntype = data.get("type", "name")
        label = data.get("label", node)
        color = NODE_COLORS.get(ntype, "#4CAF50")
        shape = NODE_SHAPES.get(ntype, "dot")
        title = f"{ntype.upper()}: {label}"
        display = _short_hwid(label) if ntype == "hwid" else label
        net.add_node(node, label=display, color=color, shape=shape, title=title, size=20)

    for u, v, data in G.edges(data=True):
        etype = data.get("type", "name-ip")
        color = EDGE_COLORS.get(etype, "#666666")
        net.add_edge(u, v, color=color, title=etype, width=1.5)

    return net


def graph_to_vis_json(G: nx.Graph) -> Dict[str, Any]:
    nodes = []
    for node, data in G.nodes(data=True):
        ntype = data.get("type", "name")
        label = data.get("label", node)
        color = NODE_COLORS.get(ntype, "#4CAF50")
        shape = NODE_SHAPES.get(ntype, "dot")
        title = f"{ntype.upper()}: {label}"
        display = _short_hwid(label) if ntype == "hwid" else label
        nodes.append({
            "id": node, "label": display, "color": color,
            "shape": shape, "title": title, "size": 20,
        })

    edges = []
    for u, v, data in G.edges(data=True):
        etype = data.get("type", "name-ip")
        color = EDGE_COLORS.get(etype, "#666666")
        edges.append({
            "from": u, "to": v, "color": color,
            "title": etype, "width": 1.5,
        })

    return {"nodes": nodes, "edges": edges}


def generate_vis_html_snippet(
    graph_data: Dict[str, Any],
    container_id: str = "graph-container",
    height: str = "700px",
) -> str:
    import json as _json
    data_json = _json.dumps(graph_data, ensure_ascii=False)
    return f"""
<div class="section-title">🔗 Граф связей</div>
<div style="display:flex;flex-wrap:wrap;gap:12px;margin-bottom:10px;padding:10px 14px;background:#252526;border-radius:6px;font-size:12px">
  <span style="display:flex;align-items:center;gap:6px"><span style="display:inline-block;width:14px;height:14px;border-radius:50%;background:#4CAF50"></span>Игрок</span>
  <span style="display:flex;align-items:center;gap:6px"><span style="display:inline-block;width:14px;height:14px;border-radius:2px;background:#42A5F5"></span>IP-адрес</span>
  <span style="display:flex;align-items:center;gap:6px"><span style="display:inline-block;width:14px;height:14px;border-radius:2px;background:#FFA726;transform:rotate(45deg)"></span>HWID</span>
  <span style="display:flex;align-items:center;gap:6px;margin-left:12px"><span style="display:inline-block;width:24px;height:3px;border-radius:2px;background:#64B5F6"></span>Связь имя–IP</span>
  <span style="display:flex;align-items:center;gap:6px"><span style="display:inline-block;width:24px;height:3px;border-radius:2px;background:#78909C"></span>Связь имя–VPN/Хостинг</span>
  <span style="display:flex;align-items:center;gap:6px"><span style="display:inline-block;width:24px;height:3px;border-radius:2px;background:#FFB74D"></span>Связь имя–HWID</span>
  <span style="display:flex;align-items:center;gap:6px"><span style="display:inline-block;width:24px;height:3px;border-radius:2px;background:#EF5350"></span>Связь IP–HWID</span>
</div>
<div id="{container_id}" style="height:{height};background:#16213e;border-radius:8px;margin-bottom:16px"></div>
<script type="text/javascript">
  (function(){{
    var data = {data_json};
    var container = document.getElementById('{container_id}');
    if (!container) return;
    var nodes = new vis.DataSet(data.nodes);
    var edges = new vis.DataSet(data.edges);
    var options = {{
      nodes: {{ font: {{ color: '#fff', size: 16 }}, borderWidth: 2 }},
      edges: {{ smooth: {{ type: 'continuous' }} }},
      physics: {{
        stabilization: {{ iterations: 200 }},
        barnesHut: {{ gravitationalConstant: -5000, centralGravity: 0.2, springLength: 280, springConstant: 0.03, damping: 0.4 }}
      }},
      interaction: {{ hover: true, tooltipDelay: 200 }}
    }};
    var network = new vis.Network(container, {{ nodes: nodes, edges: edges }}, options);
  }})();
</script>"""


def generate_vis_graph_from_report_data(
    report_data: list,
    container_id: str = "graph-container",
    height: str = "700px",
) -> str:
    G = build_graph_from_report_data(report_data)
    if G.number_of_nodes() == 0:
        return ""
    graph_data = graph_to_vis_json(G)
    return generate_vis_html_snippet(graph_data, container_id, height)


def build_graph_from_report_data(report_data: list) -> nx.Graph:
    G = nx.Graph()
    nicknames_set = set()

    for item in report_data:
        typ = item.get("type", "")

        if typ == "player_info":
            nick = item.get("nickname", "")
            if nick:
                nicknames_set.add(nick)
                G.add_node(f"name:{nick}", label=nick, type="name")

        elif typ == "associated_accounts":
            for nick in item.get("nicknames", []):
                nicknames_set.add(nick)
                G.add_node(f"name:{nick}", label=nick, type="name")

        elif typ == "associated_ips":
            for ip_entry in item.get("ips", []):
                ip = ip_entry.get("direct_ip_connections", "")
                raw_users = ip_entry.get("raw_users", [])
                vpn_info = ip_entry.get("vpn_info", {})
                is_vpn = bool(vpn_info.get("proxy") or vpn_info.get("hosting"))
                edge_type = "name-ip-vpn" if is_vpn else "name-ip"
                if ip and raw_users:
                    ip_node = f"ip:{ip}"
                    G.add_node(ip_node, label=ip, type="ip")
                    for user in raw_users:
                        user_node = f"name:{user}"
                        nicknames_set.add(user)
                        G.add_node(user_node, label=user, type="name")
                        G.add_edge(user_node, ip_node, type=edge_type)

        elif typ == "associated_hwids":
            for hw_entry in item.get("hwids", []):
                hwid = hw_entry.get("hwid", "")
                raw_users = hw_entry.get("raw_users", [])
                if hwid and raw_users:
                    hwid_node = f"hwid:{hwid}"
                    G.add_node(hwid_node, label=hwid, type="hwid")
                    for user in raw_users:
                        user_node = f"name:{user}"
                        nicknames_set.add(user)
                        G.add_node(user_node, label=user, type="name")
                        G.add_edge(user_node, hwid_node, type="name-hwid")

        elif typ == "denied_login_attempts":
            for attempt in item.get("attempts", []):
                user = attempt.get("user_name", "")
                ip = attempt.get("ip_address", "")
                hwid = attempt.get("hwid", "")
                vpn_info = attempt.get("vpn_info", {})
                is_vpn = bool(vpn_info.get("proxy") or vpn_info.get("hosting"))
                if user:
                    user_node = f"name:{user}"
                    nicknames_set.add(user)
                    G.add_node(user_node, label=user, type="name")
                if ip:
                    ip_node = f"ip:{ip}"
                    G.add_node(ip_node, label=ip, type="ip")
                    if user:
                        G.add_edge(user_node, ip_node, type="name-ip-vpn" if is_vpn else "name-ip")
                if hwid:
                    hwid_node = f"hwid:{hwid}"
                    G.add_node(hwid_node, label=hwid, type="hwid")
                    if user:
                        G.add_edge(user_node, hwid_node, type="name-hwid")
                    if ip:
                        G.add_edge(ip_node, hwid_node, type="ip-hwid-vpn" if is_vpn else "ip-hwid")

    return G


def render_html(
    G: nx.Graph,
    output_path: str,
    notebook: bool = False,
) -> str:
    net = _build_pyvis_network(G)
    html = net.generate_html(notebook=notebook)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"Graph HTML saved to '{output_path}'")
    return output_path


def _render_png_matplotlib(
    G: nx.Graph,
    output_path: str,
    figsize: Tuple[int, int] = (20, 16),
    dpi: int = 150,
) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    pos = nx.spring_layout(G, k=2.5, iterations=100, seed=42)

    fig, ax = plt.subplots(figsize=figsize, facecolor="#1a1a2e")
    ax.set_facecolor("#1a1a2e")

    node_groups: Dict[str, List[str]] = defaultdict(list)
    for node, data in G.nodes(data=True):
        ntype = data.get("type", "name")
        node_groups[ntype].append(node)

    for ntype, nodes in node_groups.items():
        color = NODE_COLORS.get(ntype, "#4CAF50")
        nx.draw_networkx_nodes(
            G, pos, nodelist=nodes, node_color=color,
            node_size=180, ax=ax, alpha=0.9,
        )

    edge_groups: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for u, v, data in G.edges(data=True):
        etype = data.get("type", "name-ip")
        edge_groups[etype].append((u, v))

    for etype, edges in edge_groups.items():
        color = EDGE_COLORS.get(etype, "#666666")
        nx.draw_networkx_edges(
            G, pos, edgelist=edges, edge_color=color,
            alpha=0.4, width=0.8, ax=ax,
        )

    labels = {n: G.nodes[n].get("label", n) for n in G.nodes()}
    nx.draw_networkx_labels(
        G, pos, labels=labels, font_size=6, font_color="#ffffff",
        font_weight="bold", ax=ax,
    )

    ax.axis("off")
    plt.tight_layout(pad=0)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    fig.savefig(output_path, dpi=dpi, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    logger.info(f"Graph PNG saved to '{output_path}'")
    return output_path


def render_png(
    G: nx.Graph,
    output_path: str,
    figsize: Tuple[int, int] = (20, 16),
    dpi: int = 150,
) -> str:
    return _render_png_matplotlib(G, output_path, figsize, dpi)


def render_identity_graph(
    identity_graph: Dict[str, Set[str]],
    output_path: str,
    fmt: str = "html",
    primary_nickname: Optional[str] = None,
) -> str:
    G = build_graph_from_identity_graph(identity_graph)
    return _render_graph(G, output_path, fmt, primary_nickname)


def render_player_graph(
    players: List[Dict[str, Any]],
    output_path: str,
    fmt: str = "html",
    primary_nickname: Optional[str] = None,
) -> str:
    G = build_graph_from_player_data(players, primary_nickname)
    return _render_graph(G, output_path, fmt, primary_nickname)


def _render_graph(
    G: nx.Graph,
    output_path: str,
    fmt: str,
    primary_nickname: Optional[str] = None,
) -> str:
    if primary_nickname and G.has_node(f"name:{primary_nickname}"):
        center = f"name:{primary_nickname}"
        nodes_to_keep = {center}
        for neighbor in G.neighbors(center):
            nodes_to_keep.add(neighbor)
            for n2 in G.neighbors(neighbor):
                nodes_to_keep.add(n2)
        G = G.subgraph(nodes_to_keep).copy()

    if G.number_of_nodes() == 0:
        logger.warning("Graph is empty, nothing to render")
        return output_path

    logger.info(f"Rendering graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

    base, ext = os.path.splitext(output_path)
    ext = ext.lower()

    if fmt == "html" or ext == ".html":
        return render_html(G, output_path)
    elif fmt == "png" or ext == ".png":
        return render_png(G, output_path)
    else:
        if fmt == "html":
            return render_html(G, f"{base}.html")
        else:
            return render_png(G, f"{base}.png")
