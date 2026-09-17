"""文档控制器：持有文档 + 撤销栈，统一发出变更通知。

所有会改变文档的操作都通过这里的方法进行，内部包装成 ``QUndoCommand``，
保证「撤销/重做」与「界面刷新」不会遗漏。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QUndoCommand, QUndoStack

from ..core.document import Document, DocumentObject, PageSpec
from ..core.geometry import AffineTransform
from ..core.reference import ReferenceItem
from ..project import apply_document_data, doc_to_data

#: 撤销历史日志的最大条数（超出丢弃最旧的快照，控制项目文件体积）
JOURNAL_LIMIT = 60


# ---------------------------------------------------------------------------
# 对象状态快照（用于「内容/扰动编辑」的撤销）
# ---------------------------------------------------------------------------
@dataclass
class _ObjectState:
    source: Any
    strokes: list
    name: str
    meta: dict


def snapshot_object(obj: DocumentObject) -> _ObjectState:
    return _ObjectState(
        source=obj.source.clone(),
        strokes=[s.clone() for s in obj.local_strokes],
        name=obj.name,
        meta=copy.copy(obj.meta),
    )


def restore_object(obj: DocumentObject, st: _ObjectState) -> None:
    obj.source = st.source.clone()
    obj.local_strokes = [s.clone() for s in st.strokes]
    obj.name = st.name
    obj.meta = dict(st.meta)


class _DocCommand(QUndoCommand):
    """所有文档命令的基类，持有控制器引用以便在 redo/undo 后发通知。"""

    def __init__(self, controller: "DocumentController", text: str) -> None:
        super().__init__(text)
        self._ctrl = controller

    def _notify(self) -> None:
        self._ctrl.notify()


class TransformCommand(_DocCommand):
    """对象的变换（移动/缩放/旋转/组合）。

    适用于任何带有 ``transform`` 与 ``name`` 的模型（DocumentObject 或
    ReferenceItem），因此参考层也能复用同一套撤销机制。
    """

    def __init__(self, controller: "DocumentController", obj,
                 new_transform: AffineTransform, text: str = "变换对象") -> None:
        super().__init__(controller, f"{text}：{obj.name}")
        self._obj = obj
        self._old = obj.transform
        self._new = new_transform

    def redo(self) -> None:
        self._obj.transform = self._new
        self._notify()

    def undo(self) -> None:
        self._obj.transform = self._old
        self._notify()


class AddReferenceCommand(_DocCommand):
    def __init__(self, controller: "DocumentController", ref: ReferenceItem,
                 index: Optional[int] = None) -> None:
        super().__init__(controller, f"添加参考图：{ref.name}")
        self._ref = ref
        self._index = index

    def redo(self) -> None:
        self._ctrl.doc.add_reference(self._ref, self._index)
        self._notify()

    def undo(self) -> None:
        self._index = self._ctrl.doc.remove_reference(self._ref)
        self._notify()


class RemoveReferenceCommand(_DocCommand):
    def __init__(self, controller: "DocumentController", ref: ReferenceItem) -> None:
        super().__init__(controller, f"删除参考图：{ref.name}")
        self._ref = ref
        self._index = controller.doc.references.index(ref)

    def redo(self) -> None:
        self._index = self._ctrl.doc.remove_reference(self._ref)
        self._notify()

    def undo(self) -> None:
        self._ctrl.doc.add_reference(self._ref, self._index)
        self._notify()


class ReferenceEditCommand(_DocCommand):
    """参考图的属性/几何修改（可见性、锁定、透明度、尺寸/变换）。"""

    def __init__(self, controller: "DocumentController", ref: ReferenceItem,
                 text: str, changes: dict) -> None:
        super().__init__(controller, f"{text}：{ref.name}")
        self._ref = ref
        self._changes = dict(changes)
        self._old = {k: getattr(ref, k) for k in self._changes}

    def redo(self) -> None:
        for k, v in self._changes.items():
            setattr(self._ref, k, v)
        self._notify()

    def undo(self) -> None:
        for k, v in self._old.items():
            setattr(self._ref, k, v)
        self._notify()


class ObjectStateCommand(_DocCommand):
    """对象「源内容 + 笔画 + 名称」的整体替换（改文字/换字体/调扰动）。

    命令创建时对象**已经**是 ``new`` 状态，因此 :meth:`redo` 只是幂等地写回
    ``new``；撤销则恢复 ``old``。这样上层可以照常调用既有的重生成函数，
    再把前后快照交给控制器入栈。

    支持一次命令覆盖多个对象；``merge_key`` 非空时同键的连续命令会合并为
    一条（用于滑杆连续拖动，避免一步撤销只退一点点）。
    """

    _CMD_ID = 0x5753_0001

    def __init__(self, controller: "DocumentController",
                 entries: list[tuple[DocumentObject, _ObjectState, _ObjectState]],
                 text: str, merge_key: Optional[str] = None) -> None:
        super().__init__(controller, text)
        self._entries = list(entries)
        self._merge_key = merge_key

    def id(self) -> int:
        return self._CMD_ID if self._merge_key else -1

    def mergeWith(self, other: QUndoCommand) -> bool:  # noqa: N802
        if not isinstance(other, ObjectStateCommand):
            return False
        if self._merge_key is None or other._merge_key != self._merge_key:
            return False
        if [id(o) for o, _, _ in self._entries] != \
                [id(o) for o, _, _ in other._entries]:
            return False
        # 保留最初的 old，采用最新的 new
        self._entries = [(o, old, new2) for (o, old, _), (_, _, new2)
                         in zip(self._entries, other._entries)]
        return True

    def redo(self) -> None:
        for obj, _, new in self._entries:
            restore_object(obj, new)
        self._notify()

    def undo(self) -> None:
        for obj, old, _ in self._entries:
            restore_object(obj, old)
        self._notify()


class ObjectFlagCommand(_DocCommand):
    """对象的可见性/锁定等布尔属性（合并为一条命令可同时改多项）。"""

    def __init__(self, controller: "DocumentController", obj: DocumentObject,
                 text: str, changes: dict) -> None:
        super().__init__(controller, f"{text}：{obj.name}")
        self._obj = obj
        self._changes = dict(changes)
        self._old = {k: getattr(obj, k) for k in self._changes}

    def redo(self) -> None:
        for k, v in self._changes.items():
            setattr(self._obj, k, v)
        self._notify()

    def undo(self) -> None:
        for k, v in self._old.items():
            setattr(self._obj, k, v)
        self._notify()


class PageCommand(_DocCommand):
    """整页参数（尺寸/边距/预设名）替换。"""

    def __init__(self, controller: "DocumentController",
                 old: PageSpec, new: PageSpec, text: str = "页面设置") -> None:
        super().__init__(controller, text)
        self._old = old
        self._new = new

    def _apply(self, p: PageSpec) -> None:
        page = self._ctrl.doc.page
        page.width = p.width
        page.height = p.height
        page.margin = p.margin
        page.preset_name = p.preset_name
        self._notify()

    def redo(self) -> None:
        self._apply(self._new)

    def undo(self) -> None:
        self._apply(self._old)


class AddObjectCommand(_DocCommand):
    def __init__(self, controller: "DocumentController", obj: DocumentObject,
                 index: Optional[int] = None) -> None:
        super().__init__(controller, f"添加对象：{obj.name}")
        self._obj = obj
        self._index = index

    def redo(self) -> None:
        self._ctrl.doc.add(self._obj, self._index)
        self._notify()

    def undo(self) -> None:
        self._index = self._ctrl.doc.remove(self._obj)
        self._notify()


class RemoveObjectCommand(_DocCommand):
    def __init__(self, controller: "DocumentController", obj: DocumentObject) -> None:
        super().__init__(controller, f"删除对象：{obj.name}")
        self._obj = obj
        self._index = controller.doc.index_of(obj)

    def redo(self) -> None:
        self._index = self._ctrl.doc.remove(self._obj)
        self._notify()

    def undo(self) -> None:
        self._ctrl.doc.add(self._obj, self._index)
        self._notify()


class MoveZCommand(_DocCommand):
    """调整图层顺序。"""

    def __init__(self, controller: "DocumentController", obj: DocumentObject, delta: int) -> None:
        super().__init__(controller, f"{'上移' if delta > 0 else '下移'}：{obj.name}")
        self._obj = obj
        self._delta = delta

    def redo(self) -> None:
        self._ctrl.doc.move_z(self._obj, self._delta)
        self._notify()

    def undo(self) -> None:
        self._ctrl.doc.move_z(self._obj, -self._delta)
        self._notify()


class RestoreSnapshotCommand(_DocCommand):
    """把整份文档恢复到快照（打开项目时重建撤销历史用）。"""

    def __init__(self, controller: "DocumentController",
                 before: dict, after: dict, text: str) -> None:
        super().__init__(controller, text)
        self._before = before
        self._after = after

    def redo(self) -> None:
        apply_document_data(self._ctrl.doc, self._after)
        self._notify()

    def undo(self) -> None:
        apply_document_data(self._ctrl.doc, self._before)
        self._notify()


class JournalingUndoStack(QUndoStack):
    """带文档快照日志的撤销栈。

    每次 ``push`` 前记录执行前的文档快照（连同命令文字），供项目文件保存/
    恢复撤销历史——关闭程序再打开后仍能逐步回撤。日志与栈条目一一对应
    （合并命令在重建后会拆回多步，粒度略有差异，不影响正确性）。
    """

    def __init__(self, snapshot_fn, parent=None) -> None:
        super().__init__(parent)
        self._snapshot_fn = snapshot_fn
        self._journal: list[dict] = []
        self._suspended = False      # 历史重建（重放）期间不再记日志
        self._dropped = 0            # 日志头部被裁掉的条数（对齐栈下标用）

    def push(self, cmd) -> None:  # noqa: N802
        if not self._suspended and self._snapshot_fn is not None:
            try:
                self._journal.append(
                    {"text": cmd.text(), "doc": self._snapshot_fn()})
            except Exception:
                pass                  # 日志失败不影响正常撤销
            while len(self._journal) > JOURNAL_LIMIT:
                self._journal.pop(0)
                self._dropped += 1
        super().push(cmd)
        # 撤销后重新 push：栈会截掉 redo 分支，日志按对齐偏移同步截断
        keep = self.index() - self._dropped
        if 0 <= keep < len(self._journal):
            del self._journal[keep:]

    def clear(self) -> None:  # noqa: N802
        self._journal.clear()
        self._dropped = 0
        super().clear()

    def journal_upto(self, index: int) -> list[dict]:
        """栈中前 ``index`` 条命令对应的日志（不含已撤销的 redo 分支）。"""
        return list(self._journal[:max(0, index - self._dropped)])

    @property
    def journal(self) -> list[dict]:
        """当前日志（每项 = 一条命令执行前的文档快照 + 命令文字）。"""
        return list(self._journal)


class DocumentController(QObject):
    """文档 + 撤销栈 + 变更信号。"""

    documentChanged = Signal()   # 文档内容/几何发生变化，画布需重建
    documentReplaced = Signal()  # 整个文档被替换，需完全重建
    selectionChanged = Signal()

    def __init__(self, doc: Optional[Document] = None, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._doc = doc if doc is not None else Document()
        self.undo_stack = JournalingUndoStack(self._snapshot_doc, self)
        self.undo_stack.setUndoLimit(200)
        # 双击对象时的回调（由主窗口设置）：on_object_activated(obj)
        self.on_object_activated = None
        # 双击参考图时的回调：on_reference_activated(ref)
        self.on_reference_activated = None

    # -- 文档访问 -----------------------------------------------------------
    @property
    def doc(self) -> Document:
        return self._doc

    def set_document(self, doc: Document) -> None:
        self._doc = doc
        self.undo_stack.clear()
        self.documentReplaced.emit()

    def _snapshot_doc(self) -> dict:
        """当前文档的快照（撤销历史日志用）。"""
        return doc_to_data(self._doc)

    def saved_history(self) -> list[dict]:
        """可保存的撤销历史日志（不含已撤销的 redo 分支）。"""
        return self.undo_stack.journal_upto(self.undo_stack.index())

    def rebuild_history(self, entries: list[dict]) -> None:
        """按保存的快照日志重建撤销历史（当前文档内容 = 最终状态）。

        日志第 k 项是第 k 条命令执行**前**的文档快照：把文档先回到日志[0]，
        再逐条 push 快照替换命令推进到当前状态——push 的重放过程即恢复历史，
        完成后与保存时的文档一致、撤销链可用。期间屏蔽信号（界面只重建一次）。
        """
        entries = [e for e in (entries or [])
                   if isinstance(e, dict) and e.get("doc")]
        self.undo_stack.clear()
        if not entries:
            return
        final = doc_to_data(self._doc)
        stack = self.undo_stack
        stack._suspended = True
        self.blockSignals(True)
        ok = True
        try:
            apply_document_data(self._doc, entries[0]["doc"])
            for i, e in enumerate(entries):
                after = entries[i + 1]["doc"] if i + 1 < len(entries) else final
                stack.push(RestoreSnapshotCommand(
                    self, e["doc"], after,
                    str(e.get("text") or f"步骤 {i + 1}")))
        except Exception:
            ok = False
        finally:
            self.blockSignals(False)
            stack._suspended = False
        if not ok:
            stack.clear()
        self.documentReplaced.emit()
        stack.setClean()

    def notify(self) -> None:
        self.documentChanged.emit()

    def notify_selection(self) -> None:
        self.selectionChanged.emit()

    # -- 高层操作（带撤销） -------------------------------------------------
    def add_object(self, obj: DocumentObject, index: Optional[int] = None) -> None:
        self.undo_stack.push(AddObjectCommand(self, obj, index))

    def remove_object(self, obj: DocumentObject) -> None:
        self.undo_stack.push(RemoveObjectCommand(self, obj))

    def remove_objects(self, objs: list[DocumentObject]) -> None:
        if not objs:
            return
        self.undo_stack.beginMacro(f"删除 {len(objs)} 个对象")
        for o in objs:
            self.undo_stack.push(RemoveObjectCommand(self, o))
        self.undo_stack.endMacro()

    def set_transform(self, obj: DocumentObject, transform: AffineTransform,
                      text: str = "变换对象") -> None:
        self.undo_stack.push(TransformCommand(self, obj, transform, text))

    def set_model_transform(self, model, transform: AffineTransform,
                            text: str = "变换对象") -> None:
        """对任意带 transform 的模型（对象或参考图）设置变换。"""
        self.undo_stack.push(TransformCommand(self, model, transform, text))

    def move_z(self, obj: DocumentObject, delta: int) -> None:
        self.undo_stack.push(MoveZCommand(self, obj, delta))

    # -- 参考层（不参与书写） -----------------------------------------------
    def add_reference(self, ref: ReferenceItem,
                      index: Optional[int] = None) -> None:
        self.undo_stack.push(AddReferenceCommand(self, ref, index))

    def remove_reference(self, ref: ReferenceItem) -> None:
        self.undo_stack.push(RemoveReferenceCommand(self, ref))

    def remove_references(self, refs: list[ReferenceItem]) -> None:
        if not refs:
            return
        self.undo_stack.beginMacro(f"删除 {len(refs)} 个参考图")
        for r in refs:
            self.undo_stack.push(RemoveReferenceCommand(self, r))
        self.undo_stack.endMacro()

    def edit_reference(self, ref: ReferenceItem, text: str,
                       changes: dict) -> None:
        self.undo_stack.push(ReferenceEditCommand(self, ref, text, changes))

    # -- 内容/扰动编辑（带撤销） --------------------------------------------
    def replace_object(self, obj: DocumentObject, old: _ObjectState,
                       new: _ObjectState, text: str) -> None:
        """把一次「内容重生成」纳入撤销栈（对象当前已处于 ``new`` 状态）。"""
        self.undo_stack.push(
            ObjectStateCommand(self, [(obj, old, new)], text))

    def replace_objects(self,
                        entries: list[tuple[DocumentObject, _ObjectState,
                                            _ObjectState]],
                        text: str, merge_key: Optional[str] = None) -> None:
        if not entries:
            return
        self.undo_stack.push(
            ObjectStateCommand(self, entries, text, merge_key))

    def set_object_flags(self, obj: DocumentObject, text: str,
                         changes: dict) -> None:
        self.undo_stack.push(ObjectFlagCommand(self, obj, text, changes))

    def set_page(self, old: PageSpec, new: PageSpec,
                 text: str = "页面设置") -> None:
        self.undo_stack.push(PageCommand(self, old, new, text))

    # -- 撤销/重做 ----------------------------------------------------------
    def undo(self) -> None:
        self.undo_stack.undo()

    def redo(self) -> None:
        self.undo_stack.redo()

    @property
    def can_undo(self) -> bool:
        return self.undo_stack.canUndo()

    @property
    def can_redo(self) -> bool:
        return self.undo_stack.canRedo()
