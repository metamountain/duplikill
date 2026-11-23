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


def test_settings_layout_never_jumps(app):
    from duplikill.app import DEFAULTS, SettingsScreen
    s = SettingsScreen(dict(DEFAULTS))
    s.resize(1600, 950)
    s._t_in = 0.0
    s.grab()
    base_geo = s._geo
    assert base_geo is not None
    for row in range(1, len(s.ROWS) + 1):
        for _ in range(3):
            s.cycle(row)
            s._t_in = 0.0
            s.grab()
            assert s._geo == base_geo, f"Row {row} caused settings layout jump: {s._geo} != {base_geo}"


def test_central_switches_pair_and_six_grid_and_right_pan(app):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    mk = lambda name, sz=1000: Photo(path=f"C:\\x\\{name}.jpg", size=sz, mtime=0, width=400, height=300, sharp=sz)
    pair = Group("similar", [mk("p1", 2000), mk("p2", 1000)])
    six = Group("similar", [mk(f"s{i}", 1000 + i * 100) for i in range(6)])
    r = Review([pair, six], FakeThumbs())
    r.resize(1600, 900)

    # 1. 2-image pair: switches near center seam, 1-click smart swap
    r.show_group(0)
    r.grab()
    assert abs(r.hub_rect.center().x() - 800) < 5 and abs(r.hub_rect.center().y() - 450) < 5
    for j, kr in r.kill_rects.items():
        assert abs(kr.center().x() - 800) < 120   # right next to vertical center seam, not at (0,0)!
        assert not kr.contains(r.rects[j].center())
    r.toggle(0)
    assert r.marked() == {0}
    r.toggle(1)                                   # 2-image smart swap: switches directly to {1}
    assert r.marked() == {1}

    # 2. 6-image group: 3 stay/3 go, keep_only, keep_best
    r.show_group(1)
    r.grab()
    assert not r.keep_best_rect.isNull()
    for j in (0, 2, 4):
        r.toggle(j)
    assert r.marked() == {0, 2, 4}                # 6 images: 3 stay (1,3,5), 3 go (0,2,4)
    r.keep_only(1)
    assert r.marked() == {0, 2, 3, 4, 5}
    r.toggle(3)                                   # keep 1 and 3
    assert r.marked() == {0, 2, 4, 5}
    r.keep_best()
    assert r.marked() == {k for k in range(6) if k != six.best}

    # 3. Configurable right-mouse pan / middle-mouse blink
    r.pan = "right"
    ev_mid = lambda t: QMouseEvent(t, QPointF(10, 10), QPointF(10, 10), Qt.MiddleButton, Qt.MiddleButton, Qt.NoModifier)
    r.mousePressEvent(ev_mid(QEvent.MouseButtonPress))
    assert r.swap == 1
    r.mouseReleaseEvent(ev_mid(QEvent.MouseButtonRelease))
    assert r.swap == 0
    ev_right = lambda t: QMouseEvent(t, QPointF(10, 10), QPointF(10, 10), Qt.RightButton, Qt.RightButton, Qt.NoModifier)
    r.mousePressEvent(ev_right(QEvent.MouseButtonPress))
    assert r._press is not None and r.swap == 0
    r.mouseReleaseEvent(ev_right(QEvent.MouseButtonRelease))
    assert r._press is None


def test_help_screen_f1(app):
    from duplikill.app import HelpScreen
    h = HelpScreen()
    h.resize(1600, 950)
    h._t_in = 0.0
    pm = h.grab()
    assert not pm.isNull() and len(h.lines()) == len(h.CORE) + len(h.ITEMS)
    assert all(len(c) <= h.COLS // 2 for c in h.CORE)        # double size must fit
    assert all(len(line) == h.COLS for line in h.lines())
