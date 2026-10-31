"""Fuxi tests for the marked ("Fuxi:") changes to this XnuPrelink 76452c6 port (not upstream)."""

import struct
import types
import unittest

from fileset_segmented import SegmentedEmit

MH_DYLIB_IN_CACHE = 0x80000000


class FuxiChanges(unittest.TestCase):
    def fake(self, sections):
        header = struct.pack('<8I', 0xfeedfacf, 0x0100000c, 0xc0000002, 2, 0, 0, 0, 0)
        return types.SimpleNamespace(
            native=types.SimpleNamespace(commands=[], data=header, sections=sections),
            data=bytearray(header + bytes(32)), fileoff=0, report={})

    def test_no_fips_hmacs_means_nothing_to_seal(self):
        # The Fuxi kernel has no source corecrypto; com.apple.kec.corecrypto is prelinked and seals itself.
        emit = self.fake([{'segment': '__TEXT_EXEC', 'name': '__text', 'va': 0, 'size': 0}])
        SegmentedEmit.patch_entry_header(emit)
        self.assertIsNone(emit.report['corecrypto_hmac_sha256'])
        self.assertTrue(struct.unpack_from('<I', emit.data, 24)[0] & MH_DYLIB_IN_CACHE)

    def test_malformed_fips_hmacs_still_rejected(self):
        emit = self.fake([{'segment': '__TEXT_EXEC', 'name': '__text', 'va': 0, 'size': 0},
                          {'segment': '__DATA_CONST', 'name': '__fips_hmacs', 'va': 0, 'size': 16}])
        with self.assertRaisesRegex(ValueError, 'Expected one 32-byte corecrypto HMAC field'):
            SegmentedEmit.patch_entry_header(emit)


if __name__ == '__main__':
    unittest.main()
