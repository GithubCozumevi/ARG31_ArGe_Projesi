"""
Firma Registry Üretici — TEK kalıcı firma-kimliği kaynağı.

Firma kişiliği artık FATURA bazlı değil, FIRMA bazlıdır: her firma bir kez
üretilip (ad + kimlik no + is_kolu + firma_türü) bu registry'de DONAR. Fatura
üretimi (rastgele_fatura) bu registry'den bir firma SEÇER, icat etmez. Böylece:
  - Aynı ad → hep aynı VKN (ayni_ad_farkli_vkn yapısal olarak imkânsız).
  - Aynı VKN → hep aynı ad (ayni_vkn_farkli_ad yapısal olarak imkânsız).
  - Batch'ler arası tutarlılık kontrolle değil, İNŞAYLA gelir.

Registry TEK dosyada harmanlanmış kaynaklardan oluşur (hepsi aynı şemada):
  1. OSM gerçek işletme adları (tüzel kişi)          -> kaynak=osm
  2. Sentetik is_kolu-bazlı adlar (tüzel kişi dolgu)  -> kaynak=sentetik
  3. Şahıs şirketi (fake.name + TCKN)                 -> kaynak=sahis

İnce OSM iş kolları (ör. lojistik) hedefe kadar SENTETİK adla doldurulur; OSM
re-pull (firma_adlari_osm_cek.py) ileride OSM payını artırıp dolguyu azaltır —
registry şeması değişmez.

Kullanım:
    python -m faz_a_fis_uretimi.firma_registry_olustur --hedef-per-iskolu 1500 --seed 42

Çıktı: data/firma_registry.csv
"""

import argparse
import csv
import random
from collections import Counter
from pathlib import Path

from ortak.schema import IsKolu, FirmaTuru
from faz_a_fis_uretimi.generators.field_generator import (
    rastgele_firma_adi, rastgele_sahis_kisi_adi, rastgele_vkn, rastgele_tckn,
    FIRMA_TURU_AGIRLIK,
)
from faz_a_fis_uretimi.firma_registry_adres_uret import adres_uret

OSM_CSV = Path("data/companies/firma_adlari_osm.csv")
REGISTRY_CSV = Path("data/firma_registry.csv")

# Tüzel türler (şahıs hariç) ve orijinal ağırlıkları — şahıs oranını registry
# kompozisyonunda ayrıca uyguladığımız için burada yalnız tüzel türleri harmanlıyoruz.
TUZEL_TURLER = [FirmaTuru.KISA_UNVAN, FirmaTuru.UZUN_UNVAN, FirmaTuru.YABANCI_ORTAKLI]
TUZEL_AGIRLIK = [FIRMA_TURU_AGIRLIK[t] for t in TUZEL_TURLER]
SAHIS_ORANI = FIRMA_TURU_AGIRLIK[FirmaTuru.SAHIS_SIRKETI] / sum(FIRMA_TURU_AGIRLIK.values())

# İsim üretiminde çakışma olursa kaç kez yeniden deneyelim (havuzlar geniş,
# çakışma nadir; tükenirse sayaç ekiyle benzersizleştirilir).
MAX_ISIM_DENEME = 60


def osm_adlarini_yukle() -> dict[str, list[str]]:
    """OSM CSV'yi is_kolu -> [isim, ...] olarak yükler. Yoksa boş döner."""
    if not OSM_CSV.exists():
        print(f"[!] {OSM_CSV} yok — registry tamamen sentetik adlarla üretilecek.")
        return {}
    gruplar: dict[str, list[str]] = {}
    with open(OSM_CSV, encoding="utf-8") as f:
        for satir in csv.DictReader(f):
            isim = (satir.get("isim") or "").strip()
            is_kolu = (satir.get("is_kolu") or "").strip()
            if isim and is_kolu:
                gruplar.setdefault(is_kolu, []).append(isim)
    return gruplar


def benzersiz_kimlik(uretici, kullanilmis: set[str]) -> str:
    """Verilen kimlik üreticisinden (VKN/TCKN) global BENZERSIZ bir no döndürür."""
    while True:
        no = uretici()
        if no not in kullanilmis:
            kullanilmis.add(no)
            return no


def benzersiz_isim(uretici, kullanilmis: set[str]) -> str | None:
    """Global benzersiz bir ad döndürür; havuz tükenirse None (çağıran ek yapar)."""
    for _ in range(MAX_ISIM_DENEME):
        ad = uretici()
        if ad not in kullanilmis:
            kullanilmis.add(ad)
            return ad
    return None


def registry_uret(taban_per_iskolu: int, osm_pay: float = 0.8) -> list[dict]:
    """OSM adlarının TAMAMINI kullanır, üstüne sentetik + şahıs ekleyerek registry'yi
    kurar. Bir iş kolunun nihai boyutu OSM adedinden TÜRETİLİR:

        toplam = max(taban_per_iskolu, round(osm_adedi / osm_pay))

    Yani OSM zengin bir iş kolunda (restoran 4000) hiçbir ad çöpe gitmez; toplam
    büyür ve OSM payı ~%80'de kalır, kalan %20 sentetik kurumsal ad + şahıs olur.
    OSM'si zayıf iş kollarında (lojistik 37) taban devreye girer ve dolgu artar.
    """
    osm_adlari = osm_adlarini_yukle()

    kullanilmis_kimlik: set[str] = set()
    kullanilmis_ad: set[str] = set()
    kayitlar: list[dict] = []
    firma_id = 0

    for is_kolu in IsKolu:
        anahtar = is_kolu.value

        havuz = list(osm_adlari.get(anahtar, []))
        random.shuffle(havuz)

        # Nihai boyut OSM adedinden türetilir; taban altına düşmez.
        toplam = max(taban_per_iskolu, round(len(havuz) / osm_pay) if havuz else 0)
        sahis_sayisi = round(toplam * SAHIS_ORANI)
        tuzel_sayisi = toplam - sahis_sayisi
        # OSM'nin tamamı kullanılır (tüzel kontenjanını aşmadığı sürece).
        osm_hedef = min(len(havuz), tuzel_sayisi)

        osm_kullanildi = 0
        sentetik_kullanildi = 0

        for i in range(tuzel_sayisi):
            tur = random.choices(TUZEL_TURLER, weights=TUZEL_AGIRLIK, k=1)[0]
            if osm_kullanildi < osm_hedef:
                ad = havuz[osm_kullanildi]
                osm_kullanildi += 1
                if ad in kullanilmis_ad:
                    continue  # nadir: aynı ad başka is_kolu'nda çıktı, atla
                kullanilmis_ad.add(ad)
                kaynak = "osm"
            else:
                ad = benzersiz_isim(
                    lambda: rastgele_firma_adi(is_kolu, tur), kullanilmis_ad
                )
                if ad is None:
                    ad = f"{rastgele_firma_adi(is_kolu, tur)} {firma_id}"
                    kullanilmis_ad.add(ad)
                sentetik_kullanildi += 1
                kaynak = "sentetik"

            kayitlar.append({
                "firma_id": firma_id,
                "is_kolu": anahtar,
                "firma_turu": tur.value,
                "satici_unvan": ad,
                "satici_kimlik": benzersiz_kimlik(rastgele_vkn, kullanilmis_kimlik),
                "sahis_adi": "",
                "kaynak": kaynak,
            })
            firma_id += 1

        # --- Şahıs şirketi: fake.name() + TCKN (ada is_kolu yansımaz, kolonda tutulur) ---
        for i in range(sahis_sayisi):
            ad = benzersiz_isim(
                lambda: rastgele_firma_adi(is_kolu, FirmaTuru.SAHIS_SIRKETI),
                kullanilmis_ad,
            )
            if ad is None:
                ad = f"{rastgele_firma_adi(is_kolu, FirmaTuru.SAHIS_SIRKETI)} {firma_id}"
                kullanilmis_ad.add(ad)
            kayitlar.append({
                "firma_id": firma_id,
                "is_kolu": anahtar,
                "firma_turu": FirmaTuru.SAHIS_SIRKETI.value,
                "satici_unvan": ad,
                "satici_kimlik": benzersiz_kimlik(rastgele_tckn, kullanilmis_kimlik),
                # Isletme adi ile SAHIBININ adi ayri satirlardir (gercek fis kaniti).
                "sahis_adi": rastgele_sahis_kisi_adi(),
                "kaynak": "sahis",
            })
            firma_id += 1

        yuzde = lambda n: 100 * n / toplam if toplam else 0
        print(f"  {anahtar:<22} toplam={toplam:<5} "
              f"osm={osm_kullanildi:<5}(%{yuzde(osm_kullanildi):.0f}) "
              f"sentetik={sentetik_kullanildi:<5}(%{yuzde(sentetik_kullanildi):.0f}) "
              f"sahis={sahis_sayisi:<4}(%{yuzde(sahis_sayisi):.0f})"
              f"   [OSM havuzu {len(havuz)}, kullanilmayan {len(havuz) - osm_kullanildi}]")

    return kayitlar


def registry_yaz(kayitlar: list[dict]) -> None:
    REGISTRY_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY_CSV, "w", newline="", encoding="utf-8") as f:
        yazici = csv.DictWriter(
            f, fieldnames=["firma_id", "is_kolu", "firma_turu", "satici_unvan",
                           "satici_kimlik", "sahis_adi", "kaynak", "adres"]
        )
        yazici.writeheader()
        yazici.writerows(kayitlar)


def main():
    parser = argparse.ArgumentParser(description="Firma registry üretici (tek kalıcı kaynak)")
    parser.add_argument("--hedef-per-iskolu", "--taban-per-iskolu", type=int, default=1500,
                        dest="taban_per_iskolu",
                        help="İş kolu başına TABAN firma sayısı. OSM'si bol iş kollarında "
                             "nihai boyut OSM adedinden türetilir, taban yalnızca alt sınırdır.")
    parser.add_argument("--osm-pay", type=float, default=0.8,
                        help="OSM adlarının nihai registry'deki hedef payı (0.8 = %%80). "
                             "OSM'nin TAMAMI kullanılır; bu oran üstüne ne kadar sentetik "
                             "ekleneceğini belirler.")
    parser.add_argument("--seed", type=int, default=42,
                        help="Determinizm için seed — aynı seed = aynı registry (VKN'ler sabit)")
    args = parser.parse_args()

    random.seed(args.seed)
    print(f"Registry üretiliyor (taban/is_kolu={args.taban_per_iskolu}, "
          f"osm-pay={args.osm_pay}, seed={args.seed})...")
    kayitlar = registry_uret(args.taban_per_iskolu, args.osm_pay)
    adres_rnd = random.Random(args.seed)
    for satir in kayitlar:
        satir["adres"] = adres_uret(adres_rnd)
    registry_yaz(kayitlar)

    kaynak_sayim = Counter(k["kaynak"] for k in kayitlar)
    print(f"\n[+] Toplam {len(kayitlar)} firma -> {REGISTRY_CSV}")
    print(f"    Kaynak dağılımı: {dict(kaynak_sayim)}")
    print(f"    Benzersiz VKN/TCKN: {len({k['satici_kimlik'] for k in kayitlar})}, "
          f"benzersiz unvan: {len({k['satici_unvan'] for k in kayitlar})}")


if __name__ == "__main__":
    main()
