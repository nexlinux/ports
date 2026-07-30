# ebuild2recipe-ci.py — batch-конвертер для GitHub Actions

import sys, os, re, subprocess, tempfile, urllib.request, argparse, random

MIRRORS = {
    "sourceforge": "https://downloads.sourceforge.net",
    "github": "https://github.com",
    "gnome": "https://download.gnome.org/sources",
    "kde": "https://download.kde.org/stable",
    "apache": "https://archive.apache.org/dist",
    "pypi": "https://files.pythonhosted.org/packages/source",
    "cran": "https://cran.r-project.org/src/contrib",
    "gentoo": "https://distfiles.gentoo.org/distfiles",
    "kernel": "https://cdn.kernel.org/pub/linux",
    "xfce": "https://archive.xfce.org/src/xfce",
    "freedesktop": "https://gitlab.freedesktop.org",
    "videolan": "https://download.videolan.org/pub",
    "cpan": "https://cpan.metacpan.org/authors/id",
    "hackage": "https://hackage.haskell.org/package",
    "rubygems": "https://rubygems.org/downloads",
    "mozilla": "https://archive.mozilla.org/pub",
    "rust-lang": "https://static.rust-lang.org/dist",
}

TEMPLATES = {
    "autotools": ["./configure --prefix=$PREFIX", "make -j$(nproc)", "make DESTDIR=$DESTDIR install"],
    "cmake": ["cmake -B build -DCMAKE_INSTALL_PREFIX=$PREFIX", "cmake --build build -j$(nproc)", "DESTDIR=$DESTDIR cmake --install build"],
    "meson": ["meson setup build --prefix=$PREFIX", "meson compile -C build", "DESTDIR=$DESTDIR meson install -C build"],
    "python": ["pip install . --root=$DESTDIR --prefix=$PREFIX"],
    "rust": ["cargo build --release", "mkdir -p $DESTDIR$PREFIX/bin", "cp target/release/{name} $DESTDIR$PREFIX/bin/"],
    "go": ["go build -o {name}", "mkdir -p $DESTDIR$PREFIX/bin", "cp {name} $DESTDIR$PREFIX/bin/"],
    "make": ["make -j$(nproc)", "make DESTDIR=$DESTDIR PREFIX=$PREFIX install"],
    "perl": ["perl Makefile.PL PREFIX=$PREFIX", "make -j$(nproc)", "make DESTDIR=$DESTDIR install"],
}

def bash_expand(content, ebuild_dir):
    script = content + '\necho "$SRC_URI"'
    try:
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True, text=True, timeout=15,
            cwd=ebuild_dir
        )
        lines = result.stdout.strip().split('\n')
        for line in reversed(lines):
            line = line.strip()
            if line and not line.startswith('set -e') and not line.startswith('inherit'):
                return line
    except:
        pass
    return None

def manual_expand(content, pn, pv, p):
    pattern = r'SRC_URI\s*=\s*"([^"]*(?:\n[^"]*)*)"'
    m = re.search(pattern, content, re.DOTALL)
    if not m:
        pattern = r"SRC_URI\s*=\s*'([^']*(?:\n[^']*)*)'"
        m = re.search(pattern, content, re.DOTALL)
    if not m:
        return None
    uri = m.group(1)
    uri = uri.replace("${P}", p).replace("${PN}", pn).replace("${PV}", pv)
    uri = uri.replace("${MY_P}", p).replace("${PV/_/.}", pv.replace("_", "."))
    uri = uri.replace("${PV%.*}", pv[:pv.rfind(".")] if "." in pv else pv)
    uri = uri.replace("${PV%%.*}", pv.split(".")[0] if "." in pv else pv)
    uri = " ".join(uri.split())
    for mirror_name, mirror_url in MIRRORS.items():
        uri = uri.replace(f"mirror://{mirror_name}/", f"{mirror_url}/")
    urls = uri.split()
    for url in urls:
        if url.startswith("http"):
            return url
    return None

def parse_manifest(manifest_path):
    dists = []
    if not os.path.exists(manifest_path):
        return dists
    with open(manifest_path) as f:
        for line in f:
            if line.startswith("DIST "):
                parts = line.split()
                if len(parts) >= 2:
                    dists.append(parts[1])
    return dists

def guess_url_from_manifest(pn, pv, dists):
    if not dists:
        return None
    fname = dists[0]
    major = pv[:pv.rfind(".")] if "." in pv else pv
    guesses = [
        f"https://github.com/{pn}/{pn}/releases/download/v{pv}/{fname}",
        f"https://github.com/{pn}/{pn}/archive/refs/tags/v{pv}.tar.gz",
        f"https://downloads.sourceforge.net/{pn}/{fname}",
        f"https://archive.xfce.org/src/xfce/{pn}/{major}/{fname}",
        f"https://download.gnome.org/sources/{pn}/{major}/{fname}",
        f"https://download.kde.org/stable/{pn}/{pv}/{fname}",
        f"https://distfiles.gentoo.org/distfiles/{fname}",
        f"https://gitlab.freedesktop.org/{pn}/{pn}/-/archive/{pv}/{fname}",
        f"https://archive.mozilla.org/pub/{pn}/releases/{pv}/source/{pn}-{pv}.source.tar.xz",
        f"https://archive.mozilla.org/pub/{pn}/releases/{pv}/source/{fname}",
        f"https://static.rust-lang.org/dist/{fname}",
    ]
    for g in guesses:
        try:
            req = urllib.request.Request(g, method='HEAD')
            urllib.request.urlopen(req, timeout=5)
            return g
        except:
            continue
    return None

def parse_deps(content, varname):
    pattern = rf'{varname}\s*=\s*"([^"]*(?:\n[^"]*)*)"'
    m = re.search(pattern, content, re.DOTALL)
    if not m:
        return []
    dep_str = m.group(1)
    dep_str
