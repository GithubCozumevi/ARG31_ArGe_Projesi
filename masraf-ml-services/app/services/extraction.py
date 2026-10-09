"""
OCR metninden VKN (Vergi Kimlik Numarasi) / TCKN adaylarini bulup
checksum ile dogrulayan, gerekirse tek haneyi deneyerek duzelten modul.

Mantik:
  1. OCR metninde 10 haneli (VKN) ya da 11 haneli (TCKN) tum sayi
     dizilerini regex ile bul.
  2. Her adayin checksum'ini kontrol et.
  3. Gecerli aday varsa onu don.
  4. Hicbiri gecerli degilse, her aday icin tek bir haneyi 0-9 arasinda
     degistirerek checksum'i gecen bir kombinasyon var mi diye dene
     (OCR'da tek hane karisma cok yaygin bir hata: 8<->3, 6<->5, 0<->8 gibi).
  5. Hala bulunamazsa, en "olasi" ham adayi (ilk bulunan) dondur ama
     'dogrulanmadi' olarak isaretle.
"""
import re
from typing import Optional, NamedTuple


class VknSonuc(NamedTuple):
    deger: Optional[str]              # bulunan/duzeltilen VKN ya da None
    durum: str                        # "gecerli_orijinal" | "coklu_gecerli" | "gecerli_duzeltildi" | "belirsiz_duzeltme" | "dogrulanamadi" | "bulunamadi"
    ham_aday: Optional[str]           # OCR'da bulunan orijinal (duzeltilmemis) aday
    belirsiz_adaylar: list            # durum "coklu_gecerli" ya da "belirsiz_duzeltme" ise, birden fazla gecerli aday burada listelenir


def normalize_sayi(s: str) -> str:
    """Bosluk, tire, nokta gibi ayiricilari temizler."""
    return re.sub(r"[\s\-./]", "", s)


def is_valid_vkn_checksum(vkn: str) -> bool:
    """Turkiye VKN (vergi kimlik no) checksum algoritmasi - 10 hane."""
    vkn = normalize_sayi(vkn)
    if not re.fullmatch(r"[0-9]{10}", vkn):
        return False
    d = [int(c) for c in vkn]
    total = 0
    for i in range(9):
        tmp = (d[i] + (9 - i)) % 10
        if tmp == 0:
            continue
        p = (tmp * (2 ** (9 - i))) % 9
        if p == 0:
            p = 9
        total += p
    return ((10 - (total % 10)) % 10) == d[9]


def is_valid_tckn_checksum(tckn: str) -> bool:
    """Turkiye TCKN (kimlik no) checksum algoritmasi - 11 hane."""
    t = normalize_sayi(tckn)
    if len(t) != 11 or not t.isdigit() or t[0] == "0":
        return False
    d = [int(x) for x in t]
    if (sum(d[0:10]) % 10) != d[10]:
        return False
    if (((d[0] + d[2] + d[4] + d[6] + d[8]) * 7 - (d[1] + d[3] + d[5] + d[7])) % 10) != d[9]:
        return False
    return True


# PaddleOCR'da (ve genel olarak matbu rakam OCR'inde) en sik gorulen
# rakam karistirma ciftleri. Checksum tek basina ayirt edici olmadigi icin
# (bkz. asagidaki not), belirsiz adaylar arasinda oncelik belirlemek icin kullanilir.
OCR_KARISMA_CIFTLERI = {
    "8": {"3", "0", "6"},
    "3": {"8"},
    "6": {"5", "8"},
    "5": {"6"},
    "0": {"8", "6"},
    "1": {"7"},
    "7": {"1", "2"},
    "2": {"7"},
    "9": {"4"},
    "4": {"9"},
}


def _hane_farki_bul(orijinal: str, degisen: str) -> Optional[tuple]:
    """Iki ayni uzunluktaki string arasinda tek hane farkini bulur.
    Donen: (pozisyon, eski_hane, yeni_hane) ya da None (fark tek hane degilse)."""
    farklar = [(i, a, b) for i, (a, b) in enumerate(zip(orijinal, degisen)) if a != b]
    if len(farklar) != 1:
        return None
    return farklar[0]


def _ocr_uyumlu_adaylari_filtrele(orijinal_aday: str, adaylar: list) -> list:
    """Verilen aday listesinden, degisen hanenin OCR'da orijinal haneyle
    karisma ihtimali olanlarini dondurur (bilinen karisma ciftlerine gore)."""
    uyumlu = []
    for aday in adaylar:
        fark = _hane_farki_bul(orijinal_aday, aday)
        if fark is None:
            continue
        _, eski, yeni = fark
        if yeni in OCR_KARISMA_CIFTLERI.get(eski, set()):
            uyumlu.append(aday)
    return uyumlu


def _tek_hane_duzelt(aday: str, checksum_fn) -> tuple[Optional[str], list[str]]:
    """
    Adayin her hanesini sirayla 0-9 arasinda degistirip checksum'i
    gecen TUM kombinasyonlari arar (sadece ilkini degil).

    Donen deger: (tek_gecerli_sonuc_veya_None, tum_gecerli_adaylar_listesi)
    Eger birden fazla farkli gecerli kombinasyon bulunursa, hangisinin
    "gercek" hata oldugu belirsizdir - bu durumda otomatik secim YAPMAYIZ.
    """
    bulunanlar = []
    for pozisyon in range(len(aday)):
        for yeni_hane in "0123456789":
            if yeni_hane == aday[pozisyon]:
                continue
            denenen = aday[:pozisyon] + yeni_hane + aday[pozisyon + 1:]
            if checksum_fn(denenen) and denenen not in bulunanlar:
                bulunanlar.append(denenen)

    if len(bulunanlar) == 1:
        return bulunanlar[0], bulunanlar
    return None, bulunanlar


_TARIH_GIBI = re.compile(r"^\d{2}(0[1-9]|1[0-2])20(1[89]|2\d)")  # ggaayyyy.. (tarih+saat damgasi)
# Etiket, numaranin HEMEN oncesinde bitmeli. "VKN/TCKN" (alici alani) bilerek yok.
# "Verglimllk No" gibi OCR ile bozulmus "Vergi Kimlik No" yazimlarini da yakalar.
_VKN_ETIKETI = re.compile(
    r"(?:V\.?\s?K\.?\s?N|V[a-zçğıöşü]{2,3}g\w{0,12}?\s*(?:N[oO0]|NUMARASI)|V\.?\s?D\.?)[\s:.\-]*$",
    re.IGNORECASE,
)
# Ayrac olarak satir sonu KABUL EDILMEZ: ayri satirlardaki rakamlar birlesip hayalet numara olusturmasin.
# 10 haneli (VKN) ya da 11 haneli (TCKN, sahis saticilar) numaralar
_NUMARA_DESENI = re.compile(r"(?<!\d)(?:\d[ \t\-./]?){9,10}\d(?!\d)")


def kimlik_gecerli(aday: str) -> bool:
    """10 hane: VKN checksum'i, 11 hane: TCKN checksum'i."""
    if len(aday) == 10:
        return is_valid_vkn_checksum(aday)
    if len(aday) == 11:
        return is_valid_tckn_checksum(aday)
    return False


def _vkn_olamaz(aday: str) -> bool:
    """VKN/TCKN olamayacak numaralar: tarih+saat damgasi, '00' ile baslayan 10 haneli (fis/islem no),
    '0' ile baslayan 11 haneli (telefon; TCKN 0 ile baslayamaz), ayni rakamin tekrari (11111111111)."""
    if _TARIH_GIBI.match(aday) or len(set(aday)) == 1:
        return True
    if len(aday) == 11:
        return aday.startswith("0")
    return aday.startswith("00")


def _etiketli_numaralar(ocr_text: str) -> set:
    """'VERGI NO', 'VKN', 'V.D.' gibi bir etiketin hemen (en cok 25 karakter) ardindan gelen 10/11 haneli numaralar."""
    bulunan = set()
    for m in _NUMARA_DESENI.finditer(ocr_text or ""):
        onceki = ocr_text[max(0, m.start() - 25):m.start()]
        if _VKN_ETIKETI.search(onceki):
            bulunan.add(normalize_sayi(m.group(0)))
    return bulunan


def vkn_bul_ve_dogrula(ocr_text: str) -> VknSonuc:
    """
    OCR metninden VKN adaylarini bulur, checksum ile dogrular,
    gerekirse tek hane duzeltmesi dener.
    """
    # 10 haneli adaylar: AYNI SATIR icinde, araya bosluk/tire girmis olabilir.
    # (Eskiden tum metin tek parca rakama cevriliyordu; ayri satirlardaki rakamlar
    #  birlesip hayalet VKN uretiyordu, ornegin "1100019" + sonraki satir.)
    adaylar = [normalize_sayi(m.group(0)) for m in _NUMARA_DESENI.finditer(ocr_text or "")]
    adaylar = [a for a in adaylar if len(a) in (10, 11)]
    # Tekrarlari at (sirayi koru) ve VKN olamayacak numaralari ele
    gorulen = set()
    adaylar = [a for a in adaylar if not (a in gorulen or gorulen.add(a)) and not _vkn_olamaz(a)]

    if not adaylar:
        return VknSonuc(deger=None, durum="bulunamadi", ham_aday=None, belirsiz_adaylar=[])

    # Dogrudan checksum'i gecenler
    gecerliler = [a for a in adaylar if kimlik_gecerli(a)]
    if len(gecerliler) == 1:
        return VknSonuc(deger=gecerliler[0], durum="gecerli_orijinal", ham_aday=gecerliler[0], belirsiz_adaylar=[])
    if len(gecerliler) > 1:
        # Birden fazla gecerli numara: "VERGI NO / VKN" etiketinin yanindakini sec
        etiketli = _etiketli_numaralar(ocr_text)
        etiketliler = [a for a in gecerliler if a in etiketli]
        if len(etiketliler) == 1:
            return VknSonuc(deger=etiketliler[0], durum="gecerli_orijinal", ham_aday=etiketliler[0], belirsiz_adaylar=[])
        # Ayirt edilemedi: karari sozluk / VLM / insana birak
        return VknSonuc(deger=None, durum="coklu_gecerli", ham_aday=None, belirsiz_adaylar=gecerliler)

    # Gecerli yoksa, her 10 haneli aday icin tek hane duzeltmesi dene (TCKN icin duzeltme yok)
    for aday in (a for a in adaylar if len(a) == 10):
        duzeltilmis, tum_adaylar = _tek_hane_duzelt(aday, is_valid_vkn_checksum)
        if duzeltilmis:
            return VknSonuc(deger=duzeltilmis, durum="gecerli_duzeltildi", ham_aday=aday, belirsiz_adaylar=[])
        if len(tum_adaylar) > 1:
            # Checksum tek basina ayirt edici degil (bkz. OCR_KARISMA_CIFTLERI notu).
            # Bilinen OCR karisma ciftlerine uyan adaylari onceliklendirerek daralt.
            ocr_uyumlu = _ocr_uyumlu_adaylari_filtrele(aday, tum_adaylar)
            if len(ocr_uyumlu) == 1:
                return VknSonuc(deger=ocr_uyumlu[0], durum="gecerli_duzeltildi", ham_aday=aday, belirsiz_adaylar=[])
            # Hala belirsizse (0 ya da 2+ OCR-uyumlu aday), belirsiz olarak isaretle,
            # daraltilmis liste varsa onu, yoksa tum listeyi don
            return VknSonuc(
                deger=None,
                durum="belirsiz_duzeltme",
                ham_aday=aday,
                belirsiz_adaylar=ocr_uyumlu if ocr_uyumlu else tum_adaylar,
            )

    # Hicbir sekilde dogrulanamadi, en azindan ilk adayi don (isaretli)
    return VknSonuc(deger=adaylar[0], durum="dogrulanamadi", ham_aday=adaylar[0], belirsiz_adaylar=[])