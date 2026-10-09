"""
Firma adi karsilastirma (katı/esnek) ve hata siniflamasi testleri. Isimler uydurmadir.

Calistirma (proje kokunde): python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import vendors
from scripts.olcum.firma_hata_analizi import sinif
from scripts.olcum.firma_olc import ayni_mi, esnek_ayni_mi


class AyniMiTesti(unittest.TestCase):
    def test_buyuk_kucuk_ve_turkce_harf_farki_onemsiz(self):
        self.assertTrue(ayni_mi("Örnek Market A.Ş.", "ORNEK MARKET A S"))

    def test_bos_ad_eslesmez(self):
        self.assertFalse(ayni_mi("", "ORNEK MARKET"))
        self.assertFalse(ayni_mi("ORNEK MARKET", None))

    def test_farkli_firmalar(self):
        self.assertFalse(ayni_mi("ORNEK MARKET", "DENEME RESTORAN"))


class EsnekAyniMiTesti(unittest.TestCase):
    def test_hukuki_ek_farki_ayni_sayilir(self):
        self.assertTrue(esnek_ayni_mi("KOVA TAVUK", "KOVA TAVUK GIDA SAN. LTD. ŞTİ."))

    def test_tek_kelimelik_kismi_eslesme_kabul_edilmez(self):
        self.assertFalse(esnek_ayni_mi("ORNEK", "ORNEK MARKET INSAAT TURIZM TICARET"))
        self.assertFalse(esnek_ayni_mi("ORNEK MARKET", "ORNEK"))

    def test_marka_ile_unvan_farkli(self):
        self.assertFalse(esnek_ayni_mi("ORNEK PIZZA", "JUBILANT GIDA A.S."))

    def test_ortak_kelime_orani_yarinin_altindaysa_farkli(self):
        self.assertFalse(esnek_ayni_mi("ALFA BETA", "ALFA BETA GAMA DELTA EPSILON ZETA"))


class HataSinifiTesti(unittest.TestCase):
    def test_siniflar(self):
        self.assertEqual(sinif("ORNEK MARKET", ""), "bos")
        self.assertEqual(sinif("ORNEK MARKET", "ORNEK MARKET SUBE 2"), "kisa/uzun ad farki")
        self.assertEqual(sinif("ALFA BETA GAMA", "ALFA BETA DELTA"), "ortak kelime cok")
        self.assertEqual(sinif("ALFA BETA GAMA DELTA", "ALFA ZETA ETA TETA"), "ortak kelime az")
        self.assertEqual(sinif("ORNEK MARKET", "DENEME RESTORAN"), "tamamen farkli")

    def test_firma_normalize_ile_tutarli(self):
        self.assertEqual(vendors.firma_normalize("Örnek Market A.Ş."), "ORNEK MARKET A S")


if __name__ == "__main__":
    unittest.main()
