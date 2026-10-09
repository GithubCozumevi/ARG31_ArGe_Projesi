"""
Olcum ve veri denetim betiklerinin ortak yardimcilari: gold/OCR/VLM dosyalarini okuma, kayma duzeltmesi,
cikti yolu, tarih adaylari ve firma adi anahtar kelimeleri.
"""
import glob
import json
import os
import re
from pathlib import Path

from app.services.vendors import firma_normalize

PROJE_KOKU = Path(__file__).resolve().parents[1]
VERI_KLASORU = PROJE_KOKU / "data"   # betik ciktilari (git'e girmez); hangi klasorden calistirilirsa calistirilsin ayni yer

VLM_EKLERI = ("_clean.json", "_pred.json", ".json")

# Hukuki ek ve genel kelimeler: karsilastirmada anlam tasimaz
EKLER = {
    "AS", "A", "S", "LTD", "STI", "SAN", "TIC", "VE", "SANAYI", "TICARET", "LIMITED", "SIRKETI", "ANONIM",
    "ISLETMELERI", "ISLETMESI", "HIZMETLERI", "GIDA", "TURIZM", "INSAAT", "PAZARLAMA", "ORGANIZASYON",
}


def veri_yolu(dosya_adi):
    """data/ altindaki bir cikti dosyasinin tam yolu (klasor yoksa olusturulur)."""
    VERI_KLASORU.mkdir(parents=True, exist_ok=True)
    return str(VERI_KLASORU / dosya_adi)


def json_oku(yol):
    try:
        with open(yol, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def gold_dosyalari(klasor):
    """Gold (etiket) klasorundeki gecerli JSON'lar: [(fis_adi, dict), ...] fis adina gore sirali.
    Bozuk dosyalar ve sozluk olmayan icerikler atlanir."""
    sonuc = []
    for yol in sorted(glob.glob(os.path.join(klasor, "*.json"))):
        veri = json_oku(yol)
        if isinstance(veri, dict):
            sonuc.append((os.path.splitext(os.path.basename(yol))[0], veri))
    return sonuc


def ocr_satirlari(yol):
    """OCR dosyasindaki satirlar (yukaridan asagi). Dosya yoksa ya da beklenen semada degilse None."""
    veri = json_oku(yol)
    if not isinstance(veri, dict) or not isinstance(veri.get("ocr"), list):
        return None
    return [str(o.get("text", "")) for o in veri["ocr"] if isinstance(o, dict)]


def ocr_metni(yol):
    satirlar = ocr_satirlari(yol)
    return None if satirlar is None else "\n".join(satirlar)


def vlm_json_oku(klasor, fis):
    """Fisin VLM ciktisini ({fis}_clean.json, {fis}_pred.json ya da {fis}.json) okur; yoksa None."""
    for ek in VLM_EKLERI:
        veri = json_oku(os.path.join(klasor, fis + ek))
        if isinstance(veri, dict):
            return veri
    return None


def vlm_oku(klasor, fis):
    """Donen: (seller_tax_id, company). Klasor verilmediyse ya da dosya yoksa (None, None)."""
    veri = vlm_json_oku(klasor, fis) if klasor else None
    if veri is None:
        return None, None
    return veri.get("seller_tax_id"), veri.get("company")


def kayma_argumanlari_ekle(ap):
    """Kayma duzeltmesi: gold fisM >= --kayma-baslangic icin OCR/VLM dosyasi fis(M + --kayma) olur."""
    ap.add_argument("--kayma", type=int, default=0)
    ap.add_argument("--kayma-baslangic", type=int, default=0)


def kayma_cozucu(args):
    """kayma_argumanlari_ekle ile okunan argumanlara gore: gold fis adi -> OCR/VLM dosya adi donduren fonksiyon."""
    return lambda fis: kaymali_ad(fis, args.kayma, args.kayma_baslangic)


def kaymali_ad(fis, kayma, baslangic):
    """Gold fis adinin karsilik geldigi OCR/VLM dosya adi."""
    m = re.fullmatch(r"(\D*)(\d+)", fis)
    if kayma and m and int(m.group(2)) >= baslangic:
        return f"{m.group(1)}{int(m.group(2)) + kayma}"
    return fis


def sadece_rakam(s):
    return re.sub(r"\D", "", str(s or ""))


def tarih_adaylari(tarih):
    """'14.05.2023' -> ['14052023', '140523']"""
    parca = re.findall(r"\d+", str(tarih or ""))
    if len(parca) != 3:
        return []
    g, a, y = parca[0].zfill(2), parca[1].zfill(2), parca[2]
    if len(y) == 4:
        return [g + a + y, g + a + y[2:]]
    if len(y) == 2:
        return [g + a + y]
    return []


def anahtar_kelimeler(s):
    """Firma adinin hukuki ekler (A.S., LTD. STI. ...) atildiktan sonra kalan kelimeleri."""
    return {k for k in firma_normalize(s).split() if k not in EKLER and len(k) > 1}
