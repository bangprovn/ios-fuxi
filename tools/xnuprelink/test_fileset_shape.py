import struct
import unittest

from fileset_shape import checked_shape


def shape(words):
    return checked_shape(struct.pack('<' + 'I' * len(words), *words), 0x10000)


class CheckedShapeTests(unittest.TestCase):
    def test_authentication_check_preserves_branch_geometry_and_reference_sites(self):
        # cbz x1,end; autda x16,x17; [check]; mov x0,x16; ret
        check = [0xaa1003f1,0xdac147f1,0xeb11021f,0x54000040,0xd4388e40]
        release = [0xb4000061,0xdac11a30,0xaa1003e0,0xd65f03c0]
        development = [0xb4000101,0xdac11a30,*check,0xaa1003e0,0xd65f03c0]
        a,b = shape(release),shape(development)
        self.assertEqual(a[0],b[0])
        self.assertEqual(b[1][28],8)
        self.assertEqual(b[1][32],12)

    def test_partial_or_unrelated_check_is_kept(self):
        check = [0xaa1003f1,0xdac147f1,0xeb11021f,0x54000040,0xd4388e40]
        self.assertEqual(len(shape([0xd503201f,*check,0xd65f03c0])[0]),7)
        self.assertEqual(len(shape([0xdac11a30,*check[:-1],0xd65f03c0])[0]),6)

    def test_branch_entering_check_disables_normalization(self):
        check = [0xaa1003f1,0xdac147f1,0xeb11021f,0x54000040,0xd4388e40]
        code = [0xb4000040,0xdac11a30,*check,0xd65f03c0]
        self.assertEqual(len(shape(code)[0]),len(code))

    def test_lr_check_requires_tail_branch_and_preserves_call_site(self):
        check = [0xca1e07d0,0xb6f00050,0xd4388e20]
        a = shape([0xd50323ff,0x14000400])
        b = shape([0xd50323ff,*check,0x140003fd])
        self.assertEqual(a[0],b[0])
        self.assertEqual(b[1][16],4)
        self.assertEqual(a[2][0][1],b[2][0][1])
        self.assertEqual(len(shape([*check,0xd65f03c0])[0]),4)


if __name__ == '__main__':
    unittest.main()
