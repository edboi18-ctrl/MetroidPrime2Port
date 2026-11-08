"""Copies the licence files of installed Python packages (those bundled into mp2_material_editor.exe)
into a folder, one <package>.txt each, from the packages' installed metadata.

Usage: bundle_licenses.py <folder> <distribution name>...  A package that isn't installed is skipped
with a note. One whose wheel carries no licence file (PyOpenGL's doesn't) gets the copy kept in
licenses/ here (licenses/PyOpenGL.txt is license.txt from the PyOpenGL 3.1.10 source distribution),
else its metadata's License field.
"""

import os
import sys
from importlib import metadata

NAMES = ("license", "licence", "copying", "notice")
KEPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "licenses")


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def license_text(dist, name):
    parts = []
    for f in dist.files or ():
        base = os.path.basename(str(f)).lower()
        if ".dist-info" in str(f) and any(base.startswith(n) for n in NAMES):
            try:
                parts.append("== %s ==\n%s" % (f, _read(str(f.locate()))))
            except OSError:
                continue
    if not parts:
        kept = os.path.join(KEPT, "%s.txt" % name)
        if os.path.isfile(kept):
            parts.append(_read(kept))
    if not parts:
        field = dist.metadata.get("License-Expression") or dist.metadata.get("License")
        if field:
            parts.append("License: %s" % field)
    return "\n\n".join(parts)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    folder = argv[0]
    os.makedirs(folder, exist_ok=True)
    for name in argv[1:]:
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            print("%s isn't installed: skipped" % name)
            continue
        text = license_text(dist, name)
        if not text:
            print("%s %s has no licence file or field" % (name, dist.version))
            continue
        path = os.path.join(folder, "%s.txt" % name.lower())
        with open(path, "w", encoding="utf-8") as out:
            out.write("%s %s (bundled in mp2_material_editor.exe)\n\n%s\n" % (dist.metadata["Name"], dist.version, text))
        print("%s %s -> %s" % (name, dist.version, path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
