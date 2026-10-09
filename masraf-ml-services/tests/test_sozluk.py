"""
Satici sozlugunun olusturulmasi, kaydedilmesi ve SozlukDeposu testleri. Numaralar sentetiktir.

Calistirma (proje kokunde): python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import vendors
from app.services.pipeline import Guven, Kaynak, vkn_karar_ver

VKN = "1234567890"
TCKN = "10000000146"


def gold_yaz(klasor, ad, veri):
    with open(os.path.join(klasor, ad), "w", encoding="utf-8") as f:
        json.dump(veri, f, ensure_ascii=False)


class SozlukOlusturTesti(unittest.TestCase):
    def test_gecerli_tckn_li_sahis_satici_sozluge_girer(self):
        with tempfile.TemporaryDirectory() as klasor:
            gold_yaz(klasor, "fis1.json", {"seller_tax_id": VKN, "company": "ORNEK MARKET"})
            gold_yaz(klasor, "fis2.json", {"seller_tax_id": TCKN, "company": "AHMET YILMAZ"})
            gold_yaz(klasor, "fis3.json", {"seller_tax_id": "10000000147", "company": "GECERSIZ TCKN"})
            gold_yaz(klasor, "fis4.json", {"seller_tax_id": VKN, "company": ""})
            gold_yaz(klasor, "fis5.json", {"company": "VKN YOK"})
            sozluk, ozet = vendors.sozluk_olustur(klasor)
        self.assertEqual(sorted(sozluk), sorted([VKN, TCKN]))
        self.assertTrue(sozluk[TCKN]["checksum_gecerli"])
        self.assertEqual(ozet["tckn_satici"], 1)
        self.assertEqual(
            ozet["atlama_nedenleri"], {"tckn_checksum_gecersiz": 1, "firma_adi_bos": 1, "vkn_bos": 1}
        )

    def test_kaydet_ve_yukle(self):
        with tempfile.TemporaryDirectory() as klasor:
            yol = os.path.join(klasor, "alt", "sozluk.json")
            sozluk = vendors.sozluk_kur([(VKN, "ORNEK MARKET")])
            vendors.sozluk_kaydet(sozluk, yol)
            self.assertEqual(vendors.sozluk_yukle(yol), sozluk)

    def test_dosya_yoksa_bos_sozluk(self):
        self.assertEqual(vendors.sozluk_yukle("/yok/boyle/bir/dosya.json"), {})


class SozlukDeposuTesti(unittest.TestCase):
    def test_ilk_kullanimda_yukler_yenile_ile_tekrar_okur(self):
        with tempfile.TemporaryDirectory() as klasor:
            yol = os.path.join(klasor, "sozluk.json")
            vendors.sozluk_kaydet(vendors.sozluk_kur([(VKN, "ORNEK MARKET")]), yol)
            depo = vendors.SozlukDeposu(yol)
            self.assertIsNone(depo._sozluk)             # henuz yuklenmedi (modul acilisinda okunmaz)
            self.assertEqual(list(depo.al()), [VKN])

            vendors.sozluk_kaydet(vendors.sozluk_kur([(VKN, "ORNEK MARKET"), (TCKN, "AHMET YILMAZ")]), yol)
            self.assertEqual(len(depo.al()), 1)         # yenilenene kadar eski sozluk
            self.assertEqual(len(depo.yenile()), 2)
            self.assertEqual(len(depo.al()), 2)

    def test_dosya_yoksa_bos(self):
        self.assertEqual(vendors.SozlukDeposu("/yok/sozluk.json").al(), {})


class PipelineSabitleriTesti(unittest.TestCase):
    def test_kaynak_ve_guven_sabitleri_sonucla_uyumlu(self):
        sonuc = vkn_karar_ver(f"VKN: {VKN}")
        self.assertEqual((sonuc["kaynak"], sonuc["guven"]), (Kaynak.OCR_REGEX, Guven.YUKSEK))
        yok = vkn_karar_ver("TOPLAM 12,50")
        self.assertEqual((yok["kaynak"], yok["guven"]), (Kaynak.YOK, Guven.YOK))

    def test_sozluk_varken_vlm_celiskisi_insana_gider(self):
        sozluk = vendors.sozluk_kur([(VKN, "ORNEK MARKET")])
        sonuc = vkn_karar_ver("VKN: 1234567891", vlm_vkn="9876543217", sozluk=sozluk)
        self.assertEqual(sonuc["kaynak"], Kaynak.SATICI_SOZLUGU)
        self.assertTrue(sonuc["insan_kontrolu"])
        self.assertEqual(sonuc["detay"]["vlm_celiskisi"], "9876543217")

    def test_tckn_li_vlm_degeri_kabul_edilir(self):
        sonuc = vkn_karar_ver("TOPLAM 12,50", vlm_vkn=TCKN)
        self.assertEqual((sonuc["vkn"], sonuc["kaynak"], sonuc["kimlik_tipi"]), (TCKN, Kaynak.VLM, "TCKN"))


if __name__ == "__main__":
    unittest.main()
