"""Preserve genuine codeless matching metadata without pretending it is code."""
import copy
import unittest
import xml.etree.ElementTree as ET
from native_prelink import macho,unserialize_xml,plan
from emit_prelink import emit
from test_emit_prelink import images
from test_native_prelink import xml_value

class CodelessTests(unittest.TestCase):
    def fixture(self, change=None):
        native,original=images();raw=bytearray(original)
        info=next(s for s in macho(raw)['segments'] if s['name']=='__PRELINK_INFO')
        at,size=info['fileoff'],info['filesize']
        metadata=unserialize_xml(raw[at:at+size])
        entry={'CFBundleIdentifier':'test.matching','OSBundleLibraries':{'test.component':'1'},
            'IOKitPersonalities':{'Actual Match':{'CFBundleIdentifier':'test.component',
                'IOClass':'TestProvider','IOProviderClass':'IOResources','IOResourceMatch':'IOBSD'}}}
        if change:change(entry)
        metadata['_PrelinkInfoDictionary'].append(entry)
        data=ET.tostring(xml_value(metadata))+b'\0';self.assertLessEqual(len(data),size)
        raw[at:at+size]=data+bytes(size-len(data))
        return native,bytes(raw),entry

    def test_preserve_metadata_and_dependency_code(self):
        native,original,entry=self.fixture()
        output,report=emit(native,original,['test.matching'],0x4000)
        self.assertEqual(report['components'],['test.component'])
        self.assertEqual(report['codeless_components'],['test.matching'])
        self.assertNotIn('test.matching',report['module_entrypoints'])
        info=next(s for s in macho(output)['segments'] if s['name']=='__PRELINK_INFO')
        metadata=unserialize_xml(output[info['fileoff']:info['fileoff']+info['filesize']])
        actual=next(e for e in metadata['_PrelinkInfoDictionary'] if e['CFBundleIdentifier']=='test.matching')
        self.assertEqual(actual,entry)
        self.assertIn('test.component',report['original_executable_sha256'])

    def test_refuse_disguised_or_empty_component(self):
        changes=[lambda e:e.pop('IOKitPersonalities'),lambda e:e.update(IOKitPersonalities={}),
                 lambda e:e.update(IOKitPersonalities=[])]
        for key in ('CFBundleExecutable','_PrelinkExecutableLoadAddr','_PrelinkExecutableSourceAddr',
                    '_PrelinkExecutableSize','_PrelinkKmodInfo'):
            changes.append(lambda e,key=key:e.update({key:0}))
        for change in changes:
            native,original,_=self.fixture(change)
            with self.assertRaisesRegex(ValueError,'Codeless component'):
                plan(native,original,['test.matching'],0x4000)

    def test_refuse_missing_declared_dependency(self):
        native,original,_=self.fixture(lambda e:e['OSBundleLibraries'].update({'absent':'1'}))
        with self.assertRaisesRegex(ValueError,'Missing declared dependency'):
            plan(native,original,['test.matching'],0x4000)

if __name__=='__main__':unittest.main()
