"""Helpers for the DOM-level UI tests (standard library only): a real server on a free localhost port in a
thread, an http.client based client, and a tiny DOM built with html.parser."""
from __future__ import annotations

import http.client
import json
import threading
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Iterator
from urllib.parse import urlencode

from pms.web.app import WebApp, make_server

VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track",
                       "wbr"})


# --------------------------------------------------------------------------- DOM
class Node:
    def __init__(self, tag: str, attrs: list[tuple[str, str | None]], parent: "Node | None" = None) -> None:
        self.tag = tag
        self.attrs = {k: (v if v is not None else "") for k, v in attrs}
        self.children: list[Any] = []
        self.parent = parent

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.attrs.get(name, default)

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def iter(self) -> Iterator["Node"]:
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.iter()

    def text(self) -> str:
        parts: list[str] = []
        for child in self.children:
            parts.append(child.text() if isinstance(child, Node) else child)
        return "".join(parts)

    def matches(self, tag: str | None, **attrs: Any) -> bool:
        if tag is not None and self.tag != tag:
            return False
        for key, wanted in attrs.items():
            name = key.rstrip("_").replace("_", "-")
            if name == "class":
                if wanted not in self.classes:
                    return False
            elif wanted is True:
                if name not in self.attrs:
                    return False
            elif self.attrs.get(name) != wanted:
                return False
        return True

    def find_all(self, tag: str | None = None, **attrs: Any) -> list["Node"]:
        return [n for n in self.iter() if n is not self and n.matches(tag, **attrs)]

    def find(self, tag: str | None = None, **attrs: Any) -> "Node | None":
        found = self.find_all(tag, **attrs)
        return found[0] if found else None

    def __repr__(self) -> str:
        return f"<{self.tag} {self.attrs}>"


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", [])
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(Node(tag, attrs, self.stack[-1]))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def parse_html(text: str) -> Node:
    builder = _Builder()
    builder.feed(text)
    builder.close()
    return builder.root


def document_index(root: Node, node: Node) -> int:
    for index, candidate in enumerate(root.iter()):
        if candidate is node:
            return index
    raise ValueError("node not in document")


# --------------------------------------------------------------------------- client and server
@dataclass
class Result:
    status: int
    headers: dict[str, str]
    text: str

    def json(self) -> Any:
        return json.loads(self.text)

    def dom(self) -> Node:
        return parse_html(self.text)


class LiveServer:
    """The web app on 127.0.0.1 with a free port, served by :func:`pms.web.app.make_server` in a thread."""

    def __init__(self, pipeline: Any) -> None:
        self.pipeline = pipeline
        self.app = WebApp(pipeline)
        self.httpd = make_server("127.0.0.1", 0, self.app)
        self.port = int(self.httpd.server_address[1])
        self.origin = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                       name="pms-ui-test-server", daemon=True)

    def __enter__(self) -> "LiveServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def start(self) -> "LiveServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    def request(self, method: str, path: str, *, user: str | None = None, cookie: str | None = None,
                json_body: Any = None, form: dict[str, Any] | None = None,
                multipart: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
                origin: str | None | bool = True, body: bytes | None = None) -> Result:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        hdrs = dict(headers or {})
        if user:
            hdrs["X-Demo-User"] = user
        if cookie:
            hdrs["Cookie"] = cookie
        data = body
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif form is not None:
            data = urlencode(form).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif multipart is not None:
            boundary = f"----pms-test-{uuid.uuid4().hex}"
            data = encode_multipart(multipart, boundary)
            hdrs.setdefault("Content-Type", f"multipart/form-data; boundary={boundary}")
        if method == "POST" and origin and (form is not None or multipart is not None):
            hdrs.setdefault("Origin", self.origin if origin is True else str(origin))
        try:
            conn.request(method, path, body=data, headers=hdrs)
            response = conn.getresponse()
            text = response.read().decode("utf-8", errors="replace")
            return Result(response.status, {k.lower(): v for k, v in response.getheaders()}, text)
        finally:
            conn.close()

    def get(self, path: str, user: str | None = "viewer", **kw: Any) -> Result:
        return self.request("GET", path, user=user, **kw)

    def page(self, path: str, user: str | None = "viewer") -> Node:
        result = self.get(path, user=user)
        if result.status != 200:
            raise AssertionError(f"GET {path} as {user}: HTTP {result.status}: {result.text[:400]}")
        return result.dom()


def encode_multipart(fields: dict[str, Any], boundary: str) -> bytes:
    """``fields``: name -> text, or name -> (filename, bytes, content type)."""
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        if isinstance(value, tuple):
            filename, content, ctype = value
            out += (f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                    f"Content-Type: {ctype}\r\n\r\n").encode()
            out += content
        else:
            out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            out += str(value).encode("utf-8")
        out += b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out)
