"""
Layout pilotu: sentetik fislerde bilinen alan degerleri OCR kutularina eslesiyor mu?

Ne yapar:
  1. Secilen bolumden (egitim/dogrulama/test) rastgele N fis alir.
  2. Her gorseli PaddleOCR ile okur, her satirin kutusunu (bbox), skorunu ve siRasini kaydeder.
  3. Girdi JSON'daki bilinen degerleri (firma, VKN, fis no, tarih, saat, toplam, KDV) OCR satirlariyla eslestirir.
  4. Alan basina eslesme oranini ve alanin fisin neresinde durdugunu (yukaridan asagi %) raporlar.
  5. Eslesen kutulari "zayif etiket" olarak kaydeder (ileride LiLT/LayoutXLM egitimi icin).

Karar kurali: cogu alanda eslesme >= %80 ise otomatik kutu etiketi uretilebilir.

Kurulum: gercek fislerde calisan Colab ortaminin aynisi (Python 3.11 venv, paddlepaddle-gpu==2.6.2, paddleocr==2.10.0, pillow)
Yerel/CPU icin:  pip install paddlepaddle==2.6.2 paddleocr==2.10.0 pillow numpy
Kullanim:
  python -m scripts.layout.layout_pilot --veri "C:\\...\\final_veriler_en" --bolum egitim --n 300 --cikti layout_pilot_cikti
Yalniz eslestirmeyi denemek (OCR kaydi varsa): --ocr-atla
"""
import argparse
import json
import os
import random
import re
import statistics
from collections import defaultdict
from difflib import SequenceMatcher

ALANLAR = ["company", "seller_tax_id", "receipt_no", "date", "time", "total_amount", "total_vat_amount"]
FIRMA_ESIK = 0.85

_TR = str.maketrans("ÇĞİIÖŞÜçğıöşüâîû", "CGIIOSUCGIOSUAIU")


def norm(s):
    s = str(s or "").translate(_TR).upper()
    return re.sub(r"[^A-Z0-9 ]+", " ", s).split()


def norm_str(s):
    return " ".join(norm(s))


def sadece_alnum(s):
    return re.sub(r"[^A-Z0-9]", "", "".join(norm(s)))


def sayi_cevir(tok):
    """'4.127,69' -> 4127.69 ; '1.021,70' -> 1021.7 ; '205,63' -> 205.63 ; '1.021' -> 1021.0"""
    t = tok.strip().strip("*")
    if not re.fullmatch(r"\d[\d.,]*", t):
        return None
    if "," in t and "." in t:
        if t.rfind(",") > t.rfind("."):
            t = t.replace(".", "").replace(",", ".")
        else:
            t = t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".") if len(t.split(",")[-1]) <= 2 else t.replace(",", "")
    elif "." in t:
        parcalar = t.split(".")
        if len(parcalar[-1]) == 3 and len(parcalar) >= 2:
            t = t.replace(".", "")
    try:
        return float(t)
    except ValueError:
        return None


# ---------------------------------------------------------------- eslestirme (OCR'dan bagimsiz, test edilebilir)

def satir_eslesir(alan, deger, metin):
    """Tek bir OCR satiri bu alanin degerini iceriyor mu?"""
    if deger in (None, ""):
        return False
    if alan == "seller_tax_id":
        return re.sub(r"\D", "", str(deger)) in re.sub(r"[^\d]", "", metin)
    if alan == "receipt_no":
        v = sadece_alnum(deger)
        return len(v) >= 3 and v in sadece_alnum(metin)
    if alan == "date":
        g, a, y = re.split(r"[./\-]", str(deger))[:3]
        # OCR bazen tarih ve saati bitisik okur ("03.07.202613:00"): yildan sonra saat gelebilir
        return bool(re.search(rf"(?<!\d)0?{int(g)}\s?[./\-]\s?0?{int(a)}\s?[./\-]\s?{y}(?:(?!\d)|(?=\d{{1,2}}:\d{{2}}))", metin))
    if alan == "time":
        return str(deger)[:5] in metin.replace(" ", "")
    if alan in ("total_amount", "total_vat_amount"):
        hedef = float(deger)
        for tok in re.findall(r"\d[\d.,]*\d|\d", metin):
            v = sayi_cevir(tok)
            if v is not None and abs(v - hedef) < 0.006:
                return True
        return False
    return False


def firma_eslesmesi(deger, satirlar):
    """Firma adi 1-3 ardisik satira bolunmus olabilir. Eslesen satir indekslerini dondurur (yoksa [])."""
    hedef = norm_str(deger)
    if not hedef:
        return []
    en_iyi, en_iyi_idx = 0.0, []
    for i in range(len(satirlar)):
        for k in (1, 2, 3):
            if i + k > len(satirlar):
                break
            parca = norm_str(" ".join(s["text"] for s in satirlar[i:i + k]))
            if not parca:
                continue
            r = SequenceMatcher(None, hedef, parca).ratio()
            if r > en_iyi:
                en_iyi, en_iyi_idx = r, list(range(i, i + k))
    return en_iyi_idx if en_iyi >= FIRMA_ESIK else []


def satir_baglami(satirlar, i):
    """Ayni yatay satirdaki (y merkezi ust uste binen) tum OCR kutularinin normalize metni: etiket + deger birlikte."""
    y1, y2 = satirlar[i]["box"][1], satirlar[i]["box"][3]

    def ayni_satir(s):
        ortak = min(y2, s["box"][3]) - max(y1, s["box"][1])
        return ortak >= 0.5 * min(y2 - y1, s["box"][3] - s["box"][1])

    return " ".join(norm_str(s["text"]) for s in satirlar if ayni_satir(s))


def tutar_sec(alan, idx, satirlar):
    """Tutar birden cok yerde gecer (satir, TOPLAM, KDV tablosu). Etiketi dogru olani sec, yoksa en alttakini."""
    def uygun(i):
        b = satir_baglami(satirlar, i)
        if alan == "total_amount":
            return ("TOPLAM" in b or "TOTAL" in b or "GENEL" in b) and "KDV" not in b
        return "KDV" in b and "ORAN" not in b
    adaylar = [i for i in idx if uygun(i)]
    secim = adaylar or idx
    return [max(secim, key=lambda i: satirlar[i]["box"][1])]


def alanlari_esle(kayit, satirlar):
    """Dondurur: {alan: [satir indeksleri]} (bulunamayan alan icin bos liste)."""
    sonuc = {}
    for alan in ALANLAR:
        deger = kayit.get(alan)
        if alan == "company":
            sonuc[alan] = firma_eslesmesi(deger, satirlar)
            continue
        idx = [i for i, s in enumerate(satirlar) if satir_eslesir(alan, deger, s["text"])]
        if alan in ("total_amount", "total_vat_amount") and idx:
            idx = tutar_sec(alan, idx, satirlar)
        elif alan in ("seller_tax_id", "receipt_no", "date", "time") and idx:
            idx = [idx[0]]
        sonuc[alan] = idx
    return sonuc


# ---------------------------------------------------------------- OCR (PaddleOCR)

def ocr_hazirla(gpu=False, motor="paddle"):
    """Gercek fisler icin Colab'da calisan ayarlarla ayni: paddlepaddle(-gpu)==2.6.2, paddleocr==2.10.0, lang='tr'.
    motor='rapidocr': modelleri pakete gomulu PP-OCR ONNX (pip install rapidocr-onnxruntime); model indirmek gerekmez."""
    if motor == "rapidocr":
        from rapidocr_onnxruntime import RapidOCR
        return RapidOCR()
    from paddleocr import PaddleOCR
    denemeler = [
        {"lang": "tr", "use_gpu": gpu, "use_angle_cls": False, "show_log": False},  # 2.x
        {"lang": "tr", "use_gpu": gpu, "use_angle_cls": False},
        {"lang": "latin", "use_angle_cls": False},
        {"lang": "latin"},  # 3.x
    ]
    son_hata = None
    for kw in denemeler:
        try:
            return PaddleOCR(**kw)
        except (TypeError, ValueError) as e:
            son_hata = e
    raise RuntimeError(f"PaddleOCR baslatilamadi: {son_hata}")


def ocr_oku(ocr, yol, olcek):
    """Dondurur: (satirlar, (genislik, yukseklik)); satir = {text, score, box[x1,y1,x2,y2]} orijinal piksel."""
    import numpy as np
    from PIL import Image
    im = Image.open(yol).convert("RGB")
    w, h = im.size
    if olcek != 1:
        im = im.resize((int(w * olcek), int(h * olcek)), Image.LANCZOS)
    arr = np.array(im)
    satirlar = []

    def ekle(poly, text, score):
        xs, ys = [p[0] for p in poly], [p[1] for p in poly]
        satirlar.append({"text": str(text), "score": float(score),
                         "box": [min(xs) / olcek, min(ys) / olcek, max(xs) / olcek, max(ys) / olcek]})

    if type(ocr).__name__ == "RapidOCR":
        res, _ = ocr(arr)
        for poly, text, score in (res or []):
            ekle(poly, text, float(score))
    elif hasattr(ocr, "predict"):  # PaddleOCR 3.x
        for r in ocr.predict(arr):
            for poly, text, score in zip(r["rec_polys"], r["rec_texts"], r["rec_scores"]):
                ekle(poly, text, score)
    else:  # PaddleOCR 2.x
        res = ocr.ocr(arr, cls=False)
        for poly, (text, score) in (res[0] or []):
            ekle(poly, text, score)
    satirlar.sort(key=lambda s: (s["box"][1], s["box"][0]))
    return satirlar, (w, h)


# ---------------------------------------------------------------- ana akis

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--veri", required=True, help="final_veriler_en klasoru")
    ap.add_argument("--bolum", default="egitim", choices=["egitim", "dogrulama", "test"])
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--olcek", type=float, default=2.0, help="kucuk gorselleri OCR oncesi buyutme carpani")
    ap.add_argument("--cikti", default="layout_pilot_cikti")
    ap.add_argument("--ocr-atla", action="store_true", help="kayitli OCR cikti dosyalarini kullan")
    ap.add_argument("--gpu", action="store_true", help="GPU kullan (paddlepaddle-gpu kuruluysa)")
    ap.add_argument("--motor", default="paddle", choices=["paddle", "rapidocr"],
                    help="OCR motoru (rapidocr: model indirmeden pip ile kurulur)")
    a = ap.parse_args()

    with open(os.path.join(a.veri, f"{a.bolum}_girdi.json"), encoding="utf-8") as f:
        kayitlar = json.load(f)
    random.Random(a.seed).shuffle(kayitlar)
    kayitlar = kayitlar[:a.n]

    ocr_klasor = os.path.join(a.cikti, "ocr")
    os.makedirs(ocr_klasor, exist_ok=True)
    ocr = None if a.ocr_atla else ocr_hazirla(a.gpu, a.motor)

    bulundu = defaultdict(int)
    konum = defaultdict(list)
    zayif = []
    islenen = 0

    for k, kayit in enumerate(kayitlar, 1):
        rid = kayit["record_id"]
        ocr_yol = os.path.join(ocr_klasor, rid + ".json")
        if a.ocr_atla:
            if not os.path.exists(ocr_yol):
                continue
            with open(ocr_yol, encoding="utf-8") as f:
                kayit_ocr = json.load(f)
            satirlar, boyut = kayit_ocr["satirlar"], tuple(kayit_ocr["boyut"])
        else:
            gorsel = os.path.join(a.veri, f"{a.bolum}_gorsel", rid + ".png")
            satirlar, boyut = ocr_oku(ocr, gorsel, a.olcek)
            with open(ocr_yol, "w", encoding="utf-8") as f:
                json.dump({"record_id": rid, "boyut": list(boyut), "satirlar": satirlar}, f, ensure_ascii=False)

        islenen += 1
        eslesme = alanlari_esle(kayit, satirlar)
        zayif.append({"record_id": rid, "boyut": list(boyut),
                      "alanlar": {al: [{"satir": i, "box": satirlar[i]["box"], "text": satirlar[i]["text"]} for i in idx]
                                  for al, idx in eslesme.items() if idx}})
        for alan, idx in eslesme.items():
            if idx:
                bulundu[alan] += 1
                yc = statistics.mean((satirlar[i]["box"][1] + satirlar[i]["box"][3]) / 2 for i in idx)
                konum[alan].append(yc / boyut[1])
        if k % 50 == 0:
            print(f"  {k}/{len(kayitlar)} fis islendi")

    if islenen == 0:
        print("Islenen fis yok.")
        return

    with open(os.path.join(a.cikti, "zayif_etiket.jsonl"), "w", encoding="utf-8") as f:
        for z in zayif:
            f.write(json.dumps(z, ensure_ascii=False) + "\n")

    print(f"\nBolum: {a.bolum}   Islenen fis: {islenen}\n")
    print(f"{'Alan':<18}{'Eslesen':>10}{'Oran':>8}   {'Medyan konum':>13}{'Ilk %25':>9}{'Son %25':>9}")
    print("-" * 70)
    for alan in ALANLAR:
        n = bulundu[alan]
        ys = konum[alan]
        if ys:
            ilk = sum(y < 0.25 for y in ys) / len(ys)
            son = sum(y > 0.75 for y in ys) / len(ys)
            print(f"{alan:<18}{n:>10}{100 * n / islenen:>7.0f}%   {100 * statistics.median(ys):>12.0f}%{100 * ilk:>8.0f}%{100 * son:>8.0f}%")
        else:
            print(f"{alan:<18}{n:>10}{100 * n / islenen:>7.0f}%")
    print(f"\nCikti klasoru: {a.cikti}  (ocr/*.json: kutulu OCR, zayif_etiket.jsonl: eslesen kutular)")
    print("Karar: cogu alanda eslesme >= %80 ise otomatik kutu etiketi uretilebilir.")


if __name__ == "__main__":
    main()
