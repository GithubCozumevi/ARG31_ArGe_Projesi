"""
Olcum betiklerinin ortak yardimcilarinin testleri (scripts/ortak.py ve kucuk olcum fonksiyonlari).

Calistirma (proje kokunde): python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from argparse import ArgumentParser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import ortak
from scripts.olcum.vkn_olc import gold_oku, hata_nedeni, yontem_ozeti

VKN = "1234567890"
BASKA_VKN = "9876543217"
TCKN = "10000000146"


def yaz(klasor, ad, icerik):
    with open(os.path.join(klasor, ad), "w", encoding="utf-8") as f:
        if isinstance(icerik, str):
            f.write(icerik)
        else:
            json.dump(icerik, f, ensure_ascii=False)


class KaymaTesti(unittest.TestCase):
    def test_kayma_yoksa_ad_degismez(self):
        self.assertEqual(ortak.kaymali_ad("fis150", 0, 0), "fis150")

    def test_baslangictan_once_degismez_sonra_kayar(self):
        self.assertEqual(ortak.kaymali_ad("fis143", 2, 144), "fis143")
        self.assertEqual(ortak.kaymali_ad("fis144", 2, 144), "fis146")
        self.assertEqual(ortak.kaymali_ad("fis300", -3, 144), "fis297")

    def test_numarasiz_ad_degismez(self):
        self.assertEqual(ortak.kaymali_ad("ornek", 2, 0), "ornek")

    def test_kayma_cozucu_argumanlardan_calisir(self):
        ap = ArgumentParser()
        ortak.kayma_argumanlari_ekle(ap)
        cozucu = ortak.kayma_cozucu(ap.parse_args(["--kayma", "2", "--kayma-baslangic", "10"]))
        self.assertEqual((cozucu("fis9"), cozucu("fis10")), ("fis9", "fis12"))


class DosyaOkumaTesti(unittest.TestCase):
    def setUp(self):
        self._gecici = tempfile.TemporaryDirectory()
        self.klasor = self._gecici.name
        self.addCleanup(self._gecici.cleanup)

    def test_gold_dosyalari_sirali_ve_bozuklari_atlar(self):
        yaz(self.klasor, "fis2.json", {"company": "B"})
        yaz(self.klasor, "fis1.json", {"company": "A"})
        yaz(self.klasor, "fis3.json", "{bozuk json")
        yaz(self.klasor, "fis4.json", ["liste", "sozluk degil"])
        self.assertEqual([f for f, _ in ortak.gold_dosyalari(self.klasor)], ["fis1", "fis2"])

    def test_ocr_satirlari_ve_metin(self):
        yaz(self.klasor, "fis1.json", {"ocr": [{"text": "A"}, {"text": "B"}]})
        yol = os.path.join(self.klasor, "fis1.json")
        self.assertEqual(ortak.ocr_satirlari(yol), ["A", "B"])
        self.assertEqual(ortak.ocr_metni(yol), "A\nB")

    def test_ocr_dosyasi_yoksa_ya_da_semasiz_ise_none(self):
        self.assertIsNone(ortak.ocr_metni(os.path.join(self.klasor, "yok.json")))
        yaz(self.klasor, "semasiz.json", {"baska": 1})
        self.assertIsNone(ortak.ocr_satirlari(os.path.join(self.klasor, "semasiz.json")))

    def test_vlm_oku_clean_dosyasini_tercih_eder(self):
        yaz(self.klasor, "fis1_clean.json", {"seller_tax_id": VKN, "company": "TEMIZ"})
        yaz(self.klasor, "fis1.json", {"seller_tax_id": BASKA_VKN, "company": "HAM"})
        self.assertEqual(ortak.vlm_oku(self.klasor, "fis1"), (VKN, "TEMIZ"))

    def test_vlm_klasoru_verilmediyse_bos_doner(self):
        self.assertEqual(ortak.vlm_oku(None, "fis1"), (None, None))
        self.assertEqual(ortak.vlm_oku(self.klasor, "yok"), (None, None))

    def test_veri_klasoru_proje_icinde(self):
        self.assertEqual(ortak.VERI_KLASORU, ortak.PROJE_KOKU / "data")
        self.assertTrue((ortak.PROJE_KOKU / "app").is_dir())


class MetinYardimcilariTesti(unittest.TestCase):
    def test_tarih_adaylari(self):
        self.assertEqual(ortak.tarih_adaylari("14.05.2023"), ["14052023", "140523"])
        self.assertEqual(ortak.tarih_adaylari("4/5/23"), ["040523"])
        self.assertEqual(ortak.tarih_adaylari("5 Mayis 2026"), [])
        self.assertEqual(ortak.tarih_adaylari(None), [])

    def test_anahtar_kelimeler_hukuki_ekleri_atar(self):
        self.assertEqual(ortak.anahtar_kelimeler("KOVA TAVUK GIDA SAN. LTD. ŞTİ."), {"KOVA", "TAVUK"})

    def test_sadece_rakam(self):
        self.assertEqual(ortak.sadece_rakam("VKN: 123 456-78"), "12345678")


class VknOlcTesti(unittest.TestCase):
    def test_gold_oku_vkn_tckn_ve_atlananlar(self):
        with tempfile.TemporaryDirectory() as klasor:
            yaz(klasor, "fis1.json", {"seller_tax_id": VKN, "company": "A"})
            yaz(klasor, "fis2.json", {"seller_tax_id": TCKN, "company": "B"})
            yaz(klasor, "fis3.json", {"seller_tax_id": "10000000147", "company": "C"})   # TCKN checksum gecmez
            yaz(klasor, "fis4.json", {"company": "D"})                                    # VKN yok
            fisler, sayac = gold_oku(klasor)
        self.assertEqual([f for f, _, _ in fisler], ["fis1", "fis2"])
        self.assertEqual(sayac, {"tckn": 1, "gecersiz_11_hane": 1, "vkn_yok": 1})

    def test_hata_nedeni_siniflari(self):
        self.assertEqual(hata_nedeni(VKN, f"VKN {BASKA_VKN} ve {VKN}"), "dogru numara metinde VAR ama baska numara secildi")
        self.assertEqual(hata_nedeni(VKN, "VKN 1234567891"), "OCR dogru numarayi tek hane yanlis okudu")
        self.assertEqual(hata_nedeni(VKN, "TOPLAM 12,50"), "metinde hic 10 haneli sayi yok")

    def test_yontem_ozeti_sayar(self):
        dogru = {"vkn": VKN, "insan_kontrolu": False}
        yanlis = {"vkn": BASKA_VKN, "insan_kontrolu": False}
        bos = {"vkn": None, "insan_kontrolu": True}
        sonuclar = [("f1", VKN, dogru, dogru, dogru, ""), ("f2", VKN, yanlis, yanlis, yanlis, ""), ("f3", VKN, bos, bos, bos, "")]
        self.assertEqual(
            yontem_ozeti(sonuclar, 0), dict(dogru=1, yanlis=1, bos=1, oto=2, oto_dogru=1)
        )


if __name__ == "__main__":
    unittest.main()
