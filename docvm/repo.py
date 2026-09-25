# -*- coding: utf-8 -*-
"""
DocVM 本地版本库引擎（SVN 风格简化实现）
========================================

设计目标
--------
对标 SVN 的使用体验，但针对「本地单机文档管理」做了大幅简化：

1. **工作副本（Working Copy）**
   - 对应 SVN 的 working copy，路径为：``{app}/.docvm/workspace``
   - 用户只在这里编辑 / 上传文件；状态检测、提交都基于此目录。

2. **增量提交（Commit）而不是全库拷贝**
   - 旧实现：每次提交复制工作区全部文件 + 再打一份全量 zip（大文件会拖垮每次提交）。
   - 新实现：内容寻址存储（Content-Addressable Storage, CAS）
     - 相同内容只存一份 blob（按 MD5）
     - 每次修订只记录「路径 → 内容哈希」的树快照，以及相对上一版的 A/M/D
     - 未改动的大文件不会被再次复制

3. **修订（Revision）**
   - 每次成功提交产生一个递增修订号（1, 2, 3...），类似 SVN revision。
   - 元数据保存在 ``.docvm/revs/{N}.json``，索引在 ``history.json``。

4. **忽略规则（Ignore）**
   - ``.docvm/ignore.txt``，语法类似简化版 gitignore / svn:ignore
   - 大工具、安装包等应写入忽略，避免进入版本库。

目录结构
--------
::

    {软件目录}/.docvm/
        workspace/          # 工作副本（用户文件）
        objects/ab/cdef...  # 内容库：文件内容按 hash 存放，去重
        revs/1.json         # 修订元数据（树快照 + 变更清单 + 提交说明）
        history.json        # 修订索引（便于列表展示）
        ignore.txt          # 忽略规则
        backups/            # 仅打包「本修订变更文件」的小 zip（可选追溯）

兼容说明
--------
- 仍可读旧版 ``versions/v00x_*/meta.json``（全量拷贝时代）的 ``files`` 哈希，
  以便升级后继续正确显示「已修改 / 已提交」。
- 新提交一律走 CAS；回滚时若修订带 ``tree`` 则从 objects 检出，
  否则回退到旧目录拷贝逻辑。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import zipfile
from datetime import datetime


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def file_md5(filepath: str) -> str:
    """计算文件 MD5（分块读取，避免大文件占满内存）。"""
    h = hashlib.md5()
    try:
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


# ---------------------------------------------------------------------------
# 忽略规则
# ---------------------------------------------------------------------------

DEFAULT_IGNORE = """\
# DocVM 忽略规则（类似 svn:ignore / .gitignore）
# - 以 # 开头为注释
# - 支持 * ? 通配；以 / 结尾表示只匹配目录名
# - 匹配相对工作区的路径，或仅文件名
#
# 建议：大工具、安装包、压缩包不要进入版本库，否则即使增量提交
# 首次加入时仍会写入 objects（只存一份），但工作区会显得很乱。

*.exe
*.msi
*.dmg
*.iso
*.zip
*.rar
*.7z
*.tar
*.gz
tools/
Tool/
Tools/
"""


class IgnoreMatcher:
    """
    简化版忽略匹配器。

    规则加载自 ignore.txt；空文件时使用 DEFAULT_IGNORE。
    """

    def __init__(self, patterns=None):
        self.patterns = list(patterns or [])

    @classmethod
    def load(cls, path: str) -> "IgnoreMatcher":
        if not os.path.isfile(path):
            # 首次创建默认规则，方便用户直接改
            try:
                ensure_dir(os.path.dirname(path))
                with open(path, "w", encoding="utf-8") as f:
                    f.write(DEFAULT_IGNORE)
            except OSError:
                pass
            return cls(_parse_ignore_text(DEFAULT_IGNORE))
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = DEFAULT_IGNORE
        return cls(_parse_ignore_text(text))

    def is_ignored(self, rel_path: str) -> bool:
        """rel_path 使用正斜杠，如 docs/a.docx 或 tools/x.exe"""
        if not rel_path:
            return False
        rel = rel_path.replace("\\", "/").strip("/")
        name = rel.split("/")[-1]
        parts = rel.split("/")
        for pat in self.patterns:
            # 目录规则：tools/  → 匹配 tools 及其子路径
            if pat.endswith("/"):
                dir_pat = pat.rstrip("/")
                if fnmatch.fnmatch(parts[0], dir_pat):
                    return True
                # 也允许匹配中间目录名
                if any(fnmatch.fnmatch(p, dir_pat) for p in parts[:-1]):
                    return True
                continue
            # 全路径或仅文件名
            if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat):
                return True
            # 任意层级：**/pat 简化为对每一段匹配
            if "/" not in pat and any(fnmatch.fnmatch(p, pat) for p in parts):
                return True
        return False


def _parse_ignore_text(text: str):
    patterns = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line.replace("\\", "/"))
    return patterns


# ---------------------------------------------------------------------------
# 内容寻址对象库
# ---------------------------------------------------------------------------

class ObjectStore:
    """
    内容寻址存储。

    布局：``objects/{hash[0:2]}/{hash[2:]}``
    同一内容多次提交只占用一份磁盘空间（引用计数语义由修订树保证，
    当前不做 GC；未引用 blob 可后续再加垃圾回收）。
    """

    def __init__(self, root: str):
        self.root = root
        ensure_dir(root)

    def _path_for(self, digest: str) -> str:
        return os.path.join(self.root, digest[:2], digest[2:])

    def exists(self, digest: str) -> bool:
        return bool(digest) and os.path.isfile(self._path_for(digest))

    def path_for(self, digest: str) -> str:
        return self._path_for(digest)

    def read_bytes(self, digest: str) -> bytes:
        src = self._path_for(digest)
        if not os.path.isfile(src):
            raise FileNotFoundError(f"对象不存在: {digest}")
        with open(src, "rb") as f:
            return f.read()

    def put_file(self, src_path: str, digest: str = None) -> str:
        """
        将文件写入对象库。

        :param src_path: 源文件绝对路径
        :param digest: 若已知 MD5 可传入，避免重复计算
        :return: 内容哈希
        """
        digest = digest or file_md5(src_path)
        if not digest:
            raise IOError(f"无法读取文件: {src_path}")
        dst = self._path_for(digest)
        if os.path.isfile(dst):
            return digest  # 已存在，增量提交的关键：未改文件零拷贝
        ensure_dir(os.path.dirname(dst))
        # 先写临时文件再 rename，降低半写入风险
        tmp = dst + ".tmp"
        shutil.copy2(src_path, tmp)
        os.replace(tmp, dst)
        return digest

    def checkout(self, digest: str, dst_path: str) -> None:
        """从对象库检出到目标路径。"""
        src = self._path_for(digest)
        if not os.path.isfile(src):
            raise FileNotFoundError(f"对象不存在: {digest}")
        ensure_dir(os.path.dirname(dst_path))
        shutil.copy2(src, dst_path)


# ---------------------------------------------------------------------------
# 版本库主体
# ---------------------------------------------------------------------------

class DocvmRepository:
    """
    SVN 风格本地版本库。

    对外语义（与 UI/API 对齐）：
    - ``status()`` / 工作区对比 HEAD  → 类似 ``svn status``
    - ``commit(message)``             → 类似 ``svn commit -m``
    - ``log()``                       → 类似 ``svn log``
    - ``checkout(revision)``          → 类似 ``svn update -r N``（此处置换工作副本）
    """

    def __init__(self, app_data_dir: str, workspace_dir: str):
        """
        :param app_data_dir: ``.docvm`` 根目录
        :param workspace_dir: 工作副本目录
        """
        self.app_data_dir = app_data_dir
        self.workspace_dir = workspace_dir
        self.objects_dir = os.path.join(app_data_dir, "objects")
        self.revs_dir = os.path.join(app_data_dir, "revs")
        self.backups_dir = os.path.join(app_data_dir, "backups")
        self.history_path = os.path.join(app_data_dir, "history.json")
        self.ignore_path = os.path.join(app_data_dir, "ignore.txt")
        # 旧版全量副本目录（只读兼容）
        self.legacy_versions_dir = os.path.join(app_data_dir, "versions")

        ensure_dir(self.workspace_dir)
        ensure_dir(self.objects_dir)
        ensure_dir(self.revs_dir)
        ensure_dir(self.backups_dir)

        self.store = ObjectStore(self.objects_dir)
        self.ignore = IgnoreMatcher.load(self.ignore_path)
        self.history = []  # 修订索引列表（按版本升序）
        self.head_tree = {}  # HEAD：path -> md5

        self._load_history()
        self._load_head_tree()

    # ---- 历史索引 ----

    def _load_history(self):
        try:
            if os.path.isfile(self.history_path):
                with open(self.history_path, "r", encoding="utf-8") as f:
                    self.history = json.load(f)
            else:
                self.history = []
        except (OSError, ValueError, TypeError):
            self.history = []

    def _save_history(self):
        try:
            with open(self.history_path, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _rev_meta_path(self, version_num: int) -> str:
        return os.path.join(self.revs_dir, f"{version_num}.json")

    def _load_revision_meta(self, version_num: int) -> dict:
        path = self._rev_meta_path(version_num)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _load_head_tree(self):
        """
        加载 HEAD 树快照（上一成功提交的 path→hash）。

        优先级：
        1. 最新修订的 ``tree`` 字段（CAS 格式）
        2. 最新修订 / 旧 meta 的 ``files`` 字段
        3. 旧版 versions/vxxx 目录扫描
        """
        self.head_tree = {}
        if not self.history:
            return
        last = self.history[-1]
        ver = last.get("version")
        # 新格式
        if ver is not None:
            meta = self._load_revision_meta(int(ver))
            if meta.get("tree"):
                self.head_tree = dict(meta["tree"])
                return
            if meta.get("files"):
                self.head_tree = dict(meta["files"])
                return
        # 旧格式：versions/v00x_*/meta.json
        folder = last.get("folder")
        if folder and os.path.isdir(self.legacy_versions_dir):
            meta_path = os.path.join(self.legacy_versions_dir, folder, "meta.json")
            try:
                if os.path.isfile(meta_path):
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    self.head_tree = dict(meta.get("files") or meta.get("tree") or {})
                    return
            except (OSError, ValueError, TypeError):
                pass

    def reload_ignore(self):
        """外部修改 ignore.txt 后可调用。"""
        self.ignore = IgnoreMatcher.load(self.ignore_path)

    # ---- 工作区扫描 ----

    def iter_workspace_files(self, include_ignored: bool = False):
        """
        列出工作副本中的文件相对路径。

        :param include_ignored: True 时包含被忽略文件（用于 UI 展示「忽略」状态）
        """
        if not os.path.isdir(self.workspace_dir):
            return []
        result = []
        for root, dirs, filenames in os.walk(self.workspace_dir):
            # 不进入隐藏目录
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            # 若整个子目录被忽略，可剪枝（目录规则以 / 结尾）
            pruned = []
            for d in dirs:
                rel_dir = os.path.relpath(os.path.join(root, d), self.workspace_dir)
                rel_dir = rel_dir.replace("\\", "/")
                if not include_ignored and self.ignore.is_ignored(rel_dir + "/"):
                    continue
                pruned.append(d)
            dirs[:] = pruned

            for fname in sorted(filenames, key=str.lower):
                if fname.startswith("."):
                    continue
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, self.workspace_dir).replace("\\", "/")
                ignored = self.ignore.is_ignored(rel)
                if ignored and not include_ignored:
                    continue
                result.append(rel)
        return result

    def scan_working_tree(self) -> dict:
        """
        扫描工作副本，返回 ``{rel_path: md5}``（已排除忽略文件）。
        """
        tree = {}
        for rel in self.iter_workspace_files(include_ignored=False):
            full = os.path.join(self.workspace_dir, rel.replace("/", os.sep))
            digest = file_md5(full)
            if digest:
                tree[rel] = digest
        return tree

    def diff_trees(self, old_tree: dict, new_tree: dict) -> dict:
        """比较两棵树，返回 added / modified / deleted 列表。"""
        old_tree = old_tree or {}
        new_tree = new_tree or {}
        added, modified, deleted = [], [], []
        for path, h in new_tree.items():
            if path not in old_tree:
                added.append(path)
            elif old_tree[path] != h:
                modified.append(path)
        for path in old_tree:
            if path not in new_tree:
                deleted.append(path)
        added.sort(key=str.lower)
        modified.sort(key=str.lower)
        deleted.sort(key=str.lower)
        return {"added": added, "modified": modified, "deleted": deleted}

    def get_status(self) -> dict:
        """
        类似 ``svn status``：对比工作副本与 HEAD。

        返回结构供提交对话框与统计使用。
        """
        current = self.scan_working_tree()
        prev = self.head_tree or {}
        changes = self.diff_trees(prev, current)
        unchanged = len(current) - len(changes["added"]) - len(changes["modified"])
        return {
            "ok": True,
            "added": changes["added"],
            "modified": changes["modified"],
            "deleted": changes["deleted"],
            "unchanged": max(0, unchanged),
            "total_files": len(current),
            "has_changes": bool(
                changes["added"] or changes["modified"] or changes["deleted"]
            ),
            "is_first": not bool(prev),
            "head_revision": self.history[-1]["version"] if self.history else 0,
            "msg": "ok",
        }

    def file_status(self, rel_path: str, filepath: str = None) -> tuple:
        """
        单个文件状态（用于文件树着色）。

        返回 ``(中文状态, 颜色)``：
        - 忽略 / 新增 / 已修改 / 正常(已提交)
        """
        rel = rel_path.replace("\\", "/")
        if self.ignore.is_ignored(rel):
            return "忽略", "#999999"
        if filepath is None:
            filepath = os.path.join(self.workspace_dir, rel.replace("/", os.sep))
        if not self.head_tree:
            return "新增", "#0066cc"
        if rel not in self.head_tree:
            return "新增", "#0066cc"
        digest = file_md5(filepath)
        if digest == self.head_tree[rel]:
            return "正常", "green"
        return "已修改", "#cc6600"

    @staticmethod
    def format_summary(changes: dict) -> str:
        parts = []
        if changes.get("added"):
            parts.append(f"新增{len(changes['added'])}")
        if changes.get("modified"):
            parts.append(f"修改{len(changes['modified'])}")
        if changes.get("deleted"):
            parts.append(f"删除{len(changes['deleted'])}")
        return "，".join(parts) if parts else "无变更"

    # ---- 提交 ----

    def commit(self, message: str) -> tuple:
        """
        增量提交（类似 svn commit）。

        步骤：
        1. 扫描工作区并与 HEAD diff
        2. 仅对 A/M 文件把内容写入 objects（已存在则跳过）
        3. 写入修订元数据（完整 tree 快照 + changes + message）
        4. 可选：把本修订变更文件打成小 zip 放到 backups/
        5. 更新 history 索引与内存中的 head_tree

        :return: (ok: bool, msg: str)
        """
        status = self.get_status()
        if not status["total_files"] and not status["deleted"]:
            return False, "工作区没有可版本管理的文件（可能都被 ignore 了）"
        if not status["has_changes"]:
            return False, "没有变更需要提交（工作区与 HEAD 一致）"

        message = (message or "").strip() or "（无提交说明）"
        current_tree = self.scan_working_tree()
        changes = {
            "added": status["added"],
            "modified": status["modified"],
            "deleted": status["deleted"],
        }
        summary = self.format_summary(changes)
        version_num = (self.history[-1]["version"] + 1) if self.history else 1
        ts = datetime.now()
        time_str = ts.strftime("%Y-%m-%d %H:%M:%S")

        try:
            # 1) 仅入库有变更的内容（未改文件复用已有 blob）
            touched = set(changes["added"]) | set(changes["modified"])
            for rel in touched:
                src = os.path.join(self.workspace_dir, rel.replace("/", os.sep))
                digest = current_tree[rel]
                self.store.put_file(src, digest)

            # HEAD 中未改路径的 hash 已在 objects 里；新 tree 直接用 current_tree
            # 保险：确保 HEAD 未改文件的对象也存在（兼容从旧库升级后 objects 为空的情况）
            for rel, digest in current_tree.items():
                if rel in touched:
                    continue
                if not self.store.exists(digest):
                    src = os.path.join(self.workspace_dir, rel.replace("/", os.sep))
                    self.store.put_file(src, digest)

            meta = {
                "version": version_num,
                "time": time_str,
                "note": message,
                "message": message,  # SVN 语义别名
                "tree": current_tree,
                # 兼容旧 UI 字段名
                "files": current_tree,
                "changes": changes,
                "change_summary": summary,
                "storage": "cas",
                "file_count": len(current_tree),
            }
            rev_path = self._rev_meta_path(version_num)
            with open(rev_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)

            # 2) 变更文件小备份（不是全库）
            zip_name = None
            if touched:
                zip_name = f"r{version_num:04d}_changes.zip"
                zip_path = os.path.join(self.backups_dir, zip_name)
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for rel in sorted(touched):
                        src = os.path.join(self.workspace_dir, rel.replace("/", os.sep))
                        zf.write(src, rel)
                    zf.writestr("commit.json", json.dumps({
                        "version": version_num,
                        "message": message,
                        "time": time_str,
                        "changes": changes,
                    }, ensure_ascii=False, indent=2))

            entry = {
                "version": version_num,
                "time": time_str,
                "note": message,
                "file_count": len(current_tree),
                "changes": changes,
                "change_summary": summary,
                "storage": "cas",
                "backup": zip_name,
            }
            self.history.append(entry)
            self._save_history()
            self.head_tree = dict(current_tree)

            msg = f"r{version_num} 已提交（{summary}，跟踪 {len(current_tree)} 个文件"
            if zip_name:
                msg += f"，变更包 {zip_name}"
            msg += "）"
            return True, msg
        except Exception as e:
            return False, f"提交失败: {e}"

    # ---- 日志 / 回滚 ----

    def log(self) -> list:
        """类似 svn log：新→旧。补齐旧记录缺失的 changes。"""
        result = []
        for v in reversed(self.history):
            item = dict(v)
            ver = item.get("version")
            if ver is not None and ("changes" not in item or not item.get("change_summary")):
                meta = self._load_revision_meta(int(ver))
                if meta:
                    if "changes" in meta:
                        item["changes"] = meta["changes"]
                    item["change_summary"] = meta.get(
                        "change_summary", self.format_summary(meta.get("changes") or {})
                    )
                    item.setdefault("note", meta.get("note") or meta.get("message"))
            # 旧 folder 布局
            if "changes" not in item and item.get("folder"):
                meta_path = os.path.join(
                    self.legacy_versions_dir, item["folder"], "meta.json"
                )
                try:
                    if os.path.isfile(meta_path):
                        with open(meta_path, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                        if "changes" in meta:
                            item["changes"] = meta["changes"]
                            item["change_summary"] = meta.get(
                                "change_summary",
                                self.format_summary(meta["changes"]),
                            )
                except (OSError, ValueError, TypeError):
                    pass
            item.setdefault("change_summary", "—")
            if ver is not None:
                item["prev_revision"] = self.prev_revision(int(ver))
            result.append(item)
        return result

    @staticmethod
    def path_in_scope(file_path: str, scope: str) -> bool:
        """判断 file_path 是否属于 scope（精确文件或目录前缀）。"""
        scope = (scope or "").replace("\\", "/").strip("/")
        file_path = (file_path or "").replace("\\", "/").strip("/")
        if not scope:
            return True
        if file_path == scope:
            return True
        return file_path.startswith(scope + "/")

    def filter_log_by_path(self, scope_path: str) -> list:
        """
        类似 svn log PATH：只保留变更列表中命中该路径（或子路径）的修订。
        返回条目中的 changes 已裁剪为范围内的路径，并附 path_status（精确文件时）。
        """
        scope = (scope_path or "").replace("\\", "/").strip("/")
        if not scope:
            return self.log()
        result = []
        for item in self.log():
            changes = item.get("changes") or {}
            matched = {"added": [], "modified": [], "deleted": []}
            exact_status = None
            for key in ("added", "modified", "deleted"):
                for p in changes.get(key) or []:
                    if not self.path_in_scope(p, scope):
                        continue
                    matched[key].append(p)
                    if p == scope and exact_status is None:
                        exact_status = key
            if not (matched["added"] or matched["modified"] or matched["deleted"]):
                continue
            new_item = dict(item)
            new_item["changes"] = matched
            new_item["change_summary"] = self.format_summary(matched)
            new_item["scope_path"] = scope
            if exact_status:
                new_item["path_status"] = exact_status
            result.append(new_item)
        return result

    def restore_path(self, version_num: int, rel_path: str) -> tuple:
        """
        将工作副本中的单个文件恢复为指定修订中的内容（类似 svn update -r N PATH）。
        不触碰其他文件。若该修订的树中无此文件，返回错误。
        """
        rel = (rel_path or "").replace("\\", "/").strip("/")
        if not rel or ".." in rel.split("/"):
            return False, "路径非法"
        version_num = int(version_num)
        tree = self.get_tree_at(version_num)
        if rel not in tree:
            return False, f"修订 r{version_num} 中不存在文件: {rel}"
        digest = tree[rel]
        if not self.store.exists(digest):
            # 尝试旧目录
            entry = next((v for v in self.history if v.get("version") == version_num), None)
            if entry and entry.get("folder"):
                legacy = os.path.join(
                    self.legacy_versions_dir, entry["folder"], rel.replace("/", os.sep)
                )
                if os.path.isfile(legacy):
                    dst = os.path.join(self.workspace_dir, rel.replace("/", os.sep))
                    ensure_dir(os.path.dirname(dst))
                    shutil.copy2(legacy, dst)
                    return True, f"已将 {rel} 恢复为 r{version_num} 的内容"
            return False, f"修订 r{version_num} 的文件内容缺失: {rel}"
        dst = os.path.join(self.workspace_dir, rel.replace("/", os.sep))
        ensure_dir(os.path.dirname(dst))
        try:
            self.store.checkout(digest, dst)
            return True, f"已将 {rel} 恢复为 r{version_num} 的内容"
        except Exception as e:
            return False, f"恢复失败: {e}"

    def checkout(self, version_num: int) -> tuple:
        """
        将工作副本恢复到指定修订（类似 svn update -r N，会覆盖本地未提交修改）。

        CAS 修订：按 tree 从 objects 检出。
        旧全量修订：从 versions/vxxx 目录拷贝。
        """
        entry = None
        for v in self.history:
            if v.get("version") == version_num:
                entry = v
                break
        if not entry:
            return False, f"未找到修订 r{version_num}"

        try:
            meta = self._load_revision_meta(version_num)
            tree = meta.get("tree") or meta.get("files")

            # 清空工作副本
            self._clear_workspace()

            if tree and meta.get("storage") == "cas":
                for rel, digest in tree.items():
                    dst = os.path.join(self.workspace_dir, rel.replace("/", os.sep))
                    self.store.checkout(digest, dst)
                self.head_tree = dict(tree)
            elif tree and all(self.store.exists(h) for h in tree.values()):
                # 有 tree 且对象齐全（可能 storage 字段缺失）
                for rel, digest in tree.items():
                    dst = os.path.join(self.workspace_dir, rel.replace("/", os.sep))
                    self.store.checkout(digest, dst)
                self.head_tree = dict(tree)
            elif entry.get("folder"):
                # 旧全量目录
                vpath = os.path.join(self.legacy_versions_dir, entry["folder"])
                if not os.path.isdir(vpath):
                    return False, f"旧版本目录不存在: {entry['folder']}"
                for root, dirs, filenames in os.walk(vpath):
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    for fname in filenames:
                        if fname == "meta.json" and os.path.normpath(root) == os.path.normpath(vpath):
                            continue
                        if fname.startswith("."):
                            continue
                        src = os.path.join(root, fname)
                        rel = os.path.relpath(src, vpath).replace("\\", "/")
                        dst = os.path.join(self.workspace_dir, rel.replace("/", os.sep))
                        ensure_dir(os.path.dirname(dst))
                        shutil.copy2(src, dst)
                self.head_tree = dict(tree or {})
                if not self.head_tree:
                    # 从刚检出的工作区重建
                    self.head_tree = self.scan_working_tree()
            else:
                return False, f"修订 r{version_num} 无法检出（缺少 tree/对象）"

            return True, f"已恢复到修订 r{version_num}"
        except Exception as e:
            return False, f"恢复失败: {e}"

    def _clear_workspace(self):
        if not os.path.isdir(self.workspace_dir):
            return
        for name in os.listdir(self.workspace_dir):
            path = os.path.join(self.workspace_dir, name)
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)

    # ---- 单文件导出 / 内容对比（类似 svn cat / svn diff） ----

    def get_tree_at(self, version_num: int) -> dict:
        """返回指定修订的 path→hash 树；不存在则空 dict。"""
        meta = self._load_revision_meta(int(version_num))
        tree = meta.get("tree") or meta.get("files")
        if tree:
            return dict(tree)
        # 旧全量目录：按文件现算 md5（慢，但兼容）
        entry = next((v for v in self.history if v.get("version") == version_num), None)
        if not entry or not entry.get("folder"):
            return {}
        vpath = os.path.join(self.legacy_versions_dir, entry["folder"])
        if not os.path.isdir(vpath):
            return {}
        tree = {}
        for root, dirs, filenames in os.walk(vpath):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in filenames:
                if fname.startswith("."):
                    continue
                if fname == "meta.json" and os.path.normpath(root) == os.path.normpath(vpath):
                    continue
                src = os.path.join(root, fname)
                rel = os.path.relpath(src, vpath).replace("\\", "/")
                digest = file_md5(src)
                if digest:
                    tree[rel] = digest
        return tree

    def prev_revision(self, version_num: int) -> int | None:
        """历史中比 version_num 更早的最近一个修订号。"""
        earlier = [v["version"] for v in self.history if v.get("version", 0) < version_num]
        return max(earlier) if earlier else None

    def resolve_file_at(self, version_num: int, rel_path: str) -> tuple:
        """
        解析修订中某文件的磁盘路径。

        :return: (abs_path|None, digest|None, err_msg)
        """
        rel = (rel_path or "").replace("\\", "/").strip("/")
        if not rel or ".." in rel.split("/"):
            return None, None, "路径非法"

        tree = self.get_tree_at(version_num)
        if rel in tree:
            digest = tree[rel]
            if self.store.exists(digest):
                return self.store.path_for(digest), digest, ""
            # CAS 缺失时尝试旧目录
        entry = next((v for v in self.history if v.get("version") == version_num), None)
        if entry and entry.get("folder"):
            legacy = os.path.join(
                self.legacy_versions_dir, entry["folder"], rel.replace("/", os.sep)
            )
            if os.path.isfile(legacy):
                return legacy, tree.get(rel), ""
        if rel in tree:
            return None, tree[rel], f"对象内容缺失: {rel}"
        return None, None, f"修订 r{version_num} 中不存在文件: {rel}"

    def cat_file(self, version_num: int, rel_path: str) -> tuple:
        """类似 svn cat：返回 (ok, bytes_or_None, err, digest)。"""
        path, digest, err = self.resolve_file_at(version_num, rel_path)
        if not path:
            return False, None, err, digest
        try:
            with open(path, "rb") as f:
                return True, f.read(), "", digest
        except OSError as e:
            return False, None, str(e), digest

    def _text_from_bytes(self, data: bytes, rel_path: str) -> tuple:
        """
        尽量抽出可对比的纯文本。
        返回 (text|None, kind)  kind: text / binary / empty
        """
        if data is None:
            return None, "empty"
        if not data:
            return "", "empty"
        ext = os.path.splitext(rel_path)[1].lower()
        if ext in (".txt", ".md", ".log", ".json", ".csv", ".py", ".js", ".css", ".html", ".xml"):
            for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
                try:
                    return data.decode(enc), "text"
                except UnicodeDecodeError:
                    continue
            return data.decode("utf-8", errors="replace"), "text"
        if ext == ".docx":
            # 临时落盘再走 mammoth / python-docx
            import tempfile
            from .preview import extract_docx_html

            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(suffix=".docx")
                os.close(fd)
                with open(tmp, "wb") as f:
                    f.write(data)
                html, is_html = extract_docx_html(tmp)
                # 粗暴去标签做文本对比
                if is_html:
                    import re

                    text = re.sub(r"<[^>]+>", "\n", html)
                    text = re.sub(r"\n{3,}", "\n\n", text).strip()
                    return text, "text"
                return html, "text"
            except Exception:
                return None, "binary"
            finally:
                if tmp and os.path.isfile(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        # 嗅探是否像文本
        sample = data[:4096]
        if b"\x00" in sample:
            return None, "binary"
        try:
            return data.decode("utf-8"), "text"
        except UnicodeDecodeError:
            try:
                return data.decode("gbk"), "text"
            except UnicodeDecodeError:
                return None, "binary"

    def diff_file(self, rel_path: str, from_rev, to_rev) -> dict:
        """
        对比同一路径在两个修订（或工作副本）之间的内容。

        from_rev / to_rev: 整数修订号，或 ``\"working\"`` / ``\"head\"``。
        """
        rel = (rel_path or "").replace("\\", "/").strip("/")
        if not rel:
            return {"ok": False, "msg": "缺少文件路径"}

        def load_side(rev):
            if rev in ("working", "wc", "work"):
                full = os.path.join(self.workspace_dir, rel.replace("/", os.sep))
                if not os.path.isfile(full):
                    return None, None, "工作副本中不存在", "missing"
                try:
                    with open(full, "rb") as f:
                        data = f.read()
                except OSError as e:
                    return None, None, str(e), "error"
                return data, "working", "", "present"
            if rev in ("head", "HEAD"):
                if not self.history:
                    return None, None, "尚无 HEAD", "missing"
                rev = self.history[-1]["version"]
            try:
                rev_n = int(rev)
            except (TypeError, ValueError):
                return None, None, f"无效修订: {rev}", "error"
            ok, data, err, digest = self.cat_file(rev_n, rel)
            if not ok:
                return None, f"r{rev_n}", err, "missing"
            return data, f"r{rev_n}", "", "present"

        left_data, left_label, left_err, left_st = load_side(from_rev)
        right_data, right_label, right_err, right_st = load_side(to_rev)

        if left_st == "error":
            return {"ok": False, "msg": left_err}
        if right_st == "error":
            return {"ok": False, "msg": right_err}
        if left_st == "missing" and right_st == "missing":
            return {"ok": False, "msg": "两侧均不存在该文件"}

        left_text, left_kind = self._text_from_bytes(left_data, rel) if left_data is not None else ("", "empty")
        right_text, right_kind = self._text_from_bytes(right_data, rel) if right_data is not None else ("", "empty")

        result = {
            "ok": True,
            "path": rel,
            "from_label": left_label or str(from_rev),
            "to_label": right_label or str(to_rev),
            "from_missing": left_st == "missing",
            "to_missing": right_st == "missing",
            "from_size": len(left_data) if left_data is not None else 0,
            "to_size": len(right_data) if right_data is not None else 0,
            "identical": left_data == right_data if left_data is not None and right_data is not None else False,
        }

        if left_kind == "binary" or right_kind == "binary":
            result["mode"] = "binary"
            result["msg"] = "二进制文件，无法做文本对比；可分别下载两端版本"
            result["unified"] = ""
            return result

        import difflib

        left_lines = (left_text or "").splitlines()
        right_lines = (right_text or "").splitlines()
        if left_st == "missing":
            left_lines = []
        if right_st == "missing":
            right_lines = []

        if left_lines == right_lines:
            result["mode"] = "text"
            result["identical"] = True
            result["unified"] = ""
            result["msg"] = "内容相同（文本层面）"
            return result

        udiff = list(
            difflib.unified_diff(
                left_lines,
                right_lines,
                fromfile=f"{rel} ({result['from_label']})",
                tofile=f"{rel} ({result['to_label']})",
                lineterm="",
                n=3,
            )
        )
        result["mode"] = "text"
        result["unified"] = "\n".join(udiff)
        result["stats"] = {
            "added": sum(1 for L in udiff if L.startswith("+") and not L.startswith("+++")),
            "removed": sum(1 for L in udiff if L.startswith("-") and not L.startswith("---")),
        }
        result["msg"] = "ok"
        return result
