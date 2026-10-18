"""Build one static API reference page per service from its OpenAPI spec.

Reads specs/<service>/index.yaml and writes site/<service>/index.html, plus
site/index.html listing every service. The output depends only on the specs,
so --check can compare it with the committed site.
"""

import argparse
import html
import json
import re
import sys
from http import HTTPStatus
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

ROOT = Path(__file__).resolve().parent.parent
METHODS = ("get", "put", "post", "patch", "delete", "head", "options", "trace")
PARAMETER_KINDS = (("path", "Path parameters"), ("query", "Query parameters"),
                   ("header", "Header parameters"), ("cookie", "Cookie parameters"))
# Keys whose values are example data, never schema to resolve.
DATA_KEYS = {"example", "value"}


class DocsError(Exception):
    """The spec cannot be documented as written."""


class _Loader(yaml.SafeLoader):
    """Safe loader that keeps timestamps as the strings written in the spec."""


_Loader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


class Spec:
    """An OpenAPI document with every $ref across its files replaced by its target."""

    def __init__(self, path: Path):
        self._files: dict[Path, object] = {}
        path = path.resolve()
        self.doc = self._resolve(self._load(path), path, ())

    def _load(self, path: Path):
        if path not in self._files:
            if not path.is_file():
                raise DocsError(f"missing spec file {path}")
            self._files[path] = yaml.load(path.read_text(), Loader=_Loader)
        return self._files[path]

    def _lookup(self, ref: str, base: Path):
        file_part, _, pointer = ref.partition("#")
        path = (base.parent / file_part).resolve() if file_part else base
        node = self._load(path)
        for token in filter(None, pointer.split("/")):
            token = unquote(token).replace("~1", "/").replace("~0", "~")
            try:
                node = node[int(token)] if isinstance(node, list) else node[token]
            except (KeyError, IndexError, ValueError, TypeError):
                raise DocsError(f"unresolved reference {ref} from {base}") from None
        return path, node, (path, pointer)

    def _resolve(self, node, base: Path, seen: tuple):
        if isinstance(node, list):
            return [self._resolve(item, base, seen) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            path, target, key = self._lookup(node["$ref"], base)
            if key in seen:
                raise DocsError(f"circular reference {node['$ref']} from {base}")
            resolved = self._resolve(target, path, seen + (key,))
            siblings = {k: self._resolve(v, base, seen) for k, v in node.items() if k != "$ref"}
            if siblings and isinstance(resolved, dict):
                return {**resolved, **siblings}
            return resolved
        return {k: v if k in DATA_KEYS else self._resolve(v, base, seen) for k, v in node.items()}


# Text helpers


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def prose(text: str | None) -> str:
    """Render a spec description as paragraphs, joining wrapped lines."""
    if not text:
        return ""
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text.strip())]
    return "".join(f"<p>{esc(p)}</p>" for p in paragraphs if p)


def one_line(text: str | None) -> str:
    return " ".join((text or "").split())


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def to_json(value) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


# Schema helpers


def _types(schema: dict) -> list[str]:
    declared = schema.get("type", [])
    return [declared] if isinstance(declared, str) else list(declared)


def type_label(schema: dict) -> str:
    """Short type for a table cell: `string (uuid)`, `DRAFT | PUBLISHED`, `integer | null`."""
    types = _types(schema)
    nullable = "null" in types
    base = [t for t in types if t != "null"]
    if "enum" in schema:
        label = " | ".join(str(v) for v in schema["enum"] if v is not None)
    elif "const" in schema:
        label = str(schema["const"])
    elif "array" in base:
        label = f"array of {type_label(schema.get('items', {}))}"
    elif "anyOf" in schema or "oneOf" in schema:
        label = " | ".join(type_label(s) for s in schema.get("anyOf", schema.get("oneOf")))
    elif base:
        label = " | ".join(base)
        if "format" in schema:
            label += f" ({schema['format']})"
    else:
        label = "any"
    return f"{label} | null" if nullable else label


def constraints(schema: dict) -> str:
    parts = []
    low, high = schema.get("minLength"), schema.get("maxLength")
    if low is not None and high is not None:
        parts.append(f"{low}–{high} characters")
    elif high is not None:
        parts.append(f"At most {high} characters")
    elif low is not None and low > 1:
        parts.append(f"At least {low} characters")
    low, high = schema.get("minimum"), schema.get("maximum")
    if low is not None and high is not None:
        parts.append(f"{low}–{high}")
    elif low is not None:
        parts.append(f"Minimum {low}")
    elif high is not None:
        parts.append(f"Maximum {high}")
    if "default" in schema:
        parts.append(f"Default {json.dumps(schema['default'])}")
    return ". ".join(parts) + "." if parts else ""


def describe(schema: dict, description: str | None = None) -> str:
    text = esc(one_line(description if description is not None else schema.get("description")))
    note = constraints(schema)
    if note:
        text += f' <span class="note">{esc(note)}</span>'
    return text


def field_rows(schema: dict, prefix: str = ""):
    """(name, type, required, description html) for each field, nested fields dotted."""
    required = set(schema.get("required", []))
    for name, prop in schema.get("properties", {}).items():
        full = prefix + name
        yield full, type_label(prop), name in required, describe(prop)
        if prop.get("properties"):
            yield from field_rows(prop, full + ".")
        elif prop.get("items", {}).get("properties"):
            yield from field_rows(prop["items"], full + "[].")


def at_least_one_of(schema: dict) -> list[str]:
    """Fields from an anyOf whose branches each only require one field."""
    branches = schema.get("anyOf", [])
    if branches and all(set(b) == {"required"} and len(b["required"]) == 1 for b in branches):
        return [b["required"][0] for b in branches]
    return []


def schema_example(schema: dict):
    if schema.get("examples"):
        return schema["examples"][0]
    if "example" in schema:
        return schema["example"]
    return None


def media_example(media: dict, what: str):
    """The first example written for a request or response body."""
    examples = media.get("examples")
    if isinstance(examples, dict) and examples:
        first = next(iter(examples.values()))
        if isinstance(first, dict) and "value" in first:
            return first["value"]
    if "example" in media:
        return media["example"]
    example = schema_example(media.get("schema", {}))
    if example is None:
        raise DocsError(f"{what} has no example")
    return example


def first_media(content: dict) -> tuple[str, dict]:
    content_type = next(iter(content))
    return content_type, content[content_type]


def render_body(content_type: str, example) -> str:
    if "json" in content_type:
        return to_json(example)
    return str(example)


# Operations


class Operation:
    def __init__(self, spec: dict, path: str, path_item: dict, method: str):
        op = path_item[method]
        self.path = path
        self.method = method.upper()
        self.id = op.get("operationId", f"{method} {path}")
        self.summary = op.get("summary") or f"{self.method} {path}"
        self.description = op.get("description", "")
        self.tags = op.get("tags") or ["Other"]
        self.layer = "Admin" if path == "/admin" or path.startswith("/admin/") else "Public"
        self.anchor = f"{self.layer.lower()}-{slug(self.summary)}"
        servers = op.get("servers") or path_item.get("servers") or spec.get("servers") or [{"url": ""}]
        self.url_path = urlsplit(servers[0]["url"]).path.rstrip("/") + path
        self.security = op.get("security", spec.get("security", []))
        self.request_body = op.get("requestBody")
        self.responses = op.get("responses", {})
        merged = {(p["name"], p["in"]): p for p in path_item.get("parameters", [])}
        merged.update({(p["name"], p["in"]): p for p in op.get("parameters", [])})
        self.parameters = list(merged.values())

    @property
    def where(self) -> str:
        return f"{self.method} {self.path}"

    def auth(self) -> tuple[str, bool]:
        """(label, send the Authorization header in the example)."""
        schemes = [req for req in self.security if req]
        if not schemes:
            return "None", False
        optional = any(not req for req in self.security)
        return ("Bearer token, optional" if optional else "Bearer token"), True


def operations(spec: dict) -> list[Operation]:
    ops = []
    for path, item in spec.get("paths", {}).items():
        for method in item:
            if method in METHODS:
                ops.append(Operation(spec, path, item, method))
    anchors: dict[str, str] = {}
    for op in ops:
        if op.anchor in anchors:
            raise DocsError(f"{op.where} and {anchors[op.anchor]} share the anchor #{op.anchor}")
        anchors[op.anchor] = op.where
    return ops


def grouped(spec: dict, ops: list[Operation]):
    """[(layer, [(tag, tag description, [ops])])] in spec order, Public first."""
    tag_order = [t["name"] for t in spec.get("tags", [])]
    tag_text = {t["name"]: t.get("description", "") for t in spec.get("tags", [])}
    for op in ops:
        if op.tags[0] not in tag_order:
            tag_order.append(op.tags[0])
    result = []
    for layer in ("Public", "Admin"):
        tags = []
        for tag in tag_order:
            members = [op for op in ops if op.layer == layer and op.tags[0] == tag]
            if members:
                tags.append((tag, tag_text.get(tag, ""), members))
        if tags:
            result.append((layer, tags))
    return result


def example_value(param: dict, what: str):
    if "example" in param:
        return param["example"]
    example = schema_example(param.get("schema", {}))
    if example is None:
        raise DocsError(f"{what} parameter {param['name']} has no example")
    return example


def curl(op: Operation) -> str:
    url = op.url_path
    query = []
    headers = []
    for param in op.parameters:
        if param["in"] == "path":
            url = url.replace("{" + param["name"] + "}", str(example_value(param, op.where)))
        elif param.get("required") and param["in"] == "query":
            query.append(f"{param['name']}={example_value(param, op.where)}")
        elif param.get("required") and param["in"] == "header":
            headers.append(f"{param['name']}: {example_value(param, op.where)}")
    if query:
        url += "?" + "&".join(query)
    lines = [f'curl -X {op.method} "$BASE_URL{url}"' if op.method != "GET" else f'curl "$BASE_URL{url}"']
    if op.auth()[1]:
        lines.append('-H "Authorization: Bearer $TOKEN"')
    lines += [f'-H "{h}"' for h in headers]
    if op.request_body:
        content_type, media = first_media(op.request_body["content"])
        body = render_body(content_type, media_example(media, f"{op.where} request body"))
        lines.append(f'-H "Content-Type: {content_type}"')
        lines.append("-d '" + body.replace("'", "'\\''") + "'")
    return " \\\n  ".join(lines)


# HTML


def table(head: list[str], rows: list[list[str]]) -> str:
    cells = "".join(f"<th>{h}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{cells}</tr></thead><tbody>{body}</tbody></table>"


def code_block(text: str, copy: bool = False) -> str:
    button = '<button class="copy" type="button">Copy</button>' if copy else ""
    return f'<div class="code">{button}<pre><code>{esc(text)}</code></pre></div>'


def status_label(code: str) -> str:
    try:
        return f"{code} {HTTPStatus(int(code)).phrase}"
    except ValueError:
        return code


def render_parameters(op: Operation) -> str:
    out = []
    for kind, title in PARAMETER_KINDS:
        params = [p for p in op.parameters if p["in"] == kind]
        if not params:
            continue
        rows = [[f"<code>{esc(p['name'])}</code>", f"<code>{esc(type_label(p.get('schema', {})))}</code>",
                 "Yes" if p.get("required") else "No",
                 describe(p.get("schema", {}), p.get("description"))] for p in params]
        out.append(f"<h5>{title}</h5>" + table(["Name", "Type", "Required", "Description"], rows))
    return "".join(out)


def render_request_body(op: Operation) -> str:
    if not op.request_body:
        return ""
    content_type, media = first_media(op.request_body["content"])
    schema = media.get("schema", {})
    out = [f"<h5>Request body <code>{esc(content_type)}</code></h5>"]
    out.append(prose(op.request_body.get("description") or schema.get("description")))
    out.append(code_block(render_body(content_type, media_example(media, f"{op.where} request body"))))
    rows = [[f"<code>{esc(name)}</code>", f"<code>{esc(kind)}</code>", "Yes" if req else "No", text]
            for name, kind, req, text in field_rows(schema)]
    if rows:
        out.append(table(["Field", "Type", "Required", "Description"], rows))
    either = at_least_one_of(schema)
    if either:
        names = ", ".join(f"<code>{esc(n)}</code>" for n in either)
        out.append(f'<p class="note">Send at least one of {names}.</p>')
    return "".join(out)


def render_responses(op: Operation) -> str:
    out = []
    successes = [(code, r) for code, r in op.responses.items() if str(code).startswith("2")]
    for code, response in successes:
        out.append(f"<h5>Response <code>{esc(status_label(str(code)))}</code></h5>")
        out.append(prose(response.get("description")))
        headers = response.get("headers", {})
        if headers:
            rows = [[f"<code>{esc(name)}</code>", f"<code>{esc(type_label(h.get('schema', {})))}</code>",
                     describe(h.get("schema", {}), h.get("description"))] for name, h in headers.items()]
            out.append(table(["Header", "Type", "Description"], rows))
        if response.get("content"):
            content_type, media = first_media(response["content"])
            example = media_example(media, f"{op.where} {code} response body")
            out.append(code_block(render_body(content_type, example)))
        else:
            out.append('<p class="note">No body.</p>')
    errors = [(code, r) for code, r in op.responses.items() if not str(code).startswith("2")]
    if errors:
        rows = [[f"<code>{esc(code)}</code>", esc(one_line(r.get("description")))] for code, r in errors]
        out.append("<h5>Errors</h5>" + table(["Status", "Description"], rows))
    return "".join(out)


def render_operation(op: Operation) -> str:
    auth = op.auth()[0]
    return (
        f'<section class="endpoint" id="{op.anchor}" data-operation-id="{esc(op.id)}">'
        f'<h4><a href="#{op.anchor}">{esc(op.summary)}</a></h4>'
        f'<p class="route"><span class="method">{op.method}</span> <code>{esc(op.url_path)}</code></p>'
        f"{prose(op.description)}"
        f'<p class="auth"><strong>Auth</strong> {esc(auth)}</p>'
        f"{render_parameters(op)}"
        f"<h5>Example request</h5>{code_block(curl(op), copy=True)}"
        f"{render_request_body(op)}"
        f"{render_responses(op)}"
        "</section>"
    )


def render_overview(spec: dict) -> str:
    info = spec.get("info", {})
    servers = spec.get("servers") or [{"url": ""}]
    base = urlsplit(servers[0]["url"]).path.rstrip("/")
    out = ['<section id="overview">', f"<h1>{esc(info.get('title', 'API'))}</h1>",
           f'<p class="version">Version {esc(info.get("version", ""))}</p>', prose(info.get("description"))]
    out.append("<h2>Base URL</h2>")
    out.append(f"<p><code>$BASE_URL{esc(base)}</code>. Set <code>BASE_URL</code> to the API host. "
               "The examples below use it as written.</p>")
    for server in servers:
        url = urlsplit(server["url"])
        if url.scheme and url.netloc:
            name = one_line(server.get("description")).rstrip(".") or url.netloc
            out.append(f"<h5>{esc(name)}</h5>")
            out.append(code_block(f"export BASE_URL={url.scheme}://{url.netloc}", copy=True))
    schemes = spec.get("components", {}).get("securitySchemes", {})
    bearer = [s for s in schemes.values() if s.get("type") == "http" and s.get("scheme") == "bearer"]
    if bearer:
        out.append("<h2>Authentication</h2>")
        out.append(prose(bearer[0].get("description")))
        out.append(code_block('-H "Authorization: Bearer $TOKEN"'))
        out.append('<p>Each endpoint states whether it needs the token.</p>')
    error = spec.get("components", {}).get("schemas", {}).get("error")
    if error and schema_example(error) is not None:
        out.append("<h2>Errors</h2>")
        out.append(prose(error.get("description")))
        out.append(code_block(to_json(schema_example(error))))
        out.append("<p>Each endpoint lists the error statuses it can return.</p>")
    out.append("</section>")
    return "".join(out)


def render_sidebar(groups) -> str:
    out = ['<nav class="sidebar"><a class="home" href="../">All APIs</a>',
           '<a class="overview" href="#overview">Overview</a>']
    for layer, tags in groups:
        out.append(f'<p class="layer">{esc(layer)}</p>')
        for tag, _, ops in tags:
            out.append(f'<p class="tag">{esc(tag)}</p><ul>')
            out += [f'<li><a href="#{op.anchor}"><span class="method">{op.method}</span> {esc(op.summary)}</a></li>'
                    for op in ops]
            out.append("</ul>")
    out.append("</nav>")
    return "".join(out)


def render_service(spec_path: Path) -> str:
    """The reference page for the service whose root spec is spec_path."""
    spec = Spec(spec_path).doc
    ops = operations(spec)
    groups = grouped(spec, ops)
    main = [render_overview(spec)]
    for layer, tags in groups:
        main.append(f'<h2 class="layer-heading">{esc(layer)}</h2>')
        for tag, text, members in tags:
            main.append(f"<h3>{esc(tag)}</h3>{prose(text)}")
            main += [render_operation(op) for op in members]
    title = spec.get("info", {}).get("title", "API")
    return page(title, render_sidebar(groups) + f'<main>{"".join(main)}</main>', SCRIPT)


def render_index(services: list[tuple[str, dict]]) -> str:
    items = "".join(
        f'<li><a href="{esc(name)}/">{esc(info.get("title", name))}</a> '
        f'<span class="note">Version {esc(info.get("version", ""))}</span></li>'
        for name, info in services
    )
    body = f'<main class="index"><h1>API reference</h1><ul class="services">{items}</ul></main>'
    return page("API reference", body, "")


def page(title: str, body: str, script: str) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title><style>{CSS}</style></head>"
        f'<body><div class="layout">{body}</div>{script}</body></html>\n'
    )


CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#e3e3e3;--code-bg:#f5f5f4;--link:#0b57d0}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#e8e8e8;--muted:#9a9a9a;--line:#2e2e2e;--code-bg:#1e1e1e;--link:#8ab4f8}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
code,pre,.method{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}
.layout{display:flex;min-height:100vh}
.sidebar{position:sticky;top:0;align-self:flex-start;width:280px;flex:none;height:100vh;overflow-y:auto;padding:24px 16px;border-right:1px solid var(--line)}
.sidebar a{display:block;padding:3px 6px;color:var(--fg);border-radius:4px}
.sidebar a:hover{background:var(--code-bg);text-decoration:none}
.sidebar .home{color:var(--muted);margin-bottom:8px}
.sidebar ul{list-style:none;margin:0;padding:0}
.sidebar li a{display:flex}
.sidebar .method{flex:none;width:52px;padding-top:2px;color:var(--muted);font-size:11px;font-weight:600}
.sidebar .layer{margin:20px 0 4px;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}
.sidebar .tag{margin:10px 0 2px 6px;font-size:13px;color:var(--muted)}
main{flex:1;min-width:0;max-width:880px;padding:32px 40px 96px}
main.index{margin:0 auto}
h1{font-size:28px;margin:0 0 4px}
h2{font-size:22px;margin:48px 0 8px;padding-top:16px;border-top:1px solid var(--line)}
h2.layer-heading{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
h3{font-size:20px;margin:32px 0 4px}
h4{font-size:18px;margin:0 0 6px}h4 a{color:var(--fg)}
h5{font-size:14px;margin:22px 0 8px}
.endpoint{margin:28px 0;padding-top:24px;border-top:1px solid var(--line);scroll-margin-top:12px}
.route{margin:0 0 10px}.route .method{font-weight:700;margin-right:6px}
.version,.note{color:var(--muted)}
.auth strong{margin-right:6px}
p code,td code{background:var(--code-bg);padding:1px 5px;border-radius:4px}
.code{position:relative}
pre{margin:0 0 12px;padding:14px 16px;background:var(--code-bg);border:1px solid var(--line);border-radius:6px;overflow-x:auto}
.copy{position:absolute;top:8px;right:8px;font:12px/1 inherit;padding:5px 8px;color:var(--muted);background:var(--bg);border:1px solid var(--line);border-radius:4px;cursor:pointer}
table{width:100%;border-collapse:collapse;margin:0 0 12px;font-size:14px;display:block;overflow-x:auto}
th,td{text-align:left;vertical-align:top;padding:7px 10px;border-bottom:1px solid var(--line)}
th{font-weight:600;color:var(--muted);white-space:nowrap}
td:first-child{white-space:nowrap}
.services{padding-left:18px}
@media (max-width:800px){
.layout{display:block}
.sidebar{position:static;width:auto;height:auto;border-right:0;border-bottom:1px solid var(--line)}
main{padding:24px 16px 64px}
}
"""

SCRIPT = """<script>
document.querySelectorAll(".copy").forEach(function (button) {
  button.addEventListener("click", function () {
    var text = button.parentElement.querySelector("code").textContent;
    navigator.clipboard.writeText(text).then(function () {
      button.textContent = "Copied";
      setTimeout(function () { button.textContent = "Copy"; }, 1500);
    });
  });
});
</script>"""


def build_site(specs_dir: Path) -> dict[Path, str]:
    """Relative output path → page HTML for every service under specs_dir."""
    pages: dict[Path, str] = {}
    services = []
    for service_dir in sorted(p for p in specs_dir.iterdir() if (p / "index.yaml").is_file()):
        root = service_dir / "index.yaml"
        pages[Path(service_dir.name) / "index.html"] = render_service(root)
        services.append((service_dir.name, yaml.load(root.read_text(), Loader=_Loader).get("info", {})))
    pages[Path("index.html")] = render_index(services)
    return pages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--specs", type=Path, default=ROOT / "specs")
    parser.add_argument("--site", type=Path, default=ROOT / "site")
    parser.add_argument("--check", action="store_true", help="fail if the site is out of date")
    args = parser.parse_args(argv)
    try:
        pages = build_site(args.specs)
    except DocsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    existing = {p.relative_to(args.site) for p in args.site.rglob("*.html")} if args.site.exists() else set()
    if args.check:
        stale = sorted(str(p) for p, text in pages.items()
                       if not (args.site / p).is_file() or (args.site / p).read_text() != text)
        stale += sorted(f"{p} (not generated)" for p in existing - pages.keys())
        if stale:
            print("site/ is out of date; run `make docs`:\n  " + "\n  ".join(stale), file=sys.stderr)
            return 1
        return 0
    for path in existing - pages.keys():
        (args.site / path).unlink()
    for path, text in pages.items():
        (args.site / path).parent.mkdir(parents=True, exist_ok=True)
        (args.site / path).write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
