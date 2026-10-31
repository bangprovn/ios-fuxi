#!/usr/bin/env python3
"""Linux runtime-model tests; native Mac frontend/smoke tests with CPU model.

Synthetic 528-byte base/root/registry ID and CPU transfers; no iOS discovery or
GPU execution is qualified by these tests. Production backend tested separately.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile
ROOT=Path(__file__).resolve().parents[1]

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--compile-only',action='store_true',help='build all applicable tests without running executables')
    a=ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        d=Path(tmp);conf=d/'smoke.conf'
        compiler=['xcrun','clang++'] if os.uname().sysname=='Darwin' else ['clang++']
        for name in ('ClientGateTests','BackboardGateTests','TextureLevelsTests','MipAccessTests','SurveyTests','ClientSetupTests','NativeSelectionTests','SurfaceLeaseTests','TranslationPolicyTests','CompileBoostTests','FrameTraceTests','FrameCPUTests','OperationAPITests','FrontendEncodingTests','TransientMemoryTests','SlabMemoryTests','DrawPlanTests','DrawPlanCacheTests','ShadowRangesTests','PrewarmStoreTests','SpirvRelaxTests','SpirvLoopsTests','SpirvUnrollTests','SpirvHalfRoundTests','OcclusionProofTests','MemoryTypeTests'):
            subprocess.run([*compiler,'-std=c++17','-Wall','-Wextra','-Werror','-pthread',
                            '-fsanitize=address,undefined',str(ROOT/'tools/fuxi-metal'/(name+'.cpp')),
                            '-o',str(d/name)],check=True)
            if not a.compile_only:subprocess.run([str(d/name)],check=True,timeout=15)
            if not a.compile_only and name == 'FrameTraceTests':
                subprocess.run([str(d/name)],env=dict(os.environ,FUXI_MTL_TEST_BOOTARGS='fuxigpuoverlap=1'),check=True,timeout=15)
            if not a.compile_only and name == 'TranslationPolicyTests':
                # FUXI_MTL_HALF_ROUND: modes 1-3 only; lossy for every pipeline unless FUXI_MTL_HALF_ROUND_PIPES lists fh hashes (a malformed entry ends the list)
                for mode,pipes,fh,want in (('0',None,'1','1'),('1',None,'1','1'),('3',None,'abc','1'),('4',None,'1','1'),('2','abc,0x12','12','1'),('2','abc,12','13','0'),('3','abc,zz,12','12','0'),('3','abc,zz','abc','1')):
                    e=dict(os.environ,FUXI_MTL_HALF_ROUND=mode);e.pop('FUXI_MTL_HALF_ROUND_PIPES',None)
                    if pipes is not None:e['FUXI_MTL_HALF_ROUND_PIPES']=pipes
                    subprocess.run([str(d/name),'half',mode if mode in ('1','2','3') else '0',fh,want],env=e,check=True,timeout=15)
                for args,env,want in (('',None,'0'),('','1','1'),('','0','0'),('fuxigpurename=1',None,'1'),('fuxigpurename=0','1','0')):
                    e=dict(os.environ,FUXI_MTL_TEST_BOOTARGS=args);e.pop('FUXI_MTL_SHADOW_RENAME',None)
                    if env is not None:e['FUXI_MTL_SHADOW_RENAME']=env
                    subprocess.run([str(d/name),'rename',want],env=e,check=True,timeout=15)
                for env,want in ((None,'0'),('1','1'),('0','0'),('2','2'),('3','0'),('1 ','0'),('2 ','0'),('','0')):
                    e=dict(os.environ);e.pop('FUXI_MTL_SUBMIT_LEAN',None)
                    if env is not None:e['FUXI_MTL_SUBMIT_LEAN']=env
                    subprocess.run([str(d/name),'lean',want],env=e,check=True,timeout=15)
                # FUXI_MTL_SPLIT_SUBMIT: exactly "1"; _US 0..100000 (default 400), _MAX 2..8 (default 3); malformed values keep the default
                for on,us,mx,want in ((None,None,None,('0','400','3')),('1',None,None,('1','400','3')),('2','0','2',('0','0','2')),('1','100000','8',('1','100000','8')),('1 ','100001','9',('0','400','3')),('1','-1','1',('1','400','3')),('1','5x','04',('1','400','4'))):
                    e=dict(os.environ)
                    for k,v in (('FUXI_MTL_SPLIT_SUBMIT',on),('FUXI_MTL_SPLIT_SUBMIT_US',us),('FUXI_MTL_SPLIT_SUBMIT_MAX',mx)):
                        e.pop(k,None)
                        if v is not None:e[k]=v
                    subprocess.run([str(d/name),'split',*want],env=e,check=True,timeout=15)
                for env,want in ((None,'0'),('1','1'),('0','0'),('2','0'),('1 ','0'),('','0')):
                    e=dict(os.environ);e.pop('FUXI_MTL_BUFFER_CACHED',None)
                    if env is not None:e['FUXI_MTL_BUFFER_CACHED']=env
                    subprocess.run([str(d/name),'cached',want],env=e,check=True,timeout=15)
            if not a.compile_only and name == 'FrameTraceTests':
                subprocess.run([str(d/name)],env=dict(os.environ,FUXI_MTL_TEST_BOOTARGS='fuxigpumerge=1 fuxigpuoverlap=1'),check=True,timeout=15)
            if not a.compile_only and name == 'FrameCPUTests':
                for level in ('1', '2'):
                    subprocess.run([str(d/name)],env=dict(os.environ,FUXI_MTL_TEST_BOOTARGS='fuxigpuframetrace='+level),check=True,timeout=15)
            if not a.compile_only and name == 'TranslationPolicyTests':
                keys = ('fuxigpustages', 'fuxigputrace', 'fuxigpuasync')
                enabled = dict(os.environ, FUXI_MTL_STAGE_CACHE='1', FUXI_MTL_TRANSLATION_TRACE='1', FUXI_MTL_ASYNC_PIPELINES='1', FUXI_MTL_COMPILE_BOOST='1')
                for args, expected in (('', '1110'), (' '.join(k+'=0' for k in keys), '0000'),
                                       (' '.join(k+'=1 '+k+'=1' for k in keys), '0000'),
                                       (' '.join(k+'=invalid' for k in keys), '0000'),
                                       (' '.join('prefix'+k+'=0' for k in keys), '1110'), ('fuxicompileboost=1', '1111'),
                                       ('fuxicompileboost=1 fuxicompileboost=1', '1110'),
                                       ('fuxicompileboost=invalid', '1110'),
                                       ('fuxigpuframetrace=1 fuxigpupipelined=1', '111011'),
                                       ('fuxigpuframetrace=1 fuxigpuframetrace=1 fuxigpupipelined=bad', '111000'),
                                       ('fuxigpuframetrace=0 fuxigpupipelined=0', '111000')):
                    subprocess.run([str(d/name), expected], env=dict(enabled, FUXI_MTL_TEST_BOOTARGS=args), check=True, timeout=15)
        if not a.compile_only:
            subprocess.run(['python3',str(ROOT/'tools/test-fuxi-metal-stage.py'),'--policy-storage-only'],check=True)
        subprocess.run([*compiler,'-std=c++17','-O1','-g','-Wall','-Wextra','-Werror',
                        '-fsanitize=address,undefined','-pthread',
                        str(ROOT/'tools/fuxi-metal/ConstantHooksTests.cpp'),'-o',str(d/'constant-hooks')],check=True)
        if not a.compile_only:subprocess.run([str(d/'constant-hooks')],check=True,timeout=15)
        subprocess.run([*compiler,'-std=c++17','-Wall','-Wextra','-Werror',
                        '-fsanitize=address,undefined',str(ROOT/'tools/fuxi-metal/VertexProfileTests.cpp'),
                        '-o',str(d/'vertex-profile')],check=True)
        if not a.compile_only:subprocess.run([str(d/'vertex-profile')],check=True,timeout=15)
        subprocess.run([*compiler,'-std=c++17','-Wall','-Wextra','-Werror','-pthread',
                        '-fsanitize=address,undefined',str(ROOT/'tools/fuxi-metal/TimelineTests.cpp'),
                        '-o',str(d/'timeline')],check=True)
        if not a.compile_only:subprocess.run([str(d/'timeline')],check=True,timeout=15)
        subprocess.run([*compiler,'-std=c++17','-Wall','-Wextra','-Werror',
                        '-fsanitize=address,undefined',str(ROOT/'tools/fuxi-metal/HostImportRangeTests.cpp'),
                        '-o',str(d/'host-import-range')],check=True)
        if not a.compile_only:subprocess.run([str(d/'host-import-range')],check=True,timeout=15)
        subprocess.run([*compiler,'-std=c++17','-Wall','-Wextra','-Werror',
                        '-fsanitize=address,undefined',str(ROOT/'tools/fuxi-metal/ResourceCacheTests.cpp'),
                        '-o',str(d/'resource-cache')],check=True)
        if not a.compile_only:subprocess.run([str(d/'resource-cache')],check=True,timeout=30)
        if os.uname().sysname!='Darwin':
            print('SKIP: native Objective-C frontend/smoke tests require Mac Foundation/Metal')
            return
        flags=['-O1','-g','-Wall','-Wextra','-Werror','-Wmissing-field-initializers','-fno-objc-arc','-fblocks','-fsanitize=address,undefined']
        frameworks=['-framework','Foundation','-framework','Metal','-framework','IOSurface']
        def call(command):subprocess.run(command,check=True)
        if a.compile_only:
            for name in ['EncodingFrontendTests','ShadowFrontendTests','OverlapFrontendTests','AsyncPipelineFrontendTests','ConstantFrontendTests','FrontendTests','PipelineFrontendTests','CAFrontendTests','MipFrontendTests','MRTFrontendTests','GateFrontendTests','CensusTests','RenderServerFrontendTests','BlitFrontendTests','LinearFrontendTests','ScanoutFrontendTests']:
                call(['xcrun','clang++','-std=c++17',*flags,str(ROOT/'tools/fuxi-metal'/(name+'.mm')),*frameworks,'-o',str(d/name)])
            print('PASS: compile-only frontend tests; no test executables run')
            return
        call(['xcrun','clang++','-std=c++17',*flags,str(ROOT/'tools/fuxi-metal/EncodingFrontendTests.mm'),*frameworks,'-o',str(d/'encoding')])
        subprocess.run([str(d/'encoding')],env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0'),check=True,timeout=30)
        call(['xcrun','clang++','-std=c++17',*flags,str(ROOT/'tools/fuxi-metal/ShadowFrontendTests.mm'),*frameworks,'-o',str(d/'shadows')])
        subprocess.run([str(d/'shadows')],env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0',FUXI_MTL_TEST_BOOTARGS='fuxigpuoverlap=1 fuxigpuframetrace=1'),check=True,timeout=30)
        call(['xcrun','clang++','-std=c++17',*flags,str(ROOT/'tools/fuxi-metal/OverlapFrontendTests.mm'),*frameworks,'-o',str(d/'overlap')])
        subprocess.run([str(d/'overlap')],env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0',FUXI_MTL_TEST_BOOTARGS='fuxigpuoverlap=1 fuxigpuframetrace=2'),check=True,timeout=30)
        call(['xcrun','clang++','-std=c++17',*flags,str(ROOT/'tools/fuxi-metal/AsyncPipelineFrontendTests.mm'),*frameworks,'-o',str(d/'async-pipelines')])
        subprocess.run([str(d/'async-pipelines')],env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0',FUXI_MTL_ASYNC_PIPELINES='1'),check=True,timeout=15)
        call(['xcrun','clang++','-std=c++17',*flags,str(ROOT/'tools/fuxi-metal/ScanoutFrontendTests.mm'),*frameworks,'-o',str(d/'scanout')])
        subprocess.run([str(d/'scanout')],env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0'),check=True,timeout=15)
        call(['xcrun','clang++','-std=c++17',*flags,str(ROOT/'tools/fuxi-metal/LinearFrontendTests.mm'),*frameworks,'-o',str(d/'linear')])
        subprocess.run([str(d/'linear')],env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0'),check=True,timeout=15)
        call(['xcrun','clang++','-std=c++17',*flags,str(ROOT/'tools/fuxi-metal/BlitFrontendTests.mm'),*frameworks,'-o',str(d/'blits')])
        subprocess.run([str(d/'blits')],env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0'),check=True,timeout=15)
        call(['xcrun','clang++','-std=c++17',*flags,str(ROOT/'tools/fuxi-metal/RenderServerFrontendTests.mm'),*frameworks,'-o',str(d/'server')])
        server=subprocess.run([str(d/'server')],env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0'),text=True,capture_output=True,check=True,timeout=15)
        assert server.stderr.count('draw dropped missing render pipeline')==1,server.stderr
        print(server.stdout,end='')
        call(['xcrun','clang++','-std=c++17',*flags,str(ROOT/'tools/fuxi-metal/MRTFrontendTests.mm'),*frameworks,'-o',str(d/'mrt')])
        mrt=subprocess.run([str(d/'mrt')],env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0'),text=True,capture_output=True,check=True,timeout=15)
        refusals=[line for line in mrt.stderr.splitlines() if 'renderPass reject ' in line]
        assert len(refusals)>=9 and all('desc=renderPass' in line and '7:texture=' in line and 'clear=' in line and 'slice=' in line for line in refusals),mrt.stderr
        print(mrt.stdout,end='')
        call(['xcrun','clang++','-std=c++17',*flags,str(ROOT/'tools/fuxi-metal/MipFrontendTests.mm'),*frameworks,'-o',str(d/'mips')])
        mip=subprocess.run([str(d/'mips')],env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0'),text=True,capture_output=True,check=True,timeout=15)
        rejects=[line for line in mip.stderr.splitlines() if 'FUXI MTL: texture reject ' in line]
        assert len(rejects)>=6 and all('desc=' in line and 'mips=' in line and 'usage=' in line and 'swizzle=' in line for line in rejects),mip.stderr
        rgb=[line for line in rejects if 'fourcc0x5f524234' in line]
        assert len(rgb)==2 and all('/planes2/' in line and 'p0=size8x8/element1/row64/offset0' in line and 'p1=size8x8/element2/row64/offset4096' in line for line in rgb),mip.stderr
        print(mip.stdout,end='')
        subprocess.run([str(d/'mips')],env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0',FUXI_MTL_TEST_BOOTARGS='fuxigpuskip=1'),check=True,timeout=15)
        call(['xcrun','clang++','-std=c++17',*flags,str(ROOT/'tools/fuxi-metal/CAFrontendTests.mm'),*frameworks,'-o',str(d/'ca-frontend')])
        subprocess.run([str(d/'ca-frontend')],env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0'),check=True,timeout=15)
        call(['xcrun','clang++','-std=c++17',*flags,str(ROOT/'tools/fuxi-metal/ConstantFrontendTests.mm'),*frameworks,'-o',str(d/'constant-frontend')])
        constants=subprocess.run([str(d/'constant-frontend')],env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0'),text=True,capture_output=True,check=True,timeout=15)
        failures=[line for line in constants.stderr.splitlines() if 'constants snapshot FAIL' in line]
        assert len(failures)==8,constants.stderr
        assert all(len(line)<4096 and 'chain=' in line and 'ctor_us=' in line and 'gate_us=' in line and 'install_us=' in line
                   and 'record=' in line and 'setConstantValue:type:atIndex:=' in line and 'imp=actual/hook/original' in line for line in failures),constants.stderr
        assert 'AddressSanitizer' not in constants.stderr and 'runtime error:' not in constants.stderr,constants.stderr
        print(constants.stdout,end='')
        call(['xcrun','clang++','-std=c++17',*flags,str(ROOT/'tools/fuxi-metal/FrontendTests.mm'),*frameworks,'-o',str(d/'frontend')])
        env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0')
        call(['xcrun','clang++','-std=c++17',*flags,str(ROOT/'tools/fuxi-metal/PipelineFrontendTests.mm'),*frameworks,'-o',str(d/'pipeline-frontend')])
        subprocess.run([str(d/'pipeline-frontend')],env=env,check=True,timeout=15)
        call(['xcrun','clang++','-std=c++17',*flags,str(ROOT/'tools/fuxi-metal/GateFrontendTests.mm'),*frameworks,'-o',str(d/'gate')])
        gate=subprocess.run([str(d/'gate'),'daemon-argv'],env=env,text=True,capture_output=True,check=True,timeout=10)
        assert gate.stderr.count('FUXI MTL: gate deny gate-test')==1,gate.stderr
        print(gate.stdout,end='')
        allowed=subprocess.run([str(d/'gate'),'daemon-argv'],env=dict(env,FUXI_TEST_GATE_ALLOW='1'),text=True,capture_output=True,check=True,timeout=10)
        assert allowed.stderr.count('FUXI MTL: gate allow gate-test')==1,allowed.stderr
        def compositor(process,config,args='',expected='deny',**extra):
            p=subprocess.run([str(d/'gate'),'SpringBoard','ignored'],env=dict(env,FUXI_TEST_GATE_ALLOW='1',
                FUXI_TEST_PROCESS=process,FUXI_TEST_GATE_CONFIG=config,FUXI_TEST_BOOTARGS=args,
                FUXI_TEST_GATE_EXPECT=expected,**extra),text=True,capture_output=True,check=True,timeout=10)
            label='allow' if expected in ('allow','census') else 'deny'
            assert p.stderr.count('FUXI MTL: gate '+label+' '+process)==1,p.stderr
            assert p.stderr.count('FUXI MTL: compression ')==(1 if expected=='allow' else 0),p.stderr
            return p
        compositor('smoke','census smoke\n',expected='census')
        compositor('backboardd','')
        compositor('backboardd','backboardd\n')
        compositor('backboardd','','fuxibbgpu=1')
        compositor('SpringBoard','SpringBoard\n','fuxibbgpu=1')
        compositor('SpringBoard','SpringBoard\n','fuxibbgpu=2')
        compositor('backboardd','','fuxibbgpu=2')
        compositor('SpringBoard','census SpringBoard\n','fuxibbgpu=1')
        private=d/'backboardd';private.mkdir(mode=0o700)
        p=compositor('backboardd','backboardd\n','fuxibbgpu=1','allow',
                     FUXI_TEST_GATE_DIR=str(private),FUXI_TEST_GATE_FAULT='1')
        assert 'backboardd gate good frames=120' in p.stderr and 'next-respawn=CPU persisted=1' in p.stderr,p.stderr
        compositor('backboardd','backboardd\n','fuxibbgpu=1',FUXI_TEST_GATE_DIR=str(private))
        for mode in ('timer','cap'):
            survey_dir=d/('survey-'+mode);survey_dir.mkdir(mode=0o700)
            survey=compositor('backboardd','backboardd\n','fuxibbgpu=2','allow',FUXI_TEST_GATE_DIR=str(survey_dir),FUXI_TEST_GATE_SURVEY=mode)
            assert survey.stderr.count('survey distinct first\n')==1 and survey.stderr.count('survey summary ')==1,survey.stderr
            assert ('reason='+mode) in survey.stderr and 'next-respawn=CPU persisted=1' in survey.stderr,survey.stderr
            assert ' denied\n' in (survey_dir/'gate.state').read_text()
            compositor('backboardd','backboardd\n','fuxibbgpu=2',FUXI_TEST_GATE_DIR=str(survey_dir))
        call(['xcrun','clang++','-std=c++17',*flags,str(ROOT/'tools/fuxi-metal/CensusTests.mm'),*frameworks,'-o',str(d/'census')])
        census=subprocess.run([str(d/'census')],env=env,text=True,capture_output=True,check=True,timeout=15)
        assert census.stderr.count(' supportsFamily: implemented')==1,census.stderr
        assert census.stderr.count('FUXI MTL: census census-test ')==256,census.stderr
        assert census.stderr.count('FUXI MTL: census limit census-test 256')==1,census.stderr
        assert 'AddressSanitizer' not in census.stderr and 'runtime error:' not in census.stderr,census.stderr
        print(census.stdout,end='')
        subprocess.run([str(d/'frontend')],env=env,check=True,timeout=15)
        call(['xcrun','clang',*flags,'-DMTLCreateSystemDefaultDevice=FuxiTestCreateDevice','-Dobjc_getClass=FuxiTestGetClass',
            '-Dgeteuid=FuxiTestGeteuid','-Dalarm=FuxiTestAlarm','-DFUXI_MTL_CONF="'+str(conf)+'"',
            '-c',str(ROOT/'tools/fuxi-metal-smoke.m'),'-o',str(d/'smoke.o')])
        call(['xcrun','clang++','-std=c++17',*flags,str(ROOT/'tools/fuxi-metal/SmokeTestDevice.mm'),str(d/'smoke.o'),*frameworks,'-o',str(d/'smoke')])
        def run(data,rc=0,**extra):
            if data is None:conf.unlink(missing_ok=True)
            else:conf.write_bytes(data)
            p=subprocess.run([str(d/'smoke'),'--host-daemon-argv','ignored'],env=dict(env,**extra),text=True,capture_output=True,timeout=10)
            assert p.returncode==rc,(p.returncode,p.stdout,p.stderr)
            assert 'AddressSanitizer' not in p.stderr and 'runtime error:' not in p.stderr,p.stderr
            return p.stdout
        print(run(None),end='')
        for stage in range(4):
            out=run(str(stage).encode());assert 'stage %u '%stage in out and 'done PASS' in out
            assert 'stage %u '%(stage+1) not in out
        for bad in (b'',b'7',b'-1',b'1junk',b'3\0',b'1'*32):assert 'stage 0 discovery FAIL' in run(bad,1)
        assert 'stage 4 pipeline FAIL' in run(b'4',1)
        assert 'stage 0 discovery FAIL' in run(b'3',1,FUXI_TEST_NOT_ROOT='1')
        assert 'returned nil' in run(b'3',1,FUXI_TEST_NO_DEVICE='1')
        assert 'stage 1 fill FAIL' in run(b'3',1,FUXI_TEST_LOST='1')
        assert 'byte 65535' in run(b'3',1,FUXI_TEST_BAD_DATA='1')
        assert 'stage 1 fill FAIL exceeded 30 s' in run(b'3',124,FUXI_TEST_HANG='1')
        print('PASS: actual smoke stages/default/config/ignored argv, nil discovery, nonroot, submit loss, last-byte corruption, alarm exit')
if __name__=='__main__':main()
