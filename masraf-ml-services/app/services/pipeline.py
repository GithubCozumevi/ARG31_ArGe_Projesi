"""
Hibrit cikarim akisi (cascading): once ucuz/hizli yontem, yetmezse sonrakine dus.

VKN icin sira:
  1. OCR metninden regex + checksum              (CPU'da aninda)
  2. Satici sozlugu: OCR'daki numara (aynen ya da tek hane farkla) biliniyor mu?
  3. OCR'da birden fazla gecerli numara var, VLM biriyle uyusuyor
  4. VLM'in verdigi VKN, checksum'i geciyorsa     (VLM ancak gerekirse)
  5. OCR'da tek hane duzeltme                     (dusuk guven)
  6. Hicbiri olmadiysa -> insan kontrolune yonlendir

VKN belli olunca satici sozlugunden firma adi da getirilir.
Her basamak ayri bir fonksiyondur: karar verirse bir sonuc sozlugu, veremezse None doner.
"""
from typing import Optional

from app.services import vendors
from app.services.extraction import VknSonuc, kimlik_gecerli, vkn_bul_ve_dogrula


class Kaynak:
    """sonuc["kaynak"] degerleri."""
    OCR_REGEX = "ocr_regex"
    SATICI_SOZLUGU = "satici_sozlugu"
    OCR_VLM_UYUMU = "ocr_vlm_uyumu"
    VLM = "vlm"
    OCR_DUZELTME = "ocr_duzeltme"
    YOK = "yok"


class Guven:
    """sonuc["guven"] degerleri."""
    YUKSEK = "yuksek"
    ORTA = "orta"
    DUSUK = "dusuk"
    YOK = "yok"


def _temiz_vkn(vkn: Optional[str]) -> str:
    return "".join(c for c in (vkn or "") if c.isdigit())


def _karar(vkn: Optional[str], kaynak: str, guven: str, insan_kontrolu: bool) -> dict:
    return {"vkn": vkn, "kaynak": kaynak, "guven": guven, "insan_kontrolu": insan_kontrolu}


def _ocr_adimi(ocr: VknSonuc) -> Optional[dict]:
    """1) OCR'daki VKN dogrudan checksum'i geciyor."""
    if ocr.durum == "gecerli_orijinal":
        return _karar(ocr.deger, Kaynak.OCR_REGEX, Guven.YUKSEK, False)
    return None


def _sozluk_adimi(ocr_text: str, sozluk: dict, vlm_temiz: str, vlm_gecerli: bool, detay: dict) -> Optional[dict]:
    """2) Satici sozlugu (OCR metnindeki tum 10 haneli adaylar uzerinden)."""
    if not sozluk:
        return None
    arama = vendors.sozlukte_ara(sozluk, vendors.vkn_adaylari(ocr_text))
    detay["sozluk_arama"] = arama["durum"]
    if arama["durum"] not in ("bilinen", "kurtarildi"):
        return None
    sonuc = _karar(
        arama["vkn"],
        Kaynak.SATICI_SOZLUGU,
        Guven.YUKSEK if arama["durum"] == "bilinen" else Guven.ORTA,
        False,
    )
    # VLM'in gecerli ama farkli bir VKN'si varsa celiski: insana birak
    if vlm_gecerli and vlm_temiz != arama["vkn"]:
        sonuc["insan_kontrolu"] = True
        detay["vlm_celiskisi"] = vlm_temiz
    return sonuc


def _ocr_vlm_uyum_adimi(ocr: VknSonuc, vlm_temiz: str, vlm_gecerli: bool, detay: dict) -> Optional[dict]:
    """3) OCR'da birden fazla gecerli numara var, VLM bunlardan birini soyluyor: iki kaynak uyusuyor."""
    if ocr.durum != "coklu_gecerli":
        return None
    detay["ocr_coklu_aday"] = ocr.belirsiz_adaylar
    if vlm_gecerli and vlm_temiz in ocr.belirsiz_adaylar:
        return _karar(vlm_temiz, Kaynak.OCR_VLM_UYUMU, Guven.YUKSEK, False)
    return None


def _vlm_adimi(vlm_temiz: str, vlm_gecerli: bool) -> Optional[dict]:
    """4) VLM'in VKN'si checksum'i geciyorsa."""
    if vlm_gecerli:
        return _karar(vlm_temiz, Kaynak.VLM, Guven.ORTA, False)
    return None


def _duzeltme_adimi(ocr: VknSonuc) -> Optional[dict]:
    """5) OCR'da tek hane duzeltmesiyle tek aday bulunduysa (%2 civari yanlis riski): insan kontrolu sart."""
    if ocr.durum == "gecerli_duzeltildi":
        return _karar(ocr.deger, Kaynak.OCR_DUZELTME, Guven.DUSUK, True)
    return None


def vkn_karar_ver(
    ocr_text: str,
    vlm_vkn: Optional[str] = None,
    sozluk: Optional[dict] = None,
    firma_ipucu: Optional[str] = None,
) -> dict:
    """
    Donen sozluk:
      vkn            : secilen VKN/TCKN ya da None
      kaynak         : Kaynak sabitlerinden biri ("ocr_regex", "satici_sozlugu", "ocr_vlm_uyumu", "vlm", "ocr_duzeltme", "yok")
      guven          : Guven sabitlerinden biri ("yuksek", "orta", "dusuk", "yok")
      insan_kontrolu : True ise fis elle kontrol edilmeli
      kimlik_tipi    : "VKN" | "TCKN" | None
      satici         : satici sozlugu eslesmesi (firma adi, yeni satici mi, uyumsuzluk)
      detay          : adim adim ne oldugu (izleme icin)
    """
    sozluk = sozluk or {}
    ocr = vkn_bul_ve_dogrula(ocr_text)
    vlm_temiz = _temiz_vkn(vlm_vkn)
    vlm_gecerli = kimlik_gecerli(vlm_temiz)  # 10 hane VKN, 11 hane TCKN (sahis saticilar)

    detay = {
        "ocr_durum": ocr.durum,
        "ocr_ham_aday": ocr.ham_aday,
        "belirsiz_aday_sayisi": len(ocr.belirsiz_adaylar),
        "vlm_vkn_gecerli": vlm_gecerli,
        "sozluk_boyutu": len(sozluk),
    }

    sonuc = (
        _ocr_adimi(ocr)
        or _sozluk_adimi(ocr_text, sozluk, vlm_temiz, vlm_gecerli, detay)
        or _ocr_vlm_uyum_adimi(ocr, vlm_temiz, vlm_gecerli, detay)
        or _vlm_adimi(vlm_temiz, vlm_gecerli)
        or _duzeltme_adimi(ocr)
        or _karar(None, Kaynak.YOK, Guven.YOK, True)   # 6) Hicbiri guvenilir degil
    )

    uzunluk = len(sonuc["vkn"] or "")
    sonuc["kimlik_tipi"] = "VKN" if uzunluk == 10 else ("TCKN" if uzunluk == 11 else None)
    sonuc["satici"] = vendors.satici_bilgisi(sozluk, sonuc["vkn"], firma_ipucu)
    # "Ayni VKN, farkli firma adi" uyarisi yalnizca bilgi olarak kaydedilir; fisi insana yollamaz.
    # Sebep: ipucu olarak kullanilan VLM firma adi ~%31 dogrulukta, yani uyari cogu zaman
    # VKN'nin degil firma adinin yanlis okunmasindan kaynaklaniyor. Firma adi icin
    # daha guvenilir bir yontem (ornegin NER) gelince bu kural yeniden acilabilir.
    if sonuc["satici"].get("firma_uyusmuyor"):
        detay["firma_uyarisi"] = True

    sonuc["detay"] = detay
    return sonuc
