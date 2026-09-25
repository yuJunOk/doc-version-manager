# -*- coding: utf-8 -*-
"""HTTP 服务：静态资源 + REST API + 主页面。"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from .paths import STATIC_MIME, get_static_dir, get_template_path


_HTML_CACHE: str | None = None


def load_index_html() -> str:
    """开发时每次读盘，便于改模板即时生效；打包后仍可走缓存。"""
    global _HTML_CACHE
    import sys

    path = get_template_path()
    if getattr(sys, "frozen", False):
        if _HTML_CACHE is None:
            with open(path, encoding="utf-8") as f:
                _HTML_CACHE = f.read()
        return _HTML_CACHE
    with open(path, encoding="utf-8") as f:
        return f.read()


class RequestHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器。"""

    def log_message(self, format, *args):  # noqa: A003
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._send_html(load_index_html())
        elif path == "/api/files":
            self._send_json(self.server.vm.get_file_tree())
        elif path == "/api/dirs":
            self._send_json({"dirs": self.server.vm.list_directories()})
        elif path == "/api/pending_changes":
            self._send_json(self.server.vm.get_pending_changes())
        elif path == "/api/history":
            qs = parse_qs(parsed.query)
            scope = qs.get("path", [""])[0]
            hist = self.server.vm.get_history(scope if scope else None)
            self._send_json({
                "history": hist,
                "path": (scope or "").replace("\\", "/").strip("/"),
            })
        elif path == "/api/workspace":
            self._send_json(self.server.vm.get_workspace_info())
        elif path == "/api/preview":
            qs = parse_qs(parsed.query)
            name = qs.get("name", [""])[0]
            rev = qs.get("rev", [None])[0]
            self._send_json(self.server.vm.preview_file(name, rev))
        elif path == "/api/pdf":
            qs = parse_qs(parsed.query)
            name = qs.get("file", [""])[0]
            if not name:
                self._send_html(
                    '<html><body style="font-family:sans-serif;padding:40px;color:#666">'
                    "<h3>缺少文件参数</h3></body></html>"
                )
                return
            pdf_path = self.server.vm.get_pdf_for_file(name)
            if pdf_path and os.path.exists(pdf_path):
                self._send_file(pdf_path, "application/pdf")
            else:
                self._send_html(
                    '<html><body style="font-family:sans-serif;padding:40px;color:#666;text-align:center">'
                    "<h3>PDF 渲染失败</h3>"
                    "<p>请确保已安装 Microsoft Word。</p>"
                    '<p style="font-size:12px;color:#999">提示：Word COM 接口用于将 .docx 渲染为高保真 PDF 预览。</p>'
                    "</body></html>"
                )
        elif path == "/api/raw":
            qs = parse_qs(parsed.query)
            name = qs.get("file", [""])[0]
            rev = qs.get("rev", [None])[0]
            if not name:
                self._send_json({"error": "缺少 file 参数"}, 400)
                return
            fpath, err = self.server.vm.resolve_file_for_read(name, rev)
            if not fpath:
                self._send_json({"error": err or "文件不存在"}, 404)
                return
            ext = os.path.splitext(name)[1].lower()
            raw_mime = {
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".txt": "text/plain; charset=utf-8",
                ".md": "text/plain; charset=utf-8",
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".webp": "image/webp",
                ".bmp": "image/bmp",
                ".svg": "image/svg+xml",
                ".pdf": "application/pdf",
                ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ".xls": "application/vnd.ms-excel",
                ".csv": "text/csv; charset=utf-8",
            }
            self._send_file(fpath, raw_mime.get(ext, "application/octet-stream"))
        elif path == "/api/download":
            qs = parse_qs(parsed.query)
            name = qs.get("file", [""])[0]
            if not name:
                self._send_json({"error": "缺少 file 参数"}, 400)
                return
            fpath, err = self.server.vm.resolve_workspace_file(name)
            if not fpath:
                self._send_json({"error": err or "文件不存在"}, 404)
                return
            base = os.path.basename(name.replace("\\", "/")) or "download"
            ext = os.path.splitext(base)[1].lower()
            raw_mime = {
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".txt": "text/plain; charset=utf-8",
                ".md": "text/plain; charset=utf-8",
                ".pdf": "application/pdf",
                ".zip": "application/zip",
            }
            self._send_download(fpath, raw_mime.get(ext, "application/octet-stream"), base)
        elif path == "/api/rev_file":
            # 类似 svn cat：下载某修订中的单个文件
            qs = parse_qs(parsed.query)
            try:
                rev = int(qs.get("rev", [""])[0])
            except (TypeError, ValueError):
                self._send_json({"error": "缺少或无效 rev"}, 400)
                return
            name = qs.get("file", [""])[0]
            if not name:
                self._send_json({"error": "缺少 file 参数"}, 400)
                return
            fpath, _digest, err = self.server.vm.resolve_rev_file(rev, name)
            if not fpath:
                self._send_json({"error": err or "文件不存在"}, 404)
                return
            base = os.path.basename(name.replace("\\", "/"))
            download_name = f"r{rev}_{base}"
            ext = os.path.splitext(base)[1].lower()
            raw_mime = {
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".txt": "text/plain; charset=utf-8",
                ".md": "text/plain; charset=utf-8",
                ".pdf": "application/pdf",
            }
            self._send_download(fpath, raw_mime.get(ext, "application/octet-stream"), download_name)
        elif path == "/api/diff":
            # 类似 svn diff：对比同一文件两端内容
            qs = parse_qs(parsed.query)
            name = qs.get("file", [""])[0]
            from_rev = qs.get("from", [""])[0]
            to_rev = qs.get("to", [""])[0]
            if not name or from_rev == "" or to_rev == "":
                self._send_json({"ok": False, "msg": "需要 file / from / to 参数"}, 400)
                return
            result = self.server.vm.diff_file(name, from_rev, to_rev)
            self._send_json(result, 200 if result.get("ok") else 400)
        elif path.startswith("/static/"):
            static_dir = get_static_dir()
            rel_path = path[len("/static/") :]
            file_path = os.path.normpath(os.path.join(static_dir, rel_path))
            if not file_path.startswith(os.path.normpath(static_dir)) or not os.path.isfile(file_path):
                self._send_json({"error": "not found"}, 404)
                return
            ext = os.path.splitext(file_path)[1].lower()
            self._send_file(file_path, STATIC_MIME.get(ext, "application/octet-stream"))
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body) if body else {}
        except Exception:
            data = {}

        if path == "/api/upload":
            import base64

            fname = data.get("name", "")
            b64 = data.get("data", "")
            dest_dir = data.get("dir", "")
            try:
                file_data = base64.b64decode(b64)
                ok, msg = self.server.vm.upload_file(fname, file_data, dest_dir)
            except Exception as e:
                ok, msg = False, f"解码失败: {e}"
            self._send_json({"ok": ok, "msg": msg})
        elif path == "/api/mkdir":
            ok, msg = self.server.vm.mkdir(data.get("path", ""))
            self._send_json({"ok": ok, "msg": msg})
        elif path == "/api/rename":
            ok, msg = self.server.vm.rename_item(data.get("src", ""), data.get("name", ""))
            self._send_json({"ok": ok, "msg": msg})
        elif path == "/api/move":
            srcs = data.get("srcs") or ([data.get("src")] if data.get("src") is not None else [])
            dest = data.get("dest", "")
            if len(srcs) > 1:
                ok, msg = self.server.vm.move_items(srcs, dest)
            elif len(srcs) == 1:
                ok, msg = self.server.vm.move_item(srcs[0], dest)
            else:
                ok, msg = False, "未指定源路径"
            self._send_json({"ok": ok, "msg": msg})
        elif path == "/api/save_version":
            ok, msg = self.server.vm.save_version(data.get("note", ""))
            self._send_json({"ok": ok, "msg": msg})
        elif path == "/api/rollback":
            ok, msg = self.server.vm.rollback(data.get("version"))
            self._send_json({"ok": ok, "msg": msg})
        elif path == "/api/restore_path":
            ok, msg = self.server.vm.restore_path(data.get("version"), data.get("path", ""))
            self._send_json({"ok": ok, "msg": msg})
        elif path == "/api/edit":
            ok, msg = self.server.vm.edit_file(data.get("name", ""))
            self._send_json({"ok": ok, "msg": msg})
        elif path == "/api/delete":
            names = data.get("names") or ([data.get("name")] if data.get("name") else [])
            if len(names) > 1:
                ok, msg = self.server.vm.delete_items(names)
            elif len(names) == 1:
                ok, msg = self.server.vm.delete_item(names[0])
            else:
                ok, msg = False, "未指定路径"
            self._send_json({"ok": ok, "msg": msg})
        elif path == "/api/open_explorer":
            ok, msg = self.server.vm.open_in_explorer(
                data.get("target", "item"),
                data.get("path", ""),
            )
            self._send_json({"ok": ok, "msg": msg})
        elif path == "/api/shutdown":
            self._send_json({"ok": True, "msg": "正在关闭..."})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self._send_json({"error": "not found"}, 404)

    def _send_html(self, html: str):
        encoded = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(encoded))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, data, code=200):
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(encoded))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_file(self, filepath: str, content_type: str):
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(data))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._send_json({"error": f"发送文件失败: {e}"}, 500)

    def _send_download(self, filepath: str, content_type: str, filename: str):
        """带 Content-Disposition 的附件下载。"""
        from urllib.parse import quote

        try:
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(data))
            # ASCII fallback + RFC5987 UTF-8
            safe_ascii = "".join(c if ord(c) < 128 and c not in '\\/"' else "_" for c in filename)
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=\"{safe_ascii}\"; filename*=UTF-8''{quote(filename)}",
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._send_json({"error": f"下载失败: {e}"}, 500)


def create_server(vm, host: str = "127.0.0.1", port: int = 404) -> HTTPServer:
    server = HTTPServer((host, port), RequestHandler)
    server.vm = vm
    return server
