import os

from duplikill.bestof import candidates
from duplikill.groups import Group
from duplikill.index import Photo


def P(path):
    return Photo(path=path, size=100, mtime=0.0, width=10, height=10)


def test_bestof_folder_found_and_source_not():
    src = [os.path.join("D:\\a", f"{i}.jpg") for i in range(40)]
    best = [os.path.join("D:\\best", f"{i}.jpg") for i in range(6)]
    groups = [Group("similar", [P(src[i]), P(best[i])]) for i in range(6)]
    groups.append(Group("similar", [P(src[10]), P(src[11])]))            # a burst inside the source
    out = candidates(groups, src + best)
    assert [(os.path.basename(f), c, n) for f, c, n in out] == [("best", 6, 6)]


def test_small_or_skipped_folders_ignored():
    src = [f"D:\\a\\{i}.jpg" for i in range(4)]
    best = [f"D:\\b\\{i}.jpg" for i in range(4)]
    groups = [Group("similar", [P(src[i]), P(best[i])]) for i in range(4)]
    assert candidates(groups, src + best) == []                            # under 5 images
    assert candidates(groups, src + best, min_images=3, skip=["d:\\B"]) == [("D:\\a", 4, 4)]
