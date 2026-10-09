"""Sirket harcama politikasi limitleri -- TEK kaynak: data/politika_limitleri.json.

Limit `birim_fiyat`a uygulanir ve iki katmanlidir: (kategori, birim) -> kategori.
Kod degistirmeden limit eklemek/guncellemek icin JSON'u duzenlemek yeterli.

Dosya yoksa ya da bozuksa RuntimeError firlatilir; sessiz dusme limit_asimi
anomalisini gorunmez sekilde yok ederdi.
"""

import json
from pathlib import Path

from ortak.schema import HarcamaKategorisi

LIMIT_DOSYASI = Path(__file__).parent.parent / "data" / "politika_limitleri.json"


def _yukle(yol: Path = LIMIT_DOSYASI):
    if not yol.exists():
        raise RuntimeError(
            f"Politika limit dosyasi bulunamadi: {yol}\n"
            "Bu dosya olmadan limit_asimi anomalisi uretilemez ve dogrulanamaz."
        )
    try:
        ham = json.loads(yol.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Politika limit dosyasi bozuk ({yol}): {e}") from e

    taban = ham.get("limit_tabani", "birim_fiyat")
    if taban != "birim_fiyat":
        raise RuntimeError(
            f"Desteklenmeyen limit_tabani: {taban!r}. Bugun yalnizca 'birim_fiyat' var; "
            "satir/fatura toplami tabani validators ve anomaly_injector'da ayrica yazilmali."
        )

    kategori: dict[HarcamaKategorisi, float] = {}
    for ad, deger in ham.get("kategori_limitleri", {}).items():
        kategori[_kategori_cevir(ad, yol)] = _sayi_cevir(deger, ad, yol)

    birim: dict[tuple[HarcamaKategorisi, str], float] = {}
    for anahtar, deger in ham.get("birim_limitleri", {}).items():
        if "|" not in anahtar:
            raise RuntimeError(
                f"birim_limitleri anahtari 'kategori|birim' biciminde olmali: {anahtar!r} ({yol})"
            )
        kategori_adi, birim_adi = anahtar.split("|", 1)
        birim[(_kategori_cevir(kategori_adi, yol), birim_adi)] = _sayi_cevir(deger, anahtar, yol)

    if not kategori and not birim:
        # Gecerli bir politika: "hicbir kategoride limit yok". Sessiz kalmasin diye
        # yazilir, hata DEGIL -- limit_asimi enjektoru bu durumda no-op'a duser.
        print(f"[i] Politika limit dosyasinda hic limit tanimli degil: {yol}")
    return kategori, birim


def _kategori_cevir(ad: str, yol: Path) -> HarcamaKategorisi:
    try:
        return HarcamaKategorisi(ad)
    except ValueError:
        gecerli = ", ".join(k.value for k in HarcamaKategorisi)
        raise RuntimeError(
            f"Tanimsiz harcama kategorisi {ad!r} ({yol}). Gecerli olanlar: {gecerli}"
        ) from None


def _sayi_cevir(deger, anahtar: str, yol: Path) -> float:
    if not isinstance(deger, (int, float)) or isinstance(deger, bool) or deger <= 0:
        raise RuntimeError(f"Limit pozitif bir sayi olmali: {anahtar}={deger!r} ({yol})")
    return float(deger)


KATEGORI_LIMITLERI, BIRIM_LIMITLERI = _yukle()


def kalem_limiti(kategori: HarcamaKategorisi, birim: str | None = None) -> float | None:
    """Kalemin birim fiyatina uygulanacak politika limiti; tanimli degilse None."""
    if birim is not None:
        birim_limiti = BIRIM_LIMITLERI.get((kategori, birim))
        if birim_limiti is not None:
            return birim_limiti
    return KATEGORI_LIMITLERI.get(kategori)


def zorunlu_kalem_limiti(kategori: HarcamaKategorisi, birim: str | None = None) -> float:
    """Limiti oldugu BILINEN cagri yerleri icin (limit_asimi enjektoru); yoksa hata."""
    limit = kalem_limiti(kategori, birim)
    if limit is None:
        raise KeyError(f"{kategori.value} icin politika limiti tanimli degil")
    return limit


def limitli_kategoriler() -> list[HarcamaKategorisi]:
    """Limiti olan kategoriler (kategori duzeyinde tanimli olanlar)."""
    return list(KATEGORI_LIMITLERI.keys())
