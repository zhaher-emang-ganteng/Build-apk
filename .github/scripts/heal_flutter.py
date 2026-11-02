#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
heal_flutter.py - dijalankan di runner GitHub Actions oleh build.yml & build_fast.yml.

Tujuan: base ZIP Flutter apa pun (lama/baru, setengah rusak) bisa dibuild.
Semua perbaikan di sini deterministik (tanpa AI). Kalau masih gagal, bot akan
meminta Gemini memperbaiki berdasarkan log, lalu build ulang.

Subcommand:
  detect-flutter <root>   -> cetak versi Flutter yang cocok dengan constraint SDK pubspec
                             (kosong = stable terbaru)
  detect-java <root>      -> cetak versi Java yang cocok dengan Gradle wrapper (11 / 17)
  heal <root>             -> perbaiki struktur project (butuh flutter di PATH)
  heal-late <root>        -> perbaikan yang butuh Java (keystore release)
"""
import base64
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

TAG = "[HEAL]"


def log(msg):
    print(f"{TAG} {msg}", flush=True)


def read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ───────────────────────── PNG generator (tanpa library) ─────────────────────────
def make_png(w, h, rgba=(33, 150, 243, 255)):
    row = b"\x00" + bytes(rgba) * w
    raw = row * h

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def write_bin(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


# ───────────────────────── pubspec ─────────────────────────
def pubspec_text(root):
    return read(os.path.join(root, "pubspec.yaml"))


def pubspec_name(txt):
    m = re.search(r"^name:\s*['\"]?([A-Za-z0-9_]+)", txt, re.M)
    return m.group(1) if m else "app"


def top_level_block(txt, key):
    """Ambil baris-baris yang menjorok di bawah `key:` tingkat atas."""
    lines = txt.splitlines()
    out = []
    inside = False
    for ln in lines:
        if not inside:
            if re.match(rf"^{re.escape(key)}:\s*(#.*)?$", ln):
                inside = True
            continue
        if ln.strip() == "" or ln.lstrip().startswith("#") or ln[:1] in (" ", "\t"):
            out.append(ln)
        else:
            break
    return out


def ver(s):
    m = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", s.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def sdk_bounds(txt):
    block = top_level_block(txt, "environment")
    raw = None
    for ln in block:
        m = re.match(r"^\s+sdk:\s*['\"]?([^'\"#\n]+)", ln)
        if m:
            raw = m.group(1).strip()
            break
    if not raw:
        return None, None
    lower = upper = None
    m = re.search(r">=\s*([\d.]+)", raw)
    if m:
        lower = ver(m.group(1))
    m = re.search(r">\s*([\d.]+)", raw) if not m else None
    if m and lower is None:
        lower = ver(m.group(1))
    m = re.search(r"<=?\s*([\d.]+)", raw)
    if m:
        upper = ver(m.group(1))
    m = re.match(r"\^\s*([\d.]+)", raw)
    if m:
        lower = ver(m.group(1))
        upper = (lower[0] + 1, 0, 0)
    return lower, upper


# Dart -> Flutter stable (wildcard dipahami subosito/flutter-action)
DART_TO_FLUTTER = {
    (3, 0): "3.10.x", (3, 1): "3.13.x", (3, 2): "3.16.x", (3, 3): "3.19.x",
    (3, 4): "3.22.x", (3, 5): "3.24.x", (3, 6): "3.27.x", (3, 7): "3.29.x",
    (3, 8): "3.32.x", (3, 9): "3.35.x", (3, 10): "3.38.x", (3, 11): "3.41.x",
}
LATEST_KNOWN_DART = (3, 11)


def pick_flutter(lower, upper):
    if upper is not None:
        if upper <= (3, 0, 0):
            return "3.7.x"  # Dart 2.19.x, batas akhir Dart 2
        if upper[0] == 3:
            max_dart = (3, upper[1] - 1) if upper[1] >= 1 else (3, 0)
            if max_dart < LATEST_KNOWN_DART:
                return DART_TO_FLUTTER.get(max_dart, "")
    return ""  # stable terbaru


def cmd_detect_flutter(root):
    lower, upper = sdk_bounds(pubspec_text(root))
    print(pick_flutter(lower, upper))


def cmd_detect_java(root):
    props = read(os.path.join(root, "android", "gradle", "wrapper", "gradle-wrapper.properties")) \
        or read(os.path.join(root, "gradle", "wrapper", "gradle-wrapper.properties"))
    m = re.search(r"gradle-(\d+)\.(\d+)", props)
    if m and int(m.group(1)) < 7:
        print("11")
    else:
        print("17")


# ───────────────────────── Android helpers ─────────────────────────
def app_gradle(root):
    for n in ("build.gradle", "build.gradle.kts"):
        p = os.path.join(root, "android", "app", n)
        if os.path.isfile(p):
            return p
    return None


def manifest_path(root):
    return os.path.join(root, "android", "app", "src", "main", "AndroidManifest.xml")


def safe_project_name(name):
    reserved = {"flutter", "test", "dart", "async", "core", "math", "io", "html", "ui", "main",
                "class", "enum", "new", "var", "void", "this", "super", "null", "true", "false"}
    n = re.sub(r"[^a-z0-9_]", "_", name.lower())
    if not n or not re.match(r"[a-z]", n[0]) or n in reserved:
        n = "app_" + n.lstrip("_0123456789") if n.lstrip("_0123456789") else "my_app"
    return n


def flutter_create(cwd, name, org=None, target="."):
    cmd = ["flutter", "create", "--platforms=android", "--project-name", name]
    if org:
        cmd += ["--org", org]
    cmd.append(target)
    log("jalankan: " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    tail = (r.stdout or "")[-600:] + (r.stderr or "")[-600:]
    if r.returncode != 0:
        log("flutter create gagal: " + tail.replace("\n", " | "))
    return r.returncode == 0


def get_application_id(root):
    g = app_gradle(root)
    if g:
        m = re.search(r"applicationId\s*=?\s*[\"']([^\"']+)[\"']", read(g))
        if m:
            return m.group(1)
        m = re.search(r"namespace\s*=?\s*[\"']([^\"']+)[\"']", read(g))
        if m:
            return m.group(1)
    m = re.search(r'package="([^"]+)"', read(manifest_path(root)))
    return m.group(1) if m else None


def gradle_files_ok(root):
    a = os.path.join(root, "android")
    has_settings = any(os.path.isfile(os.path.join(a, n)) for n in ("settings.gradle", "settings.gradle.kts"))
    has_root = any(os.path.isfile(os.path.join(a, n)) for n in ("build.gradle", "build.gradle.kts"))
    return bool(app_gradle(root)) and has_settings and has_root and os.path.isfile(manifest_path(root))


# ───────────────────────── 1. scaffold android ─────────────────────────
def ensure_android(root):
    name = safe_project_name(pubspec_name(pubspec_text(root)))
    a = os.path.join(root, "android")
    marker = os.path.join(root, ".botfix", "regen_android")

    if os.path.isfile(marker):
        log("marker regen_android ditemukan -> android/ dibuat ulang dari template terbaru")
        regen_android(root, name)
        os.remove(marker)
        return

    if not os.path.isdir(a):
        log("folder android/ TIDAK ADA -> dibuat dengan flutter create")
        flutter_create(root, name)
        return

    if not gradle_files_ok(root):
        log("struktur android/ rusak/tidak lengkap -> dibuat ulang (data lama dipertahankan)")
        regen_android(root, name)
        return

    ensure_wrapper(root)


def ensure_wrapper(root):
    a = os.path.join(root, "android")
    need = [
        os.path.join(a, "gradlew"),
        os.path.join(a, "gradle", "wrapper", "gradle-wrapper.jar"),
        os.path.join(a, "gradle", "wrapper", "gradle-wrapper.properties"),
    ]
    if all(os.path.isfile(p) for p in need):
        try:
            os.chmod(need[0], 0o755)
        except Exception:
            pass
        return
    log("gradle wrapper tidak lengkap -> disalin dari template")
    tpl = make_template()
    if not tpl:
        return
    for rel in ("gradlew", "gradlew.bat", "gradle/wrapper/gradle-wrapper.jar", "gradle/wrapper/gradle-wrapper.properties"):
        src = os.path.join(tpl, "android", rel)
        dst = os.path.join(a, rel)
        if os.path.isfile(src) and not os.path.isfile(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    try:
        os.chmod(os.path.join(a, "gradlew"), 0o755)
    except Exception:
        pass


_TPL = {}


def make_template():
    if "dir" in _TPL:
        return _TPL["dir"]
    d = tempfile.mkdtemp(prefix="flutter_tpl_")
    target = os.path.join(d, "tplapp")
    ok = flutter_create(d, "tplapp", target=target)
    _TPL["dir"] = target if ok and os.path.isdir(os.path.join(target, "android")) else None
    return _TPL["dir"]


def regen_android(root, name):
    a = os.path.join(root, "android")
    old = os.path.join(root, "android_old_bot")
    info = {}
    if os.path.isdir(a):
        if os.path.isdir(old):
            shutil.rmtree(old, ignore_errors=True)
        shutil.move(a, old)
        g_old = None
        for n in ("build.gradle", "build.gradle.kts"):
            p = os.path.join(old, "app", n)
            if os.path.isfile(p):
                g_old = read(p)
                break
        g_old = g_old or ""
        man_old = read(os.path.join(old, "app", "src", "main", "AndroidManifest.xml"))
        m = re.search(r"applicationId\s*=?\s*[\"']([^\"']+)[\"']", g_old)
        info["app_id"] = m.group(1) if m else None
        m = re.search(r"namespace\s*=?\s*[\"']([^\"']+)[\"']", g_old)
        info["namespace"] = m.group(1) if m else None
        if not info["app_id"]:
            m = re.search(r'package="([^"]+)"', man_old)
            info["app_id"] = m.group(1) if m else None
        m = re.search(r"minSdk(?:Version)?\s*=?\s*(\d+)", g_old)
        info["min_sdk"] = m.group(1) if m else None
        m = re.search(r'android:label="([^"]*)"', man_old)
        info["label"] = m.group(1) if m else None
        info["perms"] = re.findall(r"<uses-(?:permission|feature)[^>]*/>", man_old)
        info["queries"] = re.search(r"<queries>.*?</queries>", man_old, re.S)
        info["cleartext"] = 'usesCleartextTraffic="true"' in man_old
        info["legacy_storage"] = "requestLegacyExternalStorage" in man_old
        info["deps"] = re.findall(
            r"^\s*(?:implementation|api|coreLibraryDesugaring|kapt|annotationProcessor)\s*[\(\s][^\n]+", g_old, re.M
        )
        info["gradle_props"] = read(os.path.join(old, "gradle.properties"))

    app_id = info.get("app_id") or ""
    org = None
    if app_id.count(".") >= 1:
        org = app_id.rsplit(".", 1)[0]
    if not flutter_create(root, name, org=org):
        if os.path.isdir(old) and not os.path.isdir(a):
            shutil.move(old, a)  # kembalikan
        log("regen dibatalkan: flutter create gagal")
        return

    if not os.path.isdir(old):
        return

    # --- gabungkan data lama ke android baru ---
    g_new = app_gradle(root)
    if g_new:
        t = read(g_new)
        if app_id:
            t = re.sub(r"(applicationId\s*=?\s*)[\"'][^\"']*[\"']", lambda m: f'{m.group(1)}"{app_id}"', t)
            ns = info.get("namespace") or app_id
            t = re.sub(r"(namespace\s*=?\s*)[\"'][^\"']*[\"']", lambda m: f'{m.group(1)}"{ns}"', t)
        if info.get("min_sdk"):
            t = re.sub(r"minSdk(?:Version)?(\s*=?\s*)flutter\.minSdkVersion",
                       lambda m: f"minSdk{m.group(1)}{info['min_sdk']}", t)
        deps = [d for d in info.get("deps", []) if "kotlin-stdlib" not in d and "testImplementation" not in d]
        if deps:
            kts = g_new.endswith(".kts")
            conv = []
            for d in deps:
                d = d.strip()
                if kts:
                    mm = re.match(r"(\w+)\s*\(?\s*['\"]([^'\"]+)['\"]\s*\)?\s*$", d)
                    if not mm:
                        continue
                    conv.append(f'    {mm.group(1)}("{mm.group(2)}")')
                else:
                    conv.append("    " + d)
            if conv:
                t += "\n\ndependencies {\n" + "\n".join(conv) + "\n}\n"
        write(g_new, t)

    mp = manifest_path(root)
    mt = read(mp)
    if mt:
        extra = [p for p in info.get("perms", []) if p not in mt]
        if info.get("queries") and "<queries>" not in mt:
            extra.append(info["queries"].group(0))
        if extra:
            mt = mt.replace("<application", "    " + "\n    ".join(extra) + "\n    <application", 1)
        attrs = ""
        if info.get("cleartext"):
            attrs += ' android:usesCleartextTraffic="true"'
        if info.get("legacy_storage"):
            attrs += ' android:requestLegacyExternalStorage="true"'
        if attrs:
            mt = mt.replace("<application", "<application" + attrs, 1)
        if info.get("label"):
            mt = re.sub(r'(<application[^>]*?android:label=")[^"]*(")', lambda m: m.group(1) + info["label"] + m.group(2), mt, count=1, flags=re.S)
        write(mp, mt)

    # sumber native lama (MainActivity kustom, plugin native) + resource lama
    main_new = os.path.join(root, "android", "app", "src", "main")
    if any(os.path.isdir(os.path.join(old, "app", "src", "main", s)) for s in ("kotlin", "java")):
        shutil.rmtree(os.path.join(main_new, "kotlin"), ignore_errors=True)
        shutil.rmtree(os.path.join(main_new, "java"), ignore_errors=True)
    for sub in ("kotlin", "java"):
        src = os.path.join(old, "app", "src", "main", sub)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(main_new, sub), dirs_exist_ok=True)
    for sub in ("res", "assets", "jniLibs"):
        src = os.path.join(old, "app", "src", "main", sub)
        dst = os.path.join(root, "android", "app", "src", "main", sub)
        if os.path.isdir(src):
            if sub == "res":
                shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst, dirs_exist_ok=True)
    for rel in ("app/google-services.json", "app/proguard-rules.pro", "key.properties", "app/key.properties"):
        s = os.path.join(old, rel)
        if os.path.isfile(s):
            d = os.path.join(root, "android", rel)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)
    for jks in [p for ext in ("*.jks", "*.keystore") for p in _glob(old, ext)]:
        if os.path.basename(jks) != "debug.keystore":
            rel = os.path.relpath(jks, old)
            d = os.path.join(root, "android", rel)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(jks, d)

    # MainActivity harus ada di package namespace
    ensure_main_activity(root, info.get("namespace") or app_id)
    shutil.rmtree(old, ignore_errors=True)
    log("android/ baru siap (applicationId/permission/resource lama dipertahankan)")


def _glob(base, pattern):
    import fnmatch
    out = []
    for dp, _, fns in os.walk(base):
        for fn in fns:
            if fnmatch.fnmatch(fn, pattern):
                out.append(os.path.join(dp, fn))
    return out


def ensure_main_activity(root, pkg):
    if not pkg:
        return
    main = os.path.join(root, "android", "app", "src", "main")
    for sub in ("kotlin", "java"):
        base = os.path.join(main, sub)
        if os.path.isdir(base) and any(f.startswith("MainActivity") for _, _, fs in os.walk(base) for f in fs):
            return
    d = os.path.join(main, "kotlin", *pkg.split("."))
    write(
        os.path.join(d, "MainActivity.kt"),
        f"package {pkg}\n\nimport io.flutter.embedding.android.FlutterActivity\n\nclass MainActivity : FlutterActivity()\n",
    )
    log(f"MainActivity.kt dibuat untuk package {pkg}")


# ───────────────────────── 2. resource yang dirujuk manifest ─────────────────────────
def res_exists(res_dir, kind, name):
    if not os.path.isdir(res_dir):
        return False
    for d in os.listdir(res_dir):
        if d == kind or d.startswith(kind + "-"):
            for f in os.listdir(os.path.join(res_dir, d)):
                if os.path.splitext(f)[0] == name:
                    return True
    return False


def values_text(res_dir):
    buf = []
    if os.path.isdir(res_dir):
        for d in os.listdir(res_dir):
            if d.startswith("values"):
                p = os.path.join(res_dir, d)
                for f in os.listdir(p):
                    if f.endswith(".xml"):
                        buf.append(read(os.path.join(p, f)))
    return "\n".join(buf)


def fix_resources(root):
    mt = read(manifest_path(root))
    if not mt:
        return
    res = os.path.join(root, "android", "app", "src", "main", "res")
    app_name = pubspec_name(pubspec_text(root))
    fixed = []

    for kind, name in sorted(set(re.findall(r"@(mipmap|drawable)/([A-Za-z0-9_]+)", mt))):
        if res_exists(res, kind, name):
            continue
        if kind == "mipmap":
            for dn, sz in (("mdpi", 48), ("hdpi", 72), ("xhdpi", 96), ("xxhdpi", 144), ("xxxhdpi", 192)):
                write_bin(os.path.join(res, f"mipmap-{dn}", f"{name}.png"), make_png(sz, sz))
        elif name == "launch_background":
            write(
                os.path.join(res, "drawable", f"{name}.xml"),
                '<?xml version="1.0" encoding="utf-8"?>\n<layer-list xmlns:android="http://schemas.android.com/apk/res/android">\n'
                '    <item android:drawable="@android:color/white" />\n</layer-list>\n',
            )
        else:
            write_bin(os.path.join(res, "drawable", f"{name}.png"), make_png(48, 48))
        fixed.append(f"@{kind}/{name}")

    vals = values_text(res)
    new_values = []
    for name in sorted(set(re.findall(r"@style/([A-Za-z0-9_.]+)", mt))):
        if not re.search(rf'<style\s+name="{re.escape(name)}"', vals):
            bg = "@drawable/launch_background" if name == "LaunchTheme" else "?android:colorBackground"
            new_values.append(
                f'    <style name="{name}" parent="@android:style/Theme.Light.NoTitleBar">\n'
                f'        <item name="android:windowBackground">{bg}</item>\n    </style>'
            )
            fixed.append(f"@style/{name}")
    for name in sorted(set(re.findall(r"@string/([A-Za-z0-9_]+)", mt))):
        if not re.search(rf'<string\s+name="{re.escape(name)}"', vals):
            new_values.append(f'    <string name="{name}">{app_name}</string>')
            fixed.append(f"@string/{name}")
    for name in sorted(set(re.findall(r"@color/([A-Za-z0-9_]+)", mt))):
        if not re.search(rf'<color\s+name="{re.escape(name)}"', vals):
            new_values.append(f'    <color name="{name}">#FF2196F3</color>')
            fixed.append(f"@color/{name}")
    if new_values:
        write(os.path.join(res, "values", "bot_heal_values.xml"), "<resources>\n" + "\n".join(new_values) + "\n</resources>\n")

    for name in sorted(set(re.findall(r"@xml/([A-Za-z0-9_]+)", mt))):
        if res_exists(res, "xml", name):
            continue
        if "network" in name:
            body = '<network-security-config>\n    <base-config cleartextTrafficPermitted="true" />\n</network-security-config>\n'
        elif "path" in name:
            body = '<paths>\n    <external-path name="external" path="." />\n    <cache-path name="cache" path="." />\n    <files-path name="files" path="." />\n</paths>\n'
        else:
            body = "<resources />\n"
        write(os.path.join(res, "xml", f"{name}.xml"), '<?xml version="1.0" encoding="utf-8"?>\n' + body)
        fixed.append(f"@xml/{name}")

    if fixed:
        log("resource Android yang hilang dibuat: " + ", ".join(fixed))


# ───────────────────────── 3. google-services.json ─────────────────────────
def fix_google_services(root):
    g = app_gradle(root)
    if not g:
        return
    txt = read(g)
    root_txt = read(os.path.join(root, "android", "build.gradle")) + read(os.path.join(root, "android", "build.gradle.kts")) \
        + read(os.path.join(root, "android", "settings.gradle")) + read(os.path.join(root, "android", "settings.gradle.kts"))
    if "google-services" not in txt + root_txt:
        return
    p = os.path.join(root, "android", "app", "google-services.json")
    if os.path.isfile(p):
        return
    pkg = get_application_id(root) or "com.example.app"
    write(
        p,
        '{\n  "project_info": {"project_number": "000000000000", "project_id": "bot-build-dummy",'
        ' "storage_bucket": "bot-build-dummy.appspot.com"},\n  "client": [{\n    "client_info": {'
        '"mobilesdk_app_id": "1:000000000000:android:0000000000000000",'
        f' "android_client_info": {{"package_name": "{pkg}"}}}},\n'
        '    "oauth_client": [],\n    "api_key": [{"current_key": "AIzaSyDummyKeyForBuildOnly0000000000000"}],\n'
        '    "services": {"appinvite_service": {"other_platform_oauth_client": []}}\n  }],\n  "configuration_version": "1"\n}\n',
    )
    log("google-services.json tidak ada -> dibuat file dummy agar build tidak berhenti")


# ───────────────────────── 4. asset yang dideklarasikan pubspec ─────────────────────────
def declared_assets(txt):
    out = []
    block = top_level_block(txt, "flutter")
    mode = None
    base_indent = None
    for ln in block:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip())
        s = ln.strip()
        if re.match(r"^(assets|fonts|shaders)\s*:\s*(#.*)?$", s):
            mode = s.split(":")[0]
            base_indent = indent
            continue
        if mode and indent <= base_indent and not s.startswith("-"):
            mode = None
        if mode == "assets" or mode == "shaders":
            m = re.match(r"^-\s*(?:path:\s*)?['\"]?([^'\"#\n]+?)['\"]?\s*(#.*)?$", s)
            if m:
                out.append(m.group(1).strip())
        elif mode == "fonts":
            m = re.match(r"^-?\s*asset:\s*['\"]?([^'\"#\n]+?)['\"]?\s*(#.*)?$", s)
            if m:
                out.append(m.group(1).strip())
    return out


def heal_assets(root):
    txt = pubspec_text(root)
    made = []
    for a in declared_assets(txt):
        if not a or "$" in a or a.startswith("packages/"):
            continue
        p = os.path.join(root, a)
        if a.endswith("/"):
            if not os.path.isdir(p):
                os.makedirs(p, exist_ok=True)
                made.append(a)
            if not os.listdir(p):
                write(os.path.join(p, ".keep"), "")
            continue
        if os.path.exists(p):
            continue
        ext = os.path.splitext(a)[1].lower()
        if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico"):
            write_bin(p, make_png(64, 64))
        elif ext == ".svg":
            write(p, '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" fill="#2196F3"/></svg>')
        elif ext in (".json", ".lottie"):
            write(p, "{}")
        else:
            write(p, "")
        made.append(a)
    if made:
        log("asset yang dideklarasikan di pubspec tapi tidak ada -> dibuat placeholder: " + ", ".join(made[:25]))


# ───────────────────────── 5. signing release ─────────────────────────
def fix_signing(root):
    g = app_gradle(root)
    if not g:
        return
    txt = read(g)
    if "release" not in txt:
        return
    a = os.path.join(root, "android")
    kp = os.path.join(a, "key.properties")
    uses_kp = "key.properties" in txt or "keystoreProperties" in txt
    if uses_kp and not os.path.isfile(kp):
        ks = os.path.join(a, "app", "bot-release.jks")
        ok = False
        try:
            r = subprocess.run(
                ["keytool", "-genkeypair", "-noprompt", "-keystore", ks, "-alias", "bot", "-keyalg", "RSA",
                 "-keysize", "2048", "-validity", "10000", "-storepass", "android", "-keypass", "android",
                 "-dname", "CN=Bot Build, O=Bot, C=ID"],
                capture_output=True, text=True,
            )
            ok = r.returncode == 0 and os.path.isfile(ks)
        except Exception:
            ok = False
        if ok:
            write(kp, "storePassword=android\nkeyPassword=android\nkeyAlias=bot\nstoreFile=bot-release.jks\n")
            log("key.properties tidak ada -> keystore sementara dibuat agar build release tidak gagal")
            return
        log("keytool gagal membuat keystore, signing release dialihkan ke debug")
        txt = txt.replace("signingConfigs.release", "signingConfigs.debug").replace(
            'signingConfigs.getByName("release")', 'signingConfigs.getByName("debug")')
        write(g, txt)
        return
    m = re.search(r"storeFile\s*=?\s*file\(\s*[\"']([^\"']+)[\"']", txt)
    if m:
        target = os.path.normpath(os.path.join(a, "app", m.group(1)))
        if not os.path.isfile(target):
            txt = txt.replace("signingConfigs.release", "signingConfigs.debug").replace(
                'signingConfigs.getByName("release")', 'signingConfigs.getByName("debug")')
            write(g, txt)
            log(f"keystore '{m.group(1)}' tidak ada -> signing release dialihkan ke debug")


# ───────────────────────── 6. cek ringan ─────────────────────────
def sanity(root):
    if not os.path.isfile(os.path.join(root, "lib", "main.dart")):
        log("PERINGATAN: lib/main.dart tidak ditemukan")
    txt = pubspec_text(root)
    for m in re.finditer(r"^\s+path:\s*['\"]?([^'\"#\n]+)", txt, re.M):
        p = os.path.normpath(os.path.join(root, m.group(1).strip()))
        if not os.path.exists(p):
            log(f"PERINGATAN: dependency path '{m.group(1).strip()}' tidak ada")


def cmd_heal(root):
    os.chdir(root)
    log(f"project: {root}")
    lower, upper = sdk_bounds(pubspec_text(root))
    log(f"SDK constraint pubspec: lower={lower} upper={upper}")
    ensure_android(root)
    fix_resources(root)
    fix_google_services(root)
    heal_assets(root)
    sanity(root)
    log("selesai heal")


def cmd_heal_late(root):
    fix_signing(root)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    cmd, root = sys.argv[1], os.path.abspath(sys.argv[2])
    {
        "detect-flutter": cmd_detect_flutter,
        "detect-java": cmd_detect_java,
        "heal": cmd_heal,
        "heal-late": cmd_heal_late,
    }[cmd](root)


if __name__ == "__main__":
    main()
