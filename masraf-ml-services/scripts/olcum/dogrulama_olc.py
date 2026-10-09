"""
Capraz dogrulama: VLM'in verdigi tarih / tutar / firma adi OCR metninde de geciyor mu?

Mantik (VKN'deki "emin oldugunda kabul et" yaklasimi): iki bagimsiz kaynak (VLM ve OCR) uyusuyorsa
sonuc otomatik kabul edilir, uyusmuyorsa insana gider.
Olculen: dogrulananlarin orani (kapsam) ve bunlarin dogrulugu (guvenilirlik).

Kullanim (proje kokunde):
  python -m scripts.olcum.dogrulama_olc --gold "<json3>" --ocr "<ocr>" --vlm "<cleaned>" --kayma 2 --kayma-baslangic 144
"""
import argparse
import os
import re
from difflib import SequenceMatcher

from scripts.olcum.alan_olc import ALAN_ADAYLARI, deger_al, karsilastir, tarih_norm, tutar_norm
from app.services.vendors import firma_normalize
from scripts.ortak import gold_dosyalari, kayma_argumanlari_ekle, kayma_cozucu, ocr_metni, vlm_json_oku

TARIH_DESENI = re.compile(r"\d{1,2}\s?[./\-]\s?\d{1,2}\s?[./\-]\s?\d{2,4}|\d{4}[./\-]\d{1,2}[./\-]\d{1,2}")
SAYI_DESENI = re.compile(r"\d[\d.,]*\d|\d")


def tarih_dogrulandi(metin, tahmin):
    t = tarih_norm(tahmin)
    if not t:
        return False
    return any(tarih_norm(re.sub(r"\s", "", m)) == t for m in TARIH_DESENI.findall(metin))


def tutar_dogrulandi(metin, tahmin):
    t = tutar_norm(tahmin)
    if t is None:
        return False
    for tok in SAYI_DESENI.findall(metin):
        v = tutar_norm(tok)
        if v is not None and abs(v - t) < 0.01:
            return True
    return False


def firma_dogrulandi(metin, tahmin, esik=0.8):
    f = firma_normalize(tahmin)
    if len(f) < 3:
        return False
    for satir in metin.split("\n"):
        s = firma_normalize(satir)
        if len(s) < 3:
            continue
        if SequenceMatcher(None, f, s).ratio() >= esik:
            return True
        if len(s) >= 6 and (s in f or f in s):
            return True
    return False


KONTROL = {"date": tarih_dogrulandi, "total": tutar_dogrulandi, "company": firma_dogrulandi}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    ap.add_argument("--ocr", required=True)
    ap.add_argument("--vlm", required=True)
    kayma_argumanlari_ekle(ap)
    a = ap.parse_args()

    ad = kayma_cozucu(a)

    # alan -> [dogrulanan_dogru, dogrulanan_yanlis, dogrulanmayan_dogru, dogrulanmayan_yanlis, bos]
    sayac = {alan: [0, 0, 0, 0, 0] for alan in KONTROL}
    n = 0

    for fis, gold in gold_dosyalari(a.gold):
        metin = ocr_metni(os.path.join(a.ocr, ad(fis) + ".json"))
        if metin is None:
            continue
        vlm = vlm_json_oku(a.vlm, ad(fis))
        if vlm is None:
            continue
        n += 1
        for alan, kontrol in KONTROL.items():
            g = deger_al(gold, ALAN_ADAYLARI[alan])
            t = deger_al(vlm, ALAN_ADAYLARI[alan])
            sonuc = karsilastir(alan, g, t)
            if sonuc is None:
                continue
            if sonuc == "bos":
                sayac[alan][4] += 1
                continue
            dogrulandi = kontrol(metin, t)
            idx = (0 if sonuc == "dogru" else 1) if dogrulandi else (2 if sonuc == "dogru" else 3)
            sayac[alan][idx] += 1

    print(f"\nOlculen fis: {n}\n")
    print(f"{'Alan':<10}{'VLM tek':>9}  {'OCR ile dogrulanan':>20}  {'Dogrulanan dogrulugu':>21}  {'Dogrulanamayan (insana)':>24}")
    print("-" * 90)
    for alan, (vd, vy, nd, ny, bos) in sayac.items():
        toplam = vd + vy + nd + ny + bos
        if toplam == 0:
            continue
        tek = (vd + nd) / toplam
        dog_say = vd + vy
        dog_oran = dog_say / toplam
        dog_dogru = vd / dog_say if dog_say else 0
        dogrulanamayan = nd + ny + bos
        print(f"{alan:<10}{100 * tek:>8.1f}%  {dog_say:>8} (%{100 * dog_oran:>3.0f}){'':>6}  %{100 * dog_dogru:>19.1f}  {dogrulanamayan:>8} (%{100 * dogrulanamayan / toplam:>3.0f})")
        print(f"{'':<10}  Dogrulanamayanlarin {nd}'i aslinda dogruydu (gereksiz insan kontrolu), {ny}'i yanlisti (dogru yakalandi).")
    print("\nOkuma: 'Dogrulanan dogrulugu' yuksekse OCR ile uyusan sonuclar otomatik kabul edilebilir.")


if __name__ == "__main__":
    main()