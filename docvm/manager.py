# -*- coding: utf-8 -*-
"""应用层版本管理门面：工作副本操作 + 对 DocvmRepository 的委托。"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess

from .paths import get_app_dir, IS_WINDOWS
from .preview import HAS_DOCX, convert_docx_to_pdf
from .repo import DocvmRepository
from .utils import format_file_size, format_timestamp, set_hidden


class VersionManager:
    """
    应用层版本管理门面。

    数据落在「软件同目录/.docvm」下：
    - workspace/  工作副本（用户编辑的文件）
    - objects/    增量内容库（相同文件只存一份）
    - revs/       每次提交的元数据
    - ignore.txt  忽略规则（大工具请写在这里）
    - backups/    仅含「本修订变更文件」的小包
    """

    APP_FOLDER = ".docvm"
    WORKSPACE = "workspace"
    BACKUPS = "backups"

    def __init__(self):
        self.work_dir = None       # 软件目录（固定）
        self.app_data_dir = None   # .docvm
        self.workspace_dir = None  # .docvm/workspace
        self.backups_dir = None
        self.repo = None           # DocvmRepository
        # 兼容旧字段名（部分 UI/逻辑仍读这些）
        self.history = []
        self.last_version_hashes = {}
        self._open_work_dir(get_app_dir())

    # ---- 工作区初始化 ----

    def _open_work_dir(self, dir_path):
        """固定打开软件目录下的 .docvm 仓库。"""
        self.work_dir = dir_path
        self.app_data_dir = os.path.join(dir_path, self.APP_FOLDER)
        self.workspace_dir = os.path.join(self.app_data_dir, self.WORKSPACE)
        self.backups_dir = os.path.join(self.app_data_dir, self.BACKUPS)
        # versions_dir 仅用于「在资源管理器打开旧目录」；新引擎用 revs/
        self.versions_dir = os.path.join(self.app_data_dir, "revs")

        os.makedirs(self.workspace_dir, exist_ok=True)
        os.makedirs(self.backups_dir, exist_ok=True)
        set_hidden(self.app_data_dir)

        self.repo = DocvmRepository(self.app_data_dir, self.workspace_dir)
        self.history = self.repo.history
        self.last_version_hashes = self.repo.head_tree

    def _sync_repo_views(self):
        """把仓库内部状态同步到门面缓存字段（供旧代码路径读取）。"""
        if not self.repo:
            return
        self.history = self.repo.history
        self.last_version_hashes = self.repo.head_tree

    # ---- 路径安全 ----

    def _norm_rel(self, rel):
        """规范化相对路径，非法则返回 None（禁止 .. 与隐藏名）。"""
        if rel is None:
            return None
        rel = str(rel).replace("\\", "/").strip()
        if rel in ("", ".", "/"):
            return ""
        parts = []
        for p in rel.strip("/").split("/"):
            if p in ("", "."):
                continue
            if p == ".." or p.startswith("."):
                return None
            if any(c in p for c in '<>:"|?*'):
                return None
            parts.append(p)
        return "/".join(parts)

    def _resolve(self, rel):
        """将相对路径解析为工作副本内绝对路径，越界返回 None。"""
        if not self.workspace_dir:
            return None
        norm = self._norm_rel(rel)
        if norm is None:
            return None
        ws = os.path.normpath(self.workspace_dir)
        if norm == "":
            return ws
        full = os.path.normpath(os.path.join(ws, norm.replace("/", os.sep)))
        if full != ws and not full.startswith(ws + os.sep):
            return None
        return full

    def _rel_of(self, fullpath):
        return os.path.relpath(fullpath, self.workspace_dir).replace("\\", "/")

    def _iter_workspace_files(self):
        """版本管理视角的文件列表（已排除 ignore）。"""
        if not self.repo:
            return []
        return self.repo.iter_workspace_files(include_ignored=False)

    def _clear_workspace(self):
        if self.repo:
            self.repo._clear_workspace()

    # ---- 文件 / 目录操作（工作副本） ----

    def get_file_tree(self):
        """返回工作区树；忽略文件仍显示，状态为「忽略」。"""
        if not self.workspace_dir:
            return []
        return self._build_tree(self.workspace_dir, "")

    def _build_tree(self, dir_path, rel_prefix):
        nodes = []
        try:
            entries = sorted(os.listdir(dir_path), key=lambda x: x.lower())
        except Exception:
            return nodes
        dirs, files = [], []
        for name in entries:
            if name.startswith("."):
                continue
            full = os.path.join(dir_path, name)
            rel = f"{rel_prefix}/{name}" if rel_prefix else name
            if os.path.isdir(full):
                dirs.append((name, rel, full))
            elif os.path.isfile(full):
                files.append((name, rel, full))
        for name, rel, full in dirs:
            nodes.append({
                "name": name,
                "path": rel,
                "type": "dir",
                "children": self._build_tree(full, rel),
            })
        for name, rel, full in files:
            try:
                stat = os.stat(full)
                status, color = self._get_file_status(rel, full)
                nodes.append({
                    "name": name,
                    "path": rel,
                    "type": "file",
                    "size": format_file_size(stat.st_size),
                    "time": format_timestamp(stat.st_mtime),
                    "status": status,
                    "color": color,
                    "ignored": status == "忽略",
                })
            except Exception:
                pass
        return nodes

    def get_file_list(self):
        """扁平列表（统计用，不含忽略文件）。"""
        if not self.workspace_dir or not self.repo:
            return []
        files = []
        for rel in self._iter_workspace_files():
            fpath = self._resolve(rel)
            if not fpath:
                continue
            try:
                stat = os.stat(fpath)
                status, color = self._get_file_status(rel, fpath)
                files.append({
                    "name": os.path.basename(rel),
                    "path": rel,
                    "size": format_file_size(stat.st_size),
                    "time": format_timestamp(stat.st_mtime),
                    "status": status,
                    "color": color,
                })
            except Exception:
                pass
        return files

    def _get_file_status(self, relpath, filepath):
        if not self.repo:
            return "新增", "#0066cc"
        return self.repo.file_status(relpath, filepath)

    def upload_file(self, filename, data, dest_dir=""):
        if not self.workspace_dir:
            return False, "工作区未就绪"
        base = os.path.basename(str(filename).replace("\\", "/"))
        if not base or base.startswith("."):
            return False, "非法文件名"
        parent = self._resolve(dest_dir or "")
        if parent is None or not os.path.isdir(parent):
            return False, "目标目录无效"
        dst = os.path.join(parent, base)
        if self._resolve(self._rel_of(dst)) is None:
            return False, "目标路径非法"
        try:
            with open(dst, "wb") as f:
                f.write(data)
            rel = self._rel_of(dst)
            if self.repo and self.repo.ignore.is_ignored(rel):
                return True, f"已上传: {rel}（该路径被 ignore，不会进入版本库）"
            return True, f"已上传: {rel}"
        except Exception as e:
            return False, f"上传失败: {e}"

    def mkdir(self, path):
        if not self.workspace_dir:
            return False, "工作区未就绪"
        full = self._resolve(path)
        if full is None or path == "" or self._norm_rel(path) == "":
            return False, "目录路径无效"
        try:
            if os.path.exists(full):
                return False, "路径已存在"
            os.makedirs(full, exist_ok=False)
            return True, f"已创建目录: {self._norm_rel(path)}"
        except Exception as e:
            return False, f"创建失败: {e}"

    def rename_item(self, src, new_name):
        if not self.workspace_dir:
            return False, "工作区未就绪"
        src_full = self._resolve(src)
        if src_full is None or not os.path.exists(src_full):
            return False, "源路径不存在"
        new_name = os.path.basename(str(new_name).replace("\\", "/").strip())
        if not new_name or new_name.startswith(".") or any(c in new_name for c in '<>:"/\\|?*'):
            return False, "新名称无效"
        parent = os.path.dirname(src_full)
        dst_full = os.path.join(parent, new_name)
        if os.path.exists(dst_full):
            return False, "目标名称已存在"
        if self._resolve(self._rel_of(dst_full)) is None:
            return False, "目标路径非法"
        try:
            os.rename(src_full, dst_full)
            return True, f"已重命名为: {self._rel_of(dst_full)}"
        except Exception as e:
            return False, f"重命名失败: {e}"

    def move_item(self, src, dest_dir):
        if not self.workspace_dir:
            return False, "工作区未就绪"
        src_full = self._resolve(src)
        if src_full is None or not os.path.exists(src_full):
            return False, "源路径不存在"
        dest_full = self._resolve(dest_dir or "")
        if dest_full is None or not os.path.isdir(dest_full):
            return False, "目标目录无效"
        src_norm = os.path.normpath(src_full)
        dest_norm = os.path.normpath(dest_full)
        if src_norm == dest_norm:
            return False, "不能移动到自身"
        if os.path.isdir(src_full) and (dest_norm == src_norm or dest_norm.startswith(src_norm + os.sep)):
            return False, "不能移动到自身子目录"
        name = os.path.basename(src_full)
        dst_full = os.path.join(dest_full, name)
        if os.path.exists(dst_full):
            return False, "目标位置已存在同名项"
        if self._resolve(self._rel_of(dst_full)) is None:
            return False, "目标路径非法"
        if os.path.dirname(src_norm) == dest_norm:
            return True, "已在目标目录"
        try:
            shutil.move(src_full, dst_full)
            return True, f"已移动到: {self._rel_of(dst_full)}"
        except Exception as e:
            return False, f"移动失败: {e}"

    def delete_item(self, path):
        if not self.workspace_dir:
            return False, "工作区未就绪"
        full = self._resolve(path)
        if full is None or path == "" or self._norm_rel(path) == "":
            return False, "路径无效"
        if not os.path.exists(full):
            return False, "路径不存在"
        if os.path.normpath(full) == os.path.normpath(self.workspace_dir):
            return False, "不能删除工作区根目录"
        try:
            if os.path.isdir(full):
                shutil.rmtree(full)
                return True, f"已删除目录: {self._norm_rel(path)}"
            os.remove(full)
            return True, f"已删除: {self._norm_rel(path)}"
        except Exception as e:
            return False, f"删除失败: {e}"

    def delete_file(self, filename):
        return self.delete_item(filename)

    def preview_file(self, filename, rev=None):
        """预览工作区文件，或指定修订中的历史版本（rev 非空时）。"""
        result_rev = None
        if rev is not None and str(rev).strip() != "":
            try:
                result_rev = int(rev)
            except (TypeError, ValueError):
                return {
                    "type": "unsupported",
                    "error": "无效修订号",
                    "name": os.path.basename((filename or "").replace("\\", "/")),
                    "ext": "",
                    "size": "",
                }
            fpath, err = self.resolve_file_for_read(filename, result_rev)
            if not fpath:
                return {
                    "type": "unsupported",
                    "error": err or "文件不存在",
                    "name": os.path.basename((filename or "").replace("\\", "/")),
                    "ext": "",
                    "size": "",
                    "rev": result_rev,
                }
        else:
            if not self.workspace_dir:
                return {"type": "unsupported", "error": "工作区未就绪", "name": "", "ext": "", "size": ""}
            fpath = self._resolve(filename)
            if fpath is None or not os.path.exists(fpath) or not os.path.isfile(fpath):
                return {
                    "type": "unsupported",
                    "error": "文件不存在",
                    "name": os.path.basename(filename or ""),
                    "ext": "",
                    "size": "",
                }
        ext = os.path.splitext(filename)[1].lower()
        base = os.path.basename(filename.replace("\\", "/"))
        try:
            size = format_file_size(os.path.getsize(fpath))
        except OSError:
            size = ""
        meta = {"name": base, "ext": ext, "size": size}
        if result_rev is not None:
            meta["rev"] = result_rev
        if ext == ".docx":
            return {"type": "docx", **meta}
        if ext == ".doc":
            return {
                "type": "unsupported",
                **meta,
                "hint": "旧版 .doc 暂不支持在线预览，请转换为 .docx，或下载后用 Word 打开。",
            }
        if ext == ".md":
            return {"type": "markdown", **meta}
        if ext in (".csv", ".xlsx", ".xls"):
            return {"type": "spreadsheet", **meta}
        if ext in (".txt", ".log"):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    return {"type": "text", "html": f.read(), "is_html": False, **meta}
            except Exception as e:
                return {
                    "type": "unsupported",
                    **meta,
                    "hint": f"读取失败: {e}",
                }
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"):
            return {"type": "image", **meta}
        return {
            "type": "unsupported",
            **meta,
            "ext": ext or "（无扩展名）",
            "hint": "该类型暂不支持在线预览，可下载到本地查看。",
        }

    def resolve_workspace_file(self, filename):
        """解析工作副本内文件路径，供下载用。"""
        if not self.workspace_dir:
            return None, "工作区未就绪"
        fpath = self._resolve(filename)
        if fpath is None or not os.path.isfile(fpath):
            return None, "文件不存在"
        return fpath, ""


    def get_pdf_for_file(self, filename):
        if not self.workspace_dir:
            return None
        fpath = self._resolve(filename)
        if fpath is None or not os.path.exists(fpath):
            return None
        return convert_docx_to_pdf(fpath)

    def edit_file(self, filename):
        if not self.workspace_dir:
            return False, "工作区未就绪"
        fpath = self._resolve(filename)
        if fpath is None or not os.path.exists(fpath) or not os.path.isfile(fpath):
            return False, "文件不存在"
        try:
            if IS_WINDOWS:
                os.startfile(fpath)
            else:
                opener = "open" if platform.system() == "Darwin" else "xdg-open"
                subprocess.Popen([opener, fpath])
            return True, f"已打开 {filename}"
        except Exception as e:
            return False, f"打开失败: {e}"

    # ---- SVN 风格：状态 / 提交 / 日志 / 回滚 ----

    def get_pending_changes(self):
        """类似 svn status（相对 HEAD）。"""
        if not self.repo:
            return {
                "ok": False, "added": [], "modified": [], "deleted": [],
                "unchanged": 0, "total_files": 0, "has_changes": False,
                "msg": "工作区未就绪",
            }
        self.repo.reload_ignore()
        return self.repo.get_status()

    def _format_change_summary(self, changes):
        return DocvmRepository.format_summary(changes)

    def save_version(self, note):
        """类似 svn commit -m：增量写入 objects，不再全库拷贝。"""
        if not self.repo:
            return False, "工作区未就绪"
        self.repo.reload_ignore()
        ok, msg = self.repo.commit(note)
        self._sync_repo_views()
        return ok, msg

    def get_history(self, path=None):
        """类似 svn log [PATH]。"""
        if not self.repo:
            return []
        if path is None or str(path).strip() in ("", ".", "/"):
            return self.repo.log()
        norm = self._norm_rel(path)
        if norm is None:
            return []
        if norm == "":
            return self.repo.log()
        return self.repo.filter_log_by_path(norm)

    def rollback(self, version_num):
        """类似 svn update -r N（覆盖工作副本）。"""
        if not self.repo:
            return False, "工作区未就绪"
        ok, msg = self.repo.checkout(int(version_num))
        self._sync_repo_views()
        return ok, msg

    def restore_path(self, version_num, rel_path):
        """类似 svn update -r N PATH：只恢复单个文件。"""
        if not self.repo:
            return False, "工作区未就绪"
        norm = self._norm_rel(rel_path)
        if norm is None or norm == "":
            return False, "路径非法"
        ok, msg = self.repo.restore_path(int(version_num), norm)
        if ok:
            self._sync_repo_views()
        return ok, msg

    def resolve_rev_file(self, version_num, rel_path):
        """类似 svn cat：解析修订中单文件的磁盘路径。"""
        if not self.repo:
            return None, None, "工作区未就绪"
        norm = self._norm_rel(rel_path)
        if norm is None or norm == "":
            return None, None, "路径非法"
        return self.repo.resolve_file_at(int(version_num), norm)

    def resolve_file_for_read(self, filename, rev=None):
        """解析工作区或指定修订中的文件绝对路径。"""
        if rev is not None and str(rev).strip() != "":
            try:
                ver = int(rev)
            except (TypeError, ValueError):
                return None, "无效修订号"
            fpath, _digest, err = self.resolve_rev_file(ver, filename)
            if not fpath:
                return None, err or "文件不存在"
            return fpath, ""
        return self.resolve_workspace_file(filename)

    def diff_file(self, rel_path, from_rev, to_rev):
        """类似 svn diff：对比两修订（或工作副本）中同一文件。"""
        if not self.repo:
            return {"ok": False, "msg": "工作区未就绪"}
        norm = self._norm_rel(rel_path)
        if norm is None or norm == "":
            return {"ok": False, "msg": "路径非法"}
        return self.repo.diff_file(norm, from_rev, to_rev)

    def prev_revision(self, version_num):
        if not self.repo:
            return None
        return self.repo.prev_revision(int(version_num))

    def get_workspace_info(self):
        if not self.work_dir or not self.repo:
            return {"selected": False}
        self._sync_repo_views()
        files = self.get_file_list()
        # 「正常」= 已提交；兼容旧统计字段名
        committed = sum(1 for f in files if f["status"] in ("已提交", "正常"))
        modified = sum(1 for f in files if f["status"] == "已修改")
        new = sum(1 for f in files if f["status"] == "新增")
        head = self.repo.history[-1]["version"] if self.repo.history else 0
        return {
            "selected": True,
            "dir": self.work_dir,
            "workspace_dir": self.workspace_dir,
            "backup_dir": self.backups_dir,
            "versions_dir": self.versions_dir,
            "ignore_file": self.repo.ignore_path,
            "head_revision": head,
            "total": len(files),
            "committed": committed,
            "modified": modified,
            "new": new,
            "has_docx": HAS_DOCX,
        }

    def list_directories(self):
        if not self.workspace_dir:
            return [{"path": "", "label": "/（根目录）"}]
        result = [{"path": "", "label": "/（根目录）"}]
        for root, dirs, _ in os.walk(self.workspace_dir):
            dirs[:] = sorted([d for d in dirs if not d.startswith(".")], key=str.lower)
            for d in dirs:
                full = os.path.join(root, d)
                rel = self._rel_of(full)
                result.append({"path": rel, "label": "/" + rel})
        return result

    def open_in_explorer(self, target, rel=""):
        """在资源管理器中打开：work / backup / versions / ignore / item"""
        path = None
        if target == "work":
            path = self.workspace_dir
        elif target == "backup":
            path = self.backups_dir
        elif target == "versions":
            path = self.versions_dir
        elif target == "ignore":
            path = self.repo.ignore_path if self.repo else None
        elif target == "item":
            if not self.workspace_dir:
                return False, "工作区未就绪"
            if rel in ("", None):
                path = self.workspace_dir
            else:
                path = self._resolve(rel)
        else:
            return False, "未知目标"

        if not path or not os.path.exists(path):
            return False, "路径不存在"
        try:
            path = os.path.normpath(path)
            if IS_WINDOWS:
                if os.path.isfile(path):
                    subprocess.Popen(["explorer", "/select,", path])
                else:
                    subprocess.Popen(["explorer", path])
            elif platform.system() == "Darwin":
                if os.path.isfile(path):
                    subprocess.Popen(["open", "-R", path])
                else:
                    subprocess.Popen(["open", path])
            else:
                open_path = path if os.path.isdir(path) else os.path.dirname(path)
                subprocess.Popen(["xdg-open", open_path])
            return True, "已在资源管理器中打开"
        except Exception as e:
            return False, f"打开失败: {e}"

    def delete_items(self, paths):
        if not self.workspace_dir:
            return False, "工作区未就绪"
        if not paths:
            return False, "未指定要删除的路径"
        cleaned = []
        for p in paths:
            norm = self._norm_rel(p)
            if norm is None or norm == "":
                continue
            cleaned.append(norm)
        cleaned = sorted(set(cleaned), key=lambda x: (x.count("/"), x))
        final = []
        for p in cleaned:
            if any(p.startswith(parent + "/") for parent in final):
                continue
            final.append(p)
        final.sort(key=lambda p: p.count("/"), reverse=True)
        ok_n = 0
        errors = []
        for p in final:
            ok, msg = self.delete_item(p)
            if ok:
                ok_n += 1
            else:
                errors.append(msg)
        if ok_n == 0:
            return False, errors[0] if errors else "删除失败"
        if errors:
            return True, f"已删除 {ok_n} 项，{len(errors)} 项失败"
        return True, f"已删除 {ok_n} 项"

    def move_items(self, paths, dest_dir):
        if not paths:
            return False, "未指定要移动的路径"
        cleaned = []
        for p in paths:
            norm = self._norm_rel(p)
            if norm is None or norm == "":
                continue
            cleaned.append(norm)
        cleaned = list(dict.fromkeys(cleaned))
        tops = []
        for p in sorted(cleaned, key=lambda x: x.count("/")):
            if any(p.startswith(parent + "/") for parent in tops):
                continue
            tops.append(p)
        ok_n = 0
        errors = []
        for p in tops:
            ok, msg = self.move_item(p, dest_dir)
            if ok:
                ok_n += 1
            else:
                errors.append(f"{p}: {msg}")
        if ok_n == 0:
            return False, errors[0] if errors else "移动失败"
        if errors:
            return True, f"已移动 {ok_n} 项，{len(errors)} 项失败"
        return True, f"已移动 {ok_n} 项"
