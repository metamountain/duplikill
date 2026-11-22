import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from duplikill.groups import Group
from duplikill.index import Photo
from duplikill.review import Review, layout


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class FakeThumbs:
    from PySide6.QtCore import QObject, Signal

    class _S(QObject):
        from PySide6.QtCore import Signal
        ready = Signal(str)

    def __init__(self):
        self._s = self._S()
        self.ready = self._s.ready

    def get(self, *a):
        return None

    def request(self, *a, **k):
        pass


def _groups():
    mk = lambda name: Photo(path=f"C:\\x\\{name}.jpg", size=1000, mtime=0, width=4, height=3)
    return [Group("similar", [mk("a1"), mk("a2"), mk("a3")]), Group("similar", [mk("b1"), mk("b2")]),
            Group("similar", [mk("c1"), mk("c2")])]


def test_one_image_always_stays(app):
    r = Review(_groups(), FakeThumbs())
    r.show_group(0)
    for j in range(3):
        r.toggle(j)
    assert len(r.marked()) == 2


def test_fire_emits_marked_paths(app):
    r = Review(_groups(), FakeThumbs())
    r.show_group(1)
    got = []
    r.delete_requested.connect(lambda i, paths: got.append((i, paths)))
    r.fire()                       # nothing marked -> nothing happens
    r.toggle(0)
    r.fire()
    assert got == [(1, [r.groups[1].photos[0].path])]


def test_step_skips_done_groups(app):
    r = Review(_groups(), FakeThumbs())
    r.show_group(0)
    r.done.add(r.groups[1].key)
    r.step(1)
    assert r.i == 2
    r.step(1)                      # end reached: stays
    assert r.i == 2


def test_all_marked_for_end_mode(app):
    r = Review(_groups(), FakeThumbs())
    r.show_group(0); r.toggle(1)
    r.show_group(2); r.toggle(0)
    assert [i for i, _ in r.all_marked()] == [0, 2]


def test_layout_fits():
    cols, rows, cw = layout(10, 1600, 900)
    assert cols * rows >= 10
    assert cols * cw <= 1600 and rows * cw / 1.5 <= 900


def test_flip_blink_compare_keeps_zoom(app):
    r = Review(_groups(), FakeThumbs())
    r.show_group(0)
    r.zoom, r.center = 4.0, (0.3, 0.6)
    r.hover = 1
    r.flip()
    assert r.solo == 1
    r.flip()
    r.flip()
    assert r.solo == 0                 # wraps around in a group of 3
    assert (r.zoom, r.center) == (4.0, (0.3, 0.6))


def test_del_fires_and_right_hold_blinks(app):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QKeyEvent, QMouseEvent
    r = Review(_groups(), FakeThumbs())
    r.show_group(1)
    got = []
    r.delete_requested.connect(lambda i, paths: got.append(i))
    r.toggle(0)
    r.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
    assert got == []                             # Enter no longer deletes
    r.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))
    assert got == [1]
    # without the main window the cross is still set, so Backspace fires again
    r.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Backspace, Qt.NoModifier))
    assert got == [1, 1]
    ev = lambda t: QMouseEvent(t, QPointF(5, 5), QPointF(5, 5), Qt.RightButton, Qt.RightButton, Qt.NoModifier)
    r.mousePressEvent(ev(QEvent.MouseButtonPress))
    assert r.swap == 1 and got == [1, 1]         # right button no longer deletes
    r.mouseReleaseEvent(ev(QEvent.MouseButtonRelease))
    assert r.swap == 0


def test_delete_button_appears_and_fires(app):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    r = Review(_groups(), FakeThumbs())
    r.resize(1200, 800)
    r.show_group(1)
    r.grab()
    assert r.fire_rect.isNull()                  # nothing crossed: no button
    r.toggle(0)
    r.grab()
    assert not r.fire_rect.isNull()
    got = []
    r.delete_requested.connect(lambda i, paths: got.append(i))
    c = r.fire_rect.center()
    r.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, c, c, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    assert got == [1]


def test_skull_on_hovered_image_kills_one(app):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    r = Review(_groups(), FakeThumbs())
    r.show_group(0)
    g = r.group
    got = []
    r.kill_requested.connect(lambda i, p: got.append((i, p)))
    r.resize(1200, 800); r.grab()
    QTest.mouseMove(r, r.rects[1].center().toPoint()); r.hover = 1; r.grab()
    assert not r.kill_rect.isNull()
    QTest.mouseClick(r, Qt.LeftButton, pos=r.kill_rect.center().toPoint())
    target = g.photos[1].path
    assert got == [(0, target)]
    assert r.marked() == set()                    # the skull does not set a cross
    r.marks[g.key] = {2}
    j = r.remove_photo(target)
    assert j == 1 and len(g.photos) == 2 and r.marked() == {1}
