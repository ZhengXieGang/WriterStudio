"""P3 UI 冒烟测试：扰动面板、预设、重摇、应用到对象。"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.fonts.builder import TextSpec  # noqa: E402
from writerstudio.perturb.params import PerturbParams  # noqa: E402
from writerstudio.ui.main_window import MainWindow  # noqa: E402
from writerstudio.ui.perturb_panel import PerturbPanel  # noqa: E402
from writerstudio.ui.text_dialog import TextEditDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def test_panel_loads_and_reports_params(qapp):
    p = PerturbParams.natural(11.0)
    panel = PerturbPanel(p, size_hint=11.0)
    out = panel.params()
    assert out.enabled
    assert out.sine_amplitude == pytest.approx(p.sine_amplitude)
    assert panel.preset_combo.currentText() == "自然"


def test_panel_preset_applies(qapp):
    panel = PerturbPanel(PerturbParams(), size_hint=10.0)
    panel.preset_combo.setCurrentText("强烈")
    out = panel.params()
    assert out.enabled
    assert out.size_sigma == pytest.approx(PerturbParams.strong(10.0).size_sigma)
    panel.preset_combo.setCurrentText("关闭")
    assert not panel.params().enabled


def test_panel_reseed_changes_seed_keeps_params(qapp):
    p = PerturbParams.natural(10.0)
    panel = PerturbPanel(p, size_hint=10.0)
    old_seed = panel.params().seed
    amp = panel.params().sine_amplitude
    panel._on_reseed()
    assert panel.params().seed != old_seed
    assert panel.params().sine_amplitude == pytest.approx(amp)


def test_panel_manual_edit_marks_custom(qapp):
    panel = PerturbPanel(PerturbParams.natural(10.0), size_hint=10.0)
    panel.enable_check.setChecked(True)
    panel._spin["char_x_sigma"].setValue(9.99)
    assert panel.preset_combo.currentText() == "自定义"
    assert panel.params().char_x_sigma == pytest.approx(9.99)


def test_text_dialog_has_perturb_tab(qapp):
    win = MainWindow()
    dlg = TextEditDialog(TextSpec(text="AB", font_names=["futural"]),
                         win.font_manager)
    assert dlg.tabs.count() == 2
    assert dlg.tabs.tabText(1) == "手写扰动"
    # 切到扰动页、启用并设参数，结果 spec 应携带
    dlg.tabs.setCurrentIndex(1)
    dlg.perturb_panel.enable_check.setChecked(True)
    dlg.perturb_panel._spin["size_sigma"].setValue(0.05)
    out = dlg.result_spec()
    assert out.perturb.enabled
    assert out.perturb.size_sigma == pytest.approx(0.05)
    dlg.close()
    win.close()


def test_main_window_reseed_and_clear(qapp, monkeypatch):
    win = MainWindow()

    def fake_exec(self):
        self.text_edit.setPlainText("ABCD ABCD")
        return TextEditDialog.Accepted

    monkeypatch.setattr(TextEditDialog, "exec", fake_exec)
    win._new_text()
    obj = win.controller.doc.objects[-1]

    # 给它加扰动
    win.canvas.select_object(obj)
    win._reseed_selected()
    spec = TextSpec.from_data(obj.source.data)
    assert spec.perturb.enabled
    seed1 = spec.perturb.seed
    strokes1 = [s.points for s in obj.local_strokes]

    # 重摇换种子
    win._reseed_selected()
    spec2 = TextSpec.from_data(obj.source.data)
    assert spec2.perturb.seed != seed1
    assert [s.points for s in obj.local_strokes] != strokes1

    # 清除扰动
    win._clear_perturb()
    spec3 = TextSpec.from_data(obj.source.data)
    assert not spec3.perturb.enabled
    win.close()


def test_main_window_global_panel_applies_live(qapp, monkeypatch):
    win = MainWindow()

    def fake_exec(self):
        self.text_edit.setPlainText("Live 实时")
        return TextEditDialog.Accepted

    monkeypatch.setattr(TextEditDialog, "exec", fake_exec)
    win._new_text()
    obj = win.controller.doc.objects[-1]
    win.canvas.select_object(obj)
    plain = [s.points for s in obj.local_strokes]

    # 全局面板启用并改参数，应实时应用到选中对象
    win.global_perturb.enable_check.setChecked(True)
    win.global_perturb.preset_combo.setCurrentText("自然")
    assert [s.points for s in obj.local_strokes] != plain
    spec = TextSpec.from_data(obj.source.data)
    assert spec.perturb.enabled
    win.close()
