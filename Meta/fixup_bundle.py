#!/usr/bin/env python3
"""Make a macOS .app bundle self-contained by rewriting dylib references.

macdeployqt bundles the Qt frameworks (and some of their support libraries)
into Contents/Frameworks, but it does not always rewrite the cross-references
between the bundled plain .dylibs (e.g. libbrotlidec referencing
libbrotlicommon via an absolute /opt/homebrew path). This script walks every
Mach-O binary in the bundle and rewrites every LC_LOAD_DYLIB / LC_ID_DYLIB that
points outside the bundle (or to an absolute build-machine path) to use
@rpath/<basename>, copying in any referenced dylib that is missing. This makes
the .app fully relocatable, independent of the build machine's Homebrew prefix.
"""

import os
import subprocess
import sys


SYSTEM_PREFIXES = (
    "/usr/lib/",
    "/usr/lib/system/",
    "/System/Library/",
    "/Library/Frameworks/",
)


def is_system(path):
    if path.startswith("@"):
        return True
    return any(path.startswith(p) for p in SYSTEM_PREFIXES)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def is_macho(path):
    if path.endswith(".a"):
        return False
    res = run(["otool", "-h", path])
    return res.returncode == 0 and "Mach header" in res.stdout


def collect_macho_files(app_dir):
    found = []
    for root, _dirs, files in os.walk(app_dir):
        for fn in files:
            p = os.path.join(root, fn)
            if os.path.islink(p):
                continue
            if is_macho(p):
                found.append(p)
    return found


def get_id(path):
    res = run(["otool", "-D", path])
    if res.returncode != 0:
        return None
    lines = [l.strip() for l in res.stdout.splitlines()]
    # otool -D prints the file path line, then the id (or nothing).
    for l in lines[1:]:
        if l:
            return l
    return None


def get_deps(path):
    ident = get_id(path)
    res = run(["otool", "-L", path])
    if res.returncode != 0:
        return []
    deps = []
    for l in res.stdout.splitlines()[1:]:
        l = l.strip()
        if not l:
            continue
        # Format: "  <path> (compatibility version ...)"
        name = l.split(" (compatibility")[0].strip()
        # Skip the install-name line (otool -L prints the id first) and any
        # static-archive member listings.
        if name == ident:
            continue
        if "(" in name or ")" in name:
            continue
        deps.append(name)
    return deps


def get_rpaths(path):
    res = run(["otool", "-l", path])
    if res.returncode != 0:
        return []
    rpaths = []
    lines = res.stdout.splitlines()
    for i, l in enumerate(lines):
        if "LC_RPATH" in l:
            # The path line follows a few lines later.
            for j in range(i, min(i + 6, len(lines))):
                if "path" in lines[j] and "(" in lines[j]:
                    rpath = lines[j].split("path")[1].split("(")[0].strip()
                    rpaths.append(rpath)
                    break
    return rpaths


def ensure_rpath_to_frameworks(macho, frameworks_dir):
    rel = os.path.relpath(frameworks_dir, os.path.dirname(macho))
    wanted = "@loader_path/" + rel
    existing = get_rpaths(macho)
    if wanted not in existing:
        run(["install_name_tool", "-add_rpath", wanted, macho])


def set_id(macho, new_id):
    run(["install_name_tool", "-id", new_id, macho])


def change_dep(macho, old, new):
    run(["install_name_tool", "-change", old, new, macho])


def is_framework(path):
    return ".framework" in path


def rpath_ref(path):
    """Return the @rpath-based reference for a dylib or framework binary path."""
    if is_framework(path):
        idx = path.rfind("Frameworks/")
        if idx >= 0:
            return "@rpath/" + path[idx + len("Frameworks/"):]
        name = path.split(".framework")[0].split("/")[-1]
        return "@rpath/" + name + ".framework/Versions/A/" + name
    return "@rpath/" + os.path.basename(path)


def main():
    app_dir = sys.argv[1]
    frameworks_dir = os.path.join(app_dir, "Contents", "Frameworks")
    os.makedirs(frameworks_dir, exist_ok=True)

    macho_files = collect_macho_files(app_dir)
    by_basename = {}
    for p in macho_files:
        by_basename.setdefault(os.path.basename(p), p)

    changed = True
    iteration = 0
    while changed and iteration < 8:
        iteration += 1
        changed = False
        for macho in list(macho_files):
            ensure_rpath_to_frameworks(macho, frameworks_dir)
            # Make the id rpath-based. Plain dylibs in Frameworks get an
            # @rpath/<basename> id; framework binaries get an
            # @rpath/<Framework.framework/Versions/A/Name> id. This removes any
            # leftover absolute /opt/homebrew install names left by macdeployqt.
            ident = get_id(macho)
            if ident and not is_system(ident):
                if is_framework(macho):
                    new_id = rpath_ref(macho)
                elif os.path.dirname(macho).endswith("Frameworks"):
                    new_id = "@rpath/" + os.path.basename(macho)
                else:
                    new_id = None
                if new_id is not None and ident != new_id:
                    set_id(macho, new_id)
                    changed = True
                    sys.stderr.write("id %s -> %s\n" % (macho, new_id))
            for dep in get_deps(macho):
                if is_system(dep):
                    continue
                base = os.path.basename(dep)
                new_dep = rpath_ref(dep)
                if base in by_basename:
                    target = by_basename[base]
                    if dep != new_dep:
                        change_dep(macho, dep, new_dep)
                        changed = True
                        sys.stderr.write("dep %s: %s -> %s\n" % (macho, dep, new_dep))
                    ensure_rpath_to_frameworks(target, frameworks_dir)
                    if not is_framework(target):
                        tid = get_id(target)
                        if tid and not is_system(tid):
                            new_tid = "@rpath/" + base
                            if tid != new_tid:
                                set_id(target, new_tid)
                                changed = True
                                sys.stderr.write("id %s -> %s\n" % (target, new_tid))
                else:
                    # Absolute path to a dylib not yet in the bundle: copy it in.
                    if os.path.exists(dep):
                        dest = os.path.join(frameworks_dir, base)
                        if not os.path.exists(dest):
                            subprocess.run(["cp", dep, dest])
                            macho_files.append(dest)
                            by_basename[base] = dest
                            changed = True
                        change_dep(macho, dep, new_dep)
                        changed = True
                        sys.stderr.write("copy %s -> %s\n" % (dep, dest))
                    else:
                        sys.stderr.write(
                            "WARNING: cannot resolve dependency %s referenced by %s\n"
                            % (dep, macho)
                        )
        sys.stderr.write("=== iteration %d done (changed=%s) ===\n" % (iteration, changed))

    # Final report.
    remaining = []
    for macho in macho_files:
        for dep in get_deps(macho):
            if not is_system(dep):
                remaining.append((macho, dep))
    if remaining:
        sys.stderr.write("\nRemaining non-system absolute dependencies:\n")
        for macho, dep in remaining:
            sys.stderr.write("  %s -> %s\n" % (macho, dep))
        sys.exit(1)
    print("Bundle is self-contained: all dylib references resolved.")


if __name__ == "__main__":
    main()
