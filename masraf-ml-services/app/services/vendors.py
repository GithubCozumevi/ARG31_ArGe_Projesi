"""
Satici sozlugu (vendor registry): VKN -> firma eslestirmesi.

Fikir:
  - Etiketli (gold) fislerden "bu VKN su firmaya ait" sozlugu cikarilir.
  - Yeni bir fiste okunan VKN sozlukte varsa, satici ve firma adi kesinlesir.
  - OCR VKN'yi tek hane yanlis okuduysa, sozlukte bulunan tek bir komsu VKN
    varsa dogru olan odur (iki gecerli VKN birbirinden tek hane farkli olamaz,
    bu yuzden yanlis eslesme ihtimali cok dusuktur).

Sozluk simdilik bir JSON dosyasi. Ileride PostgreSQL'e tasinabilir.
"""
import glob
import json
import os
import re
import threading
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from app.services.extraction import is_valid_tckn_checksum, kimlik_gecerli, normalize_sayi

PROJE_KOKU = Path(__file__).resolve().parents[2]
SOZLUK_YOLU = os.getenv(
    "SATICI_SOZLUGU_YOLU", str(PROJE_KOKU / "data" / "satici_sozlugu.json")
)

# 10 haneli sayi: aralarda tek bosluk/tire/nokta/slash olabilir (456 123 7896 gibi)
_VKN_DESENI = re.compile(r"(?<!\d)(?:\d[ \t\-./]?){9}\d(?!\d)")  # satir sonu ayrac degil


def vkn_adaylari(metin: str) -> list:
    """Metindeki tum 10 haneli sayi adaylarini (tekrarsiz, sirali) dondurur."""
    bulunan = []
    for m in _VKN_DESENI.finditer(metin or ""):
        aday = normalize_sayi(m.group(0))
        if len(aday) == 10 and aday not in bulunan:
            bulunan.append(aday)
    return bulunan


def firma_normalize(s: Optional[str]) -> str:
    if not s:
        return ""
    s = str(s).upper().strip()
    s = s.translate(str.maketrans("ÇĞİÖŞÜ", "CGIOSU"))
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def firma_benzerlik(a: Optional[str], b: Optional[str]) -> float:
    a, b = firma_normalize(a), firma_normalize(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------- sozluk olusturma
def sozluk_kur(ciftler) -> dict:
    """(vkn, firma) ciftlerinden sozluk kurar: en sik gecen yazim 'firma', tum yazimlar 'takma_adlar' olur."""
    gruplar = defaultdict(list)
    for vkn, firma in ciftler:
        gruplar[vkn].append(firma)
    sozluk = {}
    for vkn, adlar in gruplar.items():
        sozluk[vkn] = {
            "firma": Counter(adlar).most_common(1)[0][0],
            "takma_adlar": sorted(set(adlar)),
            "fis_sayisi": len(adlar),
            "checksum_gecerli": kimlik_gecerli(vkn),
        }
    return sozluk


def sozluk_olustur(gold_klasoru: str) -> tuple:
    """
    Gold JSON klasorunden sozluk uretir.
    Donen: (sozluk, ozet)  -- ozet: okunan/kullanilan/atlanan sayilari ve atlama nedenleri
    """
    ciftler = []
    okunan = 0
    nedenler = Counter()

    for yol in sorted(glob.glob(os.path.join(gold_klasoru, "*.json"))):
        okunan += 1
        try:
            with open(yol, encoding="utf-8") as f:
                gold = json.load(f)
        except Exception:
            nedenler["bozuk_json"] += 1
            continue

        vkn = normalize_sayi(str(gold.get("seller_tax_id") or ""))
        firma = str(gold.get("company") or "").strip()

        if not vkn:
            nedenler["vkn_bos"] += 1
        elif re.fullmatch(r"\d{11}", vkn) and not is_valid_tckn_checksum(vkn):
            nedenler["tckn_checksum_gecersiz"] += 1
        elif not re.fullmatch(r"\d{10,11}", vkn):
            nedenler["vkn_baska_uzunluk_veya_harf"] += 1
        elif not firma:
            nedenler["firma_adi_bos"] += 1
        else:
            ciftler.append((vkn, firma))

    sozluk = sozluk_kur(ciftler)
    kullanilan = sum(v["fis_sayisi"] for v in sozluk.values())
    tekrar_eden = [v for v in sozluk.values() if v["fis_sayisi"] >= 2]
    ozet = {
        "okunan_dosya": okunan,
        "kullanilan_fis": kullanilan,
        "atlanan_dosya": okunan - kullanilan,
        "atlama_nedenleri": dict(nedenler),
        "benzersiz_satici": len(sozluk),
        "tek_fisli_satici": sum(1 for v in sozluk.values() if v["fis_sayisi"] == 1),
        "tekrar_eden_satici": len(tekrar_eden),
        # Bir fis, ayni saticinin BASKA bir fisi sozlukte oldugu surece sozlukten fayda gorebilir:
        "faydalanabilecek_fis": sum(v["fis_sayisi"] for v in tekrar_eden),
        "checksum_gecersiz_vkn": sum(1 for v in sozluk.values() if not v["checksum_gecerli"]),
        "tckn_satici": sum(1 for vkn in sozluk if len(vkn) == 11),
    }
    return sozluk, ozet


def sozluk_kaydet(sozluk: dict, yol: str = SOZLUK_YOLU) -> str:
    os.makedirs(os.path.dirname(yol), exist_ok=True)
    with open(yol, "w", encoding="utf-8") as f:
        json.dump(sozluk, f, ensure_ascii=False, indent=2)
    return yol


def sozluk_yukle(yol: str = SOZLUK_YOLU) -> dict:
    """Dosya yoksa bos sozluk doner (servis sozluksuz da calisir)."""
    if not os.path.exists(yol):
        return {}
    with open(yol, encoding="utf-8") as f:
        return json.load(f)


class SozlukDeposu:
    """
    Satici sozlugunu dosyadan ilk kullanimda yukler (modul acilisinda degil); yenile() ile yeniden okur.
    Thread-guvenlidir: yenileme sirasinda calisan istekler eski sozlugu kullanmaya devam eder.
    """

    def __init__(self, yol: Optional[str] = None):
        self.yol = yol or SOZLUK_YOLU
        self._sozluk: Optional[dict] = None
        self._kilit = threading.Lock()

    def al(self) -> dict:
        if self._sozluk is None:
            with self._kilit:
                if self._sozluk is None:
                    self._sozluk = sozluk_yukle(self.yol)
        return self._sozluk

    def yenile(self) -> dict:
        yeni = sozluk_yukle(self.yol)
        with self._kilit:
            self._sozluk = yeni
        return yeni


# ---------------------------------------------------------------- arama / kurtarma
def tek_hane_varyantlari(vkn: str):
    """Bir numaranin tek hanesi degistirilmis tum 90 varyantini uretir."""
    for i, eski in enumerate(vkn):
        for yeni in "0123456789":
            if yeni != eski:
                yield vkn[:i] + yeni + vkn[i + 1:]


def sozlukte_ara(sozluk: dict, adaylar: list) -> dict:
    """
    Aday numaralari sozlukte arar.
    durum: "bilinen"    -> aday aynen sozlukte
           "kurtarildi" -> aday tek hane farkla sozlukteki tek bir VKN'ye uyuyor
           "belirsiz"   -> tek hane farkla birden fazla farkli VKN'ye uyuyor
           "yok"        -> sozlukte karsiligi yok
    """
    if not sozluk or not adaylar:
        return {"durum": "yok", "vkn": None, "ham_aday": None}

    for a in adaylar:
        if a in sozluk:
            return {"durum": "bilinen", "vkn": a, "ham_aday": a}

    bulunan = {}
    for a in adaylar:
        for v in tek_hane_varyantlari(a):
            if v in sozluk:
                bulunan[v] = a

    if len(bulunan) == 1:
        vkn, ham = next(iter(bulunan.items()))
        return {"durum": "kurtarildi", "vkn": vkn, "ham_aday": ham}
    if len(bulunan) > 1:
        return {"durum": "belirsiz", "vkn": None, "ham_aday": None,
                "adaylar": sorted(bulunan)}
    return {"durum": "yok", "vkn": None, "ham_aday": None}


def satici_bilgisi(sozluk: dict, vkn: Optional[str], firma_ipucu: Optional[str] = None) -> dict:
    """Karar verilen VKN icin satici bilgisini (firma adi, uyumsuzluk uyarisi) uretir."""
    if not vkn:
        return {"durum": "vkn_yok"}
    kayit = sozluk.get(vkn) if sozluk else None
    if not kayit:
        return {"durum": "yeni_satici"}

    bilgi = {"durum": "bilinen_satici", "firma": kayit["firma"], "fis_sayisi": kayit["fis_sayisi"]}
    if firma_ipucu:
        # En iyi benzerligi kayitli tum adlarla karsilastir
        en_iyi = max(firma_benzerlik(firma_ipucu, ad) for ad in kayit["takma_adlar"])
        bilgi["firma_benzerlik"] = round(en_iyi, 2)
        if en_iyi < 0.6:
            bilgi["firma_uyusmuyor"] = True  # ayni VKN, farkli firma adi: kontrol edilmeli
    return bilgi