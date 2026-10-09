"""
VKN/TCKN cikarim akisinin birim testleri. Numaralar sentetiktir (gercek bir firmaya/kisiye ait degil).

Calistirma (proje kokunde): python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import vendors
from app.services.extraction import is_valid_tckn_checksum, is_valid_vkn_checksum, vkn_bul_ve_dogrula
from app.services.pipeline import vkn_karar_ver

VKN = "1234567890"        # checksum'i gecen sentetik VKN
BASKA_VKN = "9876543217"  # checksum'i gecen ikinci sentetik VKN
TCKN = "10000000146"      # checksum'i gecen sentetik TCKN


class ChecksumTesti(unittest.TestCase):
    def test_gecerli_vkn(self):
        self.assertTrue(is_valid_vkn_checksum(VKN))
        self.assertTrue(is_valid_vkn_checksum("123 456 78 90"))

    def test_gecersiz_vkn(self):
        self.assertFalse(is_valid_vkn_checksum("1234567891"))
        self.assertFalse(is_valid_vkn_checksum("123456789"))
        self.assertFalse(is_valid_vkn_checksum("12345678AB"))

    def test_gecerli_tckn(self):
        self.assertTrue(is_valid_tckn_checksum(TCKN))

    def test_gecersiz_tckn(self):
        self.assertFalse(is_valid_tckn_checksum("10000000147"))
        self.assertFalse(is_valid_tckn_checksum("00000000000"))
        self.assertFalse(is_valid_tckn_checksum(VKN))


class OcrdanVknTesti(unittest.TestCase):
    def test_gecerli_vkn_bulunur(self):
        sonuc = vkn_bul_ve_dogrula(f"VKN: {VKN}\nTOPLAM 12,50")
        self.assertEqual((sonuc.deger, sonuc.durum), (VKN, "gecerli_orijinal"))

    def test_araya_bosluk_girmis_vkn(self):
        self.assertEqual(vkn_bul_ve_dogrula("VKN 123 456 78 90").deger, VKN)

    def test_tckn_bulunur(self):
        self.assertEqual(vkn_bul_ve_dogrula(f"TCKN {TCKN}").deger, TCKN)

    def test_numara_yoksa_bulunamadi(self):
        self.assertEqual(vkn_bul_ve_dogrula("TOPLAM 12,50").durum, "bulunamadi")

    def test_ayri_satirlardaki_rakamlar_birlesmez(self):
        self.assertEqual(vkn_bul_ve_dogrula("12345\n67890").durum, "bulunamadi")

    def test_cift_sifirla_baslayan_numara_vkn_sayilmaz(self):
        self.assertEqual(vkn_bul_ve_dogrula("FIS NO 0012345678").durum, "bulunamadi")

    def test_birden_fazla_gecerli_numarada_etiketli_olan_secilir(self):
        sonuc = vkn_bul_ve_dogrula(f"MERSIS {BASKA_VKN}\nVERGI NO: {VKN}")
        self.assertEqual((sonuc.deger, sonuc.durum), (VKN, "gecerli_orijinal"))

    def test_etiketsiz_birden_fazla_gecerli_numara_belirsiz_kalir(self):
        sonuc = vkn_bul_ve_dogrula(f"{BASKA_VKN}\n{VKN}")
        self.assertIsNone(sonuc.deger)
        self.assertEqual(sonuc.durum, "coklu_gecerli")
        self.assertEqual(sonuc.belirsiz_adaylar, [BASKA_VKN, VKN])

    def test_tek_hane_duzeltmesi_gecerli_numara_uretir(self):
        sonuc = vkn_bul_ve_dogrula("VKN: 1234567891")
        self.assertEqual(sonuc.durum, "gecerli_duzeltildi")
        self.assertEqual(sonuc.ham_aday, "1234567891")
        self.assertTrue(is_valid_vkn_checksum(sonuc.deger))


class SaticiSozluguTesti(unittest.TestCase):
    def setUp(self):
        self.sozluk = vendors.sozluk_kur([
            (VKN, "ORNEK MARKET A.S."), (VKN, "ORNEK MARKET"), (VKN, "ORNEK MARKET A.S."),
        ])

    def test_en_sik_gecen_ad_firma_olur(self):
        kayit = self.sozluk[VKN]
        self.assertEqual(kayit["firma"], "ORNEK MARKET A.S.")
        self.assertEqual(kayit["takma_adlar"], ["ORNEK MARKET", "ORNEK MARKET A.S."])
        self.assertEqual(kayit["fis_sayisi"], 3)
        self.assertTrue(kayit["checksum_gecerli"])

    def test_bilinen_numara(self):
        self.assertEqual(vendors.sozlukte_ara(self.sozluk, [VKN])["durum"], "bilinen")

    def test_tek_hane_yanlis_okunan_numara_kurtarilir(self):
        arama = vendors.sozlukte_ara(self.sozluk, ["1234567891"])
        self.assertEqual((arama["durum"], arama["vkn"]), ("kurtarildi", VKN))

    def test_sozlukte_olmayan_numara(self):
        self.assertEqual(vendors.sozlukte_ara(self.sozluk, ["5555555555"])["durum"], "yok")

    def test_firma_normalize(self):
        self.assertEqual(vendors.firma_normalize("  Örnek Gıda San. ve Tic. A.Ş. "), "ORNEK GIDA SAN VE TIC A S")

    def test_vkn_adaylari_tekrarsiz(self):
        self.assertEqual(vendors.vkn_adaylari("a 123 456 78 90 b 1234567890 c 12345"), [VKN])


class KararAkisiTesti(unittest.TestCase):
    def test_ocr_gecerliyse_yuksek_guven(self):
        sonuc = vkn_karar_ver(f"VKN: {VKN}")
        self.assertEqual((sonuc["vkn"], sonuc["kaynak"], sonuc["guven"]), (VKN, "ocr_regex", "yuksek"))
        self.assertFalse(sonuc["insan_kontrolu"])
        self.assertEqual(sonuc["kimlik_tipi"], "VKN")

    def test_hicbir_kaynak_yoksa_insan_kontrolu(self):
        sonuc = vkn_karar_ver("TOPLAM 12,50")
        self.assertIsNone(sonuc["vkn"])
        self.assertEqual(sonuc["kaynak"], "yok")
        self.assertTrue(sonuc["insan_kontrolu"])

    def test_ocr_bulamazsa_gecerli_vlm_kullanilir(self):
        sonuc = vkn_karar_ver("TOPLAM 12,50", BASKA_VKN)
        self.assertEqual((sonuc["vkn"], sonuc["kaynak"], sonuc["guven"]), (BASKA_VKN, "vlm", "orta"))

    def test_gecersiz_vlm_kullanilmaz(self):
        self.assertIsNone(vkn_karar_ver("TOPLAM 12,50", "1234567891")["vkn"])

    def test_sozluk_tek_hane_hatasini_kurtarir(self):
        sozluk = vendors.sozluk_kur([(VKN, "ORNEK MARKET A.S.")])
        sonuc = vkn_karar_ver("VKN: 1234567891", sozluk=sozluk, firma_ipucu="Örnek Market A.Ş.")
        self.assertEqual((sonuc["vkn"], sonuc["kaynak"], sonuc["guven"]), (VKN, "satici_sozlugu", "orta"))
        self.assertEqual(sonuc["satici"]["firma"], "ORNEK MARKET A.S.")

    def test_coklu_gecerli_numarada_vlm_ile_uyusan_secilir(self):
        sonuc = vkn_karar_ver(f"{BASKA_VKN}\n{VKN}", VKN)
        self.assertEqual((sonuc["vkn"], sonuc["kaynak"]), (VKN, "ocr_vlm_uyumu"))

    def test_tek_hane_duzeltmesi_insan_kontrolune_gider(self):
        sonuc = vkn_karar_ver("VKN: 1234567891")
        self.assertEqual((sonuc["kaynak"], sonuc["guven"]), ("ocr_duzeltme", "dusuk"))
        self.assertTrue(sonuc["insan_kontrolu"])


if __name__ == "__main__":
    unittest.main()
