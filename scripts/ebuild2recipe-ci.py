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
    script = content + '\\necho "$SRC_URI"'
    try:
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True, text=True, timeout=15,
            cwd=ebuild_dir
        )
        lines = result.stdout.strip().split('\\n')
        for line in reversed(lines):
            line = line.strip()
            if line and not line.startswith('set -e') and not line.startswith('inherit'):
                return line
    except:
        pass
    return None

def manual_expand(content, pn, pv, p):
    pattern = r'SRC_URI\\s*=\\s*"([^"]*(?:\\n[^"]*)*)"'
    m = re.search(pattern, content, re.DOTALL)
    if not m:
        pattern = r"SRC_URI\\s*=\\s*'([^']*(?:\\n[^']*)*)'"
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
    pattern = rf'{varname}\\s*=\\s*"([^"]*(?:\\n[^"]*)*)"'
    m = re.search(pattern, content, re.DOTALL)
    if not m:
        return []
    dep_str = m.group(1)
    dep_str = " ".join(dep_str.split())
    tokens = re.findall(r'([a-zA-Z0-9+_-]+/[a-zA-Z0-9+_-]+)', dep_str)
    result = []
    for tok in tokens:
        name = tok.split('/')[-1]
        if name and not name.startswith('$') and not name.startswith('virtual'):
            if name not in result:
                result.append(name)
    return result

def detect_build_system(src_dir):
    if not os.path.exists(src_dir):
        return 'autotools'
    files = os.listdir(src_dir)
    checks = [
        ('meson.build', 'meson'), ('CMakeLists.txt', 'cmake'),
        ('configure', 'autotools'), ('configure.ac', 'autotools'),
        ('setup.py', 'python'), ('pyproject.toml', 'python'),
        ('Cargo.toml', 'rust'), ('go.mod', 'go'),
        ('Makefile.PL', 'perl'), ('Makefile', 'make'),
    ]
    for fname, btype in checks:
        if fname in files:
            return btype
    for f in files:
        sub = os.path.join(src_dir, f)
        if os.path.isdir(sub):
            return detect_build_system(sub)
    return 'autotools'

def download_and_extract(url, dest):
    archive = os.path.join(dest, "source.tar.gz")
    try:
        urllib.request.urlretrieve(url, archive)
    except:
        return None
    for cmd in [["tar", "xf", archive, "-C", dest], ["tar", "xf", archive, "-C", dest, "--strip-components=1"]]:
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            return dest
        except:
            continue
    return None

def convert_ebuild(ebuild_path):
    with open(ebuild_path) as f:
        content = f.read()
    basename = os.path.basename(ebuild_path).replace('.ebuild', '')
    m = re.match(r'^(.+)-([0-9][\\w\\._-]*(?:-r[0-9]+)?)$', basename)
    if m:
        pn, pv = m.group(1), m.group(2)
    else:
        pn, pv = basename, "0"
    p = f"{pn}-{pv}"
    
    ebuild_dir = os.path.dirname(ebuild_path)
    src_uri = bash_expand(content, ebuild_dir)
    if not src_uri or not src_uri.startswith("http"):
        src_uri = manual_expand(content, pn, pv, p)
    if not src_uri:
        dists = parse_manifest(os.path.join(ebuild_dir, "Manifest"))
        src_uri = guess_url_from_manifest(pn, pv, dists)
    
    bdepend = parse_deps(content, 'BDEPEND')
    depend = parse_deps(content, 'DEPEND')
    rdepend = parse_deps(content, 'RDEPEND')
    
    build_type = 'autotools'
    if src_uri:
        with tempfile.TemporaryDirectory() as tmp:
            extracted = download_and_extract(src_uri, tmp)
            if extracted:
                build_type = detect_build_system(extracted)
    
    template = TEMPLATES.get(build_type, TEMPLATES['autotools'])
    build_steps = [s.format(name=pn) for s in template]
    
    recipe = {
        'name': pn,
        'version': pv,
        'source': {'url': src_uri} if src_uri else {'url': ''},
        'build': build_steps,
    }
    deps = {}
    all_build = list(dict.fromkeys(bdepend + depend))
    if all_build:
        deps['build'] = all_build
    if rdepend:
        deps['runtime'] = rdepend
    if deps:
        recipe['depends'] = deps
    return recipe

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--count', type=int, default=10)
    parser.add_argument('--gentoo', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    
    import yaml
    
    # Собираем существующие пакеты
    existing = set()
    if os.path.exists(args.output):
        for d in os.listdir(args.output):
            if os.path.isdir(os.path.join(args.output, d)):
                existing.add(d)
    
    # Находим ebuild
    all_ebuilds = []
    for root, dirs, files in os.walk(args.gentoo):
        for f in files:
            if f.endswith('.ebuild'):
                all_ebuilds.append(os.path.join(root, f))
    random.shuffle(all_ebuilds)
    
    converted = 0
    for ebuild in all_ebuilds:
        if converted >= args.count:
            break
        
        basename = os.path.basename(ebuild).replace('.ebuild', '')
        m = re.match(r'^(.+)-[0-9]', basename)
        if not m:
            continue
        pn = m.group(1)
        
        if pn in existing:
            continue
        
        print(f"[*] Converting: {pn}")
        try:
            recipe = convert_ebuild(ebuild)
        except Exception as e:
            print(f"[!] Error: {e}")
            continue
        
        if not recipe['source'].get('url'):
            print(f"[!] Skipping {pn} — empty URL")
            continue
        
        out_dir = os.path.join(args.output, pn)
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "recipe.yaml")
        
        with open(out_path, 'w') as f:
            yaml.dump(recipe, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        
        print(f"[+] Saved: {pn}")
        converted += 1
        existing.add(pn)
    
    print(f"[+] Total converted: {converted}")

if __name__ == '__main__':
    main()
