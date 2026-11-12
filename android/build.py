#!/usr/bin/env python3
"""Build the arm64 APK with pinned sources and an externally supplied signer.

Run through scripts/dev. This script does not install anything, contact a VPS,
change networking, invoke Gradle, or use the privileged tunnel service.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

from harness.tunnel_builder import _resource, extract_archive, fetch_archive

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
ZXING_URL = "https://repo.maven.apache.org/maven2/com/google/zxing/core/3.5.4/core-3.5.4.jar"
ZXING_SHA256 = "71de5d89341b5fcf5dd89da7f44e84d825d0e084cdf3ec77c9abe26b0f0ceb13"
TAGS = "with_gvisor,with_quic,with_utls"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(
    argv: list[str | Path], *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> str:
    return subprocess.check_output([str(arg) for arg in argv], text=True, cwd=cwd, env=env)


def prepare_source(work: Path, cache: Path, recipe: dict[str, object]) -> Path:
    source = work / "engine-source"
    if source.exists():
        raise SystemExit(
            "Use a fresh --work directory; source trees are never trusted from prior runs"
        )
    spec = recipe["source"]
    assert isinstance(spec, dict)
    archive = cache / "sing-box-source.tar.gz"
    if not archive.exists():
        fetch_archive(spec, archive)
    if digest(archive) != spec["sha256"]:
        raise SystemExit("Core archive checksum mismatch")
    extract_archive(archive, source, spec["member_prefix"])
    replacements = recipe["replacements"]
    assert isinstance(replacements, list)
    for replacement in replacements:
        (source / replacement["target"]).write_bytes(_resource(replacement))
    overrides = recipe["module_overrides"]
    assert isinstance(overrides, list)
    for index, override in enumerate(overrides):
        archive = cache / f"override-{index}.zip"
        spec = override["source"]
        if not archive.exists():
            fetch_archive(spec, archive)
        if digest(archive) != spec["sha256"]:
            raise SystemExit("Module override checksum mismatch")
        target = source / override["destination"]
        extract_archive(archive, target, spec["member_prefix"])
        for replacement in override["file_replacements"]:
            path = target / replacement["target"]
            old = replacement["original_sha256"]
            if (old is None and path.exists()) or (old and digest(path) != old):
                raise SystemExit("Module patch baseline mismatch")
            path.write_bytes(_resource(replacement))
    shutil.copytree(ROOT / "native", source / "cmd/harness_android")
    return source


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--go", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--keystore", type=Path, required=True)
    parser.add_argument("--alias", default="harness-android")
    parser.add_argument("--password-env", default="HARNESS_ANDROID_SIGNING_PASSWORD")
    parser.add_argument("--module-cache", type=Path)
    parser.add_argument("--go-build-cache", type=Path)
    args = parser.parse_args()
    for name in ("sdk", "go", "work", "cache", "out", "keystore"):
        setattr(args, name, getattr(args, name).resolve())
    if not args.keystore.is_file() or not os.environ.get(args.password_env):
        raise SystemExit(
            "Supply a private keystore and its password through the named environment variable"
        )
    version = run([args.go, "version"]).strip()
    if version != "go version go1.26.10 linux/amd64":
        raise SystemExit("Pinned Go 1.26.10 linux/amd64 is required")
    args.work.mkdir(parents=True, exist_ok=True)
    args.cache.mkdir(parents=True, exist_ok=True)
    tools = args.sdk / "build-tools/36.0.0"
    platform = args.sdk / "platforms/android-36/android.jar"
    ndk = args.sdk / "ndk/27.1.12297006/toolchains/llvm/prebuilt/linux-x86_64/bin"
    recipe_path = REPO / "src/harness/tunnel_recipe/engine-source-build-recipe.json"
    recipe = json.loads(recipe_path.read_bytes())
    source = prepare_source(args.work, args.cache, recipe)
    jar = args.cache / "core-3.5.4.jar"
    if not jar.exists():
        with urllib.request.urlopen(ZXING_URL, timeout=30) as response:
            content = response.read(1024 * 1024)
        if hashlib.sha256(content).hexdigest() != ZXING_SHA256:
            raise SystemExit("ZXing checksum mismatch")
        jar.write_bytes(content)
    if digest(jar) != ZXING_SHA256:
        raise SystemExit("ZXing checksum mismatch")
    environment = {
        **os.environ,
        "GOENV": "off",
        "GOTOOLCHAIN": "local",
        "GOWORK": "off",
        "GOROOT": str(args.go.parent.parent),
        "GOPATH": str(args.work / "gopath"),
        "GOMODCACHE": str(args.module_cache or args.work / "gomodcache"),
        "GOCACHE": str(args.go_build_cache or args.work / "gocache"),
        "GOOS": "android",
        "GOARCH": "arm64",
        "CGO_ENABLED": "1",
        "CC": str(ndk / "aarch64-linux-android26-clang"),
        "CXX": str(ndk / "aarch64-linux-android26-clang++"),
        "GOPROXY": "https://proxy.golang.org",
        "GOSUMDB": "sum.golang.org",
        "GOFLAGS": "",
        "GOEXPERIMENT": "",
        "GOPRIVATE": "",
        "GONOPROXY": "",
        "GONOSUMDB": "",
    }
    graph = run([args.go, "list", "-m", "-json", "all"], cwd=source, env=environment)
    remaining = graph
    modules = []
    decoder = json.JSONDecoder()
    while remaining.strip():
        module, end = decoder.raw_decode(remaining.lstrip())
        modules.append((module["Path"], module.get("Version", "")))
        remaining = remaining.lstrip()[end:]
    locked = json.loads(_resource(recipe["resolved_modules"]))
    if sorted(modules) != sorted((m["Path"], m.get("Version", "")) for m in locked):
        raise SystemExit("Core module graph mismatch")
    run([args.go, "mod", "verify"], cwd=source, env=environment)
    lib = args.work / "libharnessbox.so"
    flags = "-X github.com/sagernet/sing-box/constant.Version=1.14.4-harness.2-android -checklinkname=0 -buildid= -s -w -extldflags=-Wl,-z,max-page-size=16384"
    subprocess.run(
        [
            str(args.go),
            "build",
            "-mod=readonly",
            "-buildvcs=false",
            "-trimpath",
            "-tags",
            TAGS,
            "-ldflags",
            flags,
            "-buildmode=c-shared",
            "-o",
            str(lib),
            "./cmd/harness_android",
        ],
        cwd=source,
        env=environment,
        check=True,
    )
    for directory in ("classes", "gen", "dex"):
        (args.work / directory).mkdir()
    run([tools / "aapt2", "compile", "--dir", ROOT / "res", "-o", args.work / "resources.zip"])
    run(
        [
            tools / "aapt2",
            "link",
            "-o",
            args.work / "resources.apk",
            "-I",
            platform,
            "--manifest",
            ROOT / "AndroidManifest.xml",
            "--java",
            args.work / "gen",
            args.work / "resources.zip",
        ]
    )
    java = sorted((ROOT / "src").rglob("*.java")) + sorted((args.work / "gen").rglob("*.java"))
    run(
        [
            "javac",
            "--release",
            "8",
            "-encoding",
            "UTF-8",
            "-classpath",
            f"{platform}:{jar}",
            "-d",
            args.work / "classes",
            *java,
        ]
    )
    run(
        [
            tools / "d8",
            "--release",
            "--min-api",
            "26",
            "--lib",
            platform,
            "--output",
            args.work / "dex",
            jar,
            *sorted((args.work / "classes").rglob("*.class")),
        ]
    )
    module_directories = set(
        run(
            [
                args.go,
                "list",
                "-mod=readonly",
                "-buildvcs=false",
                "-tags",
                TAGS,
                "-deps",
                "-f",
                "{{with .Module}}{{.Dir}}{{end}}",
                "./cmd/harness_android",
            ],
            cwd=source,
            env=environment,
        ).splitlines()
    )
    notices: list[tuple[Path, str]] = []
    for directory in sorted(module_directories):
        if not directory:
            continue
        module_path = Path(directory)
        for pattern in ("LICENSE*", "COPYING*", "NOTICE*"):
            for notice in sorted(module_path.glob(pattern)):
                if notice.is_file() and notice.stat().st_size < 1024 * 1024:
                    notices.append((notice, module_path.name + "/" + notice.name))
    unsigned = args.work / "unsigned.apk"
    shutil.copyfile(args.work / "resources.apk", unsigned)
    provenance = {
        "core_recipe_sha256": digest(recipe_path),
        "core_commit": recipe["source"]["commit"],
        "go": version,
        "abi": "arm64-v8a",
        "min_sdk": 26,
        "target_sdk": 36,
        "build_tools": "36.0.0",
        "ndk": "27.1.12297006",
        "zxing_sha256": ZXING_SHA256,
        "native_sha256": digest(lib),
        "sources": {
            str(p.relative_to(ROOT)): digest(p) for p in sorted(ROOT.rglob("*")) if p.is_file()
        },
    }
    with zipfile.ZipFile(unsigned, "a", compression=zipfile.ZIP_DEFLATED) as apk:
        apk.write(args.work / "dex/classes.dex", "classes.dex")
        apk.write(lib, "lib/arm64-v8a/libharnessbox.so")
        for path, name in notices:
            apk.write(path, "assets/third-party/" + name)
        for path in sorted((ROOT / "assets").rglob("*")):
            if path.is_file():
                apk.write(path, "assets/" + str(path.relative_to(ROOT / "assets")))
        apk.writestr("assets/build-provenance.json", json.dumps(provenance, indent=2))
    aligned = args.work / "aligned.apk"
    run([tools / "zipalign", "-P", "16", "-f", "4", unsigned, aligned])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            tools / "apksigner",
            "sign",
            "--ks",
            args.keystore,
            "--ks-key-alias",
            args.alias,
            "--ks-pass",
            "env:" + args.password_env,
            "--key-pass",
            "env:" + args.password_env,
            "--out",
            args.out,
            aligned,
        ]
    )
    signature = run([tools / "apksigner", "verify", "--verbose", "--print-certs", args.out])
    run([tools / "zipalign", "-c", "-P", "16", "4", args.out])
    provenance["apk_sha256"] = digest(args.out)
    provenance["signature_verification"] = signature
    args.out.with_suffix(".build.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(json.dumps({"apk": str(args.out), "sha256": provenance["apk_sha256"]}))


if __name__ == "__main__":
    main()
