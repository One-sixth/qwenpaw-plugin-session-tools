#!/usr/bin/env python3
"""构建 qwenpaw-plugin-session-tools 插件安装包（zip）。

用法：
    python scripts/build_release.py [版本号]

不带参数时从 plugin.json 读版本号。产出 dist/qwenpaw-plugin-session-tools-v<版本>.zip，
包内为插件根目录布局（plugin.json 在 zip 根），含 README.md / LICENSE / CHANGELOG.md。
排除 git、缓存、测试文件、内部记忆（MEMORY.md 严禁进包）。

安装：
    qwenpaw plugin install dist/qwenpaw-plugin-session-tools-v<版本>.zip --force
"""
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "dist"

# 包内包含的条目（相对仓库根）；目录会递归加入
INCLUDE = [
    "plugin.json",
    "__init__.py",     # 相对导入 .session_ops 需要
    "plugin.py",
    "session_ops.py",
    "frontend",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
]

# 递归时跳过的名字（"*" 开头为后缀模式）
EXCLUDE_NAMES = {"__pycache__", ".pytest_cache", ".DS_Store", "*.pyc"}


def collect(root: Path) -> list[Path]:
    files: list[Path] = []
    for entry in INCLUDE:
        p = root / entry
        if not p.exists():
            print(f"⚠️  跳过缺失条目: {entry}")
            continue
        if p.is_file():
            files.append(p)
            continue
        for f in sorted(p.rglob("*")):
            if not f.is_file():
                continue
            rel_parts = f.relative_to(root).parts
            if any(
                part in EXCLUDE_NAMES or part.endswith(".pyc")
                for part in rel_parts
            ):
                continue
            files.append(f)
    return files


def main() -> int:
    version = sys.argv[1] if len(sys.argv) > 1 else None
    if not version:
        manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        version = manifest["version"]
    version = version.lstrip("v")

    out = OUT_DIR / f"qwenpaw-plugin-session-tools-v{version}.zip"
    out.parent.mkdir(exist_ok=True)
    if out.exists():
        out.unlink()

    files = collect(ROOT)
    if not any(f.name == "plugin.json" for f in files):
        print("❌ plugin.json 未收集到，中止")
        return 1
    if any(f.name == "MEMORY.md" for f in files):
        print("❌ MEMORY.md 混入包内（内部记忆严禁发布），中止")
        return 1

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.relative_to(ROOT).as_posix())

    names = zipfile.ZipFile(out).namelist()
    print(f"✅ {out.relative_to(ROOT)}  ({out.stat().st_size} bytes)")
    print(f"   共 {len(names)} 个文件:")
    for n in sorted(names):
        print("   -", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
