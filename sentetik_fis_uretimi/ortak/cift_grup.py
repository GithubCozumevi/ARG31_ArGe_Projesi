"""Cift (ayni fatura iki kayitta) gruplamasinin TEK kaynagi.

Cift anahtari `(satici_vkn, fatura_no)`. Yalniz `fatura_no` YETMEZ: farkli
saticilar ayni numarayi kullanabilir ve `fatura_no_tekrarlarini_bul` da bu
yuzden ikili anahtara bakar (CLAUDE.md §7).

`cift_grup_id` MODEL GIRDISINE yazilmaz: `satici_vkn` ve `fatura_no` zaten
girdide oldugu icin yeni bilgi katmaz, sadece groupby'i hazir verir. ETIKET
dosyasina degerlendirme metadatasi olarak girer (cift bazli metrik icin).

Uc modul bunu paylasir, ayri ayri turetmez:
  batch_hazirla    -> cifti birlikte secsin
  onay_durumu_ata  -> cifti birlikte degerlendirsin
  veri_bol         -> cifti ayni bolmeye koysun
"""

import json
from collections import defaultdict

# Fise BASILMAYAN ve cift uyeleri arasinda KASTEN farklilasan alanlar. Buraya
# eklenmeyen her yeni alan imzayi bozar; `fis_uret.saat_tohumlari_kur` sessizce
# devre disi kalir, `mukerrer.py` "birebir kopya" sayacini 0 gosterir.
IMZA_DISI_ALANLAR = {"kayit_id", "aciklama_metni", "yukleme_zamani"}


def kayit_imzasi(kayit: dict) -> str:
    """Fise basilan alanlarin imzasi: iki kayit AYNI fis mi."""
    return json.dumps({k: v for k, v in kayit.items() if k not in IMZA_DISI_ALANLAR},
                      sort_keys=True, ensure_ascii=False)


def cift_grup_anahtari(kayit: dict) -> tuple[str, str]:
    """Kaydin cift anahtari. Tekil kayitlar da bir anahtar tasir (grubu tek uyeli)."""
    return (kayit["satici_vkn"], kayit["fatura_no"])


def cift_grup_id(kayit: dict) -> str:
    """Anahtarin dize hali; etiket dosyasina bu yazilir."""
    vkn, no = cift_grup_anahtari(kayit)
    return f"{vkn}:{no}"


def gruplari_kur(kayitlar: list[dict]) -> dict[tuple[str, str], list[str]]:
    """anahtar -> kayit_id listesi. Tekil gruplar da doner."""
    gruplar: dict[tuple[str, str], list[str]] = defaultdict(list)
    for r in kayitlar:
        gruplar[cift_grup_anahtari(r)].append(r["kayit_id"])
    return dict(gruplar)


def ciftleri_bul(kayitlar: list[dict]) -> dict[tuple[str, str], list[str]]:
    """Yalniz COK uyeli gruplar. Uye sirasi `kayit_id`'ye gore sabitlenir."""
    return {a: sorted(uyeler) for a, uyeler in gruplari_kur(kayitlar).items()
            if len(uyeler) > 1}
