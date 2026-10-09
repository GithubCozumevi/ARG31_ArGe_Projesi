"""
Layout betiklerinin saf (OCR gerektirmeyen) fonksiyonlarinin testleri: alan eslestirme, tutar secimi,
gorsel-etiket eslestirme. Degerler uydurmadir.

Calistirma (proje kokunde): python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.layout.layout_pilot import alanlari_esle, firma_eslesmesi, sayi_cevir, satir_eslesir, tutar_sec
from scripts.veri_denetim.gorsel_etiket_eslestir import etiketleri_oku, numara, puanla


def satir(metin, ust, alt=None, sol=0, sag=100):
    return {"text": metin, "score": 0.9, "box": [sol, ust, sag, alt if alt is not None else ust + 10]}


class SayiCevirTesti(unittest.TestCase):
    def test_turkce_sayi_bicimleri(self):
        self.assertEqual(sayi_cevir("4.127,69"), 4127.69)
        self.assertEqual(sayi_cevir("205,63"), 205.63)
        self.assertEqual(sayi_cevir("1.021"), 1021.0)
        self.assertEqual(sayi_cevir("*55,00"), 55.0)

    def test_sayi_olmayan_none(self):
        self.assertIsNone(sayi_cevir("TOPLAM"))


class SatirEslesirTesti(unittest.TestCase):
    def test_vkn_aralarinda_bosluk_olsa_da_bulunur(self):
        self.assertTrue(satir_eslesir("seller_tax_id", "1234567890", "VERGI NO: 123 456 78 90"))
        self.assertFalse(satir_eslesir("seller_tax_id", "1234567890", "VERGI NO: 123 456 78 91"))

    def test_tarih_saat_bitisik_okunsa_da_bulunur(self):
        self.assertTrue(satir_eslesir("date", "03.07.2026", "03.07.202613:00"))
        self.assertTrue(satir_eslesir("date", "3.7.2026", "TARIH 03 / 07 / 2026"))
        self.assertFalse(satir_eslesir("date", "03.07.2026", "03.07.20261"))

    def test_tutar_toleransi(self):
        self.assertTrue(satir_eslesir("total_amount", 125.5, "TOPLAM *125,50"))
        self.assertFalse(satir_eslesir("total_amount", 125.5, "TOPLAM *125,60"))

    def test_bos_deger_eslesmez(self):
        self.assertFalse(satir_eslesir("seller_tax_id", None, "123"))


class FirmaEslesmesiTesti(unittest.TestCase):
    def test_iki_satira_bolunmus_ad(self):
        satirlar = [satir("ORNEK MARKET", 0), satir("GIDA A.S.", 12), satir("ADRES", 24)]
        self.assertEqual(firma_eslesmesi("ORNEK MARKET GIDA A.S.", satirlar), [0, 1])

    def test_eslesme_yoksa_bos(self):
        self.assertEqual(firma_eslesmesi("BAMBASKA FIRMA", [satir("ORNEK MARKET", 0)]), [])


class TutarSecTesti(unittest.TestCase):
    def test_toplam_satiri_kdv_satirina_tercih_edilir(self):
        satirlar = [
            satir("URUN", 0, sag=50), satir("125,50", 0, sol=60),
            satir("KDV", 40, sag=50), satir("125,50", 40, sol=60),
            satir("TOPLAM", 80, sag=50), satir("125,50", 80, sol=60),
        ]
        self.assertEqual(tutar_sec("total_amount", [1, 3, 5], satirlar), [5])

    def test_alanlari_esle_hepsini_bulur(self):
        satirlar = [
            satir("ORNEK MARKET", 0), satir("VERGI NO 1234567890", 12), satir("03.07.2026 13:00", 24),
            satir("TOPLAM", 80, sag=50), satir("*125,50", 80, sol=60),
        ]
        kayit = {"company": "ORNEK MARKET", "seller_tax_id": "1234567890", "date": "03.07.2026",
                 "time": "13:00", "total_amount": 125.5}
        esle = alanlari_esle(kayit, satirlar)
        self.assertEqual(esle["company"], [0])
        self.assertEqual(esle["seller_tax_id"], [1])
        self.assertEqual(esle["date"], [2])
        self.assertEqual(esle["total_amount"], [4])


class GorselEtiketEslestirTesti(unittest.TestCase):
    def test_numara(self):
        self.assertEqual(numara("fis144"), 144)
        self.assertEqual(numara("ornek"), -1)

    def test_etiketleri_oku_ve_puanla(self):
        with tempfile.TemporaryDirectory() as klasor:
            with open(os.path.join(klasor, "fis1.json"), "w", encoding="utf-8") as f:
                json.dump({"seller_tax_id": "1234567890", "date": "03.07.2026", "total": "125,50"}, f)
            etiketler = etiketleri_oku(klasor)
        self.assertEqual(list(etiketler), ["fis1"])
        puan, neden = puanla(["VERGI NO 1234567890", "03.07.2026", "TOPLAM 125,50"], etiketler["fis1"])
        self.assertEqual((puan, neden), (4, "vkn+tarih+toplam"))
        puan, _ = puanla(["BASKA BIR FIS"], etiketler["fis1"])
        self.assertEqual(puan, 0)


if __name__ == "__main__":
    unittest.main()
