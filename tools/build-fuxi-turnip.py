#!/usr/bin/env python3
"""Build/sign the pinned Turnip ICD, no-submit probe and staged smoke tool on an Apple Silicon Mac.

Supply external Mesa sources and pinned UniXNU text patches; caches must stay out of
this repository. See docs-lanes/metal.md for pins, setup and audit limitations.
"""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MESA = '85fda3b36405bcaea8bffc64aa4f2651dc90770d'
UNIXNU = 'fb6aad6ab1f50199a4d99d2da1d0ca403b067633'
PATCHES = {
    'freedreno-tools-nir.patch': '77e966b6e9a725892d10889a4d8deeefb03a32ae3a007904868132e56350191f',
    'ir3-darwin-build-id.patch': 'be3ca7840a16cb7696d8d7e67195c8d097cf0e38a3b5011edc674a5aed9c6975',
    'ir3-portable-asm-roundtrip.patch': 'bfc5552bc2bd6e1668d75773cce7bf62fd19065e6281bf7da49f9ddfc2e79339',
    'offline-adreno-compiler-probe.patch': 'b7d7a113beb5ef99edea24858d906194d4b4755c987520545ad2f7f3136bb4fb',
    'turnip-darwin-kmd.patch': 'f3bc6d470f8e0bde1b1d0d370175ec105ee45fe50075e43279581e61b1455c2b',
    'turnip-graphics-cache-feedback.patch': '07f397e38d9acfb0085db4c19567c84fd8a3bcd63d6fd05192857abfbde5aed2',
    'turnip-empty-scissor.patch': 'dec657626dcd8e8d3f1447e44574f31e4afb8c90b6ebf7d589ba10ee0f8b6461',
    'turnip-builtin-shader-pack.patch': '3c606dc99643c3ca3e4736c5783348cd85cbff3fbc55161e53226ab459f34473',
    'turnip-host-import-images.patch': '8bc141db41848a59b16493b4ca8a359e0f18f1cad5f14147ff18d15f79c0c3c4',
}


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def read(cmd):
    return subprocess.check_output([str(c) for c in cmd], text=True).strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--mesa', type=Path, required=True)
    ap.add_argument('--patches', type=Path, required=True, help='UniXNU patches/mesa directory (hash verified)')
    ap.add_argument('--build', type=Path, required=True)
    ap.add_argument('--out', type=Path, default=ROOT/'tools/out')
    ap.add_argument('--meson', default='meson')
    ap.add_argument('--verify-inputs-only', action='store_true')
    args = ap.parse_args()
    if not args.verify_inputs_only and os.uname().sysname != 'Darwin':
        ap.error('requires native Mac, iPhoneOS SDK, Meson, Ninja, glslangValidator and Python mako/yaml/packaging')
    source, build, out = (p.resolve() for p in (args.mesa, args.build, args.out))
    for p in (source, build):
        if p.is_relative_to(ROOT):
            rel = p.relative_to(ROOT)
            if subprocess.run(['git', '-C', str(ROOT), 'check-ignore', '-q', str(rel)]).returncode:
                ap.error('source/build directory inside repository must be ignored: '+str(rel))
    if read(['git', '-C', source, 'rev-parse', 'HEAD']) != MESA:
        ap.error('Mesa HEAD must be '+MESA)
    ordered=[]
    for name, digest in PATCHES.items():
        patch = (args.patches/name).resolve()
        if hashlib.sha256(patch.read_bytes()).hexdigest() != digest:
            ap.error('patch hash mismatch: '+name)
        ordered.append(patch)
    ordered += [ROOT/'tools/patches/mesa-fuxi-smoke.patch',
                ROOT/'tools/patches/mesa-fuxi-hostimport.patch',
                ROOT/'tools/patches/mesa-fuxi-ir3-round-default.patch',
                ROOT/'tools/patches/mesa-fuxi-fence-wait-100ms.patch',
                ROOT/'tools/patches/mesa-fuxi-cached-coherent.patch']
    # Accept only an exact prefix of this stack, including a completed cache.
    # Reverse-checking an upstream added file fails after a local overlay.
    with tempfile.TemporaryDirectory() as tmp:
        git_env=dict(os.environ,GIT_INDEX_FILE=str(Path(tmp)/'prefix.index'))
        run(['git','-C',source,'read-tree',MESA],env=git_env)
        prefix = None
        for i in range(len(ordered)+1):
            if not subprocess.run(['git','-C',str(source),'diff','--quiet'],env=git_env).returncode:
                prefix = i
            if i<len(ordered):
                run(['git','-C',source,'apply','--cached',ordered[i]],env=git_env)
        if prefix is None:
            ap.error('Mesa worktree is not an exact known patch prefix')
    for patch in ordered[prefix:]:
        run(['git','-C',source,'apply','--check',patch])
        run(['git','-C',source,'apply',patch])
    # Reconstruct the exact expected patched tree in a temporary index. Reject
    # cache edits beyond the pinned patches, including edits in patched files.
    with tempfile.TemporaryDirectory() as tmp:
        git_env=dict(os.environ,GIT_INDEX_FILE=str(Path(tmp)/'mesa.index'))
        run(['git','-C',source,'read-tree',MESA],env=git_env)
        for patch in ordered: run(['git','-C',source,'apply','--cached',patch],env=git_env)
        run(['git','-C',source,'diff','--exit-code'],env=git_env)
        print('PASS: exact pinned patch set; ABI SHA256 '+hashlib.sha256((ROOT/'xnu/fuxi-adreno/NativeAdrenoABI.h').read_bytes()).hexdigest())
    if args.verify_inputs_only: return
    if not shutil.which('glslangValidator'):
        ap.error('missing native glslangValidator')
    if '3.8.2' not in read(['bison','--version']).splitlines()[0]:
        ap.error('requires pinned native Bison 3.8.2 first on PATH (Apple Bison 2.3 is insufficient)')
    sdk = read(['xcrun','--sdk','iphoneos','--show-sdk-path'])
    find = lambda tool: read(['xcrun','--sdk','iphoneos','--find',tool])
    flags = ['-target','arm64-apple-ios16.0','-isysroot',sdk,'-fstack-protector-strong',
             '-Werror=unguarded-availability-new']
    build.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    cross = build/'ios16.cross'
    cross.write_text('[binaries]\n'+''.join('%s = %r\n'%(k,find(v)) for k,v in
                     [('c','clang'),('cpp','clang++'),('objc','clang'),('ar','ar'),('strip','strip')])+
                     "pkg-config = '/usr/bin/false'\n[host_machine]\nsystem = 'darwin'\ncpu_family = 'aarch64'\ncpu = 'arm64'\nendian = 'little'\n"
                     '[properties]\nneeds_exe_wrapper = true\n[built-in options]\n'+
                     ''.join('%s = %r\n'%(k,flags) for k in
                             ('c_args','cpp_args','objc_args','c_link_args','cpp_link_args','objc_link_args')))
    # Empty pack options also clear an old cache's embedded-pack configuration.
    # Release keeps mesa_logi (including the smoke submit oracle) and TU_DEBUG.
    options = ['-Dgallium-drivers=', '-Dvulkan-drivers=freedreno', '-Dfreedreno-kmds=darwin',
               '-Dtu-darwin-abi-dir='+str(ROOT/'xnu/fuxi-adreno'), '-Dplatforms=macos', '-Dglx=disabled',
               '-Degl=disabled','-Dgbm=disabled','-Dopengl=false','-Dgles1=disabled','-Dgles2=disabled',
               '-Dllvm=disabled','-Dshared-glapi=disabled','-Dxmlconfig=disabled','-Dshader-cache=disabled',
               '-Dtools=','-Dbuild-tests=false','-Dvideo-codecs=','-Dvulkan-layers=',
               '-Dlibunwind=disabled','-Dlmsensors=disabled','-Dvalgrind=disabled','-Dexpat=disabled','-Dzstd=disabled',
               '-Dbuildtype=release','-Db_ndebug=true',
               '-Dtu-builtin-source-id=','-Dtu-builtin-pack-header=']
    env = {k:v for k,v in os.environ.items() if not k.startswith(('IR3_','FD_MESA_','NIR_','MESA_SHADER_','TU_'))}
    env.update(CC=find('clang'), CXX=find('clang++'), CFLAGS='', CXXFLAGS='', CPPFLAGS='', LDFLAGS='')
    setup = [args.meson,'setup',build,source,'--cross-file',cross,'--wrap-mode=nodownload']
    if (build/'build.ninja').exists():
        setup += ['--reconfigure']
    run(setup+options, env=env)
    run(['ninja','-C',build,'-j6'], env=env)
    library = out/'libvulkan_freedreno.dylib'
    shutil.copyfile(build/'src/freedreno/vulkan/libvulkan_freedreno.dylib',library)
    run(['install_name_tool','-id','/usr/local/lib/libvulkan_freedreno.dylib',library])
    if ' T _vk_icdGetInstanceProcAddr' not in read(['nm','-gU',library]):
        raise SystemExit('missing exported vk_icdGetInstanceProcAddr')
    probe = out/'fuxi-vk-probe'
    run([find('clang++'),*flags,'-std=c++17','-O2','-Wall','-Wextra','-Werror',
         '-I'+str(source/'include'),ROOT/'tools/fuxi-vk-probe.cpp','-o',probe])
    run(['python3',ROOT/'tools/build-fuxi-vk-shaders.py',out/'vk-smoke-spv.h'])
    smoke = out/'fuxi-vk-smoke'
    run([find('clang++'),*flags,'-std=c++17','-O2','-Wall','-Wextra','-Werror',
         '-I'+str(source/'include'),'-I'+str(out),ROOT/'tools/fuxi-vk-smoke.cpp','-o',smoke])
    shutil.copyfile(ROOT/'tools/vk-smoke.conf',out/'vk-smoke.conf')
    for binary in (library,probe,smoke):
        run(['codesign','-f','-s','-',binary])
        run(['codesign','--verify','--strict',binary])
    run(['python3',ROOT/'tools/audit-fuxi-vulkan.py',library,probe])
    (out/'M2-SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in (library,probe)))
    (out/'M3-SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in (library,smoke,out/'vk-smoke.conf')))
    run(['python3',ROOT/'tools/audit-fuxi-vulkan.py',library,smoke])
    print((out/'M3-SHA256SUMS').read_text())


if __name__ == '__main__':
    main()
