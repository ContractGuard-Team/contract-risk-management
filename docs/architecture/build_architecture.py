"""ContractGuard architecture on one page, AWS reference-architecture style (VPC, icons + names, numbered flows).

Run: python docs/architecture/build_architecture.py
Outputs next to this file: ContractGuard-Architecture.drawio (editable), .svg and .png (rendered with Chrome).
Topology from the source: docker-compose.yml (9 services, network `backend`, monitoring bound to 127.0.0.1),
workers/, orchestration/, agents/, mcp_server/, rag/, data_pipeline/, monitoring/ (Langfuse OTLP), config/.
Only components the source code uses are drawn (no web side, NAT/IGW/endpoint or schedulers); the VPC/subnet
frame is where the EC2 host runs — the repo does not provision it. Icons: icons/ (see icons/sources.json); functional glyphs drawn here.
"""
from pathlib import Path
from xml.etree import ElementTree as E
import base64
import html
import subprocess

OUT = Path(__file__).parent
ICONS = OUT / "icons"
W, H = 2330, 1680
INK, SUB = "#232F3E", "#545B64"
S = 52  # icon size
LEGEND_TOP = 1340


def esc(s):
    return html.escape(str(s), quote=True)


GLYPHS = {
    "users": '<circle cx="17" cy="15" r="7"/><circle cx="33" cy="15" r="7"/><path d="M3 43v-5c0-10 28-10 28 0v5M24 30c5-5 21-4 21 8v5"/>',
    "web": '<circle cx="24" cy="24" r="17"/><ellipse cx="24" cy="24" rx="7" ry="17"/><path d="M7 24h34M10 15h28M10 33h28"/>',
    "search": '<circle cx="21" cy="21" r="11"/><path d="m29 29 11 11"/>',
    "monitor": '<rect x="7" y="9" width="34" height="25" rx="2"/><path d="M18 42h12M24 34v8M12 27l8-7 7 6 9-11"/>',
    "trace": '<path d="M8 12h20M8 24h28M8 36h14"/><circle cx="34" cy="12" r="4"/><circle cx="40" cy="24" r="4"/><circle cx="26" cy="36" r="4"/>',
    "bell": '<path d="M14 33V22a10 10 0 0 1 20 0v11l4 4H10Z"/><path d="M20 41a4 4 0 0 0 8 0"/>',
    "shield": '<path d="M24 6 39 12v11c0 10-15 18-15 18S9 33 9 23V12Z"/><path d="m17 23 5 5 10-11"/>',
    "flag": '<path d="M12 42V6M12 8h22l-5 7 5 7H12"/>',
    "lock": '<rect x="11" y="22" width="26" height="19" rx="2"/><path d="M17 22v-6a7 7 0 0 1 14 0v6"/>',
    "cloudlock": '<path d="M14 34a8 8 0 0 1 1-16 11 11 0 0 1 21 3 7 7 0 0 1-1 13Z"/><rect x="19" y="26" width="10" height="8" rx="1"/><path d="M21 26v-3a3 3 0 0 1 6 0v3"/>',
    "igw": '<rect x="9" y="9" width="30" height="30" rx="3"/><path d="M17 24h14M26 18l6 6-6 6"/>',
    "nat": '<circle cx="24" cy="24" r="15"/><path d="M15 20h16l-4-4M33 28H17l4 4"/>',
    "endpoint": '<path d="M18 8v10M30 8v10M13 18h22v6a11 11 0 0 1-22 0ZM24 35v6"/>',
    "volume": '<ellipse cx="24" cy="12" rx="14" ry="5"/><path d="M10 12v24c0 7 28 7 28 0V12M10 24c0 7 28 7 28 0"/>',
}


def glyph(name, bg=None):
    tile = f'<rect width="48" height="48" rx="5" fill="{bg}"/>' if bg else ""
    stroke = "#FFFFFF" if bg else INK
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">{tile}<g fill="none" stroke="{stroke}" '
           f'stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">{GLYPHS[name]}</g></svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def aws_logo():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><rect width="48" height="48" fill="#232F3E"/>'
           '<text x="24" y="26" font-family="Arial" font-weight="bold" font-size="17" fill="#FFFFFF" text-anchor="middle">aws</text>'
           '<path d="M11 32c8 5 18 5 26 0" stroke="#FF9900" stroke-width="2.5" fill="none" stroke-linecap="round"/></svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def icon(spec):
    if isinstance(spec, tuple):
        return glyph(*spec)
    if spec == "aws":
        return aws_logo()
    for ext, mime in ((".svg", "image/svg+xml"), (".png", "image/png")):
        p = ICONS / (spec + ext)
        if p.exists():
            return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()
    raise FileNotFoundError(spec)


VPC_C, PUB, PRIV = "#8C4FFF", "#7AA116", "#00A4A6"
# ---------------------------------------------------------------- frames: x, y, w, h, title, colour, dashed, icon, fill
GROUPS = [
    (520, 380, 1790, 1150, "AWS Cloud", INK, False, "aws", None),
    (540, 410, 1750, 1100, "Region ap-southeast-1 (Singapore)", PRIV, True, ("flag",), None),
    (770, 440, 1190, 1030, "VPC", VPC_C, False, ("cloudlock", VPC_C), None),
    (790, 468, 1150, 982, "Subnet", PRIV, False, ("lock", PRIV), "#EEF9F9"),
    (830, 500, 1070, 910, "Amazon EC2 • ContractRisk-EC2-Role • Security group: không mở cổng inbound", "#ED7100", False, "ec2", "#FFFFFF"),
    (850, 560, 1030, 560, "Docker Compose • network backend (bridge) • 9 container • 7 volume SQLite", "#2496ED", False, "docker", "#F5FAFE"),
    (870, 605, 170, 495, "Tiếp nhận", "#64748B", True, None, None),
    (1060, 605, 540, 315, "Điều phối & phân tích (Hermes)", "#64748B", True, None, None),
    (1060, 940, 540, 160, "Công cụ & RAG", "#64748B", True, None, None),
    (1620, 605, 240, 495, "Nền tảng", "#64748B", True, None, None),
    (850, 1150, 1030, 240, "systemd • data pipeline pháp luật (venv riêng)", "#3776AB", False, "python", "#F5F8FC"),
    (1985, 590, 270, 200, "Bảo mật", "#DD344C", True, None, None),
    (1985, 1030, 270, 190, "Lưu trữ", PUB, True, None, None),
    (1985, 1250, 270, 220, "Thu thập", "#E7157B", True, None, None),
    (2340, 90, 370, 260, "Giám sát • Langfuse", "#E7157B", False, ("trace", "#E7157B"), "#FFF5FA"),
    (2340, 380, 370, 1150, "Model & dữ liệu ngoài (Internet)", PRIV, True, ("web",), None),
]

# ---------------------------------------------------------------- nodes key: (centre x, icon top, icon, name, sub)
NODES = {
    "sqs_req": (650, 504, "sqs", "Amazon SQS", "analysis-requests"),
    "s3_docs": (650, 640, "s3", "Amazon S3", "quarantine/ clean/ results/"),
    "sqs_res": (650, 770, "sqs", "Amazon SQS", "analysis-results"),
    "sqs_chat": (650, 900, "sqs", "Amazon SQS", "chat-requests"),
    "worker": (955, 640, "python", "worker", "intake • progress • chat"),
    "input": (955, 860, "fastapi", "input-processing", "PDF/DOCX/OCR • chunk"),
    "langgraph": (1330, 640, "langgraph", "langgraph", "workflow • cache • scoring"),
    "a1": (1150, 790, "python", "agent-1", "phân loại • trích xuất"),
    "a2": (1330, 790, "python", "agent-2", "chấm điểm rủi ro"),
    "a3": (1510, 790, "python", "agent-3", "khuyến nghị • /v1/chat"),
    "mcp": (1330, 975, "mcp", "mcp • tool gateway", "RAG hybrid • rerank"),
    "config": (1740, 640, ("shield", "#DD344C"), "config", "token • allowlist"),
    "monitoring": (1740, 860, ("monitor", "#E7157B"), "monitoring", "exporter → Langfuse"),
    "processor": (1300, 1200, "python", "contract-data-processor", "OCR • tách Điều • embed"),
    "collector": (1700, 1200, "python", "contract-data-collector", "fetch • hash • version"),
    "secrets": (2055, 640, "secrets", "Secrets Manager", "api-keys"),
    "iam": (2185, 640, "iam", "AWS IAM", "instance role"),
    "dynamo": (2090, 1090, "dynamo", "DynamoDB", "jobs • job-events"),
    "s3_ref": (2205, 1090, "s3", "Amazon S3", "văn bản luật gốc"),
    "sqs_data": (2120, 1300, "sqs", "Amazon SQS", "data-update-queue"),
    "langfuse": (2525, 160, ("trace", "#0A60FF"), "Langfuse Cloud", "OTLP traces • usage • lỗi"),
    "tavily": (2520, 600, ("search", "#1E293B"), "Tavily", "web search"),
    "gemini": (2520, 790, "gemini", "Google Gemini API", "gemini-3.5-flash-lite"),
    "qdrant": (2520, 935, "qdrant", "Qdrant Cloud", "legal_documents • vector"),
    "supa_legal": (2520, 1075, "supabase", "Supabase public legal_*", "văn bản • Điều • quan hệ"),
    "vanban": (2520, 1186, ("web", "#1D4ED8"), "vanban.chinhphu.vn", "Hiến pháp • Luật"),
}


def L(k, dy=0):
    x, y = NODES[k][:2]; return (x - S / 2, y + S / 2 + dy)
def R(k, dy=0):
    x, y = NODES[k][:2]; return (x + S / 2, y + S / 2 + dy)
def T(k, dx=0):
    x, y = NODES[k][:2]; return (x + dx, y)
def B(k, dx=0):  # under the name/sub label
    x, y = NODES[k][:2]; return (x + dx, y + S + 40)
def y_(p):
    return p[1]


A, V, O, G, RED, P = "#232F3E", "#8C4FFF", "#ED7100", "#1B998B", "#DD344C", "#E7157B"
# ---------------------------------------------------------------- edges: number, points, colour, dashed, two-way, badge seg
EDGES = [
    (1, [R("sqs_req"), (915, y_(R("sqs_req"))), (915, y_(L("worker", -14))), L("worker", -14)], A, False, False, 0),
    (2, [R("s3_docs"), L("worker")], A, False, True, 0),
    (3, [B("worker"), T("input")], A, False, True, 0),
    (4, [R("worker", -6), L("langgraph", -6)], A, False, False, 0),
    (5, [B("langgraph"), (1330, 765), (1150, 765), T("a1")], A, False, False, 0),
    (None, [(1330, 765), T("a2")], A, False, False, 0),
    (None, [(1330, 765), (1510, 765), T("a3")], A, False, False, 0),
    (6, [R("a3"), L("gemini")], O, False, False, 0),
    (7, [B("a2"), T("mcp")], V, False, False, 0),
    (None, [B("a3"), (1510, y_(R("mcp", -12))), R("mcp", -12)], V, False, False, 0),
    (8, [R("mcp", 10), (2328, y_(R("mcp", 10))), (2328, y_(L("qdrant", -8))), L("qdrant", -8)], V, False, False, 0),
    (None, [(2328, y_(R("mcp", 10))), (2328, y_(L("tavily"))), L("tavily")], V, False, False, 0),
    (None, [(2328, y_(R("mcp", 10))), (2328, y_(L("supa_legal", -8))), L("supa_legal", -8)], V, False, False, 0),
    (9, [R("worker", 18), (1050, y_(R("worker", 18))), (1050, 1112), (2064, 1112)], A, False, False, 1),
    (10, [L("worker", 14), (885, y_(L("worker", 14))), (885, y_(R("sqs_res"))), R("sqs_res")], A, False, False, 1),
    (11, [R("sqs_chat"), (900, y_(R("sqs_chat"))), (900, y_(L("worker", 22))), L("worker", 22)], V, False, False, 1),
    (12, [T("worker"), (955, 594), (1530, 594), T("a3", 20)], V, True, False, 1),
    (13, [L("sqs_data"), (1925, y_(L("sqs_data"))), (1925, y_(R("collector", 14))), R("collector", 14)], G, False, False, 1),
    (14, [L("vanban"), R("collector", -14)], G, False, False, 0),
    (15, [T("collector"), (1700, 1135), (1970, 1135), (1970, 1062), (2205, 1062), T("s3_ref")], G, False, False, 1),
    (16, [L("collector"), R("processor")], G, False, False, 0),
    (17, [B("processor"), (1300, 1400), (2318, 1400), (2318, y_(L("supa_legal", 10))), L("supa_legal", 10)], G, False, False, 1),
    (None, [(2318, y_(L("supa_legal", 10))), (2318, y_(L("qdrant", 10))), L("qdrant", 10)], G, False, False, 0),
    (18, [R("config"), (1930, y_(R("config"))), L("secrets")], RED, True, False, 1),
    (19, [R("monitoring"), (1890, y_(R("monitoring"))), (1890, 186), L("langfuse")], P, False, False, 1),
    (20, [(1720, 1200), (1720, B("monitoring")[1])], P, True, False, 0),
]

LEGEND = [
    "worker (intake) long-poll SQS analysis-requests, giữ lease + heartbeat",
    "worker tải file + request.json; ghi clean/ và results/extraction.json",
    "worker gửi bytes sang input-processing, nhận ContractInput (chunk + provenance)",
    "worker submit/poll job sang langgraph (HTTP job + Idempotency-Key)",
    "langgraph gọi tuần tự agent-1 (từng chunk) → aggregate → agent-2 → agent-3",
    "agent-1/2/3 gọi Google Gemini generateContent (gemini-3.5-flash-lite)",
    "agent-2/3 gọi tool qua MCP (tối đa 6 vòng/lượt, đánh giá lại khi thiếu bằng chứng)",
    "MCP/RAG: Qdrant (dense+sparse, RRF) • Supabase legal_* • Tavily web search",
    "worker ghi lease / tiến độ / completed vào DynamoDB (jobs + job-events)",
    "worker (progress) gửi progress / completed / failed / chat_answered",
    "chat process của worker nhận câu hỏi từ SQS chat-requests (khi có WORKER_CHAT_QUEUE_URL)",
    "worker gọi agent-3 /v1/chat (một lượt Gemini, không qua LangGraph)",
    "collector nhận message từ SQS data-update-queue (một lượt crawl)",
    "collector tải văn bản từ vanban.chinhphu.vn",
    "collector lưu file gốc vào S3 tham chiếu",
    "collector chuyển bytes (RAM) sang processor",
    "processor ghi Điều/quan hệ vào Supabase legal_* và vector vào Qdrant",
    "service lấy key qua config ← Secrets Manager (IAM instance role)",
    "mọi service gửi event tới monitoring → xuất OTLP sang Langfuse Cloud",
    "data pipeline gửi sự kiện tới monitoring qua 127.0.0.1:8099",
]
KEY = ((A, "luồng phân tích", False), (V, "chat / tool MCP", False), (O, "gọi model", False),
       (G, "dữ liệu pháp luật", False), (P, "giám sát Langfuse", False), (A, "tùy chọn / phụ trợ", True))
ON_BORDER = set()
TITLE = "ContractGuard — Kiến trúc hệ thống AI phân tích rủi ro hợp đồng"
SUBTITLE = ("SQS/S3 ↔ EC2 trong VPC (9 container + data pipeline) ↔ Gemini / RAG pháp luật • Giám sát: Langfuse • "
            "Chỉ gồm thành phần có trong source code")

# The web side was removed: shift everything left into the freed space.
DX = -400
GROUPS = [(x + DX, *rest) for x, *rest in GROUPS]
NODES = {k: (x + DX, *rest) for k, (x, *rest) in NODES.items()}
EDGES = [(n, [(x + DX, y) for x, y in pts], *rest) for n, pts, *rest in EDGES]
# ...and up into the space above AWS Cloud; Langfuse sits top-right, level with AWS Cloud.
DY = -260
GROUPS = [(x, y + DY, *rest) for x, y, *rest in GROUPS]
NODES = {k: (x, y + DY, *rest) for k, (x, y, *rest) in NODES.items()}
EDGES = [(n, [(x, y + DY) for x, y in pts], *rest) for n, pts, *rest in EDGES]
GROUPS[-2] = (1940, 100, 370, 180, *GROUPS[-2][4:])   # Giám sát • Langfuse
GROUPS[-1] = (1940, 300, 370, 970, *GROUPS[-1][4:])   # Model & dữ liệu ngoài
NODES["langfuse"] = (2125, 140, *NODES["langfuse"][2:])
i = next(i for i, e in enumerate(EDGES) if e[0] == 19)
n, pts, *rest = EDGES[i]
EDGES[i] = (n, [pts[0], pts[1], (pts[1][0], 166), (2099, 166)], *rest[:-1], 2)  # badge on the Langfuse leg
COLOURS = sorted({e[2] for e in EDGES})

def badge_point(points, seg):
    (x1, y1), (x2, y2) = points[seg], points[seg + 1]
    return ((x1 + x2) / 2, (y1 + y2) / 2)


def label_width(text, size=13):
    return len(text) * size * 0.56 + 8


# ================================================================ SVG
def t(x, y, text, size, colour, bold=False, anchor="middle"):
    weight = ' font-weight="bold"' if bold else ""
    return (f'<text x="{x}" y="{y}" font-family="Arial, Helvetica, sans-serif" font-size="{size}"{weight} '
            f'fill="{colour}" text-anchor="{anchor}">{esc(text)}</text>')


def make_svg():
    a = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">', "<defs>"]
    a += [f'<marker id="m{i}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
          f'orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="{c}"/></marker>' for i, c in enumerate(COLOURS)]
    a += ["</defs>", f'<rect width="{W}" height="{H}" fill="#FFFFFF"/>',
          t(30, 42, TITLE, 24, INK, True, "start"), t(30, 68, SUBTITLE, 14, SUB, False, "start")]
    for x, y, w, h, title, colour, dashed, ic, fill in GROUPS:
        dash = ' stroke-dasharray="7 5"' if dashed else ""
        a.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill or "none"}" stroke="{colour}" stroke-width="1.6"{dash}/>')
    for n, pts, colour, dashed, both, seg in EDGES:
        m = COLOURS.index(colour)
        dash = ' stroke-dasharray="6 4"' if dashed else ""
        start = f' marker-start="url(#m{m})"' if both else ""
        a.append(f'<path d="M{" L".join(f"{x} {y}" for x, y in pts)}" fill="none" stroke="{colour}" '
                 f'stroke-width="1.8"{dash}{start} marker-end="url(#m{m})"/>')
    for x, y, w, h, title, colour, dashed, ic, fill in GROUPS:  # labels above the lines
        tx = x + (36 if ic else 10)
        lines = title.split("\n")
        a.append(f'<rect x="{x + 2}" y="{y + 2}" width="{tx - x - 2 + max(label_width(s) for s in lines)}" '
                 f'height="{8 + 16 * len(lines)}" fill="{fill or "#FFFFFF"}"/>')
        if ic:
            a.append(f'<image href="{icon(ic)}" x="{x}" y="{y}" width="28" height="28"/>')
        for i, line in enumerate(lines):
            a.append(t(tx, y + 19 + i * 16, line, 13, colour, True, "start"))
    for key, (x, y, ic, name, sub) in NODES.items():
        a.append(f'<image href="{icon(ic)}" x="{x - S / 2}" y="{y}" width="{S}" height="{S}"/>')
        if key in ON_BORDER:  # icon sits on a frame border: keep the border line off its label
            a.append(f'<rect x="{x - 62}" y="{y + S + 3}" width="124" height="{32 if sub else 18}" fill="#FFFFFF"/>')
        a.append(t(x, y + S + 16, name, 12.5, INK, True))
        if sub:
            a.append(t(x, y + S + 31, sub, 11, SUB))
    for n, pts, colour, dashed, both, seg in EDGES:
        if n is not None:
            bx, by = badge_point(pts, seg)
            a.append(f'<circle cx="{bx}" cy="{by}" r="11" fill="{INK}"/>' + t(bx, by + 4, n, 11, "#FFFFFF", True))
    top = LEGEND_TOP
    a.append(f'<rect x="30" y="{top - 34}" width="{W - 60}" height="{H - top + 14}" fill="#F8FAFC" stroke="#CBD5E1"/>')
    a.append(t(46, top - 10, "Luồng xử lý (số trên mũi tên)", 14, INK, True, "start"))
    for i, text in enumerate(LEGEND):
        cx, cy = 60 + (i // 10) * 1130, top + 20 + (i % 10) * 31
        a.append(f'<circle cx="{cx}" cy="{cy - 4}" r="10" fill="{INK}"/>' + t(cx, cy, i + 1, 10.5, "#FFFFFF", True))
        a.append(t(cx + 20, cy, text, 13, INK, False, "start"))
    for i, (colour, label, dashed) in enumerate(KEY):
        kx = 760 + i * 250
        dash = ' stroke-dasharray="6 4"' if dashed else ""
        a.append(f'<path d="M{kx} {top - 14} L{kx + 40} {top - 14}" stroke="{colour}" stroke-width="2.2"{dash}/>')
        a.append(t(kx + 48, top - 10, label, 12.5, SUB, False, "start"))
    a.append("</svg>")
    return "".join(a)


# ================================================================ draw.io
def cell(root, cid, value, style, x, y, w, h, parent="1"):
    c = E.SubElement(root, "mxCell", id=cid, value=value, style=style, vertex="1", parent=parent)
    E.SubElement(c, "mxGeometry", x=str(x), y=str(y), width=str(w), height=str(h), attrib={"as": "geometry"})


def make_drawio():
    mx = E.Element("mxfile", host="app.diagrams.net", type="device", version="24.7.17")
    d = E.SubElement(mx, "diagram", id="architecture", name="ContractGuard • Kiến trúc")
    model = E.SubElement(d, "mxGraphModel", dx=str(W), dy=str(H), grid="1", gridSize="10", page="1",
                         pageWidth=str(W), pageHeight=str(H), background="#FFFFFF")
    root = E.SubElement(model, "root")
    E.SubElement(root, "mxCell", id="0"); E.SubElement(root, "mxCell", id="1", parent="0")
    txt = "text;html=1;align=left;verticalAlign=top;fillColor=none;strokeColor=none;fontFamily=Arial;"
    cell(root, "title", f"<b>{esc(TITLE)}</b>", txt + f"fontSize=24;fontColor={INK};", 26, 16, 1600, 34)
    cell(root, "subtitle", esc(SUBTITLE), txt + f"fontSize=14;fontColor={SUB};", 26, 52, 2200, 24)
    for i, (x, y, w, h, title, colour, dashed, ic, fill) in enumerate(GROUPS):
        st = (f"rounded=0;html=1;whiteSpace=wrap;align=left;verticalAlign=top;fontFamily=Arial;fontSize=13;fontStyle=1;"
              f"fontColor={colour};strokeColor={colour};strokeWidth=1.6;fillColor={fill or 'none'};"
              f"spacingLeft={38 if ic else 10};spacingTop=4;" + ("dashed=1;dashPattern=7 5;" if dashed else ""))
        cell(root, f"g{i}", esc(title).replace("\n", "<br>"), st, x, y, w, h)
        if ic:
            cell(root, f"g{i}i", "", f"shape=image;aspect=fixed;image={icon(ic).replace(';base64,', ',')};", x, y, 28, 28)
    for i, (n, pts, colour, dashed, both, seg) in enumerate(EDGES):
        st = (f"edgeStyle=none;html=1;rounded=0;endArrow=block;endFill=1;strokeColor={colour};strokeWidth=1.8;"
              + ("dashed=1;" if dashed else "") + ("startArrow=block;startFill=1;" if both else ""))
        c = E.SubElement(root, "mxCell", id=f"e{i}", value="", style=st, edge="1", parent="1")
        geo = E.SubElement(c, "mxGeometry", relative="1", attrib={"as": "geometry"})
        E.SubElement(geo, "mxPoint", x=str(pts[0][0]), y=str(pts[0][1]), attrib={"as": "sourcePoint"})
        E.SubElement(geo, "mxPoint", x=str(pts[-1][0]), y=str(pts[-1][1]), attrib={"as": "targetPoint"})
        if len(pts) > 2:
            arr = E.SubElement(geo, "Array", attrib={"as": "points"})
            for x, y in pts[1:-1]:
                E.SubElement(arr, "mxPoint", x=str(x), y=str(y))
    for key, (x, y, ic, name, sub) in NODES.items():
        label = f"<b>{esc(name)}</b>" + (f'<br><font style="font-size:11px" color="{SUB}">{esc(sub)}</font>' if sub else "")
        st = (f"shape=image;html=1;aspect=fixed;image={icon(ic).replace(';base64,', ',')};verticalLabelPosition=bottom;"
              f"verticalAlign=top;labelPosition=center;align=center;fontFamily=Arial;fontSize=12;fontColor={INK};"
              "spacingTop=-2;labelBackgroundColor=#FFFFFF;")
        cell(root, key, label, st, x - S / 2, y, S, S)
    badge = f"ellipse;html=1;fillColor={INK};strokeColor=none;fontColor=#FFFFFF;fontStyle=1;fontSize=11;fontFamily=Arial;"
    for i, (n, pts, colour, dashed, both, seg) in enumerate(EDGES):
        if n is not None:
            bx, by = badge_point(pts, seg)
            cell(root, f"b{i}", str(n), badge, bx - 11, by - 11, 22, 22)
    top = LEGEND_TOP
    cell(root, "legend", "", "rounded=0;fillColor=#F8FAFC;strokeColor=#CBD5E1;", 30, top - 34, W - 60, H - top + 14)
    cell(root, "legend-t", "<b>Luồng xử lý (số trên mũi tên)</b>", txt + f"fontSize=14;fontColor={INK};", 40, top - 30, 400, 22)
    for i, text in enumerate(LEGEND):
        cx, cy = 60 + (i // 10) * 1130, top + 20 + (i % 10) * 31
        cell(root, f"lb{i}", str(i + 1), badge.replace("fontSize=11", "fontSize=10"), cx - 10, cy - 14, 20, 20)
        cell(root, f"lt{i}", esc(text), txt + f"fontSize=13;fontColor={INK};verticalAlign=middle;", cx + 16, cy - 15, 860, 22)
    E.indent(mx, space="  ")
    return mx


if __name__ == "__main__":
    svg_path = OUT / "ContractGuard-Architecture.svg"
    svg_path.write_text(make_svg(), encoding="utf-8")
    tree = make_drawio()
    E.ElementTree(tree).write(OUT / "ContractGuard-Architecture.drawio", encoding="utf-8", xml_declaration=True)
    ids = [c.get("id") for c in tree.iter("mxCell")]
    assert len(ids) == len(set(ids)), "duplicate draw.io ids"
    png = OUT / "ContractGuard-Architecture.png"
    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={W},{H}",
                    f"--screenshot={png}", svg_path.resolve().as_uri()], check=True, capture_output=True, timeout=120)
    print(f"{len(NODES)} icons, {sum(1 for e in EDGES if e[0])} numbered flows, {len(GROUPS)} frames -> {png.name}")
