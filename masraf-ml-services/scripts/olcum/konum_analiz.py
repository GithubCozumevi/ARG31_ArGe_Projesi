"""
Konum analizi: firma adi ve toplam tutar, fisin neresinde (satir sirasina gore) bulunuyor?

Koordinat (bbox) olmadan da OCR satirlari yukaridan asagi siralidir; bu betik,
"firma adi ilk satirlarda, toplam son satirlarda" gibi bir konum kuralinin ise yarayip yaramayacagini olcer.

Kullanim (proje kokunde):
  python -m scripts.olcum.konum_analiz --gold "<json3>" --ocr "<ocr>" --kayma 2 --kayma-baslangic 144
"""
import argparse
import os
import re
from difflib import SequenceMatcher

from scripts.olcum.alan_olc import tutar_norm
from app.services.vendors import firma_normalize
from scripts.ortak import gold_dosyalari, kayma_argumanlari_ekle, kayma_cozucu, ocr_satirlari


def firma_satiri(satirlar, gold_firma):
    """Gold firma adina en cok benzeyen satirin indeksi (yoksa None)."""
    g = firma_normalize(gold_firma)
    en_iyi, idx = 0.0, None
    for i, s in enumerate(satirlar):
        n = firma_normalize(s)
        if len(n) < 3:
            continue
        r = SequenceMatcher(None, g, n).ratio()
        if len(n) >= 6 and (n in g or g in n):  # kismi: firma adi iki satira bolunmus olabilir
            r = max(r, 0.8)
        if r > en_iyi:
            en_iyi, idx = r, i
    return idx if en_iyi >= 0.7 else None


def tutar_satiri(satirlar, gold_tutar):
    """Gold tutarin gectigi SON satirin indeksi (toplam genelde altta, birden cok yerde gecebilir)."""
    hedef = tutar_norm(gold_tutar)
    if hedef is None:
        return None
    bulunan = None
    for i, s in enumerate(satirlar):
        for tok in re.findall(r"\d[\d.,]*\d|\d", s):
            t = tutar_norm(tok)
            if t is not None and abs(t - hedef) < 0.01:
                bulunan = i
    return bulunan


def dagilim(oranlar, adim=0.2):
    kovalar = [0] * int(round(1 / adim))
    for o in oranlar:
        kovalar[min(int(o / adim), len(kovalar) - 1)] += 1
    return kovalar


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    ap.add_argument("--ocr", required=True)
    kayma_argumanlari_ekle(ap)
    a = ap.parse_args()

    ocr_adi = kayma_cozucu(a)

    n = f_bulunan = t_bulunan = 0
    f_oran, t_oran = [], []
    f_ilk3 = f_ilk5 = t_son3 = t_son5 = 0

    for fis, g in gold_dosyalari(a.gold):
        satirlar = ocr_satirlari(os.path.join(a.ocr, ocr_adi(fis) + ".json"))
        if not satirlar:
            continue
        n += 1
        L = len(satirlar)

        firma = str(g.get("company") or "").strip()
        if firma:
            i = firma_satiri(satirlar, firma)
            if i is not None:
                f_bulunan += 1
                f_oran.append(i / max(L - 1, 1))
                f_ilk3 += i < 3
                f_ilk5 += i < 5

        tutar = g.get("total") or g.get("total_amount")
        if tutar:
            j = tutar_satiri(satirlar, tutar)
            if j is not None:
                t_bulunan += 1
                t_oran.append(j / max(L - 1, 1))
                t_son3 += j >= L - 3
                t_son5 += j >= L - 5

    if n == 0:
        print("Olculecek fis bulunamadi.")
        return

    print(f"\nOCR'i olan fis: {n}\n")
    print("FIRMA ADI (gold firma adina benzeyen satir)")
    print(f"  Metinde bulunan : {f_bulunan} / {n}  (%{100 * f_bulunan / n:.0f})")
    if f_bulunan:
        d = dagilim(f_oran)
        print(f"  Ilk 3 satirda   : %{100 * f_ilk3 / f_bulunan:.0f}   Ilk 5 satirda: %{100 * f_ilk5 / f_bulunan:.0f}   (bulunanlar icinde)")
        print("  Fisin neresinde (yuzde dilim -> fis sayisi):")
        for k, c in enumerate(d):
            print(f"    %{k * 20:>3}-{(k + 1) * 20:<3} : {c}")

    print("\nTOPLAM TUTAR (gold tutarin gectigi son satir)")
    print(f"  Metinde bulunan : {t_bulunan} / {n}  (%{100 * t_bulunan / n:.0f})")
    if t_bulunan:
        d = dagilim(t_oran)
        print(f"  Son 3 satirda   : %{100 * t_son3 / t_bulunan:.0f}   Son 5 satirda: %{100 * t_son5 / t_bulunan:.0f}   (bulunanlar icinde)")
        print("  Fisin neresinde (yuzde dilim -> fis sayisi):")
        for k, c in enumerate(d):
            print(f"    %{k * 20:>3}-{(k + 1) * 20:<3} : {c}")

    print("\nYorum: Bir dilimde fislerin cogu toplaniyorsa konum kurali ise yarar; dagiliksa koordinat (bbox) gerekir.")


if __name__ == "__main__":
    main()