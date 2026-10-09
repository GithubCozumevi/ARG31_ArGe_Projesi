"""
Gercek fislerin hepsini OCR'dan gecirir ve her satirin kutusunu (bbox) kaydeder.

Cikti formati, eski Colab OCR dosyalariyla ayni (vkn_olc.py vb. betikler okuyabilsin diye):
  {"image": "fis1.jpg", "boyut": [genislik, yukseklik],
   "ocr": [{"text": "...", "bbox": [[x,y],[x,y],[x,y],[x,y]], "confidence": 0.93}, ...]}
OCR dosyasinin adi gorselin adiyla ayni oldugu icin eski "numara kaymasi" sorunu olusmaz.

Kurulum:  pip install rapidocr-onnxruntime pillow
Kullanim: python -m scripts.layout.ocr_bbox_kaydet --gorsel <gorsel klasoru> --cikti <cikti klasoru>
Daha once islenen dosyalar atlanir (kesilirse kaldigi yerden devam eder).
"""
import argparse
import glob
import json
import os
import time

from PIL import Image


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gorsel", required=True)
    ap.add_argument("--cikti", required=True)
    a = ap.parse_args()

    from rapidocr_onnxruntime import RapidOCR
    motor = RapidOCR()
    os.makedirs(a.cikti, exist_ok=True)

    yollar = sorted(glob.glob(os.path.join(a.gorsel, "*.jpg")) + glob.glob(os.path.join(a.gorsel, "*.png")))
    print(f"Gorsel: {len(yollar)}  Cikti: {a.cikti}", flush=True)
    baslangic = time.time()
    islenen = 0
    for i, yol in enumerate(yollar, 1):
        ad = os.path.basename(yol)
        cikti_yolu = os.path.join(a.cikti, os.path.splitext(ad)[0] + ".json")
        if os.path.exists(cikti_yolu):
            continue
        try:
            boyut = Image.open(yol).size
            sonuc, _ = motor(yol)
            satirlar = [{"text": str(t),
                         "bbox": [[float(x), float(y)] for x, y in poly],
                         "confidence": float(s)} for poly, t, s in (sonuc or [])]
            # yukaridan asagiya, soldan saga sirala
            satirlar.sort(key=lambda s: (min(p[1] for p in s["bbox"]), min(p[0] for p in s["bbox"])))
            with open(cikti_yolu, "w", encoding="utf-8") as f:
                json.dump({"image": ad, "boyut": list(boyut), "ocr": satirlar}, f, ensure_ascii=False)
            islenen += 1
        except Exception as e:  # tek fis hatasi tum isi durdurmasin
            print(f"  HATA {ad}: {type(e).__name__}: {e}", flush=True)
        if i % 20 == 0:
            gecen = time.time() - baslangic
            print(f"  {i}/{len(yollar)}  ({gecen:.0f} sn)", flush=True)
    print(f"Bitti. Yeni islenen: {islenen}  Toplam sure: {time.time() - baslangic:.0f} sn", flush=True)


if __name__ == "__main__":
    main()
