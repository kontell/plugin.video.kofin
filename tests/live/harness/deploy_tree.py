"""RunScript(deploy_tree.py,<src dir>,<dest special:// path>) — copy an add-on
tree into Kodi's own addons directory from inside Kodi.

On Android adb can push to /sdcard but not into Kodi's addons directory, and
Kodi's Python can. ``<src>/.deployed`` names the file count once the copy is
done, for the driver to poll. The add-on still needs a disable→enable bounce
afterwards.
"""

import sys

import xbmc
import xbmcvfs


def copy_tree(src, dest):
    if not src.endswith("/"):
        src += "/"
    if not dest.endswith("/"):
        dest += "/"
    xbmcvfs.mkdirs(dest)
    count = 0
    dirs, files = xbmcvfs.listdir(src)
    for name in files:
        if name == ".deployed":
            continue
        if not xbmcvfs.copy(src + name, dest + name):
            raise RuntimeError("copy failed: %s" % (src + name))
        count += 1
    for name in dirs:
        count += copy_tree(src + name, dest + name)
    return count


def main(argv):
    src, dest = argv[1], argv[2]
    count = copy_tree(src, dest)
    marker = xbmcvfs.File(src.rstrip("/") + "/.deployed", "w")
    marker.write("%d files" % count)
    marker.close()
    xbmc.log("[deploy_tree] %d files -> %s" % (count, dest), xbmc.LOGINFO)


if __name__ == "__main__":
    main(sys.argv)
